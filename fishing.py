# -*- coding: utf-8 -*-
"""
Бот рыбалки Majestic RP - полный цикл, как в игре:
  1) E - начать рыбалку (если включено);
  2) заброс: по полоске бегает белая метка, Space нужно нажать, когда она в зелёной зоне -
     бот предугадывает движение метки с поправкой на задержку;
  3) ожидание: в окошке с поплавком справа от хотбара при поклёвке появляются брызги -
     бот подсекает (Space);
  4) вываживание: плашка с рыбкой слева от брызг - держит D, справа - держит A;
  5) окно "Вы поймали рыбу": забрать всё или отпустить всё (ради опыта) - и снова заброс.
"""
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
from sewing_bot import key_pressed, vk_name, SKIP_VKS, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}            # True, пока идёт рыбалка (анти-АФК в это время молчит)
CONFIG_PATH = os.path.join(AF.app_dir(), "fishing_config.json")
ZONE = (500, 250, 1420, 760)                 # где искать плашку с рыбкой на воде (для 1920x1080)


def default_config():
    return {"toggle_vk": 0x6F,               # Numpad /
            "invert": False, "hold_after_lost": 1.2, "zone": None, "focus_only": True,
            "auto_start": True, "start_key": "E", "auto_cast": True, "cast_key": "Space",
            "latency_ms": 45, "auto_hook": True, "hook_key": "Space",
            "after_catch": "keep"}           # keep | release | none


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        cfg.update({k: v for k, v in d.items() if k in cfg})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)
    except Exception:
        pass


# ================================================================ состояния игры (координаты для 1920x1080)
BASE_W, BASE_H = 1920, 1080
CAST_BAND = (600, 878, 1320, 907)
FLOAT_BOX = (1350, 845, 1520, 1005)
HINTS_BOX = (1450, 1020, 1920, 1075)
PROG_LEFT = (677, 891, 702, 899)
BAR_BAND = (660, 862, 1210, 890)
PANEL = (760, 300, 1170, 770)
KEEP_BOX = (778, 706, 955, 748)
REL_BOX = (966, 706, 1143, 748)

def sc(box, W, H):
    kx, ky = W / BASE_W, H / BASE_H
    return int(box[0] * kx), int(box[1] * ky), int(box[2] * kx), int(box[3] * ky)

def crop(img, box):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = sc(box, W, H)
    return img[y0:y1, x0:x1], x0, y0

def hints_on(img):
    a, _, _ = crop(img, HINTS_BOX)
    return (a.min(axis=2) > 200).mean() > 0.04

def bite(img):
    a, _, _ = crop(img, FLOAT_BOX)
    return (a.min(axis=2) > 150).mean() > 0.012

def minigame_on(img):
    """Полоска прогресса с белой рыбкой (подсказок Esc/Space в это время нет)."""
    a, _, _ = crop(img, BAR_BAND)
    return (a.min(axis=2) > 200).sum() > 40 * (img.shape[1] / BASE_W) ** 2 and not hints_on(img)

def cast_bar(img):
    """-> (зона_слева, зона_справа, x_метки) в пикселях экрана или None"""
    a, x0, _ = crop(img, CAST_BAND)
    a = a.astype(np.int16)
    green = ((a[:, :, 1] - np.maximum(a[:, :, 0], a[:, :, 2])) > 60).sum(axis=0) >= 3
    white = (a.min(axis=2) > 215).sum(axis=0) >= max(6, a.shape[0] // 4)
    gx = np.nonzero(green)[0]
    if len(gx) < 8:
        return None
    # самый длинный непрерывный кусок зелёного
    runs, s = [], gx[0]
    for p, q in zip(gx, gx[1:]):
        if q - p > 3:
            runs.append((s, p)); s = q
    runs.append((s, gx[-1]))
    g0, g1 = max(runs, key=lambda r: r[1] - r[0])
    k = img.shape[1] / BASE_W
    if not (25 * k <= g1 - g0 <= 130 * k):
        return None
    wx = np.nonzero(white)[0]
    if len(wx) == 0:
        return (x0 + g0, x0 + g1, None)
    return (x0 + g0, x0 + g1, x0 + float(wx.mean()))

TITLE = (772, 322, 1000, 346)
TITLE_BG = (772, 300, 1000, 318)


def dialog_on(img):
    """Окно "Вы поймали рыбу": белый заголовок на тёмной панели и две кнопки."""
    t, _, _ = crop(img, TITLE)
    bg, _, _ = crop(img, TITLE_BG)
    k = img.shape[1] / BASE_W
    if (t.min(axis=2) > 200).sum() < 500 * k * k or bg.max() > 70:
        return False
    kb, _, _ = crop(img, KEEP_BOX)
    rb, _, _ = crop(img, REL_BOX)
    return (kb.max(axis=2) > 95).sum() > 150 * k * k and (rb.max(axis=2) > 95).sum() > 150 * k * k

def button_center(img, which):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = sc(KEEP_BOX if which == "keep" else REL_BOX, W, H)
    return (x0 + x1) // 2, (y0 + y1) // 2


# ================================================================ зрение

def _glyph(img):
    """Плашка по тёмному силуэту рыбки (серые плашки, и зелёные с тёмной рыбкой)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mx = img.max(axis=2)
    for thr in (70, 95):                         # near-black, затем тёмно-зелёный
        dark = (mx < thr).astype(np.uint8)
        n, lab, st, cen = cv2.connectedComponentsWithStats(dark, connectivity=8)
        H, W = gray.shape
        best = None
        for k in range(1, n):
            x, y, w, h, a = st[k]
            if not (6 <= w <= 60 and 3 <= h <= 40 and 1.2 <= w / max(1, h) <= 4.8
                    and 0.25 < a / (w * h) < 0.92):
                continue
            p = max(3, int(w * 0.4))
            X0, Y0, X1, Y1 = max(0, x - p), max(0, y - p), min(W, x + w + p), min(H, y + h + p)
            ring = gray[Y0:Y1, X0:X1]
            if ring.size == 0:
                continue
            light = float((ring > 110).mean())
            if light < 0.33:
                continue
            score = a * light
            if best is None or score > best[0]:
                best = (score, x + w / 2, y + h / 2, w * 1.7)
        if best is not None:
            return best
    return None


def _green_plate(img):
    """Зелёная плашка целиком (когда рыбка внутри неё тоже зеленоватая)."""
    b, g, r = [img[:, :, i].astype(np.int16) for i in range(3)]
    green = ((g - np.maximum(r, b) > 22) & (g > 70)).astype(np.uint8)
    green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(green, connectivity=8)
    best = None
    for k in range(1, n):
        x, y, w, h, a = st[k]
        if not (16 <= w <= 90 and 16 <= h <= 90 and 0.7 < w / h < 1.4 and a > 0.7 * w * h):
            continue
        if best is None or a > best[0]:
            best = (a, float(cen[k][0]), float(cen[k][1]), float(w))
    return best


def find_marker(img):
    a = _glyph(img)
    b = _green_plate(img)
    if a and b:
        best = a if a[0] >= b[0] else b          # у зелёной плашки площадь больше - обычно она ближе/крупнее
    else:
        best = a or b
    return None if best is None else best[1:]


def fish_heading(sub):
    """Куда смотрит рыбка внутри плашки: +1 голова справа, -1 слева, 0 не ясно.
    Голова - толстый конец (силуэт выше), хвост - тонкий."""
    if sub.size == 0:
        return 0
    gi = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY).astype(np.int16)
    dark = gi < gi.mean() - 16
    xs = np.nonzero(dark.any(axis=0))[0]
    if len(xs) < 6:
        return 0
    x0, x1 = xs.min(), xs.max()
    heights = np.array([np.ptp(np.nonzero(dark[:, c])[0]) if dark[:, c].any() else 0
                        for c in range(x0, x1 + 1)], dtype=float)
    n = len(heights)
    if n < 6:
        return 0
    lh = heights[:n // 2].mean()
    rh = heights[n // 2:].mean()
    if abs(rh - lh) < 1.0:
        return 0
    return 1 if rh > lh else -1


def splash_x(img, cx, cy, size):
    """Центр брызг рядом с плашкой (работает только на тёмной воде). -> x или None"""
    H, W = img.shape[:2]
    half = int(size * 6)
    x0, x1 = max(0, int(cx - half)), min(W, int(cx + half))
    y0, y1 = max(0, int(cy - size * 2.5)), min(H, int(cy + size * 2.5))
    win = img[y0:y1, x0:x1]
    if win.size == 0:
        return None
    gray = cv2.cvtColor(win, cv2.COLOR_BGR2GRAY).astype(np.int16)
    spread = win.max(axis=2).astype(np.int16) - win.min(axis=2).astype(np.int16)
    # брызги видны, только если фон вокруг тёмный (открытая вода)
    if gray.mean() > 110:
        return None
    bright = (gray > 150) & (spread < 40)
    mx0, mx1 = int(cx - size * 0.8) - x0, int(cx + size * 0.8) - x0
    my0, my1 = int(cy - size * 0.8) - y0, int(cy + size * 0.8) - y0
    bright[max(0, my0):max(0, my1), max(0, mx0):max(0, mx1)] = False
    cols = bright.sum(axis=0).astype(float)
    if cols.sum() < size * 3:
        return None
    k = max(3, int(size)) | 1
    sm = np.convolve(cols, np.ones(k) / k, "same")
    return x0 + float(np.argmax(sm))


class Tracker:
    """Определяет, куда тянет рыба. Два способа:
    1) брызги: если видно, в какую сторону от плашки они, тянет в ту сторону (точно);
    2) движение плашки: плашку тянет в сторону, куда она уезжает (работает везде, в т.ч. у причала).
    Рыбу держим клавишей в противоположную сторону."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.prev_x = None
        self.prev_t = None
        self.vel = 0.0
        self.last_dir = 0
        self.last_seen = 0.0

    def update(self, screen, zone, now):
        x0, y0, x1, y1 = zone
        z = screen[y0:y1, x0:x1]
        m = find_marker(z)
        if m is None:
            self.prev_x = None
            # плашку заслонило удочкой/брызгами - недолго держим прежнее направление
            if self.last_dir and now - self.last_seen < 0.6:
                return self.last_dir
            return None
        self.last_seen = now
        mx, my, size = m
        half = int(size / 2)
        sub = z[max(0, int(my - half)):int(my + half), max(0, int(mx - half)):int(mx + half)]
        # способ 1 - куда смотрит сама рыбка (видно всегда, не зависит от фона и размера)
        dir_fish = fish_heading(sub)
        # способ 2 - брызги (только на открытой тёмной воде)
        sx = splash_x(z, mx, my, size)
        dir_splash = None
        if sx is not None and abs(mx - sx) > size * 0.4:
            dir_splash = -1 if mx < sx else 1
        # способ 3 - движение плашки
        if self.prev_x is not None and now > self.prev_t:
            v = (mx - self.prev_x) / (now - self.prev_t)
            self.vel = 0.6 * self.vel + 0.4 * v
        self.prev_x, self.prev_t = mx, now
        dir_move = -1 if self.vel < -12 else 1 if self.vel > 12 else None
        d = dir_fish or dir_splash or dir_move
        if d:
            self.last_dir = d
        return d


# ================================================================ движок
class Fisher:
    """Полный цикл рыбалки. grab() - снимок экрана, grab_box(box) - снимок кусочка
    (для быстрого слежения за меткой заброса), clock() - время."""

    def __init__(self, cfg, inp, log, grab, grab_box=None, clock=time.time, sleep=time.sleep):
        self.cfg, self.inp, self.log = cfg, inp, log
        self.grab, self.grab_box = grab, grab_box
        self.clock, self.sleep = clock, sleep
        self.held = None
        self.last_seen = 0.0
        self.bite_frames = 0
        self.last = {}
        self.e_tries = 0
        self.ui_seen = 0.0
        self.in_fight = False
        self.tracker = Tracker()
        self.stats = {"casts": 0, "bites": 0, "kept": 0, "released": 0}
        self.state = "Жду"

    def hold(self, key):
        if self.held == key:
            return
        if self.held:
            self.inp.key(self.held, False)
        if key:
            self.inp.key(key, True)
        self.held = key

    def ago(self, what):
        return self.clock() - self.last.get(what, -1e9)

    def mark(self, what):
        self.last[what] = self.clock()

    # ---------------------------------------------------------------- заброс
    def cast(self, first):
        """Следит за меткой и жмёт Space, когда она будет в зелёной зоне."""
        cfg = self.cfg
        lat = cfg["latency_ms"] / 1000.0
        prev = None
        t_end = self.clock() + 8
        t_check = self.clock()
        cb = first
        while self.clock() < t_end:
            if self.clock() - t_check > 0.3:          # не открылось ли окно улова
                t_check = self.clock()
                if dialog_on(self.grab()):
                    return False
            if cb is None:
                return False
            g0, g1, x = cb
            now = self.clock()
            if x is not None:
                if prev is not None and now > prev[0]:
                    v = (x - prev[1]) / (now - prev[0])
                    if abs(v) > 50:                  # метка движется
                        xp = x + v * lat
                        m = (g1 - g0) * 0.2
                        if g0 + m <= xp <= g1 - m:
                            self.inp.tap(cfg["cast_key"], 60)
                            self.stats["casts"] += 1
                            self.log(f"Заброс: метка в зелёной зоне ({int(xp)} в {int(g0)}-{int(g1)})")
                            self.mark("cast")
                            return True
                prev = (now, x)
            if self.grab_box:
                img, bx0 = self.grab_box(CAST_BAND)
                cb = cast_bar_band(img, bx0, g0, g1)
            else:
                cb = cast_bar(self.grab())
        return False

    # ---------------------------------------------------------------- один шаг
    def step(self):
        cfg = self.cfg
        shot = self.grab()
        H, W = shot.shape[:2]
        now = self.clock()

        if dialog_on(shot):
            self.hold(None)
            self.ui_seen = now
            self.mark("dialog_seen")
            act = cfg["after_catch"]
            self.state = "Улов"
            if act != "none" and self.ago("dialog") > 2.5:
                self.mark("dialog")
                self.sleep(random.uniform(0.4, 0.8))
                x, y = button_center(shot, act)
                self.inp.click(x, y)
                self.stats["kept" if act == "keep" else "released"] += 1
                self.log("Забрал улов себе" if act == "keep" else "Отпустил рыбу (+опыт)")
                self.sleep(0.4)
            return

        if minigame_on(shot):
            self.ui_seen = now
            self.state = "Тяну рыбу"
            if not self.in_fight:
                self.in_fight = True
                self.tracker.reset()
                self.log("Рыба на крючке, тяну")
            d = self.tracker.update(shot, cfg["zone"] or sc(ZONE, W, H), now)
            if d is not None:
                self.last_seen = now
                want = "D" if d < 0 else "A"
                if cfg["invert"]:
                    want = "A" if want == "D" else "D"
                self.hold(want)
            elif now - self.last_seen > cfg["hold_after_lost"]:
                self.hold(None)
            return
        self.hold(None)
        if self.in_fight:
            self.in_fight = False
            self.log("Вываживание закончилось")

        cb = cast_bar(shot)
        if cb is not None and hints_on(shot):
            self.ui_seen = now
            self.e_tries = 0
            self.state = "Заброс"
            if cfg["auto_cast"] and self.ago("cast") > 2 and self.ago("dialog_seen") > 1.0:
                self.sleep(0.25)                       # окно улова появляется чуть позже полоски
                if not dialog_on(self.grab()):
                    self.cast(cb)
            return

        if hints_on(shot):
            self.ui_seen = now
            self.e_tries = 0
            self.state = "Жду поклёвку"
            if bite(shot):
                self.bite_frames += 1
            else:
                self.bite_frames = 0
            if cfg["auto_hook"] and self.bite_frames >= 2 and self.ago("hook") > 2:
                self.sleep(random.uniform(0.08, 0.2))
                self.inp.tap(cfg["hook_key"], 60)
                self.mark("hook")
                self.stats["bites"] += 1
                self.log("Поклёвка! Подсёк")
            return

        self.state = "Не рыбачу"
        if cfg["auto_start"] and now - self.ui_seen > 3 and self.ago("start") > 6:
            if self.e_tries < 3:
                self.inp.tap(cfg["start_key"], 60)
                self.mark("start")
                self.e_tries += 1
                self.log(f"Нажал {cfg['start_key']} - начать рыбалку")
            elif self.e_tries == 3:
                self.e_tries += 1
                self.log("Рыбалка не начинается по E - подойди к воде с удочкой")


def cast_bar_band(band, x0, g0_hint, g1_hint):
    """Разбор быстрого снимка только полоски заброса (координаты - экранные)."""
    a = band.astype(np.int16)
    green = ((a[:, :, 1] - np.maximum(a[:, :, 0], a[:, :, 2])) > 60).sum(axis=0) >= 3
    white = (a.min(axis=2) > 215).sum(axis=0) >= max(6, a.shape[0] // 4)
    if green.sum() < 8:
        return None
    wx = np.nonzero(white)[0]
    return (g0_hint, g1_hint, None if len(wx) == 0 else x0 + float(wx.mean()))



# ================================================================ окно
AFTER = {"keep": "Забирать всё", "release": "Отпускать всё (ради опыта)", "none": "Ничего не делать"}


def build_fishing_tab(parent):
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    q = queue.Queue()
    log = lambda m: q.put(m)
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен", "info": ""}
    pad = {"padx": 6, "pady": 3}

    def persist():
        save_config(cfg)

    def toggle():
        if not switches.is_on("fish") and not ctl["active"]:
            log("Рыбалка выключена в боковом меню - сначала включи её там")
            return
        ctl["active"] = not ctl["active"]

    status_var, info_var = tk.StringVar(value="Выключен"), tk.StringVar()
    st_lbl = ttk.Label(parent, textvariable=status_var, style="Status.TLabel")
    st_lbl.grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    ttk.Button(parent, text="Старт / стоп", style="Accent.TButton", command=toggle).grid(
        row=2, column=0, columnspan=4, sticky="ew", **pad)
    key_var = tk.StringVar(value=vk_name(cfg["toggle_vk"]))
    ttk.Label(parent, text="Горячая клавиша:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Label(parent, textvariable=key_var, style="Key.TLabel", width=16).grid(row=3, column=1, sticky="w", **pad)
    ttk.Button(parent, text="Назначить", command=lambda: bind_key()).grid(row=3, column=2, sticky="w", **pad)

    cyc = ttk.LabelFrame(parent, text="Цикл рыбалки")
    cyc.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    v = {k: tk.BooleanVar(value=cfg[k]) for k in ("auto_start", "auto_cast", "auto_hook", "invert", "focus_only")}
    ttk.Checkbutton(cyc, text="1. Сам начинать рыбалку клавишей E", variable=v["auto_start"],
                    command=lambda: save()).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(cyc, text="2. Сам забрасывать точно в зелёную зону (Space)", variable=v["auto_cast"],
                    command=lambda: save()).grid(row=1, column=0, columnspan=3, sticky="w", **pad)
    lr = ttk.Frame(cyc)
    lr.grid(row=2, column=0, columnspan=3, sticky="w", padx=28)
    lat_var = tk.StringVar(value=str(cfg["latency_ms"]))
    ttk.Label(lr, text="Поправка на задержку:").pack(side="left")
    ttk.Spinbox(lr, from_=0, to=200, increment=5, width=5, textvariable=lat_var, command=lambda: save()).pack(side="left", padx=4)
    ttk.Label(lr, text="мс  (если бот жмёт поздно - увеличь, если рано - уменьши)", style="Muted.TLabel").pack(side="left")
    ttk.Checkbutton(cyc, text="3. Сам подсекать при поклёвке (Space)", variable=v["auto_hook"],
                    command=lambda: save()).grid(row=3, column=0, columnspan=3, sticky="w", **pad)
    ttk.Label(cyc, text="4. Вываживание A / D - всегда, пока бот включён", style="Muted.TLabel").grid(
        row=4, column=0, columnspan=3, sticky="w", **pad)
    ar = ttk.Frame(cyc)
    ar.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
    ttk.Label(ar, text="5. После улова:").pack(side="left")
    after_var = tk.StringVar(value=AFTER[cfg["after_catch"]])
    acb = ttk.Combobox(ar, textvariable=after_var, values=list(AFTER.values()), state="readonly", width=28)
    acb.pack(side="left", padx=6)
    acb.bind("<<ComboboxSelected>>", lambda e: save())
    ttk.Label(cyc, text="После выбора в окне улова игра сама начинает новый заброс - и всё по кругу.",
              style="Muted.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", **pad)

    opt = ttk.LabelFrame(parent, text="Тонкая настройка")
    opt.grid(row=5, column=0, columnspan=4, sticky="ew", **pad)
    ttk.Checkbutton(opt, text="Поменять A и D местами (если бот тянет не туда)", variable=v["invert"],
                    command=lambda: save()).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(opt, text="Только когда активно окно игры", variable=v["focus_only"],
                    command=lambda: save()).grid(row=1, column=0, columnspan=3, sticky="w", **pad)
    hr = ttk.Frame(opt)
    hr.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
    hold_var = tk.StringVar(value=str(cfg["hold_after_lost"]))
    ttk.Label(hr, text="Если плашку с рыбкой не видно, держать прежнюю клавишу ещё").pack(side="left")
    ttk.Spinbox(hr, from_=0, to=5, increment=0.1, width=4, textvariable=hold_var, command=lambda: save()).pack(side="left", padx=4)
    ttk.Label(hr, text="сек").pack(side="left")
    zr = ttk.Frame(opt)
    zr.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
    zone_var = tk.StringVar()
    ttk.Button(zr, text="Выбрать зону воды", command=lambda: pick_zone()).pack(side="left")
    ttk.Button(zr, text="Сбросить", command=lambda: (cfg.update(zone=None), save(), show_zone())).pack(side="left", padx=4)
    ttk.Label(zr, textvariable=zone_var, style="Muted.TLabel").pack(side="left", padx=6)

    logbox = tk.Text(parent, height=9, width=74, state="disabled", font=("Consolas", 9))
    logbox.grid(row=6, column=0, columnspan=4, sticky="ew", **pad)

    def save():
        for k, var in v.items():
            cfg[k] = var.get()
        try:
            cfg["hold_after_lost"] = max(0.0, float(hold_var.get()))
            cfg["latency_ms"] = max(0, min(300, int(float(lat_var.get()))))
        except ValueError:
            pass
        cfg["after_catch"] = next(k for k, t in AFTER.items() if t == after_var.get())
        persist()

    def show_zone():
        zone_var.set("своя зона" if cfg["zone"] else "по умолчанию: середина экрана")

    def pick_zone():
        top = parent.winfo_toplevel()
        was = top.attributes("-topmost")
        top.withdraw()
        time.sleep(0.35)
        try:
            r = AF.pick_region(top, "Обведи участок воды, где появляются брызги и плашка с рыбкой")
        except Exception as ex:
            r = None
            log(f"Снимок экрана не получился: {ex}")
        finally:
            top.deiconify()
            top.attributes("-topmost", was)
        if r:
            img, x, y = r
            cfg["zone"] = [x, y, x + img.shape[1], y + img.shape[0]]
            persist()
            show_zone()

    bind_state = {"armed": False, "t0": 0}

    def bind_key():
        ctl["binding"] = True
        bind_state.update(armed=False, t0=time.time())
        key_var.set("нажми клавишу...")
        parent.after(30, poll_bind)

    def poll_bind():
        down = [vk for vk in range(1, 255) if vk not in SKIP_VKS and key_pressed(vk)]
        if time.time() - bind_state["t0"] > 10:
            key_var.set(vk_name(cfg["toggle_vk"]))
            ctl["binding"] = False
            return
        if not bind_state["armed"]:
            bind_state["armed"] = not down
        elif down:
            if down[0] != 0x1B:
                cfg["toggle_vk"] = down[0]
                persist()
            key_var.set(vk_name(cfg["toggle_vk"]))
            ctl["binding"] = False
            return
        parent.after(30, poll_bind)

    # ---------------------------------------------------------------- поток бота
    def worker():
        if AF.IS_WIN:
            try:
                import ctypes
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        inp = AF.Input2()
        res = {"scr": None}

        def screen():
            if res["scr"] is None:
                res["scr"] = AF.Screen()
            return res["scr"]

        def grab():
            return screen().grab()

        def grab_box(box):
            s = screen()
            x0, y0, x1, y1 = sc(box, s.mon["width"], s.mon["height"])
            shot = s.sct.grab({"left": s.mon["left"] + x0, "top": s.mon["top"] + y0,
                               "width": x1 - x0, "height": y1 - y0})
            return np.ascontiguousarray(np.asarray(shot)[:, :, :3]), x0

        fi = Fisher(cfg, inp, log, grab, grab_box)
        prev = False
        was = False
        while not ctl["quit"]:
            if not switches.is_on("fish"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                fi.hold(None)
                fi.e_tries = 0
                fi.ui_seen = time.time()
                log("Рыбалка включена" if was else "Рыбалка выключена")
            if not ctl["active"] or SEWING_BUSY["on"]:
                fi.hold(None)
                BUSY["on"] = False
                ctl["status"] = ("Выключен" if switches.is_on("fish") else "Отключён в меню") \
                    if not ctl["active"] else "Пауза: работает швейный бот"
                time.sleep(0.1)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                fi.hold(None)
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            BUSY["on"] = True
            try:
                fi.step()
            except Exception as ex:
                fi.hold(None)
                log(f"Ошибка: {ex}")
                time.sleep(1)
            s_ = fi.stats
            ctl["status"] = fi.state
            ctl["info"] = (f"держу: {fi.held or '-'}   ·   забросов {s_['casts']}, поклёвок {s_['bites']}, "
                           f"забрал {s_['kept']}, отпустил {s_['released']}")
            time.sleep(0.02 if fi.state in ("Тяну рыбу", "Жду поклёвку") else 0.05)
        fi.hold(None)
        BUSY["on"] = False

    def pump():
        try:
            while True:
                m = q.get_nowait()
                logbox.configure(state="normal")
                logbox.insert("end", time.strftime("%H:%M:%S ") + m + "\n")
                logbox.see("end")
                logbox.configure(state="disabled")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            s = ctl["status"]
            status_var.set(s)
            st_lbl.configure(style="StatusOn.TLabel" if s in ("Тяну рыбу", "Жду поклёвку", "Заброс", "Улов")
                             else "Status.TLabel")
            info_var.set(ctl["info"] if ctl["active"] else f"Включить: кнопка ниже или {vk_name(cfg['toggle_vk'])}")
        except tk.TclError:
            return
        parent.after(150, pump)

    def stop():
        ctl["quit"] = True
        ctl["active"] = False

    show_zone()
    threading.Thread(target=worker, daemon=True).start()
    parent.after(150, pump)
    return stop
