# -*- coding: utf-8 -*-
"""Компактный HUD поверх игры: по карточке на каждого работающего бота.
Окно маленькое, прозрачное для кликов, не берёт фокус и не лезёт в области экрана, которые
читает бот. Как и оверлей рыбалки, исключено из снимков экрана (WDA_EXCLUDEFROMCAPTURE):
иначе бот видел бы собственный HUD и ошибался. Показывается только пока игра на переднем плане.
У каждого бота свой цвет и своя анимация; карточки плавно выезжают и уходят."""
import math
import sys
import time
import tkinter as tk
import tkinter.font as tkfont

import widgets
from widgets import mix, _rrect, fmt_time, ease
import theme as T

KEY = "#010203"
CARD = "#13131a"
EDGE = "#262630"
TRACK = "#0b0b10"
ORDER = ["fish", "bot", "wood", "mine", "gym", "afk", "sand"]
TITLES = {"fish": "Рыбалка", "bot": "Швейный бот", "afk": "Анти-АФК", "sand": "Шлифовка", "wood": "Лесоруб", "mine": "Шахта", "gym": "Качалка"}
GLYPH = {"fish": "⚓", "bot": "✂", "afk": "♻", "sand": "⚒", "wood": "♣", "mine": "⛏", "gym": "✊"}
COLORS = {"fish": "#38c6e0", "bot": "#ff4d6d", "afk": "#9a86ff", "sand": "#ffb347", "wood": "#7ccf5a", "mine": "#5aa9ff", "gym": "#ff8c42"}
WARN = "#ffb020"
GAME_TITLES = ("gta", "rage", "majestic", "grand theft")
CW, CH, GAP = 284, 92, 10
STEPS = {"Заброс": 0, "Жду поклёвку": 1, "Тяну рыбу": 2, "Улов": 3}


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
        self.cards = {}                     # key -> состояние анимации карточки
        self._f8 = None
        self._loop()

    def set_enabled(self, on):
        self.enabled = on
        if not on:
            self.cards.clear()
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
        self._f8 = tkfont.Font(family="Segoe UI", size=8, weight="bold")
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
    def _active(self):
        out = {}
        for key in ORDER:
            fn = widgets.PROVIDERS.get(key)
            if not fn:
                continue
            try:
                d = fn()
            except Exception:
                continue
            if d.get("active"):
                out[key] = d
        return out

    def _loop(self):
        delay = 200
        try:
            act = self._active() if self.enabled else {}
            now = time.time()
            for key, d in act.items():
                c = self.cards.get(key)
                if c is None:
                    c = self.cards[key] = {"t_in": now, "t_out": None, "prog": 0.0, "num": 0.0,
                                           "pulse": 0.0, "last": 0}
                elif c["t_out"] is not None:
                    c["t_in"], c["t_out"] = now - 0.1, None
                c["d"] = d
            for key, c in list(self.cards.items()):
                if key not in act:
                    if c["t_out"] is None:
                        c["t_out"] = now
                    elif now - c["t_out"] > 0.35:
                        del self.cards[key]
            if self.cards and self._game_focused():
                delay = 33
                self._show()
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

    def _show(self):
        self._ensure()
        keys = [k for k in ORDER if k in self.cards]
        n = len(keys)
        h = n * CH + (n - 1) * GAP
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = sw - CW - 20 if self.side == "right" else 20
        y = int(sh * 0.58) if self.side == "right" else int(sh * 0.56)
        self.win.geometry(f"{CW}x{h}+{x}+{y}")
        self.cv.configure(height=h)
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        self.cv.delete("all")
        for i, key in enumerate(keys):
            self._card(key, self.cards[key], i * (CH + GAP))

    # ---- примитивы
    def _txt(self, x, y, s, size=9, bold=False, fill=T.TEXT, anchor="w"):
        self.cv.create_text(x, y, text=s, anchor=anchor, fill=fill,
                            font=("Segoe UI", size, "bold" if bold else "normal"))

    def _key(self, cx, cy, s, lit, col):
        _rrect(self.cv, cx - 11, cy - 11, cx + 11, cy + 11, 6, fill=col if lit else "#1f1f27",
               outline=mix(col, "#ffffff", 0.3) if lit else EDGE)
        self._txt(cx, cy, s, 9, True, "#ffffff" if lit else T.MUTED, "center")

    def _pill(self, x, y, text, col):
        text = text if len(text) <= 20 else text[:19] + "…"
        w = self._f8.measure(text) + 24
        _rrect(self.cv, x, y - 10, x + w, y + 10, 10, fill=mix(col, CARD, 0.82), outline=mix(col, CARD, 0.55))
        self.cv.create_oval(x + 7, y - 3, x + 13, y + 3, fill=col, outline="")
        self._txt(x + 17, y, text, 8, True, mix(col, "#ffffff", 0.35))
        return x + w

    def _bar(self, x0, x1, y, frac, col, h=7, glow=True):
        _rrect(self.cv, x0, y - h / 2, x1, y + h / 2, h / 2, fill=TRACK, outline=EDGE)
        if frac > 0.01:
            xe = x0 + max(h, (x1 - x0) * frac)
            if glow:
                _rrect(self.cv, x0 - 1, y - h / 2 - 2, xe + 1, y + h / 2 + 2, h / 2 + 2,
                       fill="", outline=mix(col, CARD, 0.7))
            _rrect(self.cv, x0, y - h / 2, xe, y + h / 2, h / 2, fill=col, outline="")
            return xe
        return x0

    # ---- карточка
    def _card(self, key, c, y0):
        cv, d = self.cv, c["d"]
        now = time.time()
        col = COLORS[key]
        # въезд / уход
        if c["t_out"] is None:
            k = ease(min(1.0, (now - c["t_in"]) / 0.35))
        else:
            k = 1 - ease(min(1.0, (now - c["t_out"]) / 0.3))
        off = (1 - k) * (CW + 20) * (1 if self.side == "right" else -1)
        if abs(off) > CW:
            return
        t = now - self.t0
        sec = self.dash.sess.get(key, [0.0, None])[0]
        items = dict(d.get("items", []))
        main_label = {"fish": "Поймано", "bot": "Комплектов", "afk": "Нажатий", "sand": "Досок", "wood": "Деревьев", "mine": "Камней", "gym": "Подходов"}[key]
        val = items.get(main_label, 0)
        c["num"] += (val - c["num"]) * 0.25
        if int(val) != c["last"]:
            c["last"], c["pulse"] = int(val), now
        pf = max(0.0, 1 - (now - c["pulse"]) / 0.9)
        rate = ""
        if d.get("rate_of") and sec >= 60:
            rate = f" · {int(items.get(d['rate_of'], 0) / (sec / 3600.0))}/ч"
        paused = str(d.get("status", "")).startswith("Пауза") or bool(d.get("paused"))
        ac = WARN if paused else col

        def X(x):
            return x + off
        # свечение и тело
        glow = 0.80 + 0.12 * math.sin(t * 2.2) - 0.25 * pf
        _rrect(cv, X(-1), y0 - 1, X(CW + 1), y0 + CH + 1, 15, fill="", outline=mix(ac, CARD, glow))
        _rrect(cv, X(0), y0, X(CW), y0 + CH, 14, fill=CARD, outline=EDGE)
        _rrect(cv, X(0), y0 + 14, X(4), y0 + CH - 14, 2, fill=ac, outline="")
        # строка 1: значок, название, время
        self._txt(X(18), y0 + 20, GLYPH[key], 11, False, ac)
        self._txt(X(40), y0 + 20, TITLES[key], 10, True)
        self._txt(X(CW - 14), y0 + 20, fmt_time(sec), 9, False, T.MUTED, "e")
        # строка 2: визуал бота
        ry = y0 + 47
        if key == "fish":
            stage = STEPS.get(d.get("status"), -1)
            p = d.get("progress")
            if stage == 2 and p is not None:
                c["prog"] += (p - c["prog"]) * 0.3
                xe = self._bar(X(16), X(188), ry, c["prog"], mix("#e6b800", T.OK, c["prog"]), 9)
                if c["prog"] > 0.02:
                    cv.create_oval(xe - 3, ry - 3, xe + 3, ry + 3, fill="#ffffff", outline="")
                self._txt(X(16), ry + 14, f"{int(c['prog'] * 100)}%", 7, True, T.MUTED)
                held = d.get("held")
                self._key(X(214), ry, "A", held == "A", ac)
                self._key(X(244), ry, "D", held == "D", ac)
            else:
                c["prog"] = 0.0
                for i in range(4):
                    cx = X(24 + i * 78)
                    if i:
                        cv.create_line(cx - 78 + 7, ry, cx - 7, ry, fill=ac if i <= stage else EDGE, width=2)
                    on = i == stage
                    r = 5 + (1.6 * math.sin(t * 6) if on else 0)
                    cv.create_oval(cx - r, ry - r, cx + r, ry + r, fill=ac if i <= stage else EDGE, outline="")
        elif key == "bot":
            x0, x1 = 16, CW - 16
            xs = list(range(x0, x1, 6))
            base = [q for x in xs for q in (X(x), ry + 5 * math.sin(x / 14.0))]
            cv.create_line(base, fill=EDGE, width=2, dash=(5, 5), smooth=True)
            nx = x0 + (t * 70) % (x1 - x0)
            done = [q for x in xs if x <= nx for q in (X(x), ry + 5 * math.sin(x / 14.0))]
            if len(done) >= 4:
                cv.create_line(done, fill=ac, width=3, dash=(9, 4), smooth=True)
            ny = ry + 5 * math.sin(nx / 14.0)
            cv.create_line(X(nx), ny - 14, X(nx), ny + 2, fill="#e6e6ee", width=2)
            cv.create_polygon(X(nx) - 3, ny + 2, X(nx) + 3, ny + 2, X(nx), ny + 9, fill="#e6e6ee", outline="")
        elif key == "sand":
            n_left = int(d.get("left") or 0)
            if n_left > c.get("max", 0) or not d.get("view"):
                c["max"] = n_left if d.get("view") else 0
            frac = 0.0 if not c.get("max") else 1.0 - n_left / c["max"]
            c["prog"] += (frac - c["prog"]) * 0.3
            self._bar(X(16), X(230), ry, c["prog"], mix(ac, "#ffffff", 0.3 * pf), 9)
            self._txt(X(CW - 14), ry, f"{n_left} пятен" if d.get("view") else "жду игру", 11, True, T.TEXT, "e")
        elif key == "mine":
            n_left = int(d.get("left") or 0)
            if n_left > c.get("max", 0) or not d.get("view"):
                c["max"] = n_left if d.get("view") else 0
            frac = 0.0 if not c.get("max") else 1.0 - n_left / c["max"]
            c["prog"] += (frac - c["prog"]) * 0.3
            self._bar(X(16), X(230), ry, c["prog"], mix(ac, "#ffffff", 0.3 * pf), 9)
            txt = {"mini": f"{n_left} руды", "dig": "добыча"}.get(d.get("state"), "жду камень")
            self._txt(X(CW - 14), ry, txt, 11, True, T.TEXT, "e")
        elif key == "gym":
            stage = int(d.get("stage") or 0)                # кружков пройдено в подходе (из 4)
            for i in range(4):
                cx = X(24 + i * 34)
                cv.create_oval(cx - 6, ry - 6, cx + 6, ry + 6, fill=ac if i < stage else EDGE, outline="")
            L = d.get("letter")
            if L:
                self._key(X(180), ry, L, True, ac)
            txt = {"key": "буква", "prompt": "жму E", "set": "подход"}.get(d.get("state"), "жду тренажёр")
            self._txt(X(CW - 14), ry, txt, 11, True, T.TEXT, "e")
        else:
            left, span = d.get("left", 0.0), max(1.0, d.get("span", 1.0))
            frac = min(1.0, max(0.0, 1 - left / span))
            self._bar(X(16), X(206), ry, frac, mix(ac, "#ffffff", 0.5 * pf), 9)
            m, s = divmod(int(left), 60)
            self._txt(X(CW - 14), ry, f"{m}:{s:02d}", 13, True, T.TEXT, "e")
        # строка 3: статус и главная метрика
        by = y0 + CH - 17
        self._pill(X(14), by, str(d.get("status") or "Работает"), ac)
        num = str(int(round(c["num"])))
        unit = {"fish": "поймано", "bot": "компл.", "afk": "нажатий", "sand": "досок", "wood": "деревьев", "mine": "камней", "gym": "подходов"}[key]
        right = X(CW - 14)
        tail = f" {unit}{rate}"
        self._txt(right, by, tail, 8, False, T.MUTED, "e")
        w_tail = self._f8.measure(tail) * 0.95
        self._txt(right - w_tail, by, num, 12, True, mix(ac, "#ffffff", 0.2 + 0.6 * pf), "e")
