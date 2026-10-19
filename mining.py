# -*- coding: utf-8 -*-
"""Шахта (работа в карьере): к камню подходишь и жмёшь E сам. Бот:
  1) пока внизу справа «Добыча руды» с полоской - кликает ЛКМ (сама добыча, ~16 с);
  2) когда открылась мини-игра «Прогресс сбора руды» (коврик с камнем) - кликает по каждому куску руды
     на камне, пока кусков не останется (мини-игра закрывается сама вместе с последним).

Что выяснено по записи 20261018_204147 (1920x1080, 19 камней):
  - мини-игра - картинка интерфейса: коврик, лампа, каска на одних и тех же местах; камней 1-2, они разные
    по форме, цвету (синие, серо-лиловые) и месту; куски руды - золото, красная/розовая руда, голубой
    кристалл, чёрный уголь, серое железо; клик по куску убирает его за ~0,1 с;
  - открывается мини-игра с «наездом» ~0,3 с - кликать, когда картинка встала;
  - камень = всё, что не ковёр (ковёр розовый); кусок = пятно внутри камня, сильно отличающееся от камня
    вокруг (Lab, медиана по окрестности); тёмное синеватое - трещины и тени камня, а не уголь (уголь бесцветный);
    уголь на тусклом сером камне почти не отличается по яркости - его ищем отдельно как бесцветное пятно;
  - по всем кадрам всех мини-игр записи: ни одного пропущенного куска, лишние пятна изредка бывают -
    по ним бот кликает не больше 2 раз и пропускает."""
import os
import sys
import json
import time
import queue
import random
import threading

import numpy as np
import cv2

import switches
from sewing_bot import key_pressed, vk_name, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}
BASE_W, BASE_H = 1920, 1080
PANEL = (935, 740, 1110, 775)          # надпись «Прогресс сбора руды» на листке под ковриком
HINT = (1600, 992, 1692, 1014)         # «Добыча руды» над полоской добычи
ROI = (600, 240, 1400, 720)            # внутренняя часть коврика, где лежат камни
IGNORE = [(1056, 256, 1096, 296)]      # светлое пятно у края коврика в части мини-игр - не руда
SETTLE = 0.25                          # мини-игра «наезжает»: сколько ждать после появления надписи
WAIT_GONE = 0.2                        # после серии кликов: куски пропадают за ~0,1 с
TRIES = 2                              # кликов по пятну, которое не пропадает, - потом пропускаем
STUCK = 1.2                            # кусков не видно, а мини-игра открыта: столько ждём и ищем чувствительнее
# пороги поиска кусков: обычные и чувствительные (если обычные ничего не нашли, а мини-игра не закрылась)
P = dict(dev=46, cs=120, min_area=260, max_area=4000, med=41, er=7, op=7, cl=15, ro=3,
         dark_l90=60, dark_ch=12, dark_min=20, grey=7, gop=13, gl=150)
P_SENS = dict(P, dev=34)


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


CONFIG_PATH = os.path.join(app_dir(), "mining_config.json")


def default_config():
    return {"toggle_vk": 0x67,            # Numpad 7 (Numpad 9 в игре - навигатор)
            "cps": 6.0, "mini_cps": 3.0, "dig": True, "ores": True, "focus_only": True}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ====================================================================== зрение
def _K(n):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))


class Vision:
    """Кадр экрана (BGR, любой размер) -> фаза и куски руды. Всё считается в координатах 1920x1080."""

    def __init__(self):
        a = np.load(res_path("mining_assets.npz"))
        self.tp, self.th = a["panel"], a["hint"]

    @staticmethod
    def norm(frame):
        """Экран другого размера -> 1920x1080 (интерфейс игры масштабируется по высоте и стоит по центру)."""
        H, W = frame.shape[:2]
        if (W, H) == (BASE_W, BASE_H):
            return frame
        k = BASE_H / H
        f = cv2.resize(frame, (int(round(W * k)), BASE_H))
        x0 = (f.shape[1] - BASE_W) // 2
        if x0 >= 0:
            return f[:, x0:x0 + BASE_W]
        pad = np.zeros((BASE_H, BASE_W, 3), np.uint8)
        pad[:, -x0:-x0 + f.shape[1]] = f
        return pad

    def _score(self, g, box, tpl):
        x0, y0, x1, y1 = box
        sub = g[y0 - 10:y1 + 10, x0 - 10:x1 + 10]
        return float(cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED).max())

    def phase(self, f):
        """-> "mini" (мини-игра с камнем), "dig" (идёт добыча) или None."""
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if self._score(g, PANEL, self.tp) > 0.6:        # пока «наезжает», надпись не на месте - не совпадает
            return "mini"
        if self._score(g, HINT, self.th) > 0.6:
            return "dig"
        return None

    @staticmethod
    def stone(c, p=P):
        """Маска камней на коврике: всё, что не розовый ковёр, крупными кусками, с заливкой дыр (там руда)."""
        hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
        h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        carpet = ((h >= 140) | (h <= 6)) & (s > 30) & (v > 60)
        rock = cv2.morphologyEx((~carpet).astype(np.uint8), cv2.MORPH_OPEN, _K(p["ro"]))
        n, lab, st, _ = cv2.connectedComponentsWithStats(rock, 8)
        keep = np.isin(lab, [i for i in range(1, n) if st[i][4] > 8000]).astype(np.uint8)
        keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, _K(p["cl"]))
        cnts, _ = cv2.findContours(keep, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        fill = np.zeros_like(keep)
        cv2.drawContours(fill, cnts, -1, 1, -1)
        return fill

    def ores(self, f, p=P):
        """-> [{"pt": (x, y) куда кликать, "area": ..}] в координатах 1080p."""
        x0, y0, x1, y1 = ROI
        c = f[y0:y1, x0:x1]
        fill = self.stone(c, p)
        inner = cv2.erode(fill, _K(p["er"]))
        lab = cv2.cvtColor(c, cv2.COLOR_BGR2LAB)
        # фон камня - медиана по окрестности; ковёр заменяем средним цветом камня, чтобы не тянул фон у краёв
        mean = np.array(cv2.mean(lab, mask=fill)[:3], np.uint8)
        src = lab.copy()
        src[fill == 0] = mean
        sm = cv2.resize(src, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        bg = cv2.resize(cv2.medianBlur(sm, p["med"] // 2 | 1), (c.shape[1], c.shape[0]))
        d = lab.astype(np.float32) - bg.astype(np.float32)
        dev = np.sqrt(d[..., 0] ** 2 + (p["cs"] / 30.0) * (d[..., 1] ** 2 + d[..., 2] ** 2))
        cand = ((dev > p["dev"]) & (inner > 0)).astype(np.uint8)
        cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, _K(p["op"]))
        chm = np.hypot(lab[..., 1].astype(np.int16) - 128, lab[..., 2].astype(np.int16) - 128)
        if p["grey"]:
            g = ((chm < p["grey"]) & (lab[..., 0] > 15) & (lab[..., 0] < p["gl"]) & (inner > 0)).astype(np.uint8)
            cand |= cv2.morphologyEx(g, cv2.MORPH_OPEN, _K(p["gop"]))
        n, labl, st, cen = cv2.connectedComponentsWithStats(cv2.dilate(cand, _K(7)), 8)
        out = []
        for i in range(1, n):
            a = st[i][4]
            if a < p["min_area"] or a > p["max_area"]:
                continue
            X, Y = int(cen[i][0]) + x0, int(cen[i][1]) + y0
            if any(b[0] <= X <= b[2] and b[1] <= Y <= b[3] for b in IGNORE):
                continue
            m = (labl == i) & (cand > 0)
            L90 = np.percentile(lab[..., 0][m], 90)
            ch = np.median(chm[m])
            if L90 < p["dark_l90"] and (ch > p["dark_ch"] or L90 < p["dark_min"]):
                continue                    # тёмное и синее - трещина/тень камня; тёмный кусок (уголь) - бесцветный
            out.append({"pt": (X, Y), "area": int(a)})
        out.sort(key=lambda o: (o["pt"][1] // 60, o["pt"][0]))      # рядами сверху вниз - короче путь мыши
        return out


def analyze_video(path, out=None, step=1):
    """Проверка зрения на записи: фазы и найденные куски по кадрам (out - видео с пометками)."""
    vis = Vision()
    cap = cv2.VideoCapture(path)
    wr = None
    i = 0
    last = None
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if i % step:
            continue
        f = vis.norm(fr)
        ph = vis.phase(f)
        os_ = vis.ores(f) if ph == "mini" else []
        if ph != last:
            print(f"кадр {i}: {ph or '-'}" + (f", кусков {len(os_)}" if ph == "mini" else ""))
            last = ph
        if out:
            v = f.copy()
            cv2.putText(v, f"{ph or '-'} {len(os_)}", (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 255, 255), 3)
            for o in os_:
                cv2.circle(v, o["pt"], 22, (255, 0, 255), 3)
            v = cv2.resize(v, (960, 540))
            if wr is None:
                wr = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), 15, (960, 540))
            wr.write(v)
    if wr:
        wr.release()


# ====================================================================== интерфейс и рабочий поток
def build_mine_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"rocks": 0, "ores": 0, "clicks": 0, "state": "idle", "view": None, "left": 0}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("mine") and not ctl["active"]:
            log("Шахта выключена: включи её переключателем в меню слева")
            return
        ctl["active"] = not ctl["active"]

    status_var, info_var = tk.StringVar(value="Выключен"), tk.StringVar()
    ttk.Label(parent, textvariable=status_var, style="Status.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    ttk.Button(parent, text="Старт / стоп", style="Accent.TButton", command=toggle).grid(
        row=2, column=0, columnspan=4, sticky="ew", **pad)
    key_var = tk.StringVar(value=vk_name(cfg["toggle_vk"]))
    ttk.Label(parent, text="Горячая клавиша:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Label(parent, textvariable=key_var, style="Key.TLabel", width=16).grid(row=3, column=1, sticky="w", **pad)

    def bind_key():
        ctl["binding"] = True
        key_var.set("нажми клавишу…")

        def wait():
            end = time.time() + 6
            while time.time() < end and not ctl["quit"]:
                for vk in range(8, 255):
                    if vk in (0x01, 0x02, 0x04):
                        continue
                    if key_pressed(vk):
                        q.put(("bound", vk))
                        return
                time.sleep(0.02)
            q.put(("bound", None))
        threading.Thread(target=wait, daemon=True).start()
    ttk.Button(parent, text="Назначить", command=bind_key).grid(row=3, column=2, sticky="w", **pad)

    box = ttk.LabelFrame(parent, text="Как работать")
    box.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    ttk.Label(box, text="Бегаешь и жмёшь E у камня сам. Пока идёт добыча (внизу справа «Добыча руды»), бот кликает; "
                        "когда откроется мини-игра с камнем на коврике, бот сам соберёт все куски руды.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    cps_var = tk.DoubleVar(value=float(cfg["cps"]))
    dig_var, ore_var = tk.BooleanVar(value=cfg["dig"]), tk.BooleanVar(value=cfg["ores"])
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Checkbutton(box, text="Кликать во время добычи", variable=dig_var, command=lambda: save()).grid(
        row=1, column=0, sticky="w", **pad)
    ttk.Label(box, text="кликов в секунду:").grid(row=1, column=1, sticky="e", **pad)
    ttk.Spinbox(box, from_=2, to=15, increment=0.5, width=6, textvariable=cps_var,
                command=lambda: save()).grid(row=1, column=2, sticky="w", **pad)
    ttk.Checkbutton(box, text="Собирать руду в мини-игре", variable=ore_var, command=lambda: save()).grid(
        row=2, column=0, sticky="w", **pad)
    mini_var = tk.DoubleVar(value=float(cfg["mini_cps"]))
    ttk.Label(box, text="скорость, кликов в секунду:").grid(row=2, column=1, sticky="e", **pad)
    ttk.Spinbox(box, from_=0.5, to=10, increment=0.5, width=6, textvariable=mini_var,
                command=lambda: save()).grid(row=2, column=2, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var, command=lambda: save()).grid(
        row=3, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["cps"] = max(2.0, min(15.0, float(cps_var.get())))
        except (tk.TclError, ValueError):
            pass
        try:
            cfg["mini_cps"] = max(0.5, min(10.0, float(mini_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["dig"], cfg["ores"] = bool(dig_var.get()), bool(ore_var.get())
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    cps_var.trace_add("write", lambda *a: save())
    mini_var.trace_add("write", lambda *a: save())

    logbox = tk.Text(parent, height=10, width=80, state="disabled", font=("Consolas", 9))
    logbox.grid(row=5, column=0, columnspan=4, **pad)

    # ---------------- рабочий поток
    def worker():
        import ctypes
        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        try:
            vis = Vision()
        except Exception as ex:
            log(f"Не загрузились образцы мини-игры: {ex}")
            return
        scr = AF.Screen()
        inp = AF.Input2()
        W, H = scr.mon["width"], scr.mon["height"]

        def to_screen(p):
            """1080p -> экран (обратное к Vision.norm)."""
            k = H / BASE_H
            x0 = (W - BASE_W * k) / 2
            return int(scr.mon["left"] + x0 + p[0] * k), int(scr.mon["top"] + p[1] * k)

        def near(p, pts, r=24):
            return any(abs(p[0] - q[0]) < r and abs(p[1] - q[1]) < r for q in pts)

        def lclick():
            inp.mouse(AF.MOUSEEVENTF_LEFTDOWN)
            time.sleep(random.uniform(0.03, 0.06))
            inp.mouse(AF.MOUSEEVENTF_LEFTUP)

        def grab():
            return vis.norm(np.ascontiguousarray(scr.grab()[:, :, :3]))

        def pace():
            """Пауза перед кликом в мини-игре - по скорости из настроек. False - бота выключили."""
            end = last_hit[0] + random.uniform(0.85, 1.15) / max(0.5, cfg["mini_cps"])
            while time.time() < end:
                if ctl["quit"] or not ctl["active"]:
                    return False
                time.sleep(0.01)
            return True

        prev = was = False
        last_hit = [0.0]            # когда был последний клик в мини-игре
        phase_prev = None
        next_click = 0.0
        tries = []                  # [(x, y, кликов)] - куски в этой мини-игре, по которым уже кликали
        skip = []                   # пропущенные (не пропали)
        mini_t0, mini_n0, last_mini, last_seen, sens = 0.0, 0, 0.0, 0.0, False
        while not ctl["quit"]:
            if not switches.is_on("mine"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                log("Шахта включена: жми E у камня" if was else "Шахта выключена")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("mine") else "Выключен (тумблер)"
                stats["view"] = None
                time.sleep(0.1)
                continue
            if SEWING_BUSY["on"]:
                ctl["status"] = "Пауза: работает швейный бот"
                time.sleep(0.2)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            now = time.time()
            try:
                f = grab()
                ph = vis.phase(f)
            except Exception as ex:
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if ph != phase_prev:
                if ph == "dig":
                    log("Добыча: кликаю")
                elif ph == "mini":
                    tries, skip, sens = [], [], False
                    last_hit[0] = now
                    mini_t0 = last_seen = now
                    mini_n0 = 0
                    log("Мини-игра: собираю руду")
                elif phase_prev == "mini":
                    stats["rocks"] += 1
                    stats["ores"] += 1                    # последний кусок пропадает вместе с мини-игрой
                    mini_n0 += 1
                    log(f"Камень готов: кусков собрано {mini_n0} за {last_mini - mini_t0:.1f} с")
                phase_prev = ph
            if ph == "dig":
                BUSY["on"] = True
                stats["state"] = "dig"
                stats["view"] = None
                ctl["status"] = "Добываю руду"
                if cfg["dig"] and now >= next_click:
                    lclick()
                    stats["clicks"] += 1
                    next_click = now + random.uniform(0.8, 1.2) / max(1.0, cfg["cps"])
                time.sleep(0.01)
            elif ph == "mini":
                BUSY["on"] = True
                last_mini = now
                stats["state"] = "mini"
                if now - mini_t0 < SETTLE:
                    ctl["status"] = "Мини-игра открывается…"
                    time.sleep(0.03)
                    continue
                found = vis.ores(f, P_SENS if sens else P)
                live = [o for o in found if not near(o["pt"], skip)]
                if live:
                    last_seen = now
                elif now - last_seen > STUCK and not sens:
                    sens = True                           # ничего не видно, а мини-игра открыта - ищем чувствительнее
                    skip = []
                    log("Кусков не вижу - ищу внимательнее")
                    continue
                stats["view"] = {"ores": [o["pt"] for o in found], "skip": list(skip)}
                stats["left"] = len(live)
                ctl["status"] = f"Собираю руду: осталось {len(live)}"
                if not cfg["ores"] or not live:
                    time.sleep(0.03)
                    continue
                # серия: по разу в каждый кусок, потом одна проверка
                clicked = []
                for o in live:
                    t = next((x for x in tries if near(o["pt"], [x])), None)
                    if t is not None and any(t is c for c in clicked):
                        continue
                    if t is None:
                        t = [o["pt"][0], o["pt"][1], 0]
                        tries.append(t)
                    if t[2] >= TRIES:
                        skip.append((t[0], t[1]))
                        continue
                    if not pace():
                        break
                    stats["view"]["t"] = o["pt"]
                    inp.move_to(*to_screen(o["pt"]))
                    time.sleep(0.03)
                    lclick()
                    last_hit[0] = time.time()
                    t[2] += 1
                    stats["clicks"] += 1
                    clicked.append(t)
                    time.sleep(0.04)
                if not clicked:
                    continue
                time.sleep(WAIT_GONE)
                f2 = grab()
                left = vis.ores(f2, P_SENS if sens else P) if vis.phase(f2) == "mini" else []
                for t in clicked:
                    if not near((t[0], t[1]), [o["pt"] for o in left]) and t in tries:
                        if left:                          # последний кусок засчитается при закрытии мини-игры
                            stats["ores"] += 1
                            mini_n0 += 1
                        tries.remove(t)
            else:
                BUSY["on"] = False
                stats["state"] = "idle"
                stats["view"] = None
                stats["left"] = 0
                ctl["status"] = "Жду добычу (нажми E у камня)"
                time.sleep(0.05)
        BUSY["on"] = False

    threading.Thread(target=worker, daemon=True).start()

    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", time.strftime("%H:%M:%S ") + m[1] + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                elif m[0] == "bound":
                    ctl["binding"] = False
                    if m[1]:
                        cfg["toggle_vk"] = m[1]
                        save_config(cfg)
                    key_var.set(vk_name(cfg["toggle_vk"]))
        except queue.Empty:
            pass
        except tk.TclError:
            return
        status_var.set(ctl["status"])
        info_var.set(f"Камней: {stats['rocks']}   ·   кусков руды: {stats['ores']}   ·   кликов: {stats['clicks']}")
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Камней", "view": stats["view"],
                "state": stats["state"], "cps": cfg["cps"], "left": stats["left"],
                "items": [("Камней", stats["rocks"]), ("Руды", stats["ores"])]}
    widgets.register("mine", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
