# -*- coding: utf-8 -*-
"""Стройка: один бот на все мини-игры работы. Включаешь один раз - бот сам узнаёт, что открыто:
  - шлифовка доски (WASD по пятнам)          - sanding.py: Vision, Controller;
  - грузовой лифт (держать метку пробелом)   - lift.py: Vision, Controller;
  - забивка досок молотком (пробел в зону)  - hammer.py: Vision, Tracker, travel.
E (начать мини-игру) жмёт игрок. Модули мини-игр - только зрение и управление, интерфейс и рабочий поток - здесь."""
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
import sanding
import lift
import hammer

BUSY = {"on": False}
GAME_TITLES = ("gta", "rage", "majestic", "grand theft")
NAMES = {"sand": "Шлифовка", "lift": "Лифт", "hammer": "Молоток"}


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "stroyka_config.json")


def default_config():
    return {"toggle_vk": 0x65,            # Numpad 5
            "focus_only": True,
            "sand": sanding.default_config(), "lift": lift.default_config(), "hammer": hammer.default_config()}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        for k in ("toggle_vk", "focus_only"):
            if k in d:
                cfg[k] = d[k]
        for k in NAMES:
            if isinstance(d.get(k), dict):
                cfg[k].update({kk: v for kk, v in d[k].items() if kk in cfg[k]})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def grab(scr, box, W, H):
    x0, y0, x1, y1 = sanding.sc(box, W, H)
    shot = scr.sct.grab({"left": scr.mon["left"] + x0, "top": scr.mon["top"] + y0, "width": x1 - x0, "height": y1 - y0})
    return np.ascontiguousarray(np.asarray(shot)[:, :, :3])


# ====================================================================== мини-игры
class Sand:
    """Шлифовка: машинку WASD к пятнам."""
    key = "sand"

    def __init__(self, scr, inp, W, H, cfg):
        self.scr, self.inp, self.W, self.H, self.cfg = scr, inp, W, H, cfg
        self.vis = sanding.Vision(W, H)
        self.held = {"x": 0, "y": 0}

    def probe(self):
        d = self.vis.detect(grab(self.scr, sanding.GRAB, self.W, self.H))
        return d if d["active"] else None

    def release(self):
        for ax, (neg, pos) in (("x", ("A", "D")), ("y", ("W", "S"))):
            if self.held[ax]:
                self.inp.key(neg if self.held[ax] < 0 else pos, False)
                self.held[ax] = 0

    def apply(self, ux, uy):
        for ax, u, (neg, pos) in (("x", ux, ("A", "D")), ("y", uy, ("W", "S"))):
            if u != self.held[ax]:
                if self.held[ax]:
                    self.inp.key(neg if self.held[ax] < 0 else pos, False)
                if u:
                    self.inp.key(neg if u < 0 else pos, True)
                self.held[ax] = u

    def start(self, d):
        self.ctrl = sanding.Controller(self.cfg)
        self.prev_n = len(d["stains"])
        self.cleared = 0
        return f"шлифовка: пятен {self.prev_n}"

    def step(self, now, d):
        disc = (d["g"][0] + sanding.OFFSET[0], d["g"][1] + sanding.OFFSET[1]) if d["g"] is not None else None
        n = len(d["stains"])
        if 0 < self.prev_n - n <= 3:
            self.cleared += self.prev_n - n
        self.prev_n = n
        ux, uy, tgt = self.ctrl.step(now, disc, d["stains"])
        self.apply(ux, uy)
        return {"left": n, "stains": [(s[0], s[1], s[2]) for s in d["stains"]], "g": disc, "t": tgt}

    def done(self, dur):
        ok = self.cleared >= 3 or self.prev_n == 0
        return ok, f"Доска отшлифована: пятен {self.cleared} за {dur:.1f} с" if ok else "Шлифовка закрылась раньше времени"


class Lift:
    """Грузовой лифт: пробел держать, пока метка ниже зоны этажа."""
    key = "lift"

    def __init__(self, scr, inp, W, H, cfg):
        self.scr, self.inp, self.W, self.H, self.cfg = scr, inp, W, H, cfg
        self.vis = lift.Vision(W, H)
        self.on = False

    def probe(self):
        d = self.vis.detect(grab(self.scr, lift.GRAB, self.W, self.H))
        return d if d["active"] else None

    def release(self):
        if self.on:
            self.inp.key("Space", False)
            self.on = False

    def start(self, d):
        self.ctrl = lift.Controller(self.cfg)
        self.zone = None
        self.n_in = self.n_all = 0
        return f"лифт: зона {d['zone'][0]:.0f}-{d['zone'][1]:.0f}"

    def step(self, now, d):
        z = d["zone"]                              # метка закрывает край зоны - держим целую зону
        zn = self.zone
        if zn is None or z[1] - z[0] >= zn[1] - zn[0] or abs(z[0] - zn[0]) > 30 and abs(z[1] - zn[1]) > 30:
            self.zone = z
        hold, tgt = self.ctrl.step(now, d["y"], self.zone)
        if hold != self.on:
            self.inp.key("Space", hold)
            self.on = hold
        top, bot = self.zone
        self.n_all += 1
        self.n_in += top <= d["y"] - 5 and d["y"] + 5 <= bot
        return {"y": d["y"], "zone": self.zone, "hold": hold, "inside": self.n_in / self.n_all}

    def done(self, dur):
        ok = dur > 3
        return ok, (f"Лифт доехал за {dur:.1f} с (метка в зоне {100 * self.n_in / max(1, self.n_all):.0f}%)"
                    if ok else "Лифт закрылся раньше времени")


class Hammer:
    """Молоток: пробел, когда метка придёт в центр зелёной зоны (с упреждением на задержку игры)."""
    key = "hammer"

    def __init__(self, scr, inp, W, H, cfg):
        self.scr, self.inp, self.W, self.H, self.cfg = scr, inp, W, H, cfg
        self.vis = hammer.Vision(W, H)
        self.tr = hammer.Tracker()

    def probe(self):
        d = self.vis.detect(grab(self.scr, hammer.GRAB, self.W, self.H))
        return d if d["active"] else None

    def release(self):
        pass

    def start(self, d):
        self.tr.reset()
        self.hits = 0
        self.cool = time.time() + 0.3
        self.hit_zone = None
        return "молоток"

    def step(self, now, d):
        v = self.tr.update(now, d["x"])
        z = d["zone"]
        # зона не переехала после удара - ждём (иначе второй удар по той же зоне)
        same = self.hit_zone is not None and abs(z[0] - self.hit_zone[0]) < 4 and abs(z[1] - self.hit_zone[1]) < 4
        if same and now < self.cool + 0.6:
            v = None
        if v and now >= self.cool:
            lead = float(self.cfg["latency_ms"]) / 1000.0
            tt = hammer.travel(self.tr.x, v, (z[0] + z[1]) / 2.0, self.tr.a, self.tr.b)
            if tt is not None and tt - lead < 0.015:
                wait = tt - lead - (time.time() - now)
                if wait > 0:
                    time.sleep(wait)
                self.inp.key("Space", True)
                time.sleep(0.03)
                self.inp.key("Space", False)
                self.hits += 1
                self.hit_zone = z
                self.cool = time.time() + hammer.COOLDOWN
        return {"x": d["x"], "zone": z, "hits": self.hits}

    def done(self, dur):
        ok = dur > 2
        return ok, f"Доска прибита: {self.hits} ударов за {dur:.1f} с" if ok else "Молоток закрылся раньше времени"


# ====================================================================== интерфейс и рабочий поток
def build_stroyka_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"sand": 0, "lift": 0, "hammer": 0, "hits": 0, "mode": None, "view": None, "last": None}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("build") and not ctl["active"]:
            log("Стройка выключена: включи её переключателем в меню слева")
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

    box = ttk.LabelFrame(parent, text="Как работает")
    box.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    ttk.Label(box, text="Включи один раз и работай как обычно: E у стола, у лифта или у стены жмёшь сам, "
                        "а бот сам узнаёт, какая мини-игра открылась (шлифовка, грузовой лифт или молоток), и проходит её.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    vmax_var = tk.IntVar(value=int(cfg["sand"]["vmax"]))
    aim_var = tk.IntVar(value=int(round(float(cfg["lift"]["aim"]) * 100)))
    lat_var = tk.IntVar(value=int(cfg["hammer"]["latency_ms"]))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    rows = [("Шлифовка: максимальная скорость машинки, px/с", vmax_var, 300, 900, 50),
            ("Лифт: держать метку на высоте зоны, % (0 - верх, 100 - низ)", aim_var, 20, 80, 5),
            ("Молоток: упреждение удара, мс (бьёт рано - меньше, поздно - больше)", lat_var, 0, 150, 5)]
    for i, (text, var, lo, hi, step) in enumerate(rows):
        ttk.Label(box, text=text + ":").grid(row=1 + i, column=0, sticky="w", **pad)
        ttk.Spinbox(box, from_=lo, to=hi, increment=step, width=6, textvariable=var,
                    command=lambda: save()).grid(row=1 + i, column=1, sticky="w", **pad)
        var.trace_add("write", lambda *a: save())
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

    def save():
        for var, sect, k, lo, hi, f in ((vmax_var, "sand", "vmax", 300, 900, 1), (aim_var, "lift", "aim", 20, 80, 100),
                                        (lat_var, "hammer", "latency_ms", 0, 150, 1)):
            try:
                v = max(lo, min(hi, int(var.get())))
                cfg[sect][k] = v / 100.0 if f == 100 else v
            except (tk.TclError, ValueError):
                pass
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)

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
        games = [Hammer(scr, inp, W, H, cfg["hammer"]), Lift(scr, inp, W, H, cfg["lift"]),
                 Sand(scr, inp, W, H, cfg["sand"])]

        def release_all():
            for g in games:
                g.release()

        prev = was = False
        cur, last_seen, t0 = None, 0.0, 0.0
        while not ctl["quit"]:
            if not switches.is_on("build"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                release_all()
                cur = None
                log("Стройка включена: жду мини-игру" if was else "Стройка выключена")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("build") else "Выключен (тумблер)"
                stats["mode"] = stats["view"] = None
                time.sleep(0.1)
                continue
            if SEWING_BUSY["on"]:
                release_all()
                ctl["status"] = "Пауза: работает швейный бот"
                time.sleep(0.2)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower() for t in GAME_TITLES):
                release_all()
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            now = time.time()
            try:
                if cur is not None:                       # идёт мини-игра - смотрим только её
                    d = cur.probe()
                    now = time.time()
                    if d:
                        last_seen = now
                        stats["view"] = cur.step(now, d)
                        time.sleep(0.002)
                        continue
                    cur.release()
                    if now - last_seen > 0.6:
                        ok, msg = cur.done(last_seen - t0)
                        log(msg)
                        if ok:
                            stats[cur.key] += 1
                            stats["last"] = (cur.key, last_seen - t0)
                        cur = None
                        stats["mode"] = stats["view"] = None
                        BUSY["on"] = False
                    time.sleep(0.02)
                    continue
                for g in games:                           # ждём: какая мини-игра открылась
                    d = g.probe()
                    if d:
                        cur, last_seen, t0 = g, time.time(), time.time()
                        BUSY["on"] = True
                        stats["mode"] = g.key
                        log("Мини-игра: " + g.start(d))
                        ctl["status"] = NAMES[g.key]
                        break
                else:
                    BUSY["on"] = False
                    ctl["status"] = "Жду мини-игру (E жмёшь сам)"
                    time.sleep(0.05)
            except Exception as ex:
                release_all()
                log(f"Ошибка: {ex}")
                time.sleep(1)
        release_all()
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
        info_var.set(f"Отшлифовано досок: {stats['sand']}   ·   подъёмов лифта: {stats['lift']}   ·   "
                     f"прибито досок: {stats['hammer']}")
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        total = stats["sand"] + stats["lift"] + stats["hammer"]
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Выполнено", "mode": stats["mode"],
                "view": stats["view"], "last": stats["last"],
                "items": [("Выполнено", total), ("Шлифовка", stats["sand"]), ("Лифт", stats["lift"]),
                          ("Молоток", stats["hammer"])]}
    widgets.register("build", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
