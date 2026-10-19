# -*- coding: utf-8 -*-
"""Качалка (упражнения на тренажёре): встаёшь к тренажёру сам. Бот:
  1) когда слева вверху «Нажмите E чтобы сделать упражнения» - жмёт E (подход);
  2) внизу по центру по очереди появляются 4 белых кружка с большой чёрной буквой W/A/S/D -
     бот узнаёт букву и жмёт её.

Что выяснено по записи 20261019_120945 (1920x1080, 10 подходов, 39 кружков):
  - кружок всегда на одном месте (центр 960,1002, радиус ~56); вокруг буквы - обманки: цветные буквы
    поменьше, тонкие линии, чёрные точки;
  - верная клавиша - кружок синеет и улетает вверх, неверная - темнеет и гаснет; следующий кружок
    появляется через ~0,5 с; за подход 4 кружка, потом снова подсказка с E;
  - буква: тёмное после медианного размытия (точки и тонкие линии уходят), крупные куски, сравнение
    с образцами по корреляции. Образцы - буквы из шрифтов Windows (повороты, наклон) + кружки записи;
    только по шрифтам - 0 ошибок на 281 кадре записи (там и буквы двух разных шрифтов)."""
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
PROMPT = (20, 22, 350, 64)             # «Нажмите E чтобы сделать упражнения» слева вверху
CENTER, R_DISK = (960, 1002), 56       # кружок с буквой
N = 28                                 # размер образца буквы
MIN_SIM = 0.55                         # ниже - не уверены в букве, не жмём
AFTER_E = 2.0                          # после E столько не жмём E снова (ждём кружок)
RESULT_WAIT = 1.0                      # сколько ждать, пока кружок посинеет/погаснет после нажатия


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


CONFIG_PATH = os.path.join(app_dir(), "gym_config.json")


def default_config():
    return {"toggle_vk": 0x68,            # Numpad 8
            "react": 0.5, "auto_e": True, "e_delay": 0.6, "focus_only": True}


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
_yy, _xx = np.mgrid[-R_DISK - 14:R_DISK + 15, -R_DISK - 14:R_DISK + 15]
_rr = np.hypot(_xx, _yy)
RING = (_rr > 38) & (_rr < 50)         # белый край кружка (буква и обманки сюда почти не заходят)
OUTER = (_rr > 61) & (_rr < 68)        # снаружи кружка - не белое
DISK = _rr < 47                        # где ищем букву


def glyph_vec(mask):
    """Маска буквы -> нормированный вектор N*N (по рамке буквы, квадрат, лёгкое размытие)."""
    x, y, w, h = cv2.boundingRect(mask)
    if w < 4 or h < 4:
        return None
    s = max(w, h)
    pad = np.zeros((s, s), np.float32)
    pad[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = mask[y:y + h, x:x + w]
    g = cv2.GaussianBlur(cv2.resize(pad, (N, N), interpolation=cv2.INTER_AREA), (3, 3), 0)
    v = g.ravel() - g.mean()
    return v / (np.linalg.norm(v) + 1e-6)


class Vision:
    """Кадр экрана (BGR, любой размер) -> подсказка E и кружок с буквой. Всё в координатах 1920x1080."""

    def __init__(self):
        a = np.load(res_path("gym_assets.npz"))
        self.tp = a["prompt"]
        self.tx, self.ty = a["tx"].astype(np.float32), [str(c) for c in a["ty"]]

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

    def prompt(self, f):
        x0, y0, x1, y1 = PROMPT
        g = cv2.cvtColor(f[y0 - 10:y1 + 10, x0 - 10:x1 + 10], cv2.COLOR_BGR2GRAY)
        return float(cv2.matchTemplate(g, self.tp, cv2.TM_CCOEFF_NORMED).max()) > 0.7

    @staticmethod
    def crop(f):
        r = R_DISK + 14
        return f[CENTER[1] - r:CENTER[1] + r + 1, CENTER[0] - r:CENTER[0] + r + 1]

    @staticmethod
    def disk(f):
        """-> "white" (кружок ждёт нажатия), "blue" (верно, улетает), None."""
        hsv = cv2.cvtColor(Vision.crop(f), cv2.COLOR_BGR2HSV)
        h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        white = (v > 200) & (s < 40)
        if white[RING].mean() > 0.6 and white[OUTER].mean() < 0.3:
            return "white"
        if ((h > 95) & (h < 110) & (s > 120) & (v > 180))[RING].mean() > 0.3:
            return "blue"
        return None

    def letter(self, f):
        """-> (буква, уверенность) или (None, 0)."""
        v = cv2.medianBlur(self.crop(f).max(axis=2), 5)       # точки и тонкие линии пропадают
        m = ((v < 110) & DISK).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
        if n < 2:
            return None, 0.0
        big = max(st[j, 4] for j in range(1, n))
        if big < 150:
            return None, 0.0
        keep = np.isin(lab, [j for j in range(1, n) if st[j, 4] > 0.15 * big]).astype(np.uint8)
        vec = glyph_vec(keep)
        if vec is None:
            return None, 0.0
        sim = self.tx @ vec
        j = int(np.argmax(sim))
        return self.ty[j], float(sim[j])


def analyze_video(path, step=1):
    """Проверка зрения на записи: печатает, когда видна подсказка и какую букву бот видит в кружке."""
    vis = Vision()
    cap = cv2.VideoCapture(path)
    i, last = 0, None
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if i % step:
            continue
        f = vis.norm(fr)
        d = vis.disk(f)
        s = "E" if vis.prompt(f) else ""
        if d == "white":
            L, sim = vis.letter(f)
            s = f"{L} {sim:.2f}"
        elif d:
            s = d
        if s != last:
            print(f"кадр {i}: {s or '-'}")
            last = s


# ====================================================================== интерфейс и рабочий поток
def build_gym_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"sets": 0, "reps": 0, "miss": 0, "state": "idle", "letter": None, "stage": 0}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("gym") and not ctl["active"]:
            log("Качалка выключена: включи её переключателем в меню слева")
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
    ttk.Label(box, text="Подходишь к тренажёру сам. Бот жмёт E, когда слева вверху «Нажмите E чтобы сделать упражнения», "
                        "и сам нажимает буквы W/A/S/D из кружков внизу по центру.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    react_var = tk.DoubleVar(value=float(cfg["react"]))
    e_var, edel_var = tk.BooleanVar(value=cfg["auto_e"]), tk.DoubleVar(value=float(cfg["e_delay"]))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Label(box, text="Пауза перед нажатием буквы, с:").grid(row=1, column=0, sticky="w", **pad)
    ttk.Spinbox(box, from_=0.1, to=3, increment=0.05, width=6, textvariable=react_var,
                command=lambda: save()).grid(row=1, column=1, sticky="w", **pad)
    ttk.Checkbutton(box, text="Сам жать E (новый подход)", variable=e_var, command=lambda: save()).grid(
        row=2, column=0, sticky="w", **pad)
    ttk.Label(box, text="пауза, с:").grid(row=2, column=1, sticky="e", **pad)
    ttk.Spinbox(box, from_=0.2, to=5, increment=0.1, width=6, textvariable=edel_var,
                command=lambda: save()).grid(row=2, column=2, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var, command=lambda: save()).grid(
        row=3, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["react"] = max(0.1, min(3.0, float(react_var.get())))
        except (tk.TclError, ValueError):
            pass
        try:
            cfg["e_delay"] = max(0.2, min(5.0, float(edel_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["auto_e"], cfg["focus_only"] = bool(e_var.get()), bool(focus_var.get())
        save_config(cfg)
    react_var.trace_add("write", lambda *a: save())
    edel_var.trace_add("write", lambda *a: save())

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

        def grab():
            return vis.norm(np.ascontiguousarray(scr.grab()[:, :, :3]))

        def wait(sec):
            """Пауза; False - бота выключили."""
            end = time.time() + sec
            while time.time() < end:
                if ctl["quit"] or not ctl["active"]:
                    return False
                time.sleep(0.01)
            return True

        prev = was = False
        seen_t, seen_l = 0.0, None   # когда появился кружок и какую букву в нём видели
        prompt_t, last_e = 0.0, 0.0
        in_set = False
        while not ctl["quit"]:
            if not switches.is_on("gym"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                log("Качалка включена: встань к тренажёру" if was else "Качалка выключена")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("gym") else "Выключен (тумблер)"
                stats["state"], stats["letter"] = "idle", None
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
                d = vis.disk(f)
            except Exception as ex:
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if d == "white":
                BUSY["on"] = True
                in_set = True
                L, sim = vis.letter(f)
                if L is None or sim < MIN_SIM:
                    seen_l = None
                    ctl["status"] = "Не узнаю букву…"
                    time.sleep(0.03)
                    continue
                if L != seen_l:                              # новый кружок (или первый кадр ещё не встал)
                    seen_t, seen_l = now, L
                    time.sleep(0.03)
                    continue
                stats["state"], stats["letter"] = "key", L
                ctl["status"] = f"Кружок: {L}"
                if not wait(max(0.0, seen_t + cfg["react"] * random.uniform(0.8, 1.25) - time.time())):
                    continue
                inp.tap(L, random.randint(60, 110))
                res, end = None, time.time() + RESULT_WAIT
                while time.time() < end and res is None:
                    time.sleep(0.03)
                    st = vis.disk(grab())
                    if st == "blue":
                        res = "ok"
                    elif st is None:
                        res = "miss"
                if res == "ok":
                    stats["reps"] += 1
                    stats["stage"] = stats["stage"] % 4 + 1
                elif res == "miss":
                    stats["miss"] += 1
                    stats["stage"] = stats["stage"] % 4 + 1
                    log(f"Нажал {L} - не засчитано")
                seen_l = None
                continue
            if d == "blue":
                time.sleep(0.03)
                continue
            if vis.prompt(f):
                BUSY["on"] = True
                if in_set:
                    in_set = False
                    stats["sets"] += 1
                    log(f"Подход готов (всего {stats['sets']})")
                stats["state"], stats["letter"], stats["stage"] = "prompt", None, 0
                if not prompt_t:
                    prompt_t = now
                if not cfg["auto_e"]:
                    ctl["status"] = "Жми E - новый подход"
                elif now - last_e > AFTER_E:
                    ctl["status"] = "Сейчас нажму E"
                    if now - prompt_t >= cfg["e_delay"] * random.uniform(0.85, 1.2):
                        inp.tap("E", random.randint(60, 110))
                        last_e = now
                        log("E: новый подход")
                time.sleep(0.03)
                continue
            prompt_t = 0.0
            if in_set and now - last_e < 4:
                ctl["status"] = "Жду кружок…"
                time.sleep(0.03)
                continue
            BUSY["on"] = in_set
            stats["state"], stats["letter"] = ("set" if in_set else "idle"), None
            ctl["status"] = "Жду кружок…" if in_set else "Жду тренажёр (встань к нему)"
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
        info_var.set(f"Подходов: {stats['sets']}   ·   букв верно: {stats['reps']}   ·   мимо: {stats['miss']}")
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Подходов",
                "state": stats["state"], "letter": stats["letter"], "stage": stats["stage"], "react": cfg["react"],
                "miss": stats["miss"], "items": [("Подходов", stats["sets"]), ("Букв", stats["reps"])]}
    widgets.register("gym", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
