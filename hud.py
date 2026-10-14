# -*- coding: utf-8 -*-
"""Компактный HUD поверх игры: по карточке на каждого работающего бота.
Окно маленькое, прозрачное для кликов, не берёт фокус и не лезёт в области экрана, которые
читает бот. Как и оверлей рыбалки, исключено из снимков экрана (WDA_EXCLUDEFROMCAPTURE):
иначе бот видел бы собственный HUD и ошибался. Показывается только пока игра на переднем плане."""
import math
import sys
import time
import tkinter as tk

import widgets
from widgets import mix, _rrect, fmt_time
import theme as T

KEY = "#010203"
CARD = "#15151b"
ORDER = ["fish", "bot", "afk"]
TITLES = {"fish": "Рыбалка", "bot": "Швейный бот", "afk": "Анти-АФК"}
GAME_TITLES = ("gta", "rage", "majestic", "grand theft")
CW, CH, GAP = 260, 64, 8


class Hud:
    def __init__(self, root, dash, exclude_capture=True):
        self.root, self.dash = root, dash
        self.exclude = exclude_capture      # False - только для предпросмотра в тестах
        self.win = self.cv = None
        self.enabled = False
        self.side = "right"
        self.t0 = time.time()
        self.in_game = False
        self._focus_t = 0.0
        self._loop()

    def set_enabled(self, on):
        self.enabled = on
        if not on:
            self._hide()

    def set_side(self, side):
        self.side = side

    # ---- окно
    def _ensure(self):
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY)
        try:
            self.win.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(self.win, width=CW, height=CH, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        self.win.update_idletasks()
        if sys.platform == "win32":
            try:
                import ctypes
                u = ctypes.windll.user32
                hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
                style = u.GetWindowLongW(hwnd, -20)
                # слой, сквозной для кликов, не активируется, нет в панели задач
                u.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
                if self.exclude:
                    u.SetWindowDisplayAffinity(hwnd, 0x11)
            except Exception:
                pass

    def _hide(self):
        if self.win is not None:
            try:
                self.win.withdraw()
            except tk.TclError:
                pass

    def destroy(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None

    def _game_focused(self):
        now = time.time()
        if now - self._focus_t > 0.6:
            self._focus_t = now
            try:
                import antiafk
                title = antiafk.foreground_title().lower()
                self.in_game = any(t in title for t in GAME_TITLES)
            except Exception:
                self.in_game = True
        return self.in_game

    # ---- цикл
    def _items(self):
        out = []
        for key in ORDER:
            fn = widgets.PROVIDERS.get(key)
            if not fn:
                continue
            try:
                d = fn()
            except Exception:
                continue
            if d.get("active"):
                out.append((key, d))
        return out

    def _loop(self):
        delay = 200
        try:
            items = self._items() if self.enabled else []
            if items and self._game_focused():
                delay = 40
                self._show(items)
            else:
                self._hide()
        except tk.TclError:
            return
        except Exception:
            pass
        try:
            self.root.after(delay, self._loop)
        except tk.TclError:
            pass

    def _show(self, items):
        self._ensure()
        n = len(items)
        h = n * CH + (n - 1) * GAP
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = sw - CW - 20 if self.side == "right" else 20
        y = int(sh * 0.60) if self.side == "right" else int(sh * 0.58)
        self.win.geometry(f"{CW}x{h}+{x}+{y}")
        self.cv.configure(height=h)
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        self.cv.delete("all")
        for i, (key, d) in enumerate(items):
            self._card(key, d, i * (CH + GAP))

    # ---- карточки
    def _txt(self, x, y, s, size=9, bold=False, fill=T.TEXT, anchor="w"):
        self.cv.create_text(x, y, text=s, anchor=anchor, fill=fill,
                            font=("Segoe UI", size, "bold" if bold else "normal"))

    def _key(self, cx, cy, s, lit):
        _rrect(self.cv, cx - 10, cy - 10, cx + 10, cy + 10, 5, fill=T.ACCENT if lit else "#23232b",
               outline=T.ACCENT_HOVER if lit else T.BORDER)
        self._txt(cx, cy, s, 9, True, "#fff" if lit else T.MUTED, "center")

    def _card(self, key, d, y0):
        cv, t = self.cv, time.time() - self.t0
        y1 = y0 + CH
        _rrect(cv, 0, y0, CW, y1, 12, fill=CARD, outline=T.BORDER)
        # пульсирующая точка и заголовок
        ph = t % 1.6 / 1.6
        cv.create_oval(14 - 4 - 6 * ph, y0 + 16 - 4 - 6 * ph, 14 + 4 + 6 * ph, y0 + 16 + 4 + 6 * ph,
                       outline=mix(T.OK, CARD, ph))
        cv.create_oval(10, y0 + 12, 18, y0 + 20, fill=T.OK, outline="")
        self._txt(28, y0 + 16, TITLES[key], 9, True)
        sec = self.dash.sess.get(key, [0.0, None])[0]
        self._txt(CW - 12, y0 + 16, fmt_time(sec), 9, False, T.MUTED, "e")
        items = dict(d.get("items", []))
        if key == "fish":
            stage = d.get("status", "")
            p, held = d.get("progress"), d.get("held")
            if stage == "Тяну рыбу" and p is not None:
                _rrect(cv, 12, y0 + 38, 112, y0 + 46, 4, fill="#0a141a", outline="#1c3743")
                if p > 0.02:
                    _rrect(cv, 12, y0 + 38, 12 + 100 * p, y0 + 46, 4, fill=mix("#e6b800", T.OK, p), outline="")
                self._key(132, y0 + 43, "A", held == "A")
                self._key(156, y0 + 43, "D", held == "D")
            else:
                self._txt(12, y0 + 43, stage or "…", 9, False, T.MUTED)
            self._txt(CW - 12, y0 + 43, f"поймано {items.get('Поймано', 0)}", 9, True, T.ACCENT_HOVER, "e")
        elif key == "bot":
            self._txt(12, y0 + 43, (d.get("status") or "")[:9], 9, False, T.MUTED)
            # мини-строчка с бегущей иглой
            x0, x1 = 78, CW - 100
            pts = [c for x in range(int(x0), int(x1), 6) for c in (x, y0 + 43 + 3 * math.sin(x / 9.0))]
            cv.create_line(pts, fill="#3a3040", dash=(4, 4), smooth=True)
            nx = x0 + (t * 50) % (x1 - x0)
            done = [c for x in range(int(x0), int(nx), 6) for c in (x, y0 + 43 + 3 * math.sin(x / 9.0))]
            if len(done) >= 4:
                cv.create_line(done, fill=T.ACCENT, width=2, smooth=True)
            cv.create_line(nx, y0 + 34, nx, y0 + 46, fill="#d8d8e0", width=2)
            self._txt(CW - 12, y0 + 43, f"комплектов {items.get('Комплектов', 0)}", 9, True, T.ACCENT_HOVER, "e")
        else:
            left, span = d.get("left", 0.0), max(1.0, d.get("span", 1.0))
            frac = min(1.0, max(0.0, 1 - left / span))
            cx, cy, r = 22, y0 + 43, 11
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=3)
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-max(2, 359.9 * frac),
                          style="arc", outline=T.WARN if d.get("paused") else T.ACCENT, width=3)
            m, s = divmod(int(left), 60)
            self._txt(42, y0 + 43, f"до нажатия {m}:{s:02d}", 9, False, T.MUTED)
            self._txt(CW - 12, y0 + 43, f"нажатий {items.get('Нажатий', 0)}", 9, True, T.ACCENT_HOVER, "e")
