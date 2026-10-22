# -*- coding: utf-8 -*-
"""Забивка досок молотком (работа «Стройка»): бот проходит мини-игру «Ударить молотком». Доску приносишь
и E у стены жмёшь сам: бот включается, когда справа внизу появилась полоска с бегущей красной меткой,
и жмёт пробел, когда метка в зелёной зоне.

Что выяснено по записи 20261023_000423 (1920x1080):
  - полоска y 954-999, метка (красная «I», ~6 px) бегает туда-сюда между x ~1610 и ~1875 со скоростью
    ~420-480 px/с; зелёная зона - 38 px, после КАЖДОГО удара (и попадания, и промаха) переезжает в другое место;
  - попадание добавляет прогресс, промах - отнимает; на 100% мини-игра закрывается;
  - снизу белые кнопки «Ударить молотком [пробел]» (1716-1746) и «Выход ESC» (1820-1884), y 1030-1056."""
import os
import sys
import json
import time
import queue
import threading

import numpy as np

import switches
from sewing_bot import key_pressed, vk_name, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}
BASE_W, BASE_H = 1920, 1080
GRAB = (1590, 940, 1900, 1060)         # что снимаем (полоска + кнопки)
BAR = (1595, 950, 1890, 1003)          # полоска: метка и зелёная зона
SPACE_BOX = (1719, 1033, 1743, 1053)   # белая кнопка пробела у «Ударить молотком»
ESC_BOX = (1824, 1034, 1880, 1052)     # белая кнопка «ESC»
X_MIN, X_MAX = 1610.5, 1874.5          # где разворачивается метка (уточняется на ходу)
COOLDOWN = 0.35                        # после удара ждём, пока зона переедет


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "hammer_config.json")


def default_config():
    return {"toggle_vk": 0x64,            # Numpad 4
            "latency_ms": 40, "margin": 7, "focus_only": True}


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


def sc(box, W, H):
    kx, ky = W / BASE_W, H / BASE_H
    return int(box[0] * kx), int(box[1] * ky), int(box[2] * kx), int(box[3] * ky)


class Vision:
    """По снимку GRAB: открыта ли мини-игра, где метка (центр по x) и зелёная зона (лево, право).
    Координаты - экранные, в пикселях 1080p."""

    def __init__(self, W, H):
        self.kx, self.ky = W / BASE_W, H / BASE_H
        g = sc(GRAB, W, H)
        self.ox = g[0]

        def rel(box):
            b = sc(box, W, H)
            return b[0] - g[0], b[1] - g[1], b[2] - g[0], b[3] - g[1]
        self.bar, self.esc, self.space = rel(BAR), rel(ESC_BOX), rel(SPACE_BOX)

    def _white(self, a, box):
        x0, y0, x1, y1 = box
        c = a[y0:y1, x0:x1]
        return float((c.min(axis=2) > 200).mean()) if c.size else 0.0

    def detect(self, img):
        a = img.astype(np.int16)
        out = {"active": False, "x": None, "zone": None}
        if self._white(a, self.esc) < 0.35 or self._white(a, self.space) < 0.35:
            return out
        x0, y0, x1, y1 = self.bar
        c = a[y0:y1, x0:x1]
        b, g, r = c[:, :, 0], c[:, :, 1], c[:, :, 2]
        red = (r > 180) & (g < 90) & (b > 40) & (b < 150)
        grn = (g > 110) & (g - np.maximum(r, b) > 40)          # зона внутри тёмно-зелёная (77,140,80), края ярче
        rx = np.where(red.sum(0) >= 25 * self.ky)[0]
        gx = np.where(grn.sum(0) >= 12 * self.ky)[0]
        if not len(rx) or not len(gx):
            return out
        runs = np.split(rx, np.where(np.diff(rx) > 2)[0] + 1)
        m = max(runs, key=len)
        zr = np.split(gx, np.where(np.diff(gx) > 10 * self.kx)[0] + 1)   # метка поверх зоны режет её на два куска
        z = max(zr, key=len)
        if not 20 * self.kx <= z[-1] - z[0] + 1 <= 70 * self.kx:
            return out
        to = lambda v: (v + x0 + self.ox) / self.kx
        out.update(active=True, x=to((m[0] + m[-1]) / 2.0), zone=(to(z[0]), to(z[-1])))
        return out


def travel(p, v, target, a, b):
    """Через сколько секунд метка (позиция p, скорость v, разворот у a и b) окажется в target."""
    if abs(v) < 1:
        return None
    target = min(max(target, a), b)
    if v > 0:
        d = target - p if target >= p else (b - p) + (b - target)
    else:
        d = p - target if target <= p else (p - a) + (target - a)
    return d / abs(v)


class Tracker:
    """Скорость и края хода метки по последним кадрам."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.hist = []
        self.speed = 450.0
        self.a, self.b = X_MIN, X_MAX

    def update(self, now, x):
        self.hist = [(t, v) for t, v in self.hist if now - t < 0.3] + [(now, x)]
        self.a, self.b = min(self.a, x), max(self.b, x)
        if len(self.hist) < 2:
            return None
        (t1, x1), (t2, x2) = self.hist[-2], self.hist[-1]
        if t2 - t1 < 1e-3 or abs(x2 - x1) < 0.5:
            return None
        v = (x2 - x1) / (t2 - t1)
        near_edge = min(x1, x2) - self.a < 25 or self.b - max(x1, x2) < 25
        if not near_edge and 150 < abs(v) < 1500:       # у края метка развернулась между кадрами - скорость врёт
            self.speed += (abs(v) - self.speed) * 0.3
        return self.speed if v > 0 else -self.speed


def simulate(cfg=None, n=200, seed=0, lat=0.035, frame=0.006, speed=450.0):
    """Виртуальный молоток: n ударов по случайным зонам -> доля попаданий."""
    import random
    rnd = random.Random(seed)
    cfg = cfg or default_config()
    a, b = X_MIN, X_MAX
    p, v, t = a, speed, 0.0
    tr = Tracker()
    hits = tries = 0
    zone = None
    cool = 0.0

    def pos(t_):
        span = b - a
        s = (t_ * speed) % (2 * span)
        return a + (s if s < span else 2 * span - s)
    while tries < n:
        if zone is None:
            z0 = rnd.uniform(1608, 1840)
            zone = (z0, z0 + 37)
        seen = pos(t - frame * rnd.uniform(0.5, 1.5)) + rnd.uniform(-0.8, 0.8)
        vv = tr.update(t, seen)
        fire = None
        if vv and t >= cool:
            lead = float(cfg["latency_ms"]) / 1000.0
            tt = travel(seen, vv, (zone[0] + zone[1]) / 2, tr.a, tr.b)
            if tt is not None and tt - lead < 0.012:
                fire = t + max(0.0, tt - lead)
        if fire is not None:
            land = fire + lat * rnd.uniform(0.7, 1.3)
            x = pos(land)
            m = 3.0
            hits += zone[0] - m <= x <= zone[1] + m
            tries += 1
            zone = None
            cool = fire + COOLDOWN
            t = fire
        t += frame + rnd.uniform(0, 0.004)
    return hits / tries


# ====================================================================== интерфейс и рабочий поток
def build_hammer_tab(parent, page_name="Стройка"):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"boards": 0, "hits": 0, "view": None, "state": "idle", "last": None}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("hammer") and not ctl["active"]:
            log(f"Молоток выключен: включи его переключателем наверху страницы «{page_name}»")
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

    box = ttk.LabelFrame(parent, text="Как проходить")
    box.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    ttk.Label(box, text="Принеси доску и нажми E у стены как обычно: когда справа внизу появится полоска с красной меткой, "
                        "бот сам будет бить молотком (пробел), когда метка в зелёной зоне.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    lat_var = tk.IntVar(value=int(cfg["latency_ms"]))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Label(box, text="Упреждение удара, мс (бьёт мимо в сторону движения метки - меньше, не доходит - больше):").grid(
        row=1, column=0, sticky="w", **pad)
    ttk.Spinbox(box, from_=0, to=150, increment=5, width=6, textvariable=lat_var,
                command=lambda: save()).grid(row=1, column=1, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["latency_ms"] = max(0, min(150, int(lat_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    lat_var.trace_add("write", lambda *a: save())

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
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        scr = AF.Screen()
        inp = AF.Input2()
        W, H = scr.mon["width"], scr.mon["height"]
        vis = Vision(W, H)
        gx0, gy0, gx1, gy1 = sc(GRAB, W, H)
        tr = Tracker()

        def tap():
            inp.key("Space", True)
            time.sleep(0.03)
            inp.key("Space", False)

        prev = False
        was = False
        playing = False
        last_seen = 0.0
        game_t0 = 0.0
        cool_until = 0.0
        hit_zone = None
        hits = 0
        while not ctl["quit"]:
            if not switches.is_on("hammer"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                tr.reset()
                playing = False
                log("Молоток включён: жду мини-игру" if was else "Молоток выключен")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("hammer") else "Выключен (тумблер)"
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
                shot = scr.sct.grab({"left": scr.mon["left"] + gx0, "top": scr.mon["top"] + gy0,
                                     "width": gx1 - gx0, "height": gy1 - gy0})
                now = time.time()
                d = vis.detect(np.asarray(shot)[:, :, :3])
            except Exception as ex:
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if d["active"]:
                last_seen = now
                BUSY["on"] = True
                if not playing:
                    playing = True
                    game_t0 = now
                    tr.reset()
                    hits = 0
                    cool_until = now + 0.3
                    hit_zone = None
                    log("Мини-игра: бью молотком")
                v = tr.update(now, d["x"])
                z = d["zone"]
                # зона не переехала после удара - ждём (иначе второй удар по той же зоне)
                same = hit_zone is not None and abs(z[0] - hit_zone[0]) < 4 and abs(z[1] - hit_zone[1]) < 4
                if same and now < cool_until + 0.6:
                    v = None
                if v and now >= cool_until:
                    lead = float(cfg["latency_ms"]) / 1000.0
                    mid = (z[0] + z[1]) / 2.0
                    tt = travel(d["x"], v, mid, tr.a, tr.b)
                    if tt is not None and tt - lead < 0.015:
                        wait = tt - lead - (time.time() - now)
                        if wait > 0:
                            time.sleep(wait)
                        tap()
                        hits += 1
                        stats["hits"] += 1
                        hit_zone = z
                        cool_until = time.time() + COOLDOWN
                ctl["status"] = "Бью молотком"
                stats["state"] = "play"
                stats["view"] = {"x": d["x"], "zone": z}
                time.sleep(0.001)
            else:
                if playing and now - last_seen > 0.6:
                    playing = False
                    BUSY["on"] = False
                    dur = last_seen - game_t0
                    if dur > 2:
                        stats["boards"] += 1
                        stats["last"] = dur
                        log(f"Доска прибита: {hits} ударов за {dur:.1f} с")
                    else:
                        log("Мини-игра закрылась раньше времени")
                    stats["state"] = "idle"
                    stats["view"] = None
                if not playing:
                    BUSY["on"] = False
                    ctl["status"] = "Жду мини-игру (принеси доску и нажми E)"
                time.sleep(0.03)
        BUSY["on"] = False
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass

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
        info_var.set(f"Досок прибито: {stats['boards']}   ·   ударов: {stats['hits']}"
                     + (f"   ·   последняя за {stats['last']:.1f} с" if stats["last"] else ""))
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Досок", "view": stats["view"],
                "state": stats["state"], "last": stats["last"],
                "items": [("Досок", stats["boards"]), ("Ударов", stats["hits"])]}
    widgets.register("hammer", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
