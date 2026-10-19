# -*- coding: utf-8 -*-
"""Ферма (сбор апельсинов): к дереву подходишь и жмёшь E сам. Бот, пока открыта мини-игра
«Соберите спелые апельсины нажатием» (крона дерева во весь экран), кликает по каждому апельсину.

Что выяснено по записи 20261019_161019 (1920x1080, 13 деревьев):
  - мини-игра - одна и та же картинка куста по центру экрана, апельсины (5-10) на разных местах,
    часть спрятана за листьями (видны кусками); клик по апельсину убирает его сразу;
  - после последнего апельсина куст пропадает, а подсказка слева вверху висит ещё ~1,3 с -
    позади видно настоящее дерево с плодами и жёлтые перчатки игрока, поэтому «мини-игра открыта» =
    на месте куст (сравнение с эталоном по точкам: открыта >= 0,95, закрыта <= 0,34), а не подсказка;
  - апельсин = насыщенный оранжевый (H 6-26, S>150, V>170); куски одного спрятанного апельсина
    склеиваются в один, клик - в самую «толстую» точку оранжевого (на плоде, а не на листе);
  - все 83 клика игрока - рядом с найденными апельсинами, лишних пятен на кусте нет."""
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
ROI = (560, 60, 1360, 820)             # куст мини-игры
BUSH_ON = 0.7                          # доля точек куста, совпавших с эталоном: мини-игра открыта
SETTLE = 0.25                          # после появления куста
WAIT_GONE = 0.15                       # после клика: апельсин пропадает сразу
TRIES = 3                              # кликов по апельсину, который не пропадает, - потом пропускаем
MIN_AREA = 60                          # оранжевых пикселей в апельсине (спрятанный за листьями - от ~130)


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


CONFIG_PATH = os.path.join(app_dir(), "farm_config.json")


def default_config():
    return {"toggle_vk": 0x66,            # Numpad 6
            "mini_cps": 3.0, "focus_only": True}


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
    """Кадр экрана (BGR, любой размер) -> открыта ли мини-игра и где апельсины (координаты 1920x1080)."""

    def __init__(self):
        a = np.load(res_path("farm_assets.npz"))
        self.ys, self.xs = a["ys"].astype(np.intp), a["xs"].astype(np.intp)
        self.ref = a["ref"].astype(np.int16)

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

    def bush(self, f):
        """Доля точек куста, совпавших с эталоном (0..1)."""
        px = f[self.ys, self.xs].astype(np.int32)
        g = (px[:, 0] * 114 + px[:, 1] * 587 + px[:, 2] * 299 + 500) // 1000
        return float((np.abs(g - self.ref) < 20).mean())

    def is_open(self, f):
        return self.bush(f) > BUSH_ON

    @staticmethod
    def fruits(f):
        """-> [{"pt": (x, y) куда кликать, "area": ..}]."""
        x0, y0, x1, y1 = ROI
        hsv = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, (6, 150, 170), (26, 255, 255))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        big = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21)))
        n, lab = cv2.connectedComponents(big)
        dist = cv2.distanceTransform(m, cv2.DIST_L2, 3)
        out = []
        for i in range(1, n):
            part = (lab == i) & (m > 0)
            a = int(part.sum())
            if a < MIN_AREA:
                continue
            d = np.where(part, dist, 0)
            y, x = np.unravel_index(int(np.argmax(d)), d.shape)
            out.append({"pt": (int(x) + x0, int(y) + y0), "area": a})
        return out


def order(pts, start):
    """Порядок обхода: каждый раз ближайший к прошлой точке."""
    left, cur, res = list(pts), start, []
    while left:
        nxt = min(left, key=lambda p: (p["pt"][0] - cur[0]) ** 2 + (p["pt"][1] - cur[1]) ** 2)
        left.remove(nxt)
        res.append(nxt)
        cur = nxt["pt"]
    return res


def analyze_video(path, out=None, step=1):
    """Проверка зрения на записи: открыта ли мини-игра и найденные апельсины по кадрам."""
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
        on = vis.is_open(f)
        fs = vis.fruits(f) if on else []
        if on != last:
            print(f"кадр {i}: " + (f"мини-игра, апельсинов {len(fs)}" if on else "-"))
            last = on
        if out:
            v = f.copy()
            cv2.putText(v, f"{'mini' if on else '-'} {len(fs)}", (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 255, 255), 3)
            for o in fs:
                cv2.circle(v, o["pt"], 22, (255, 0, 255), 3)
            v = cv2.resize(v, (960, 540))
            if wr is None:
                wr = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), 15, (960, 540))
            wr.write(v)
    if wr:
        wr.release()


# ====================================================================== интерфейс и рабочий поток
def build_farm_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    stats = {"trees": 0, "fruits": 0, "clicks": 0, "state": "idle", "view": None, "left": 0}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    def toggle():
        if not switches.is_on("farm") and not ctl["active"]:
            log("Ферма выключена: включи её переключателем в меню слева")
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
    ttk.Label(box, text="Бегаешь от дерева к дереву и жмёшь E сам. Когда откроется крона с апельсинами "
                        "(«Соберите спелые апельсины нажатием»), бот сам кликнет по каждому апельсину.",
              style="Muted.TLabel", wraplength=720, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    mini_var = tk.DoubleVar(value=float(cfg["mini_cps"]))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    ttk.Label(box, text="Скорость сбора, кликов в секунду:").grid(row=1, column=0, sticky="w", **pad)
    ttk.Spinbox(box, from_=0.5, to=10, increment=0.5, width=6, textvariable=mini_var,
                command=lambda: save()).grid(row=1, column=1, sticky="w", **pad)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var, command=lambda: save()).grid(
        row=2, column=0, columnspan=3, sticky="w", **pad)

    def save():
        try:
            cfg["mini_cps"] = max(0.5, min(10.0, float(mini_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    mini_var.trace_add("write", lambda *a: save())

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

        def near(p, q_, r=28):
            return abs(p[0] - q_[0]) < r and abs(p[1] - q_[1]) < r

        def lclick():
            inp.mouse(AF.MOUSEEVENTF_LEFTDOWN)
            time.sleep(random.uniform(0.03, 0.06))
            inp.mouse(AF.MOUSEEVENTF_LEFTUP)

        def grab():
            return vis.norm(np.ascontiguousarray(scr.grab()[:, :, :3]))

        def pace():
            """Пауза перед кликом - по скорости из настроек. False - бота выключили."""
            end = last_hit[0] + random.uniform(0.85, 1.15) / max(0.5, cfg["mini_cps"])
            while time.time() < end:
                if ctl["quit"] or not ctl["active"]:
                    return False
                time.sleep(0.01)
            return True

        prev = was = False
        last_hit = [0.0]
        opened = False
        t0 = 0.0
        cur = (960, 100)            # где была мышь в прошлый клик - следующий апельсин ближайший
        tries = []                  # [[x, y, кликов]]
        skip = []
        n_tree = 0
        while not ctl["quit"]:
            if not switches.is_on("farm"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                log("Ферма включена: жми E у дерева" if was else "Ферма выключена")
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("farm") else "Выключен (тумблер)"
                stats["view"] = None
                opened = False
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
                on = vis.is_open(f)
            except Exception as ex:
                log(f"Ошибка: {ex}")
                time.sleep(1)
                continue
            if on and not opened:
                opened, t0 = True, now
                last_hit[0] = now
                tries, skip, n_tree, cur = [], [], 0, (960, 100)
                log("Мини-игра: собираю апельсины")
            elif not on and opened:
                opened = False
                stats["trees"] += 1
                log(f"Дерево готово: апельсинов {n_tree} за {now - t0:.1f} с")
            if not on:
                BUSY["on"] = False
                stats["state"] = "idle"
                stats["view"] = None
                stats["left"] = 0
                ctl["status"] = "Жду мини-игру (нажми E у дерева)"
                time.sleep(0.05)
                continue
            BUSY["on"] = True
            stats["state"] = "mini"
            if now - t0 < SETTLE:
                ctl["status"] = "Мини-игра открывается…"
                time.sleep(0.03)
                continue
            found = vis.fruits(f)
            live = [o for o in found if not any(near(o["pt"], s) for s in skip)]
            stats["view"] = {"ores": [o["pt"] for o in found], "skip": list(skip)}
            stats["left"] = len(live)
            ctl["status"] = f"Собираю апельсины: осталось {len(live)}"
            if not live:
                if skip and now - last_hit[0] > 2.0:
                    skip = []                         # мини-игра не закрывается - пробуем пропущенные ещё раз
                    tries = []
                time.sleep(0.03)
                continue
            o = order(live, cur)[0]
            t = next((x for x in tries if near(o["pt"], x)), None)
            if t is None:
                t = [o["pt"][0], o["pt"][1], 0]
                tries.append(t)
            if t[2] >= TRIES:
                skip.append((t[0], t[1]))
                continue
            if not pace():
                continue
            stats["view"]["t"] = o["pt"]
            inp.move_to(*to_screen(o["pt"]))
            time.sleep(0.03)
            lclick()
            last_hit[0] = time.time()
            cur = o["pt"]
            t[2] += 1
            stats["clicks"] += 1
            time.sleep(WAIT_GONE)
            f2 = grab()
            if not vis.is_open(f2) or not any(near(p["pt"], t) for p in vis.fruits(f2)):
                tries.remove(t)
                n_tree += 1
                stats["fruits"] += 1
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
        info_var.set(f"Деревьев: {stats['trees']}   ·   апельсинов: {stats['fruits']}   ·   кликов: {stats['clicks']}")
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Деревьев", "view": stats["view"],
                "state": stats["state"], "cps": cfg["mini_cps"], "left": stats["left"],
                "items": [("Деревьев", stats["trees"]), ("Апельсинов", stats["fruits"])]}
    widgets.register("farm", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
