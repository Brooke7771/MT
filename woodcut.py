# -*- coding: utf-8 -*-
"""Лесоруб (работа «Лесоруб»): к дереву подходишь и жмёшь E сам. Бот:
  1) пока внизу справа подсказка «Рубка дерева» - кликает ЛКМ (сама рубка, ~15 с);
  2) когда открылась мини-игра «Прогресс очистки ствола от веток» - кликает по основанию каждой ветки,
     пока веток не останется (мини-игра закрывается сама).

Что выяснено по записи 20261018_022832 (1920x1080):
  - мини-игра - картинка интерфейса на одном и том же месте: площадка с травой, бревно, пень, лампы;
    ветки торчат из бревна (над травой или над миром позади), клик по ветке у основания убирает её
    примерно за 0,3 с; последняя ветка пропадает вместе с мини-игрой;
  - ветки ищутся как отличие от «чистой» мини-игры (эталон в woodcut_assets.npz, собран по записи и
    очищен кадрами сразу после кликов игрока) внутри площадки и бревна; мир позади не смотрим - он разный;
  - если ветка не убралась с двух кликов у основания, кликаем дальше по ней, потом пропускаем."""
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
PANEL = (700, 985, 1260, 1020)         # надпись «Прогресс очистки ствола от веток»
HINT = (1590, 990, 1700, 1018)         # «Рубка дерева» над полоской рубки
AXIS = ((680, 510), (1232, 330))       # ось бревна, концы
DIFF, MIN_AREA = 40, 200
IGNORE = (1222, 332, 1302, 382)        # верх пня (1080p): не ветка
AIMS = (0.6, 0.85, 0.45, 0.75)         # куда кликать вдоль ветки (доля длины от основания), по попыткам
STUMP = (1172, 335, 1300, 400)         # пень (1080p): клик по ветке над ним перехватывает пень
CLICK_OFF = 14                        # px от края бревна: ближе ветка не кликается
WAIT_GONE = 0.36                       # сколько ждать после серии кликов, пока ветки исчезнут


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


CONFIG_PATH = os.path.join(app_dir(), "woodcut_config.json")


def default_config():
    return {"toggle_vk": 0x68,            # Numpad 8
            "cps": 8.0, "chop": True, "branches": True, "focus_only": True}


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
class Vision:
    """Кадр экрана (BGR, любой размер) -> фаза и ветки. Всё считается в координатах 1920x1080."""

    def __init__(self):
        a = np.load(res_path("woodcut_assets.npz"))
        self.ref, self.over = a["ref"], a["over"].astype(bool)
        self.tp, self.th = a["panel"], a["hint"]
        self.roi = tuple(int(v) for v in a["roi"])
        A = np.array(AXIS[0], np.float32) - self.roi[:2]
        B = np.array(AXIS[1], np.float32) - self.roi[:2]
        h, w = self.ref.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        p = np.stack([xx.ravel(), yy.ravel()], 1).astype(np.float32)
        k = np.clip(((p - A) @ (B - A)) / ((B - A) @ (B - A)), 0, 1)
        self.axd = np.linalg.norm(p - (A + k[:, None] * (B - A)), axis=1).reshape(h, w)
        self.k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        K = lambda n: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))
        # само бревно (без веток): всё не-травяное на площадке у оси; ветки и ножки ламп тонкие - срезаются
        hsv = cv2.cvtColor(self.ref, cv2.COLOR_BGR2HSV)
        grass = (hsv[..., 0] >= 28) & (hsv[..., 0] <= 90) & (hsv[..., 1] > 70) & (hsv[..., 2] > 22)   # и трава в тени
        logm = (self.over & ~grass & (self.axd < 110)).astype(np.uint8)
        logm = cv2.morphologyEx(logm, cv2.MORPH_OPEN, K(17))
        n, lab, st, _ = cv2.connectedComponentsWithStats(logm, 8)
        self.logm = (lab == 1 + int(np.argmax(st[1:, 4]))).astype(np.uint8) if n > 1 else logm
        self.dlog = cv2.distanceTransform((1 - self.logm).astype(np.uint8), cv2.DIST_L2, 3)
        self.touch = cv2.dilate(self.logm, K(15)).astype(bool)
        # мир за площадкой рядом с бревном: тут ищем ветки, торчащие над лесом (эталон тут бесполезен)
        self.world = ~self.over & (self.dlog < 230)
        self.K7 = K(7)
        # верх пня: в 90% кадров обеих записей там «находилась ветка», которой нет (и она склеивалась с настоящей)
        ix0, iy0, ix1, iy1 = IGNORE
        self.world[iy0 - self.roi[1]:iy1 - self.roi[1], ix0 - self.roi[0]:ix1 - self.roi[0]] = False
        self.ign = np.zeros_like(self.over)
        self.ign[iy0 - self.roi[1]:iy1 - self.roi[1], ix0 - self.roi[0]:ix1 - self.roi[0]] = True

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
        """-> "mini" (мини-игра с ветками), "chop" (идёт рубка) или None."""
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if self._score(g, PANEL, self.tp) > 0.6:
            return "mini"
        if self._score(g, HINT, self.th) > 0.6:
            return "chop"
        return None

    def branches(self, f, debug=False):
        """-> [{"pt": куда кликать, "far": запасная точка на другом конце ветки, "area": ..}] в координатах 1080p."""
        x0, y0, x1, y1 = self.roi
        c = f[y0:y1, x0:x1]
        diff = cv2.absdiff(c, self.ref).max(axis=2)
        b, g, r = [c[..., i].astype(np.int16) for i in range(3)]
        v = c.max(axis=2)
        twig = (r >= g - 6) & (g >= b - 10) & (v > 60)
        cand = ((diff > DIFF) & twig & self.over & ~self.ign).astype(np.uint8)
        # над лесом: светлое тонкое коричневое (толстое - стволы деревьев на фоне)
        wc = (twig & (v > 85) & (r - b < 90) & self.world).astype(np.uint8)
        wc = wc & ~cv2.morphologyEx(wc, cv2.MORPH_OPEN, self.K7)
        cand = cv2.morphologyEx(cand | wc, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(cv2.dilate(cand, self.k5), 8)    # куски одной ветки - вместе
        dt = cv2.distanceTransform(cand, cv2.DIST_L2, 3)
        out = []
        for i in range(1, n):
            if st[i][4] < MIN_AREA:
                continue
            m = lab == i
            if not (m & self.touch).any():
                continue                                          # не растёт из бревна - фон
            ys, xs = np.where(m & (cand > 0))                     # точки клика - только по самой ветке
            if len(xs) == 0:
                continue
            # по двум записям: клик во внешнюю половину ветки срабатывает в 96% случаев, у основания - в 33%;
            # поэтому целимся на ~60% длины от основания (основание - ближе всего к оси бревна), запасная - ~85%
            base = int(np.argmin(self.axd[ys, xs]))
            dist = np.hypot(xs - xs[base], ys - ys[base])
            L = max(1.0, float(dist.max()))
            th = dt[ys, xs]
            # над пнём клик не проходит (пень лежит поверх ветки, запись 20261018_042530) - туда не целимся
            sx0, sy0, sx1, sy1 = STUMP
            okp = ~((xs + x0 >= sx0) & (xs + x0 <= sx1) & (ys + y0 >= sy0) & (ys + y0 <= sy1))
            if okp.sum() < 5:
                okp[:] = True
            cost = lambda a: np.where(okp, np.abs(dist - a * L) - 0.8 * np.minimum(th, 3), 1e9)
            aims = [(int(xs[k]) + x0, int(ys[k]) + y0) for k in (int(np.argmin(cost(a))) for a in AIMS)]
            out.append({"pt": aims[0], "far": aims[1], "aims": aims,
                        "base": (int(xs[base]) + x0, int(ys[base]) + y0), "area": int(st[i][4])})
            if debug:
                out[-1]["pix"] = (xs + x0, ys + y0)
        out.sort(key=lambda bb: bb["pt"][0])
        return out


def analyze_video(path, out=None, step=1):
    """Проверка зрения на записи: фазы и найденные ветки по кадрам (out - видео с пометками)."""
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
        bs = vis.branches(f) if ph == "mini" else []
        if ph != last:
            print(f"кадр {i}: {ph or '-'}" + (f", веток {len(bs)}" if ph == "mini" else ""))
            last = ph
        if out:
            v = f.copy()
            cv2.putText(v, f"{ph or '-'} {len(bs)}", (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 255, 255), 3)
            for bb in bs:
                cv2.circle(v, bb["pt"], 12, (255, 0, 255), 3)
            v = cv2.resize(v, (960, 540))
            if wr is None:
                wr = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), 15, (960, 540))
            wr.write(v)
    if wr:
        wr.release()


# ====================================================================== интерфейс и рабочий поток
def build_wood_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"trees": 0, "branches": 0, "clicks": 0, "state": "idle", "view": None}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("wood") and not ctl["active"]:
            log("Лесоруб выключен: включи его переключателем в меню слева")
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
    ttk.Label(box, text="Бегаешь и жмёшь E у дерева сам. Пока идёт рубка (внизу справа «Рубка дерева»), бот кликает; "
                        "когда откроется мини-игра с бревном, бот сам уберёт все ветки.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    cps_var = tk.DoubleVar(value=float(cfg["cps"]))
    chop_var, br_var = tk.BooleanVar(value=cfg["chop"]), tk.BooleanVar(value=cfg["branches"])
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Checkbutton(box, text="Кликать во время рубки", variable=chop_var, command=lambda: save()).grid(
        row=1, column=0, sticky="w", **pad)
    ttk.Label(box, text="кликов в секунду:").grid(row=1, column=1, sticky="e", **pad)
    ttk.Spinbox(box, from_=2, to=15, increment=0.5, width=6, textvariable=cps_var,
                command=lambda: save()).grid(row=1, column=2, sticky="w", **pad)
    ttk.Checkbutton(box, text="Убирать ветки в мини-игре", variable=br_var, command=lambda: save()).grid(
        row=2, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var, command=lambda: save()).grid(
        row=3, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["cps"] = max(2.0, min(15.0, float(cps_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["chop"], cfg["branches"] = bool(chop_var.get()), bool(br_var.get())
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    cps_var.trace_add("write", lambda *a: save())

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

        prev = was = False
        phase_prev = None
        next_click = 0.0
        tries = []                  # [(x, y, попыток)] - ветки в этой мини-игре, по которым уже кликали
        skip = []                   # пропущенные (не убрались)
        mini_t0, mini_n0, last_mini = 0.0, 0, 0.0
        while not ctl["quit"]:
            if not switches.is_on("wood"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                log("Лесоруб включён: жми E у дерева" if was else "Лесоруб выключен")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("wood") else "Выключен (тумблер)"
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
                f = vis.norm(np.ascontiguousarray(scr.grab()[:, :, :3]))
                ph = vis.phase(f)
            except Exception as ex:
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if ph != phase_prev:
                if ph == "chop":
                    log("Рубка: кликаю")
                elif ph == "mini":
                    tries, skip = [], []
                    mini_t0 = now
                    mini_n0 = 0
                    log("Мини-игра: убираю ветки")
                elif phase_prev == "mini":
                    stats["trees"] += 1
                    log(f"Дерево готово: веток убрано {mini_n0} за {last_mini - mini_t0:.1f} с")
                phase_prev = ph
            if ph == "chop":
                BUSY["on"] = True
                stats["state"] = "chop"
                stats["view"] = None
                ctl["status"] = "Рублю дерево"
                if cfg["chop"] and now >= next_click:
                    lclick()
                    stats["clicks"] += 1
                    next_click = now + random.uniform(0.8, 1.2) / max(1.0, cfg["cps"])
                time.sleep(0.01)
            elif ph == "mini":
                BUSY["on"] = True
                last_mini = now
                stats["state"] = "mini"
                bs = vis.branches(f)
                live = [b for b in bs if not near(b["base"], skip)]
                stats["view"] = {"br": [b["pt"] for b in bs], "skip": list(skip)}
                ctl["status"] = f"Убираю ветки: осталось {len(live)}"
                if not cfg["branches"] or not live:
                    time.sleep(0.03)
                    continue
                # серия: по разу в каждую ветку, потом одна проверка (раньше - клик, ждать, проверить)
                clicked = []
                for b in live:
                    t = next((x for x in tries if near(b["base"], [x])), None)
                    if t is not None and any(t is c for c in clicked):
                        continue                                  # две находки у одного основания - одна ветка
                    if t is None:
                        t = [b["base"][0], b["base"][1], 0]
                        tries.append(t)
                    if t[2] >= 4:
                        skip.append((t[0], t[1]))
                        log(f"Ветка у {t[0]},{t[1]} не убирается - пропускаю")
                        continue
                    target = b["aims"][t[2] % len(b["aims"])]                # каждая попытка - другое место ветки
                    stats["view"]["t"] = target
                    inp.move_to(*to_screen(target))
                    time.sleep(0.03)
                    lclick()
                    t[2] += 1
                    stats["clicks"] += 1
                    clicked.append(t)
                    time.sleep(0.04)
                if not clicked:
                    continue
                time.sleep(WAIT_GONE)
                f2 = vis.norm(np.ascontiguousarray(scr.grab()[:, :, :3]))
                left = vis.branches(f2) if vis.phase(f2) == "mini" else []
                for t in clicked:
                    if not near((t[0], t[1]), [b["base"] for b in left]) and t in tries:
                        stats["branches"] += 1
                        mini_n0 += 1
                        tries.remove(t)
            else:
                BUSY["on"] = False
                stats["state"] = "idle"
                stats["view"] = None
                ctl["status"] = "Жду рубку (нажми E у дерева)"
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
        info_var.set(f"Деревьев: {stats['trees']}   ·   веток убрано: {stats['branches']}   ·   кликов: {stats['clicks']}")
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Деревьев", "view": stats["view"],
                "state": stats["state"], "cps": cfg["cps"],
                "items": [("Деревьев", stats["trees"]), ("Веток", stats["branches"])]}
    widgets.register("wood", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
