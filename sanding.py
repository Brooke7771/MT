# -*- coding: utf-8 -*-
"""Шлифовка (работа «Стройка»): бот проходит мини-игру. E жмёшь сам: бот включается, когда на экране
появилась доска с тёмными пятнами и зелёная шлифмашинка, и водит её клавишами WASD по пятнам.

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
        stain = ((rr - b >= 18) & (rr < 110) & (g < 70) & (b < 40)).astype(np.uint8)
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


# ====================================================================== интерфейс и рабочий поток
def build_sand_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"boards": 0, "stains": 0, "view": None, "state": "idle", "left": 0}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("sand") and not ctl["active"]:
            log("Шлифовка выключена: включи её переключателем наверху страницы «Тест»")
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
    ttk.Label(box, text="Нажми E у стола как обычно: когда откроется мини-игра, бот сам поведёт шлифмашинку по пятнам.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    vmax_var = tk.IntVar(value=int(cfg["vmax"]))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Label(box, text="Максимальная скорость машинки, px/с:").grid(row=1, column=0, sticky="w", **pad)
    ttk.Spinbox(box, from_=300, to=900, increment=50, width=6, textvariable=vmax_var,
                command=lambda: save()).grid(row=1, column=1, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["vmax"] = max(300, min(900, int(vmax_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    vmax_var.trace_add("write", lambda *a: save())

    logbox = tk.Text(parent, height=10, width=80, state="disabled", font=("Consolas", 9))
    logbox.grid(row=5, column=0, columnspan=4, **pad)

    # ---------------- рабочий поток
    def worker():
        import ctypes
        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        scr = AF.Screen()
        inp = AF.Input2()
        W, H = scr.mon["width"], scr.mon["height"]
        vis = Vision(W, H)
        gx0, gy0, gx1, gy1 = sc(GRAB, W, H)
        ctrl = Controller(cfg)
        held = {"x": 0, "y": 0}

        def release():
            for ax, (neg, pos) in (("x", ("A", "D")), ("y", ("W", "S"))):
                if held[ax]:
                    inp.key(neg if held[ax] < 0 else pos, False)
                    held[ax] = 0

        def apply(ux, uy):
            for ax, u, (neg, pos) in (("x", ux, ("A", "D")), ("y", uy, ("W", "S"))):
                if u != held[ax]:
                    if held[ax]:
                        inp.key(neg if held[ax] < 0 else pos, False)
                    if u:
                        inp.key(neg if u < 0 else pos, True)
                    held[ax] = u

        prev = False
        was = False
        playing = False
        last_seen = 0.0
        game_t0 = 0.0
        prev_n = 0
        cleared = 0
        while not ctl["quit"]:
            cfg_local = cfg
            if not switches.is_on("sand"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                release()
                ctrl.reset()
                playing = False
                log("Шлифовка включена: жду мини-игру" if was else "Шлифовка выключена")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("sand") else "Выключена переключателем на странице «Тест»"
                stats["view"] = None
                time.sleep(0.1)
                continue
            if SEWING_BUSY["on"]:
                release()
                ctl["status"] = "Пауза: работает швейный бот"
                time.sleep(0.2)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                release()
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            now = time.time()
            try:
                shot = scr.sct.grab({"left": scr.mon["left"] + gx0, "top": scr.mon["top"] + gy0,
                                     "width": gx1 - gx0, "height": gy1 - gy0})
                img = np.ascontiguousarray(np.asarray(shot)[:, :, :3])
                d = vis.detect(img)
            except Exception as ex:
                release()
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if d["active"] and d["g"] is not None:
                last_seen = now
                BUSY["on"] = True
                if not playing:
                    playing = True
                    game_t0 = now
                    ctrl.reset()
                    prev_n = len(d["stains"])
                    cleared = 0
                    log(f"Мини-игра: пятен {prev_n}, веду машинку")
                disc = (d["g"][0] + OFFSET[0], d["g"][1] + OFFSET[1])
                n = len(d["stains"])
                if 0 < prev_n - n <= 3:
                    cleared += prev_n - n
                    stats["stains"] += prev_n - n
                prev_n = n
                ux, uy, tgt = ctrl.step(now, disc, d["stains"])
                apply(ux, uy)
                ctl["status"] = "Шлифую" if n else "Пятен нет"
                stats["state"] = "play"
                stats["left"] = n
                stats["view"] = {"stains": [(s[0], s[1], s[2]) for s in d["stains"]], "g": disc, "t": tgt}
                time.sleep(0.004)
            else:
                release()
                if playing and now - last_seen > 0.6:
                    playing = False
                    BUSY["on"] = False
                    if cleared >= 3 or prev_n == 0:
                        stats["boards"] += 1
                        log(f"Доска готова: счищено пятен {cleared} за {last_seen - game_t0:.1f} с")
                    else:
                        log("Мини-игра закрылась раньше времени")
                    stats["state"] = "idle"
                    stats["view"] = None
                if not playing:
                    BUSY["on"] = False
                    ctl["status"] = "Жду мини-игру (нажми E у стола)"
                time.sleep(0.05)
        release()
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
        info_var.set(f"Досок готово: {stats['boards']}   ·   пятен счищено: {stats['stains']}"
                     + (f"   ·   осталось на доске: {stats['left']}" if stats["state"] == "play" else ""))
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Досок", "view": stats["view"],
                "left": stats["left"], "vmax": int(cfg["vmax"]),
                "items": [("Досок", stats["boards"]), ("Пятен", stats["stains"])]}
    widgets.register("sand", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
