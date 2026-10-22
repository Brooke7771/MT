# -*- coding: utf-8 -*-
"""Забивка досок молотком (работа «Стройка»): зрение и расчёт удара; интерфейс и поток - stroyka.py. Доску приносишь
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
    """Скорость и место метки: прямая по точкам с последнего разворота (за ~0,25 с), а не по двум соседним снимкам -
    игра под нагрузкой двигает метку рывками (запись 20261023_003039: между кадрами то 8, то 40 px, в среднем
    ~375 px/с), и по двум снимкам скорость выходила то нулём, то в разы больше -> удар мимо края зоны."""
    WIN = 0.25

    def __init__(self):
        self.reset()

    def reset(self):
        self.hist = []
        self.speed = 400.0
        self.a, self.b = X_MIN, X_MAX
        self.x = None

    def update(self, now, x):
        self.hist = [(t, v) for t, v in self.hist if now - t < 0.6] + [(now, x)]
        self.a, self.b = min(self.a, x), max(self.b, x)
        self.x = x
        h = self.hist
        if len(h) < 3:
            return None
        # последний разворот: точка экстремума по ходу
        xs = np.array([p[1] for p in h])
        ts = np.array([p[0] for p in h])
        k = len(xs) - 1
        d = 0
        i = k - 1
        while i >= 0:
            s = np.sign(xs[i + 1] - xs[i])
            if s != 0:
                if d == 0:
                    d = s
                elif s != d and abs(xs[i + 1] - xs[i]) > 2:
                    break
            i -= 1
        j0 = max(i + 1, 0)
        seg = (ts[j0:] >= now - self.WIN)
        tt, xx = ts[j0:][seg], xs[j0:][seg]
        if d == 0:
            return None
        if len(tt) >= 4 and tt[-1] - tt[0] > 0.08 and abs(xx[-1] - xx[0]) > 15:
            b, a = np.polyfit(tt - now, xx, 1)
            if 100 < abs(b) < 1500:
                near_edge = min(xx) - self.a < 20 or self.b - max(xx) < 20
                if not near_edge or tt[-1] - tt[0] > 0.15:
                    self.speed += (abs(b) - self.speed) * 0.4
                self.x = float(a)              # сглаженное место сейчас
        return self.speed * d


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
            tt = travel(tr.x, vv, (zone[0] + zone[1]) / 2, tr.a, tr.b)
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
