# -*- coding: utf-8 -*-
"""Шлифовка (работа «Стройка»): зрение и регулятор мини-игры; интерфейс и рабочий поток - stroyka.py.
Бот включается, когда на экране появилась доска с пятнами и зелёная шлифмашинка, и водит её WASD по пятнам.

Что выяснено по записям (1920x1080):
  - доска (7-8 планок) лежит в BOARD, поверх неё тёмно-коричневые пятна; пятно исчезает целиком, когда
    диск машинки его касается (диск ~ 50 px в радиусе), мини-игра закрывается, когда пятен не осталось;
  - машинка - зелёный корпус; её движение: ускорение от клавиши ~1350-1550 px/с^2, линейное затухание
    ~1.4-1.75 1/с (то есть разгон и долгий выбег), задержка входа ~40 мс;
  - сверху панель с подсказкой «WASD» (белые кнопки) - по ней видно, что мини-игра открыта."""
import os
import sys
import json
import time
import math
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
BOARD = (573, 330, 1348, 824)          # область планок
GRAB = (560, 200, 1360, 840)           # что снимаем (доска + панель с WASD)
KEYS_BOX = (1023, 258, 1151, 284)      # белые кнопки W A S D на панели
GREEN_LO, GREEN_HI = (35, 90, 60), (90, 255, 255)
LIGHT_SUM = 430                        # сумма B+G+R: темнее - бледное пятно (дерево ~490, тени волокон ~460)
MIN_STAIN = 150                        # px^2: мельче - шум (настоящие пятна от ~500)
SEAM_OPEN = 9                          # px: тоньше - это шов между планками, а не пятно
MIN_GREEN = 1500
OFFSET = (0, -38)                      # центр диска относительно центроида зелёного корпуса (px, 1080p)

# физика машинки (оценка по записи 20261015_223828)
GX, GY, KX, KY = 1347.0, 1546.0, 1.43, 1.75
LATENCY = 0.10                         # суммарная задержка ввод -> кадр, с (упреждение регулятора)


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "sanding_config.json")


def default_config():
    return {"toggle_vk": 0x67,            # Numpad 7
            "vmax": 650, "kp": 4.0, "kd": 10.0, "dead": 200.0, "focus_only": True}


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


# ====================================================================== зрение
class Vision:
    """Работает по кадру-области GRAB (BGR) в масштабе экрана W x H. Координаты наружу - в пикселях 1080p
    относительно ЛЕВОГО ВЕРХНЕГО угла GRAB (чтобы не зависеть от разрешения)."""

    def __init__(self, W, H):
        self.W, self.H = W, H
        self.k = W / BASE_W
        gx0, gy0, _, _ = GRAB
        self.board = tuple(int(v) for v in (BOARD[0] - gx0, BOARD[1] - gy0, BOARD[2] - gx0, BOARD[3] - gy0))
        self.keys = tuple(int(v) for v in (KEYS_BOX[0] - gx0, KEYS_BOX[1] - gy0, KEYS_BOX[2] - gx0, KEYS_BOX[3] - gy0))

    def _norm(self, img):
        """Приводит кадр к масштабу 1080p (если экран другого размера)."""
        if abs(self.k - 1.0) < 0.01:
            return img
        return cv2.resize(img, None, fx=1 / self.k, fy=1 / self.k, interpolation=cv2.INTER_AREA)

    def detect(self, img):
        """-> dict(active, g=(x,y) или None, stains=[(x,y,area)], wood, keys)"""
        img = self._norm(img)
        bx0, by0, bx1, by1 = self.board
        kx0, ky0, kx1, ky1 = self.keys
        out = {"active": False, "g": None, "stains": [], "wood": 0.0, "keys": 0.0}
        kp = img[ky0:ky1, kx0:kx1]
        if kp.size:
            out["keys"] = float((kp.min(axis=2) > 225).mean())
        roi = img[by0:by1, bx0:bx1]
        if roi.size == 0:
            return out
        r = roi.astype(np.int16)
        b, g, rr = r[:, :, 0], r[:, :, 1], r[:, :, 2]
        wood = (np.abs(b - 113) < 40) & (np.abs(g - 170) < 40) & (np.abs(rr - 221) < 40)
        out["wood"] = float(wood.mean())
        if not (out["keys"] > 0.2 and out["wood"] > 0.2):
            return out
        out["active"] = True
        hsv = cv2.cvtColor(img[by0 - 10:by1 + 10, bx0 - 10:bx1 + 10], cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, GREEN_LO, GREEN_HI)
        n = cv2.countNonZero(green)
        if n >= MIN_GREEN:
            ys, xs = np.nonzero(green)
            out["g"] = (float(xs.mean()) + bx0 - 10, float(ys.mean()) + by0 - 10)
        # тёмные пятна (BGR ~(4,18,37)) и бледные светло-коричневые (до ~(48,85,120); запись 20261023_002035:
        # бот их не видел и вставал на 29/33) - бледные: заметно темнее дерева (~(109,166,217)) и того же оттенка
        stain = (((rr - b >= 18) & (rr < 110) & (g < 70) & (b < 40)) |
                 ((b + g + rr < LIGHT_SUM) & (rr - b >= 30))).astype(np.uint8)
        # швы между планками того же цвета, что пятна: тонкие линии убираем открытием, иначе они склеивают всё в одно
        stain = cv2.morphologyEx(stain, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (SEAM_OPEN, SEAM_OPEN)))
        stain[:5, :] = 0
        stain[-5:, :] = 0
        stain[:, :5] = 0
        stain[:, -5:] = 0
        num, lab, st, cen = cv2.connectedComponentsWithStats(stain, connectivity=8)
        out["stains"] = [(float(cen[i][0]) + bx0, float(cen[i][1]) + by0, int(st[i, 4]))
                         for i in range(1, num) if st[i, 4] >= MIN_STAIN]
        return out


# ====================================================================== регулятор
class Controller:
    """Ведёт центр диска к пятнам. Решение: желаемая скорость к цели -> желаемое ускорение -> клавиша
    по каждой оси (-1/0/+1). Учитывает инерцию (выбег) и задержку через упреждение по скорости."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.hist = []                 # (t, x, y) центра диска
        self.target = None
        self.target_since = 0.0
        self.skip = {}                 # id цели -> до какого времени пропускать
        self.vx = self.vy = 0.0

    def velocity(self):
        h = self.hist[-5:]
        if len(h) < 3 or h[-1][0] - h[0][0] < 1e-3:
            return 0.0, 0.0
        t = np.array([p[0] for p in h]) - h[-1][0]
        x = np.array([p[1] for p in h])
        y = np.array([p[2] for p in h])
        return float(np.polyfit(t, x, 1)[0]), float(np.polyfit(t, y, 1)[0])

    def choose(self, stains, pos, vel, now):
        live = [s for s in stains if self.skip.get(self._id(s), 0) < now]
        if not live:
            return None
        sp = math.hypot(*vel)

        def cost(s):
            dx, dy = s[0] - pos[0], s[1] - pos[1]
            d = math.hypot(dx, dy) + 1e-6
            turn = 0.0 if sp < 60 else (1 - (dx * vel[0] + dy * vel[1]) / (d * sp)) * 0.5
            return d / 700.0 + turn * 0.35
        return min(live, key=cost)

    @staticmethod
    def _id(s):
        return (round(s[0] / 20), round(s[1] / 20))

    def step(self, now, disc, stains):
        """disc - центр диска (x, y) или None. -> (ux, uy, target)"""
        cfg = self.cfg
        if disc is None:
            return 0, 0, self.target
        self.hist.append((now, disc[0], disc[1]))
        del self.hist[:-12]
        vx, vy = self.velocity()
        self.vx, self.vy = vx, vy
        # упреждение на задержку
        px, py = disc[0] + vx * LATENCY, disc[1] + vy * LATENCY
        # актуальная цель: ближайшее пятно к прежней цели (пятна не двигаются), иначе выбор заново
        tgt = None
        if self.target is not None:
            near = [s for s in stains if math.hypot(s[0] - self.target[0], s[1] - self.target[1]) < 25]
            tgt = near[0] if near else None
        if tgt is None:
            tgt = self.choose(stains, (px, py), (vx, vy), now)
            self.target_since = now
        if tgt is None:
            self.target = None
            return 0, 0, None
        # цель, к которой долго не можем подобраться (застряла у края и т.п.), пропускаем на время
        if now - self.target_since > 2.5:
            self.skip[self._id(tgt)] = now + 3.0
            self.target = None
            return 0, 0, None
        self.target = (tgt[0], tgt[1])
        ex, ey = tgt[0] - px, tgt[1] - py
        d = math.hypot(ex, ey) + 1e-6
        sp_des = min(cfg["vmax"], cfg["kp"] * d)
        vdx, vdy = ex / d * sp_des, ey / d * sp_des
        ax, ay = cfg["kd"] * (vdx - vx), cfg["kd"] * (vdy - vy)
        dead = cfg["dead"]
        ux = 0 if abs(ax) < dead else (1 if ax > 0 else -1)
        uy = 0 if abs(ay) < dead else (1 if ay > 0 else -1)
        return ux, uy, self.target


# ====================================================================== проверка по видео
def analyze_video(path, out=None, limit=None):
    """Печатает, что бот видит на каждом кадре записи; out - видео с пометками."""
    cap = cv2.VideoCapture(path)
    H0 = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    W0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    vis = Vision(W0, H0)
    gx0, gy0, gx1, gy1 = sc(GRAB, W0, H0)
    writer = None
    i = 0
    rows = []
    while True:
        ok, f = cap.read()
        if not ok or (limit and i >= limit):
            break
        d = vis.detect(f[gy0:gy1, gx0:gx1])
        rows.append((i, d["active"], d["g"], len(d["stains"]), d["wood"], d["keys"]))
        if out:
            v = f.copy()
            if d["active"]:
                for (x, y, a) in d["stains"]:
                    cv2.circle(v, (int(x + GRAB[0]), int(y + GRAB[1])), 8, (0, 0, 255), 2)
                if d["g"]:
                    cv2.circle(v, (int(d["g"][0] + GRAB[0] + OFFSET[0]), int(d["g"][1] + GRAB[1] + OFFSET[1])), 45, (0, 255, 0), 2)
            cv2.putText(v, f"active={d['active']} stains={len(d['stains'])}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
            if writer is None:
                writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), 15, (v.shape[1] // 2, v.shape[0] // 2))
            writer.write(cv2.resize(v, (v.shape[1] // 2, v.shape[0] // 2)))
        i += 1
    if writer:
        writer.release()
    return rows
