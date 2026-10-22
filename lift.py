# -*- coding: utf-8 -*-
"""Грузовой лифт (работа «Стройка»): бот проходит мини-игру «Тянуть». E у лифта жмёшь сам: бот включается,
когда справа внизу появилась шахта лифта с красной меткой, и держит метку в зелёной зоне этажа пробелом.

Что выяснено по записи 20261023_000020 (1920x1080):
  - шахта - тёмная полоса x 1707-1742, y ~835-1040; метка - красная скобка высотой ~10 px;
  - зелёная зона этажа (табличка «3 Этаж» слева от неё) - 25 px по высоте (у 3 этажа - y 840-864);
  - держишь пробел - метка поднимается ~125 px/с, отпустил - падает ~90-100 px/с, отклик ~1 кадр;
  - метка в зоне - прогресс растёт ~15%/с, вне зоны - падает ~5%/с; на 100% мини-игра закрывается
    (у игрока ~6,5 с); справа белые кнопки «Тянуть [пробел]» и «Выход ESC» - по ним видно, что игра открыта."""
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
GRAB = (1690, 800, 1900, 1060)         # что снимаем (шахта + кнопки справа)
SHAFT = (1700, 800, 1750, 1060)        # шахта лифта: метка и зелёная зона
ESC_BOX = (1836, 1023, 1886, 1037)     # белая кнопка «ESC»
SPACE_BOX = (1869, 985, 1887, 1005)     # белая кнопка пробела у «Тянуть»
V_UP, V_DOWN = -125.0, 95.0            # скорость метки, px/с (1080p): с пробелом / без
LATENCY = 0.06                         # ввод -> кадр, с (упреждение)


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "lift_config.json")


def default_config():
    return {"toggle_vk": 0x65,            # Numpad 5
            "aim": 0.5, "dead": 1.5, "focus_only": True}


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
    """По снимку GRAB: открыта ли мини-игра, где метка (центр по y) и зелёная зона (верх, низ).
    Координаты - экранные, в пикселях 1080p."""

    def __init__(self, W, H):
        self.ky = H / BASE_H
        g = sc(GRAB, W, H)
        self.ox, self.oy = g[0], g[1]

        def rel(box):
            b = sc(box, W, H)
            return b[0] - g[0], b[1] - g[1], b[2] - g[0], b[3] - g[1]
        self.shaft, self.esc, self.space = rel(SHAFT), rel(ESC_BOX), rel(SPACE_BOX)

    def _white(self, a, box):
        x0, y0, x1, y1 = box
        c = a[y0:y1, x0:x1]
        return float((c.min(axis=2) > 200).mean()) if c.size else 0.0

    def detect(self, img):
        a = img.astype(np.int16)
        out = {"active": False, "y": None, "zone": None}
        if self._white(a, self.esc) < 0.35 or self._white(a, self.space) < 0.35:
            return out
        x0, y0, x1, y1 = self.shaft
        c = a[y0:y1, x0:x1]
        b, g, r = c[:, :, 0], c[:, :, 1], c[:, :, 2]
        red = (r > 180) & (g < 90) & (b > 40) & (b < 150)
        grn = (g > 150) & (r < 140) & (b < 140)
        k = self.ky
        ry = np.where(red.sum(1) >= 10 * k)[0]
        gy = np.where(grn.sum(1) >= 8 * k)[0]
        if not len(ry) or not len(gy):
            return out
        # метка - самый большой кусок подряд идущих красных строк (без случайных пикселей)
        runs = np.split(ry, np.where(np.diff(ry) > 2)[0] + 1)
        m = max(runs, key=len)
        if len(m) < 4 * k:
            return out
        zr = np.split(gy, np.where(np.diff(gy) > 14 * k)[0] + 1)   # зона, разрезанная меткой, - один кусок
        z = max(zr, key=len)
        if not 12 * k <= z[-1] - z[0] + 1 <= 60 * k:
            return out
        to = lambda v: (v + y0 + self.oy) / k
        out.update(active=True, y=to((m[0] + m[-1]) / 2.0), zone=(to(z[0]), to(z[-1])))
        return out


class Controller:
    """Пробел: держать, пока метка ниже цели, иначе отпустить. Упреждение на задержку ввода: где будет метка,
    когда сработает новое решение, считаем по уже отданным, но ещё не видным командам (предиктор Смита);
    без него метка болтается на ~±10 px и вываливается из зоны (25 px при метке 10 px)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.cmds = []                   # (время, держим ли пробел)
        self.hold = False

    def _pending(self, now):
        """Сдвиг метки за LATENCY от уже отданных команд, которые на картинке ещё не видны."""
        t0 = now - LATENCY
        self.cmds = [c for c in self.cmds if c[0] > t0 - 1.0]
        dy, t, h = 0.0, t0, False
        for tc, hc in self.cmds:
            if tc <= t0:
                h = hc
                continue
            dy += (V_UP if h else V_DOWN) * (tc - t)
            t, h = tc, hc
        dy += (V_UP if h else V_DOWN) * (now - t)
        return dy

    def step(self, now, y, zone):
        top, bot = zone
        target = top + (bot - top) * float(self.cfg.get("aim", 0.5))
        dead = float(self.cfg.get("dead", 1.5))
        pred = y + self._pending(now)
        if pred > target + dead:
            hold = True
        elif pred < target - dead:
            hold = False
        else:
            hold = self.hold
        if hold != self.hold or not self.cmds:
            self.cmds.append((now, hold))
        self.hold = hold
        return hold, target


def simulate(cfg=None, dt=0.012, lat=0.05, secs=12.0, zone=(840, 864), start=1034.5, seed=0):
    """Виртуальный лифт по записи: -> (время до 100%, доля времени метки в зоне)."""
    import random
    rnd = random.Random(seed)
    cfg = cfg or default_config()
    ctrl = Controller(cfg)
    y, t, prog, inside, n = start, 0.0, 0.0, 0, 0
    queue_ = []                          # нажатия с задержкой
    hold = False
    while t < secs:
        seen = y + rnd.uniform(-0.7, 0.7)
        h, _ = ctrl.step(t, seen, zone)
        queue_.append((t + lat * rnd.uniform(0.7, 1.3), h))
        while queue_ and queue_[0][0] <= t:
            hold = queue_.pop(0)[1]
        y += (V_UP if hold else V_DOWN) * dt
        y = max(831.0, min(1034.5, y))
        ok = zone[0] <= y - 5 and y + 5 <= zone[1]
        prog = max(0.0, prog + (15.0 if ok else -5.0) * dt)
        inside += ok
        n += 1
        t += dt + rnd.uniform(0, 0.004)
        if prog >= 100:
            return t, inside / n
    return None, inside / n


# ====================================================================== интерфейс и рабочий поток
def build_lift_tab(parent, page_name="Стройка"):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"runs": 0, "view": None, "state": "idle", "inside": 0.0, "last": None}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("lift") and not ctl["active"]:
            log(f"Лифт выключен: включи его переключателем наверху страницы «{page_name}»")
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
    ttk.Label(box, text="Нажми E у пульта грузового лифта как обычно: когда справа внизу появится шахта с красной меткой, "
                        "бот сам будет держать её пробелом в зелёной зоне этажа до 100%.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    aim_var = tk.IntVar(value=int(round(float(cfg["aim"]) * 100)))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Label(box, text="Держать метку на высоте зоны, % (0 - верх, 100 - низ):").grid(row=1, column=0, sticky="w", **pad)
    ttk.Spinbox(box, from_=20, to=80, increment=5, width=6, textvariable=aim_var,
                command=lambda: save()).grid(row=1, column=1, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["aim"] = max(20, min(80, int(aim_var.get()))) / 100.0
        except (tk.TclError, ValueError):
            pass
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    aim_var.trace_add("write", lambda *a: save())

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
        ctrl = Controller(cfg)
        held = {"on": False}

        def press(on):
            if on != held["on"]:
                inp.key("Space", on)
                held["on"] = on

        prev = False
        was = False
        playing = False
        last_seen = 0.0
        game_t0 = 0.0
        n_in = n_all = 0
        zone = None
        while not ctl["quit"]:
            if not switches.is_on("lift"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                press(False)
                ctrl.reset()
                playing = False
                log("Лифт включён: жду мини-игру" if was else "Лифт выключен")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("lift") else "Выключен (тумблер)"
                stats["view"] = None
                time.sleep(0.1)
                continue
            if SEWING_BUSY["on"]:
                press(False)
                ctl["status"] = "Пауза: работает швейный бот"
                time.sleep(0.2)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                press(False)
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            now = time.time()
            try:
                shot = scr.sct.grab({"left": scr.mon["left"] + gx0, "top": scr.mon["top"] + gy0,
                                     "width": gx1 - gx0, "height": gy1 - gy0})
                d = vis.detect(np.asarray(shot)[:, :, :3])
            except Exception as ex:
                press(False)
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if d["active"]:
                last_seen = now
                BUSY["on"] = True
                if not playing:
                    playing = True
                    game_t0 = now
                    ctrl.reset()
                    n_in = n_all = 0
                    zone = None
                    log(f"Мини-игра: зона {d['zone'][0]:.0f}-{d['zone'][1]:.0f}, тяну")
                z = d["zone"]                              # метка закрывает край зоны - держим целую зону
                if zone is None or z[1] - z[0] >= zone[1] - zone[0] or abs(z[0] - zone[0]) > 30 and abs(z[1] - zone[1]) > 30:
                    zone = z
                hold, tgt = ctrl.step(now, d["y"], zone)
                press(hold)
                top, bot = zone
                n_all += 1
                n_in += top <= d["y"] - 5 and d["y"] + 5 <= bot
                stats["inside"] = n_in / n_all
                ctl["status"] = "Тяну лифт"
                stats["state"] = "play"
                stats["view"] = {"y": d["y"], "zone": zone, "t": tgt, "hold": hold}
                time.sleep(0.002)
            else:
                press(False)
                if playing and now - last_seen > 0.5:
                    playing = False
                    BUSY["on"] = False
                    dur = last_seen - game_t0
                    if dur > 3:
                        stats["runs"] += 1
                        stats["last"] = dur
                        log(f"Лифт доехал за {dur:.1f} с (метка в зоне {100 * stats['inside']:.0f}% времени)")
                    else:
                        log("Мини-игра закрылась раньше времени")
                    stats["state"] = "idle"
                    stats["view"] = None
                if not playing:
                    BUSY["on"] = False
                    ctl["status"] = "Жду мини-игру (нажми E у лифта)"
                time.sleep(0.03)
        press(False)
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
        info_var.set(f"Подъёмов: {stats['runs']}"
                     + (f"   ·   последний за {stats['last']:.1f} с" if stats["last"] else "")
                     + (f"   ·   метка в зоне {100 * stats['inside']:.0f}%" if stats["state"] == "play" else ""))
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Подъёмов", "view": stats["view"],
                "state": stats["state"], "inside": stats["inside"], "last": stats["last"],
                "items": [("Подъёмов", stats["runs"])]}
    widgets.register("lift", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
