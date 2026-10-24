# -*- coding: utf-8 -*-
"""Швейный бот - БЕТА (страница «Тест»): то же, что обычная швейка, но быстрее думает.
  - снимки в отдельном потоке (Grabber): пока разбирается кадр N, снимается N+1 (шаг ~27 -> ~18 мс);
  - поворот, поданный после снимка кадра, на кадре ещё не виден - окно «ещё не видимых» поворотов считается
    от времени снимка (`t_cap`), а не от «сейчас минус шаг»: в 1.49 без этого кадр с потоком был старше на шаг,
    бот докручивал уже поданный поворот и раскачивался;
  - смелее повороты (K_TURN).
Свой выключатель (`sewbeta`), клавиша Numpad 9, настройки - sewing_beta_config.json. Обычную швейку не трогает."""
import os
import sys
import json
import time
import math
import ctypes
import threading
import numpy as np
import cv2

import switches

# ================================================================ настройки
BASE_W, BASE_H = 1920, 1080          # координаты ниже даны для 1920x1080
NEEDLE = (959, 383)
GRID = (690, 235, 1230, 655)
ICON = (740, 755, 800, 800)
PROGRESS_Y, PROGRESS_X = 757, (862, 1226)
HINT_BOXES = [(1610, 1032, 1636, 1058),   # A
              (1697, 1032, 1723, 1058),   # Space
              (1872, 1032, 1898, 1058)]   # D
E_BOX = (20, 5, 330, 80)    # где искать подсказку «Нажмите [E] чтобы начать шить» (белая клавиша E ~26 px;
                            # раньше стояла в (104,29)-(130,55), в игре v1.48 сдвинулась на (90,20)-(117,45))

OFFSET_GAIN_L = 28.0      # px: насколько резко подтягиваться к линии сбоку
FEEDFORWARD_T = 0.25      # с: упреждение поворота по кривизне

TURN_SIGN = -1            # -1: A уменьшает угол, D увеличивает
OMEGA0, OMEGA1 = 57.7, 44.0   # град/с: поворот ткани = OMEGA0 + OMEGA1*скорость (0..1), замерено по записи
LAT_GAME = 0.06           # с: нажатие -> видно на снимке (ввод + кадр игры); от времени снимка t_cap
LAT_VIS = 0.02            # с: задержка от нажатия до снимка сверх шага цикла (подобрано на виртуальной швейке)
PULSE_LOSS = 0.006        # с: столько игра «съедает» от каждого нажатия A/D (запись 20261020_191855: короткие нажатия давали ~75% поворота)
CORNER_K = 0.15           # 1/px: линия впереди не прослеживается посреди сетки = острый угол, тормозим до него
MIN_TURN = 1.0            # град: меньше - не крутим
K_TURN = 1.45              # запас по повороту на изгибах: >1 - смелее (срезает углы в пределах ширины штриха);
                          # 1.3 -> 1.7 (v1.49.0): виртуальная швейка -1,4..-1,6 с на комплект, мимо узора ~1,5 с (красная ~9%)

# скорость машинки (0 = зелёная иконка, 1 = красная)
SPEED_MAX = 1.00          # максимум на прямой
RECOVER_SPEED = 0.40      # с какой скоростью подшиваться обратно к линии
ACCEL, DECEL = 0.144, 0.62  # скорость (0..1) за секунду: разгон с газом, торможение без газа
V0_PX, V1_PX = 22.0, 151.0  # px/с: скорость ткани = V0_PX + V1_PX*скорость
STOP_ANGLE = 35.0         # линия настолько сбоку - сначала довернуть на месте

STALL_TIME = 0.4          # с: стоим и не крутим дольше - всё равно едем
ONLINE_DIST = 48          # px: ближе этого есть непрошитое - мы на линии
MAX_JUMP = 75             # град: на сколько может прыгнуть направление за кадр

AUTO_REPEAT = True
E_DELAY = 0.45            # с после закрытия игры (подсказка E появляется через ~0,27 с)
E_RETRY = 2.0
E_MAX_TRIES = 5

UI_CHECK = 0.15           # с: как часто проверять подсказки внизу (лишние снимки замедляют цикл)

# ================================================================ ввод
SC_A, SC_D, SC_SPACE, SC_E = 0x1E, 0x20, 0x39, 0x12
VK_START, VK_EXIT = 0x69, 0          # Numpad 9; выхода по клавише у беты нет
from sewing_bot import BUSY          # общий с обычной швейкой: анти-АФК и другие боты видят и бету

PUL = ctypes.POINTER(ctypes.c_ulong)


class KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]


class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                ("wParamH", ctypes.c_ushort)]


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]


class InputI(ctypes.Union):
    _fields_ = [("ki", KeyBdInput), ("mi", MouseInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", InputI)]


KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP = 0x0008, 0x0002


class Keys:
    def __init__(self, dry=False):
        import threading
        self.down = set()
        self.live = (sys.platform == "win32") and not dry
        self.lock = threading.Lock()          # отпускание по таймеру идёт из другого потока

    def _send(self, sc, up):
        if not self.live:
            return
        extra = ctypes.c_ulong(0)
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
        ii = InputI()
        ii.ki = KeyBdInput(0, sc, flags, 0, ctypes.pointer(extra))
        x = Input(ctypes.c_ulong(1), ii)
        ctypes.windll.user32.SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))

    def set(self, sc, pressed):
        with self.lock:
            if pressed and sc not in self.down:
                self._send(sc, False)
                self.down.add(sc)
            elif not pressed and sc in self.down:
                self._send(sc, True)
                self.down.discard(sc)

    def pulse(self, sc, dur):
        """Нажать и отпустить через dur секунд (не дожидаясь); новое нажатие отменяет старое отпускание."""
        self.gen = getattr(self, "gen", 0) + 1
        g = self.gen
        self.set(sc, True)
        if not self.live:
            return

        def release():
            if self.gen == g:
                self.set(sc, False)
        import threading
        t = threading.Timer(dur, release)
        t.daemon = True
        t.start()

    def tap(self, sc, hold=0.06):
        self.set(sc, True)
        time.sleep(hold)
        self.set(sc, False)

    def release_all(self):
        for sc in list(self.down):
            self.set(sc, False)


def key_pressed(vk):
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


# ================================================================ геометрия
class Geometry:
    def __init__(self, w, h):
        self.sx, self.sy = w / BASE_W, h / BASE_H
        s = self.s = (self.sx + self.sy) / 2
        self.needle = self.pt(*NEEDLE)
        self.grid = self.box(GRID)
        self.icon = self.box(ICON)
        self.py = int(PROGRESS_Y * self.sy)
        self.px = (int(PROGRESS_X[0] * self.sx), int(PROGRESS_X[1] * self.sx))
        self.hints = [self.box(b) for b in HINT_BOXES]
        self.ebox = self.box(E_BOX)
        self.sew_r = 13 * s
        self.needle_r = int(14 * s)
        self.min_blob = int(80 * s * s)
        self.min_thread = max(4, int(10 * s))
        self.border = int(18 * s)
        self.online = ONLINE_DIST * s
        self.main = (self.grid[0], self.grid[1], self.grid[2],
                     max(self.icon[3], self.py + 2))

    def pt(self, x, y):
        return int(x * self.sx), int(y * self.sy)

    def box(self, b):
        return self.pt(b[0], b[1]) + self.pt(b[2], b[3])


# ================================================================ зрение
K5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
ONES5 = np.ones((5, 5), np.uint8)


def white_frac(img, b, off):
    x0, y0, x1, y1 = b
    a = img[y0 - off[1]:y1 - off[1], x0 - off[0]:x1 - off[0]]
    if a.size == 0:
        return 0.0
    return float((a.min(axis=2) > 220).mean())


def masks(grid, cx, cy, g):
    """W - всё белое, U - непрошитые штрихи."""
    c0, c1, c2 = cv2.split(np.ascontiguousarray(grid))
    mn = cv2.min(cv2.min(c0, c1), c2)
    mx = cv2.max(cv2.max(c0, c1), c2)
    spread = cv2.subtract(mx, mn)
    # маски 0/1 (uint8) операциями OpenCV - в несколько раз быстрее, чем через numpy
    W = cv2.bitwise_and(cv2.compare(mn, 200, cv2.CMP_GT), cv2.compare(spread, 40, cv2.CMP_LT)) // 255
    b = g.border
    W[:, :b] = 0
    W[:, -b:] = 0
    inside = cv2.subtract(cv2.morphologyEx(W, cv2.MORPH_CLOSE, K5), W)       # нитка по штриху
    grey = cv2.compare(mn, 108, cv2.CMP_GT)
    grey = cv2.bitwise_and(grey, cv2.compare(mx, 200, cv2.CMP_LT))
    grey = cv2.bitwise_and(grey, cv2.compare(spread, 22, cv2.CMP_LT)) // 255
    thread = cv2.bitwise_or(inside, cv2.subtract(grey, cv2.dilate(W, ONES5)))  # нитка по фону
    thread[:, :b] = 0
    thread[:, -b:] = 0
    n, lab, stats, _ = cv2.connectedComponentsWithStats(thread, connectivity=8)
    keep = np.zeros(n, np.uint8)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= g.min_thread
    thread = keep[lab]
    cv2.circle(thread, (cx, cy), g.needle_r, 1, -1)
    dist = cv2.distanceTransform(1 - thread, cv2.DIST_L2, 3)
    sewn = cv2.bitwise_and(W, cv2.compare(dist, float(g.sew_r), cv2.CMP_LE) // 255)
    # штрих, который заметно задет ниткой, считаем прошитым целиком
    # (кроме штриха под самой иглой - у него прошита только задняя часть)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(W, connectivity=8)
    U = cv2.subtract(W, sewn)
    if n > 1:
        frac = np.bincount(lab[sewn > 0], minlength=n) / np.maximum(stats[:, cv2.CC_STAT_AREA], 1)
        whole = frac > 0.3
        whole[0] = False
        r = g.needle_r + int(6 * g.s)
        under = np.unique(lab[max(0, cy - r):cy + r, max(0, cx - r):cx + r])
        whole[under] = False
        if whole.any():
            U[whole[lab]] = 0
    n, lab, stats, _ = cv2.connectedComponentsWithStats(U, connectivity=8)
    area = stats[:, cv2.CC_STAT_AREA].copy()
    area[0] = 0
    keep = area >= g.min_blob
    return W.astype(bool), keep[lab], area[lab]


def angdiff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def ring_peaks(ang, d, r0, r1, min_px=25):
    """Направления на непрошитое в кольце: [(угол, масса)], 0 = вниз, + = вправо."""
    sel = (d > r0) & (d < r1)
    if sel.sum() < min_px:
        return []
    a = ang[sel]
    hist, edges = np.histogram(a, bins=72, range=(-180, 180))
    h = hist.astype(float)
    hs = np.convolve(np.r_[h[-2:], h, h[:2]], [1, 2, 3, 2, 1], "valid")
    peaks, used = [], np.zeros(72, bool)
    for k in np.argsort(-hs):
        if hs[k] < min_px or used[k]:
            continue
        c = (edges[k] + edges[k + 1]) / 2
        diff = angdiff(a, c)
        near = np.abs(diff) < 15
        if near.sum() < min_px:
            continue
        peaks.append((float(angdiff(c + diff[near].mean(), 0)), int(near.sum())))
        for j in range(k - 3, k + 4):
            used[j % 72] = True
    return peaks


def pick(peaks, ref, max_jump=None):
    best = None
    for a, _ in peaks:
        dd = abs(angdiff(a, ref))
        if max_jump is not None and dd > max_jump:
            continue
        if best is None or dd < best[1]:
            best = (a, dd)
    return None if best is None else best[0]


def polar(mask, cx, cy, rmax=None):
    ys, xs = np.nonzero(mask)
    dx = (xs - cx).astype(np.float32)
    dy = (ys - cy).astype(np.float32)
    d = np.hypot(dx, dy)
    if rmax is not None:
        k = d < rmax
        dx, dy, d = dx[k], dy[k], d[k]
    return np.degrees(np.arctan2(dx, dy)), d


def _dir(a):
    r = math.radians(a)
    return math.sin(r), math.cos(r)


def _bearing(dx, dy):
    return math.degrees(math.atan2(dx, dy))


def trace(M, x, y, a, g, n, cone=45.0):
    """Идём по белому вдоль линии от точки (x, y) в направлении a.
    Возвращает список точек [(x, y, a)] - работает и на крутых разворотах."""
    step, R = 12 * g.s, int(13 * g.s) + 1
    H, Wd = M.shape
    pts = []
    for _ in range(n):
        ok = False
        sx, sy = _dir(a)
        for mult in (1.0, 1.7, 2.5):            # 1.7 и 2.5 - перепрыгнуть зазор
            qx, qy = x + mult * step * sx, y + mult * step * sy
            x0, x1 = max(0, int(qx) - R), min(Wd, int(qx) + R + 1)
            y0, y1 = max(0, int(qy) - R), min(H, int(qy) + R + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            ys, xs = np.nonzero(M[y0:y1, x0:x1])
            if len(xs) < 6:
                continue
            xs = xs + x0
            ys = ys + y0
            sel = (np.hypot(xs - qx, ys - qy) < R) & \
                  (((xs - x) * sx + (ys - y) * sy) > step * 0.4)
            if sel.sum() < 6:
                continue
            cxp, cyp = float(xs[sel].mean()), float(ys[sel].mean())
            b = _bearing(cxp - x, cyp - y)
            if abs(angdiff(b, a)) > cone:
                continue
            a = angdiff(a + float(np.clip(angdiff(b, a), -cone, cone)), 0)
            x, y = cxp, cyp
            pts.append((x, y, a))
            ok = True
            break
        if not ok:
            break
        if not (R <= x < Wd - R and R <= y < H - R):
            break
    return pts


def start_point(st, g, ref, jump):
    """Первая непрошитая точка линии перед иглой (ближе ~60 px), направление ref +- jump."""
    ang, d = st["ang"], st["d"]
    sel = d < 62 * g.s
    if sel.sum() < 15:
        return None
    a_s, d_s = ang[sel], d[sel]
    a0 = pick(ring_peaks(a_s, d_s, 0, 62 * g.s, min_px=15), ref, jump)
    if a0 is None:
        return None
    k = np.abs(angdiff(a_s, a0)) < 14
    dd = np.sort(d_s[k])[: max(5, int(k.sum() * 0.3))]
    r0 = float(dd.mean())
    sx, sy = _dir(a0)
    return st["cx"] + r0 * sx, st["cy"] + r0 * sy, a0, r0


def fit_line(points, cx, cy, h):
    """Точки (x, y) рядом с иглой -> направление у иглы (град), смещение (px, + = вправо),
    кривизна (1/px, + = вправо)."""
    if len(points) < 2:
        return h, 0.0, 0.0
    P = np.array(points, float) - (cx, cy)
    r = math.radians(h)
    # поворот в систему, где линия идёт по +y
    xr = P[:, 0] * math.cos(r) - P[:, 1] * math.sin(r)
    yr = P[:, 0] * math.sin(r) + P[:, 1] * math.cos(r)
    if len(P) >= 4 and np.ptp(yr) > 30:
        p2, p1, p0 = np.polyfit(yr, xr, 2)
    elif np.ptp(yr) > 5:
        p1, p0 = np.polyfit(yr, xr, 1)
        p2 = 0.0
    else:
        return h, 0.0, 0.0
    theta = angdiff(h + math.degrees(math.atan(p1)), 0)
    kappa = 2 * p2 / (1 + p1 * p1) ** 1.5
    e = p0 * math.cos(math.atan(p1))
    return float(theta), float(e), float(kappa)


def icon_speed(img, g, off):
    """None = машинка стоит, иначе 0..1 (зелёный..красный)."""
    x0, y0, x1, y1 = g.icon
    a = img[y0 - off[1]:y1 - off[1], x0 - off[0]:x1 - off[0]].reshape(-1, 3).astype(int)
    sat = a[(a.max(axis=1) - a.min(axis=1)) > 80]
    if len(sat) < 30:
        return None
    bgr = sat.mean(axis=0).astype(np.uint8).reshape(1, 1, 3)
    hue = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0, 0]) * 2
    return float(np.clip((110 - hue) / 104, 0, 1))


def analyze(img, g, off):
    gx0, gy0, gx1, gy1 = g.grid
    grid = img[gy0 - off[1]:gy1 - off[1], gx0 - off[0]:gx1 - off[0]]
    cx, cy = g.needle[0] - gx0, g.needle[1] - gy0
    W, U, area = masks(grid, cx, cy, g)
    ang, d = polar(U, cx, cy)
    uarea = area[U]                     # тот же порядок, что у np.nonzero
    row = img[g.py - off[1], g.px[0] - off[0]:g.px[1] - off[0]].astype(int)
    progress = float(((row[:, 1] > 150) & (row[:, 2] < 100)).mean()) if len(row) else 0.0
    return dict(ang=ang, d=d, uarea=uarea,
                nearest=float(d.min()) if len(d) else None,
                speed=icon_speed(img, g, off), progress=progress,
                W=W, U=U, cx=cx, cy=cy)


# ================================================================ управление
def px_speed(s):
    return V0_PX + V1_PX * s


def speed_from_px(v):
    return (v - V0_PX) / V1_PX


def omega(s):
    return OMEGA0 + OMEGA1 * (s or 0.0)


def curve_speed(k):
    """Наибольшая скорость (0..1), на которой поворот ткани успевает за изгибом с кривизной k (1/px)."""
    kd = math.degrees(abs(k))
    a = V1_PX * kd - K_TURN * OMEGA1
    b = K_TURN * OMEGA0 - V0_PX * kd
    if a <= 0:
        return 1.0 if b >= 0 else 0.0
    return float(np.clip(b / a, 0.0, 1.0))


class Controller:
    """Рулёжка с памятью направления линии."""

    def __init__(self, keys, g):
        self.keys = keys
        self.g = g
        self.turn_sign = TURN_SIGN
        self.reset()

    def reset(self):
        self.heading = None      # направление линии у иглы
        self.road = None         # куда линия идёт дальше
        self.rot = 0             # +1 угол растёт, -1 уменьшается, 0 не крутим
        self.cmds = []           # [(начало, длительность, знак, град/с)] - поданные повороты
        self.last_cmd = (0, 0.0)
        self.loop_dt = 0.05
        self.last_apply = None
        self.last_t = None
        self.mode = "start"
        self.lost_t = None
        self.idle_t = None
        self.trace_dbg = []
        self.keys.release_all()

    def _turned(self, t0, t1):
        """На сколько градусов (со знаком) повернулась ткань по поданным командам за [t0, t1]."""
        tot = 0.0
        for ts, d, sg, w in self.cmds:
            a, b = max(t0, ts), min(t1, ts + d)
            if b > a:
                tot += sg * w * (b - a)
        return tot

    def _rotate_memory(self, now):
        if self.last_t is not None:
            delta = self._turned(self.last_t, now)
            if self.heading is not None:
                self.heading = angdiff(self.heading + delta, 0)
            if self.road is not None:
                self.road = angdiff(self.road + delta, 0)
        self.last_t = now

    def _target_speed_follow(self, err, kappa, path, s_now):
        """path: [(дистанция по линии px, кривизна 1/px)] впереди."""
        brake = V1_PX * DECEL
        v = px_speed(min(SPEED_MAX, curve_speed(kappa)))
        for dist, k in path:            # успеть оттормозиться до поворота
            v = min(v, math.sqrt(px_speed(curve_speed(k)) ** 2 + 2 * brake * dist / self.g.s))
        err = abs(err)
        if err > 1:
            v = min(v, 45 * self.g.s * omega(s_now) / err)
        if err > STOP_ANGLE:
            return 0.0
        return float(np.clip(speed_from_px(v), 0.0, SPEED_MAX))

    def _pick_recovery(self, st):
        """Непрошитая точка, лучше всего продолжающая запомненную линию."""
        ang, d = st["ang"], st["d"]
        ok = d > 20 * self.g.s
        big = ok & (st["uarea"] >= 350 * self.g.s * self.g.s)   # целые штрихи, не огрызки
        if big.any():
            ok = big
        if not ok.any():
            return None
        ang, d = ang[ok], d[ok]
        ref = self.road if self.road is not None else (self.heading or 0.0)
        score = d / (60 * self.g.s) + np.abs(angdiff(ang, ref)) / 45.0
        k = int(score.argmin())
        return float(ang[k]), float(d[k])

    def decide(self, st, now):
        self._rotate_memory(now)
        g = self.g
        if self.mode == "follow" and self.heading is not None:
            ref, jump = self.heading, MAX_JUMP
        else:
            ref = self.road if self.road is not None else (self.heading or 0.0)
            jump = 110
        sp = start_point(st, g, ref, jump)
        online = sp is not None and st["nearest"] is not None and st["nearest"] < g.online

        if online:
            x0, y0, a0, r0 = sp
            cx, cy = st["cx"], st["cy"]
            fwd = trace(st["U"], x0, y0, a0, g, 26)
            back = trace(st["W"], cx, cy, angdiff(a0 + 180, 0), g, 4)
            near = [(x0, y0)] + [(x, y) for x, y, _ in fwd
                                 if math.hypot(x - cx, y - cy) < 80 * g.s]
            theta, e, kappa = fit_line(near + [(x, y) for x, y, _ in back], cx, cy, a0)
            # кривизна по всей видимой линии впереди
            path, dist = [], r0
            heads = [a0] + [a for _, _, a in fwd]
            step = 12 * g.s
            for i in range(1, len(heads)):
                dist += step
                j0, j1 = max(0, i - 2), min(len(heads) - 1, i + 2)
                if j1 - j0 < 2:
                    continue
                k = math.radians(angdiff(heads[j1], heads[j0])) / ((j1 - j0) * step)
                if abs(k) > 1 / (400 * g.s):
                    path.append((dist, k))
            if len(fwd) < 26:
                # трасса оборвалась, а до края сетки далеко: впереди острый угол (на нём бот шил мимо узора)
                ex, ey = (fwd[-1][0], fwd[-1][1]) if fwd else (x0, y0)
                H_, W_ = st["U"].shape
                m_ = 25 * g.s
                if m_ < ex < W_ - m_ and m_ < ey < H_ - m_:
                    path.append((r0 + step * len(fwd), CORNER_K / g.s))
            v_now = px_speed(st["speed"]) if st["speed"] is not None else 0.0
            err = theta + math.degrees(math.atan2(e, OFFSET_GAIN_L * g.s))
            err += math.degrees(kappa * v_now * FEEDFORWARD_T * self.g.s)
            err = angdiff(err, 0)
            self.heading = a0
            # память: направление линии (касательная) в ~80 px впереди
            mem = None
            for x, y, a in fwd:
                if math.hypot(x - cx, y - cy) > 80 * g.s:
                    mem = a
                    break
            if mem is None and fwd:
                mem = fwd[-1][2]
            self.road = mem if mem is not None else theta
            self.mode = "follow"
            self.lost_t = None
            self.trace_dbg = fwd
            target = self._target_speed_follow(err, kappa, path, st["speed"] or 0.0)
            return "follow", err, target, (theta, self.road, None)

        tgt = self._pick_recovery(st)
        if tgt is None:
            self.mode = "search"
            spin = 90.0 if (self.road or 0.0) >= 0 else -90.0
            return "search", spin, 0.0, None
        if self.lost_t is None:
            self.lost_t = now
        self.mode = "recover"
        a, dist = tgt
        self.heading = a          # память о линии (self.road) не трогаем
        if abs(a) < 25:
            target = RECOVER_SPEED
        elif abs(a) < 45:
            target = RECOVER_SPEED * 0.4
        else:
            target = 0.0
        return "recover", a, target, (a, None, None)

    def apply(self, st, now=None):
        now = time.time() if now is None else now
        if self.last_apply is not None:
            self.loop_dt += (float(np.clip(now - self.last_apply, 0.01, 0.15)) - self.loop_dt) * 0.2
        self.last_apply = now
        mode, err, target, dbg = self.decide(st, now)
        # поворот, который уже подан, но ещё не виден на снимке (задержка), - как будто уже случился
        self.cmds = [c for c in self.cmds if c[0] + c[1] > now - 1.0]
        t_vis = st["t_cap"] - LAT_GAME if st.get("t_cap") else now - self.loop_dt - LAT_VIS
        err_eff = err + self._turned(t_vis, now + 1.0)
        need = -err_eff
        w = omega(st["speed"])
        inc_key = SC_A if self.turn_sign == 1 else SC_D
        dec_key = SC_D if self.turn_sign == 1 else SC_A
        if abs(need) < MIN_TURN:
            self.rot = 0
            self.last_cmd = (0, 0.0)
            self.keys.set(inc_key, False)
            self.keys.set(dec_key, False)
        else:
            # нажатие ровно на нужный угол, но не дольше шага цикла (дальше решит следующий снимок)
            self.rot = 1 if need > 0 else -1
            dur = min(abs(need) / w, self.loop_dt * 1.1)
            self.cmds = [(ts, min(d, now - ts), sg, ww) for ts, d, sg, ww in self.cmds]   # прошлое нажатие отменяется
            self.cmds.append((now + PULSE_LOSS, dur, self.rot, w))
            dur += PULSE_LOSS
            self.last_cmd = (self.rot, dur)
            self.keys.set(dec_key if self.rot == 1 else inc_key, False)
            self.keys.pulse(inc_key if self.rot == 1 else dec_key, dur)
        # защита от зависания: если ничего не крутим и стоим - чуть-чуть едем
        if self.rot == 0 and target <= 0:
            if self.idle_t is None:
                self.idle_t = now
            elif now - self.idle_t > STALL_TIME:
                target = RECOVER_SPEED * 0.5
        else:
            self.idle_t = None
        speed = st["speed"] if st["speed"] is not None else 0.0
        self.keys.set(SC_SPACE, target > 0 and speed < target)
        return mode, err, target, dbg


def draw_debug(img, g, off, st=None, info="", dbg=None, trace_pts=None):
    out = img.copy()
    ox, oy = off
    gx0, gy0, gx1, gy1 = g.grid
    if st is not None:
        sub = out[gy0 - oy:gy1 - oy, gx0 - ox:gx1 - ox]
        sub[st["W"] & ~st["U"]] = (0, 0, 255)
        sub[st["U"]] = (0, 255, 0)
    cv2.rectangle(out, (gx0 - ox, gy0 - oy), (gx1 - ox, gy1 - oy), (255, 255, 0), 1)
    n = (g.needle[0] - ox, g.needle[1] - oy)
    cv2.circle(out, n, 6, (0, 255, 255), 2)
    if dbg is not None:
        for a, rr, col in zip(dbg, (40 * g.s, 110 * g.s, 190 * g.s),
                              ((255, 0, 255), (255, 128, 0), (0, 200, 255))):
            if a is None:
                continue
            p = (int(n[0] + rr * math.sin(math.radians(a))), int(n[1] + rr * math.cos(math.radians(a))))
            cv2.line(out, n, p, col, 2)
    for x, y, _ in (trace_pts or []):
        cv2.circle(out, (int(x + gx0 - ox), int(y + gy0 - oy)), 3, (255, 0, 0), -1)
    for b in [g.icon] + g.hints + [g.ebox]:
        cv2.rectangle(out, (b[0] - ox, b[1] - oy), (b[2] - ox, b[3] - oy), (255, 0, 255), 1)
    if info:
        cv2.putText(out, info, (gx0 - ox + 5, gy0 - oy + 25), 0, 0.7, (0, 255, 255), 2)
    return out


def game_visible(img, g, off):
    return any(white_frac(img, b, off) > 0.5 for b in g.hints)


def prompt_visible(img, g, off):
    """Белая квадратная клавиша (E) в области подсказки - ищем её саму, а не жёсткую рамку:
    игра двигала подсказку, и бот переставал жать E."""
    x0, y0, x1, y1 = g.ebox
    a = img[y0 - off[1]:y1 - off[1], x0 - off[0]:x1 - off[0]]
    if a.size == 0:
        return False
    m = (a.min(axis=2) > 200).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(m)
    lo, hi = 18 * g.s, 36 * g.s
    for x, y, w, h, area in st[1:n]:
        if lo <= w <= hi and lo <= h <= hi and abs(w - h) <= 6 * g.s and area >= 0.6 * w * h:
            return True
    return False


# ================================================================ бот
class Grabber:
    """Снимки в своём потоке: пока бот разбирает кадр N, здесь уже снимается N+1 (снимок ~13 мс и разбор ~14 мс
    раньше шли друг за другом). Подсказки (игра открыта / «Нажмите E») - здесь же, раз в UI_CHECK."""

    def __init__(self, mon, g):
        self.mon, self.g = mon, g
        self.cv = threading.Condition()
        self.frame = None            # (время снимка, картинка, смещение)
        self.ui = (False, False)
        self.want = True
        self.stop = False
        self.err = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import mss
        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        g = self.g
        sct = mss.mss()
        hb = (min(b[0] for b in g.hints), min(b[1] for b in g.hints),
              max(b[2] for b in g.hints), max(b[3] for b in g.hints))

        def grab(b):
            x0, y0, x1, y1 = b
            shot = sct.grab({"left": self.mon["left"] + x0, "top": self.mon["top"] + y0,
                             "width": x1 - x0, "height": y1 - y0})
            return np.asarray(shot)[:, :, :3], (x0, y0)
        last_ui = 0.0
        try:
            while not self.stop:
                now = time.time()
                if now - last_ui > UI_CHECK:          # подсказки - и когда кадры игры не нужны (ждём E)
                    img, off = grab(hb)
                    game = game_visible(img, g, off)
                    img, off = grab(g.ebox)
                    self.ui = (game, prompt_visible(img, g, off))
                    last_ui = now
                with self.cv:
                    if not self.want:
                        self.cv.wait(0.03)
                        continue
                t = time.time()
                img, off = grab(g.main)
                with self.cv:
                    self.frame = (t, img, off)
                    self.want = False
                    self.cv.notify_all()
        except Exception as ex:
            self.err = ex
            with self.cv:
                self.cv.notify_all()

    def get(self, timeout=0.5):
        """Самый свежий кадр, которого ещё не брали; сразу заказывает следующий."""
        with self.cv:
            end = time.time() + timeout
            while self.err is None and time.time() < end:
                if self.frame is not None and time.time() - self.frame[0] < 0.08:
                    break
                if self.frame is not None:            # залежался (бот ждал E) - нужен свежий
                    self.frame = None
                    self.want = True
                    self.cv.notify_all()
                self.cv.wait(0.05)
            if self.err is not None:
                raise self.err
            fr, self.frame = self.frame, None
            self.want = True
            self.cv.notify_all()
            return fr

    def close(self):
        with self.cv:
            self.stop = True
            self.cv.notify_all()


class Bot:
    def __init__(self, log=print):
        import mss
        self.log = log
        self.sct = mss.mss()
        self.mon = self.sct.monitors[1]
        self.g = Geometry(self.mon["width"], self.mon["height"])
        self.keys = Keys()
        self.ctl = Controller(self.keys, self.g)

    def grab(self, b):
        x0, y0, x1, y1 = b
        shot = self.sct.grab({"left": self.mon["left"] + x0, "top": self.mon["top"] + y0,
                              "width": x1 - x0, "height": y1 - y0})
        return np.asarray(shot)[:, :, :3], (x0, y0)

    def ui_state(self):
        g = self.g
        box = (min(b[0] for b in g.hints), min(b[1] for b in g.hints),
               max(b[2] for b in g.hints), max(b[3] for b in g.hints))
        img, off = self.grab(box)
        game = game_visible(img, g, off)
        img, off = self.grab(g.ebox)
        return game, prompt_visible(img, g, off)

    def read(self):
        if getattr(self, "grabber", None) is not None:
            fr = self.grabber.get()
            if fr is not None:
                st = analyze(fr[1], self.g, fr[2])
                st["t_cap"] = fr[0]
                return st
        t_cap = time.time()
        img, off = self.grab(self.g.main)
        st = analyze(img, self.g, off)
        st["t_cap"] = t_cap
        return st

    def run(self, ctrl):
        """ctrl - кто управляет ботом (окно или консоль): active, quit,
        start_vk, exit_vk, auto_repeat, binding, toggle(), request_exit(), status()."""
        log = self.log
        prev_start = prev_exit = False
        was_active = in_game = False
        game = prompt = False
        game_gone_t = None
        last_e = last_ui = 0.0
        e_tries = 0
        last_progress = 0.0
        last_mode = None
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)    # точные короткие нажатия A/D (таймер Windows иначе ~15 мс)
        except Exception:
            pass
        self.grabber = Grabber(self.mon, self.g)
        try:
            while not ctrl.quit:
                if not switches.is_on("sewbeta"):
                    ctrl.active = False
                    prev_start = prev_exit = True        # клавиши не срабатывают, пока бот выключен в меню
                elif not ctrl.binding:
                    k = key_pressed(ctrl.start_vk)
                    if k and not prev_start:
                        ctrl.toggle()
                    prev_start = k
                    k = key_pressed(ctrl.exit_vk)
                    if k and not prev_exit:
                        ctrl.request_exit()
                    prev_exit = k
                if ctrl.active != was_active:
                    was_active = ctrl.active
                    self.ctl.reset()
                    in_game = False
                    e_tries = 0
                    log("СТАРТ" if was_active else "ПАУЗА")
                    ctrl.status("Работает" if was_active else
                                ("Пауза" if switches.is_on("sewbeta") else "Отключён в меню"))
                if not ctrl.active:
                    BUSY["on"] = False
                    time.sleep(0.03)
                    continue

                now = time.time()
                game, prompt = self.grabber.ui            # снимает поток Grabber раз в UI_CHECK

                BUSY["on"] = bool(game)
                if game:
                    if not in_game:
                        log("Игра открыта, шью")
                        ctrl.status("Шью")
                        in_game = True
                        e_tries = 0
                        last_progress = 0.0
                        self.ctl.reset()
                    st = self.read()
                    # максимум за комплект: кадр, снятый уже после закрытия игры, прогресс не обнуляет
                    last_progress = max(last_progress, st["progress"])
                    mode = self.ctl.apply(st)[0]
                    if mode != last_mode and mode in ("recover", "search"):
                        log("  вылетел с линии, возвращаюсь" if mode == "recover"
                            else "  линии не видно, ищу")
                    last_mode = mode
                    continue

                if in_game:
                    in_game = False
                    self.ctl.reset()
                    game_gone_t = now
                    if last_progress > 0.9:
                        ctrl.done += 1
                        log(f"Комплект готов, всего за сессию: {ctrl.done}")
                    ctrl.status("Жду игру")
                if not ctrl.auto_repeat:
                    time.sleep(0.05)
                    continue
                if prompt and (game_gone_t is None or now - game_gone_t > E_DELAY) \
                        and now - last_e > E_RETRY:
                    if e_tries >= E_MAX_TRIES:
                        log("E нажата много раз, игра не открылась - пауза")
                        ctrl.active = False
                        continue
                    self.keys.tap(SC_E)
                    last_e = now
                    e_tries += 1
                    log("Нажал E")
                time.sleep(0.05)
        finally:
            BUSY["on"] = False
            self.grabber.close()
            self.keys.gen = getattr(self.keys, "gen", 0) + 1     # отменить отложенные отпускания
            self.keys.release_all()
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    def debug_shot(self):
        img, off = self.grab((0, 0, self.mon["width"], self.mon["height"]))
        st = analyze(img, self.g, off)
        mode, err, target, dbg = self.ctl.decide(st, time.time())
        path = os.path.join(app_dir(), "sewing_debug.png")
        cv2.imwrite(path, draw_debug(img, self.g, off, st, f"{mode} err={err:+.0f}", dbg,
                                     self.ctl.trace_dbg))
        self.log(f"игра={game_visible(img, self.g, off)} подсказка_E={prompt_visible(img, self.g, off)} "
                 f"режим={mode}")
        self.log(f"Сохранено {path}")
        self.log("зелёное - что шить, красное - прошито, синие точки - куда поедет")
        return path


def test_video(path, gui=True):
    cap = cv2.VideoCapture(path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    g = Geometry(w, h)
    ctl = Controller(Keys(dry=True), g)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    f = lambda a: "  --" if a is None else f"{a:+5.0f}"
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = i / fps
        st = dbg = None
        if game_visible(frame, g, (0, 0)):
            st = analyze(frame, g, (0, 0))
            mode, err, target, dbg = ctl.apply(st, t)
        else:
            ctl.reset()
            mode, err, target = ("prompt_E" if prompt_visible(frame, g, (0, 0)) else "no_game"), None, 0
        if i % int(fps // 2) == 0:
            if st:
                sp = st["speed"]
                print(f"{t:6.2f}s {mode:8s} err={f(err)} target={target:.2f} "
                      f"speed={'stop' if sp is None else f'{sp:.2f}'} progress={st['progress']:.2f}")
            else:
                print(f"{t:6.2f}s {mode}")
        if gui:
            cv2.imshow("sewing", draw_debug(frame, g, (0, 0), st, mode, dbg,
                                            ctl.trace_dbg if st else None))
            if cv2.waitKey(max(1, int(1000 / fps))) & 0xFF == 27:
                break
        i += 1
    if gui:
        cv2.destroyAllWindows()


# ================================================================ настройки клавиш
VK_NAMES = {0x04: "Средняя кнопка мыши", 0x05: "Боковая мыши 1", 0x06: "Боковая мыши 2",
            0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock",
            0x1B: "Esc", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
            0x25: "Стрелка влево", 0x26: "Стрелка вверх", 0x27: "Стрелка вправо",
            0x28: "Стрелка вниз", 0x2D: "Insert", 0x2E: "Delete",
            0x6A: "Numpad *", 0x6B: "Numpad +", 0x6D: "Numpad -", 0x6E: "Numpad .",
            0x6F: "Numpad /", 0x90: "NumLock", 0x91: "ScrollLock", 0xC0: "`"}
VK_NAMES.update({0x30 + i: str(i) for i in range(10)})
VK_NAMES.update({0x41 + i: chr(0x41 + i) for i in range(26)})
VK_NAMES.update({0x60 + i: f"Numpad {i}" for i in range(10)})
VK_NAMES.update({0x70 + i: f"F{i + 1}" for i in range(24)})
GAME_VKS = {0x41: "A", 0x44: "D", 0x45: "E", 0x20: "Space"}   # их жмёт сам бот
SKIP_VKS = {0x01, 0x02, 0x10, 0x11, 0x12}                       # ЛКМ, ПКМ, общие Shift/Ctrl/Alt


def vk_name(vk):
    return VK_NAMES.get(vk, f"клавиша 0x{vk:02X}")


def app_dir():
    if getattr(sys, "frozen", False):          # собранный exe
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "sewing_beta_config.json")


def load_config():
    global K_TURN
    cfg = {"start_vk": VK_START, "exit_vk": VK_EXIT, "auto_repeat": AUTO_REPEAT, "k_turn": K_TURN}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    try:
        K_TURN = min(2.0, max(1.0, float(cfg["k_turn"])))
    except (TypeError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


class Ctrl:
    """Общее состояние между окном и потоком бота."""

    def __init__(self, cfg, on_status=None, on_exit=None):
        self.active = False
        self.quit = False
        self.binding = False
        self.done = 0
        self.start_vk = cfg["start_vk"]
        self.exit_vk = cfg["exit_vk"]
        self.auto_repeat = cfg["auto_repeat"]
        self._on_status = on_status
        self._on_exit = on_exit

    def toggle(self):
        if not switches.is_on("sewbeta"):
            self.status("Отключён в меню")
            return
        self.active = not self.active

    def request_exit(self):
        self.active = False
        self.quit = True
        if self._on_exit:
            self._on_exit()

    def status(self, text):
        if self._on_status:
            self._on_status(text)


def build_bot_tab(parent, request_close=None):
    """Рисует интерфейс бота внутри parent. Возвращает функцию остановки."""
    import threading
    import queue
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    q = queue.Queue()
    ctrl = Ctrl(cfg, on_status=lambda t: q.put(("status", t)),
                on_exit=lambda: q.put(("exit", None)))
    log = lambda msg: q.put(("log", msg))

    pad = {"padx": 8, "pady": 4}
    status_var = tk.StringVar(value="Пауза")
    import widgets
    widgets.register("sewbeta", lambda: {"active": ctrl.active, "status": status_var.get(), "rate_of": "Комплектов",
                                     "items": [("Комплектов", ctrl.done)]})
    status_lbl = ttk.Label(parent, textvariable=status_var, style="Status.TLabel")
    status_lbl.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    ttk.Button(parent, text="Старт / пауза", style="Accent.TButton", command=ctrl.toggle).grid(
        row=1, column=0, columnspan=3, sticky="ew", **pad)

    key_vars = {}

    def key_row(row, title, attr):
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky="w", **pad)
        var = tk.StringVar(value=vk_name(getattr(ctrl, attr)))
        key_vars[attr] = var
        ttk.Label(parent, textvariable=var, width=18, style="Key.TLabel").grid(row=row, column=1, **pad)
        ttk.Button(parent, text="Назначить", command=lambda: start_binding(attr)).grid(
            row=row, column=2, **pad)

    key_row(2, "Старт / пауза:", "start_vk")
    kt_var = tk.StringVar(value=f"{K_TURN:.2f}")
    ttk.Label(parent, text="Смелость в поворотах:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Spinbox(parent, from_=1.0, to=2.0, increment=0.05, width=6, textvariable=kt_var).grid(row=3, column=1, sticky="w", **pad)
    ttk.Label(parent, text="1.30 - как обычная, 1.45 - по умолч., 1.60 - быстрее (больше мимо узора)",
              style="Muted.TLabel").grid(row=3, column=2, sticky="w", **pad)

    def kt_changed(*a):
        global K_TURN
        try:
            K_TURN = min(2.0, max(1.0, float(kt_var.get().replace(",", "."))))
        except ValueError:
            return
        persist()
    kt_var.trace_add("write", kt_changed)

    auto_var = tk.BooleanVar(value=ctrl.auto_repeat)

    def persist():
        save_config({"start_vk": ctrl.start_vk, "exit_vk": ctrl.exit_vk,
                     "auto_repeat": ctrl.auto_repeat, "k_turn": K_TURN})

    def on_auto():
        ctrl.auto_repeat = auto_var.get()
        persist()

    ttk.Checkbutton(parent, text="Сам жать E и шить следующий комплект",
                    variable=auto_var, command=on_auto).grid(
        row=4, column=0, columnspan=3, sticky="w", **pad)

    def do_debug():
        def work():
            try:
                Bot(log=log).debug_shot()
            except Exception as ex:
                log(f"Ошибка скриншота: {ex}")
        threading.Thread(target=work, daemon=True).start()

    ttk.Button(parent, text="Проверочный скриншот", command=do_debug).grid(
        row=5, column=0, columnspan=3, sticky="ew", **pad)

    logbox = tk.Text(parent, height=12, width=64, state="disabled", font=("Consolas", 9))
    logbox.grid(row=6, column=0, columnspan=3, **pad)

    # ---- назначение клавиши: ждём, пока всё отпущено, потом первую нажатую
    bind_state = {"attr": None, "armed": False, "t0": 0.0}

    def start_binding(attr):
        ctrl.binding = True
        bind_state.update(attr=attr, armed=False, t0=time.time())
        key_vars[attr].set("нажми клавишу...")
        parent.after(30, poll_binding)

    def any_down():
        return [vk for vk in range(1, 255)
                if vk not in SKIP_VKS and key_pressed(vk)]

    def finish_binding(vk=None, msg=None):
        attr = bind_state["attr"]
        if vk is not None:
            setattr(ctrl, attr, vk)
            persist()
        key_vars[attr].set(vk_name(getattr(ctrl, attr)))
        if msg:
            log(msg)
        bind_state["attr"] = None
        ctrl.binding = False

    def poll_binding():
        if bind_state["attr"] is None:
            return
        if time.time() - bind_state["t0"] > 10:
            finish_binding(msg="Назначение отменено (10 секунд без нажатия)")
            return
        down = any_down()
        if not bind_state["armed"]:
            if not down:
                bind_state["armed"] = True
        elif down:
            vk = down[0]
            other = "exit_vk" if bind_state["attr"] == "start_vk" else "start_vk"
            if vk == 0x1B:
                finish_binding(msg="Назначение отменено")
                return
            if vk in GAME_VKS:
                finish_binding(msg=f"{GAME_VKS[vk]} нельзя: эту клавишу жмёт сам бот")
                return
            if vk == getattr(ctrl, other):
                finish_binding(msg=f"{vk_name(vk)} уже занята другой командой")
                return
            finish_binding(vk, f"Назначено: {vk_name(vk)}")
            return
        parent.after(30, poll_binding)

    def pump():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", time.strftime("%H:%M:%S ") + str(val) + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                elif kind == "status":
                    status_var.set(val)
                    status_lbl.configure(style="StatusOn.TLabel" if val in ("Работает", "Шью")
                                         else "StatusErr.TLabel" if val == "Ошибка" else "Status.TLabel")
                elif kind == "exit":
                    if request_close:
                        request_close()
                    return
        except queue.Empty:
            pass
        except tk.TclError:          # окно уже закрыто
            return
        parent.after(80, pump)

    def worker():
        try:
            Bot(log=log).run(ctrl)
        except Exception as ex:
            log(f"Ошибка: {ex}")
            q.put(("status", "Ошибка"))

    def stop():
        ctrl.quit = True

    log(f"Старт/пауза: {vk_name(ctrl.start_vk)}" + (f", выход: {vk_name(ctrl.exit_vk)}" if ctrl.exit_vk else "")
        + f", смелость в поворотах {K_TURN:.2f}")
    threading.Thread(target=worker, daemon=True).start()
    parent.after(80, pump)
    return stop


def run_gui():
    import tkinter as tk
    root = tk.Tk()
    root.title("Швейный бот")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    try:
        import theme
        theme.apply(root)
    except Exception:
        pass
    stop = build_bot_tab(root, request_close=lambda: root.after(200, root.destroy))

    def on_close():
        stop()
        root.after(200, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    stop()


def run_console():
    cfg = load_config()
    ctrl = Ctrl(cfg)
    print(f"{vk_name(ctrl.start_vk)} : старт/пауза, {vk_name(ctrl.exit_vk)} : выход")
    Bot().run(ctrl)


if __name__ == "__main__":
    if "--video" in sys.argv:
        test_video(sys.argv[sys.argv.index("--video") + 1], gui="--nogui" not in sys.argv)
    elif "--debug" in sys.argv:
        Bot().debug_shot()
    elif "--console" in sys.argv:
        run_console()
    else:
        run_gui()
