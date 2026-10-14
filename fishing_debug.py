# -*- coding: utf-8 -*-
"""
Отладка рыбалки.

  Recorder - записывает маленькое видео того, что видит бот, с пометками
             (какую клавишу держит, где плашка, где брызги, движение фона)
             + таблицу значений по кадрам. Это видео присылай для разбора.
  Overlay  - прозрачная надпись и рамки поверх игры наживо. Сквозь неё проходят клики,
             и она спрятана от захвата экрана, чтобы бот не видел собственных рамок.
"""
import os
import sys
import time
import queue
import threading

import numpy as np
import cv2

STATE_COLORS = {"Тяну рыбу": (60, 200, 255), "Жду поклёвку": (120, 220, 120),
                "Заброс": (255, 200, 80), "Улов": (200, 140, 255)}


def _app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def _put(img, text, org, scale=0.6, color=(255, 255, 255), thick=1):
    # латиница/цифры через cv2; русские подписи рисуем значками и цифрами
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


STATE_EN = {"Тяну рыбу": "FIGHT", "Жду поклёвку": "WAIT BITE", "Заброс": "CAST",
            "Улов": "CATCH", "Не рыбачу": "IDLE", "Жду": "IDLE"}
SRC_EN = {"фон": "background", "брызги": "splash", "фон+брызги": "bg+splash", "-": "-"}


def annotate(frame, dbg, out_w=960):
    """Кадр с пометками бота (уменьшенный)."""
    H, W = frame.shape[:2]
    k = out_w / W
    img = cv2.resize(frame, (out_w, int(H * k)))
    st = dbg.get("state", "")
    held = dbg.get("held")
    # верхняя панель
    cv2.rectangle(img, (0, 0), (out_w, 54), (15, 15, 20), -1)
    _put(img, STATE_EN.get(st, st), (10, 22), 0.6, STATE_COLORS.get(st, (220, 220, 220)), 2)
    _put(img, f"t={dbg.get('t', 0):.1f}", (10, 44), 0.45, (180, 180, 180))
    # клавиша
    key = held or "-"
    col = (60, 60, 255) if held == "A" else (255, 120, 60) if held == "D" else (120, 120, 120)
    cv2.rectangle(img, (out_w // 2 - 30, 4), (out_w // 2 + 30, 50), col, -1)
    _put(img, key, (out_w // 2 - 12, 38), 1.1, (255, 255, 255), 3)
    if st == "Тяну рыбу":
        d = dbg.get("dir", 0)
        arrow = "fish ->" if d == 1 else "<- fish" if d == -1 else "fish ?"
        _put(img, arrow, (out_w // 2 + 45, 22), 0.6, (255, 255, 255), 2)
        _put(img, f"by: {SRC_EN.get(dbg.get('source', '-'), dbg.get('source', '-'))}",
             (out_w // 2 + 45, 44), 0.45, (200, 200, 200))
        _put(img, f"fish v {dbg.get('bg', 0):+6.1f}px/s", (out_w - 230, 22), 0.45, (200, 230, 255))
        _put(img, f"confidence x{dbg.get('bg_conf', 0):.1f}", (out_w - 230, 44), 0.45, (200, 255, 200))
        mk = dbg.get("marker")
        if mk:
            x, y, s = mk
            cv2.rectangle(img, (int((x - s / 2) * k), int((y - s / 2) * k)),
                          (int((x + s / 2) * k), int((y + s / 2) * k)), (0, 255, 255), 2)
        cl = dbg.get("cloud")
        if cl:
            cv2.circle(img, (int(cl[0] * k), int(cl[1] * k)), 14, (255, 0, 255), 2)
        sx = dbg.get("splash")
        if sx is not None and mk:
            cv2.line(img, (int(sx * k), int((mk[1] - 60) * k)), (int(sx * k), int((mk[1] + 60) * k)), (255, 255, 255), 2)
            cv2.circle(img, (int(sx * k), int(mk[1] * k)), 5, (255, 255, 255), -1)
    cb = dbg.get("cast")
    if cb:
        g0, g1, x = cb
        yb = int(893 * H / 1080 * k)
        cv2.rectangle(img, (int(g0 * k), yb - 10), (int(g1 * k), yb + 10), (0, 255, 0), 2)
        if x is not None:
            cv2.line(img, (int(x * k), yb - 16), (int(x * k), yb + 16), (255, 255, 255), 2)
    return img


class Recorder:
    """Пишет отладочное видео (10 кадров/с) и таблицу CSV рядом с программой."""

    def __init__(self, fps=10, only_fight=False):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.join(_app_dir(), f"debug_fishing_{stamp}")
        self.video_path = base + ".mp4"
        self.raw_path = base + "_raw.mp4"           # чистое видео в полном качестве - для разбора
        self.csv_path = base + ".csv"
        self.raw = None
        self.q = None
        self.th = None
        self.fps = fps
        self.only_fight = only_fight
        self.writer = None
        self.last = 0.0
        self.csv = open(self.csv_path, "w", encoding="utf-8")
        self.csv.write("t;state;held;dir;source;bg;bg_conf;splash;splash_n;marker_x;marker_y;splash_x\n")

    def add(self, frame, dbg):
        """Быстро: только кладёт кадр в очередь. Пишет отдельный поток, чтобы не тормозить бота."""
        now = time.time()
        if now - self.last < 1.0 / self.fps:
            return
        if self.only_fight and dbg.get("state") != "Тяну рыбу":
            return
        self.last = now
        if self.q is None:
            self.q = queue.Queue(maxsize=30)
            self.th = threading.Thread(target=self._writer, daemon=True)
            self.th.start()
        try:
            self.q.put_nowait((frame.copy(), dict(dbg)))
        except queue.Full:
            pass                                # не успеваем писать - пропускаем кадр, бот важнее

    def _writer(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            frame, dbg = item
            try:
                self._write(frame, dbg)
            except Exception:
                pass

    def _write(self, frame, dbg):
        img = annotate(frame, dbg)
        if self.writer is None:
            h, w = img.shape[:2]
            self.writer = cv2.VideoWriter(self.video_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        self.writer.write(img)
        if self.raw is None:
            h, w = frame.shape[:2]
            self.raw = cv2.VideoWriter(self.raw_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        self.raw.write(frame)
        mk = dbg.get("marker") or (None, None, None)
        self.csv.write(";".join(str(v) for v in (
            round(dbg.get("t", 0), 2), dbg.get("state"), dbg.get("held"), dbg.get("dir"), dbg.get("source"),
            round(dbg.get("bg", 0), 1), round(dbg.get("bg_conf", 0), 2), round(dbg.get("sp", 0), 2),
            dbg.get("sp_n", 0), None if mk[0] is None else int(mk[0]), None if mk[1] is None else int(mk[1]),
            None if dbg.get("splash") is None else int(dbg["splash"]))) + "\n")

    def close(self):
        if self.q is not None:
            self.q.put(None)
            self.th.join(timeout=10)
        try:
            if self.writer is not None:
                self.writer.release()
            if self.raw is not None:
                self.raw.release()
            self.csv.close()
        except Exception:
            pass


class Overlay:
    """Прозрачный слой поверх игры: клики проходят сквозь, бот его не видит."""

    KEY = "#010203"

    def __init__(self, root):
        import tkinter as tk
        self.tk = tk
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.sw, self.sh = sw, sh
        self.win.geometry(f"{sw}x{sh}+0+0")
        self.win.configure(bg=self.KEY)
        try:
            self.win.attributes("-transparentcolor", self.KEY)
        except Exception:
            pass
        self.cv = tk.Canvas(self.win, width=sw, height=sh, bg=self.KEY, highlightthickness=0)
        self.cv.pack()
        self.win.update_idletasks()
        self._click_through_and_hide()

    def _click_through_and_hide(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x80
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
            u.SetWindowDisplayAffinity(hwnd, 0x11)        # WDA_EXCLUDEFROMCAPTURE: не попадает в снимки
        except Exception:
            pass

    def draw(self, dbg):
        cv = self.cv
        cv.delete("all")
        if not dbg:
            return
        W, H = dbg.get("W", self.sw), dbg.get("H", self.sh)
        kx, ky = self.sw / W, self.sh / H
        st = dbg.get("state", "")
        held = dbg.get("held")
        x0, y0 = 20, 120
        cv.create_rectangle(x0, y0, x0 + 300, y0 + 96, fill="#0a0a0d", outline="#e3263d", width=2)
        cv.create_text(x0 + 12, y0 + 16, anchor="w", text=f"Рыбалка: {st}", fill="#ffffff",
                       font=("Segoe UI", 11, "bold"))
        col = "#ff4d4d" if held == "A" else "#4da6ff" if held == "D" else "#555560"
        cv.create_rectangle(x0 + 12, y0 + 32, x0 + 62, y0 + 84, fill=col, outline="")
        cv.create_text(x0 + 37, y0 + 58, text=held or "-", fill="#ffffff", font=("Segoe UI", 22, "bold"))
        if st == "Тяну рыбу":
            d = dbg.get("dir", 0)
            cv.create_text(x0 + 76, y0 + 44, anchor="w", fill="#ffffff", font=("Segoe UI", 10, "bold"),
                           text="рыба → вправо" if d == 1 else "рыба ← влево" if d == -1 else "рыба ?")
            cv.create_text(x0 + 76, y0 + 64, anchor="w", fill="#9a9aa6", font=("Segoe UI", 9),
                           text=f"по: {dbg.get('source', '-')}   скорость {dbg.get('bg', 0):+.0f} px/с")
            cv.create_text(x0 + 76, y0 + 82, anchor="w", fill="#9a9aa6", font=("Segoe UI", 9),
                           text=f"уверенность x{dbg.get('bg_conf', 0):.1f}")
            mk = dbg.get("marker")
            if mk:
                x, y, s = mk
                cv.create_rectangle((x - s / 2) * kx, (y - s / 2) * ky, (x + s / 2) * kx, (y + s / 2) * ky,
                                    outline="#ffe000", width=2)
                sx = dbg.get("splash")
                if sx is not None:
                    cv.create_line(sx * kx, (y - 70) * ky, sx * kx, (y + 70) * ky, fill="#ffffff", width=2)
        cb = dbg.get("cast")
        if cb:
            g0, g1, x = cb
            yb = 893 * H / 1080
            cv.create_rectangle(g0 * kx, (yb - 12) * ky, g1 * kx, (yb + 12) * ky, outline="#00ff66", width=2)

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass
