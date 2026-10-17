# -*- coding: utf-8 -*-
"""У каждого бота свой блок над настройками, нарисованный на одном Canvas:
  рыбалка   - вода с поплавком, ступени цикла, полоска вываживания и клавиши A/D;
  швейный   - строчка, по которой бежит игла;
  анти-АФК  - кольцо обратного отсчёта и ряд последних нажатых клавиш.
Данные приходят от Dashboard (widgets.py): set_data(d, секунды_работы, в_час)."""
import os
import math
import time
import tkinter as tk

import theme as T
from widgets import mix, _rrect, fmt_time, chart_range

CARD = "#17171d"
FONT = "Segoe UI"


class Hero(tk.Canvas):
    H = 140

    def __init__(self, parent):
        super().__init__(parent, height=self.H, bg=T.BG, highlightthickness=0, bd=0)
        self.d = {"active": False, "status": "", "items": []}
        self.sec, self.rate = 0.0, 0
        self.t0 = time.time()
        self.vals = {}                  # подпись -> {t: цель, s: показано, f: время вспышки}
        self.intro = 0.0                # когда начался «въезд» блока (replay)
        self._loop()

    def replay(self):
        """Блок заново «въезжает» справа (зовётся при открытии страницы)."""
        self.intro = time.time()

    # ---- данные
    def set_data(self, d, sec, rate):
        self.d, self.sec, self.rate = d, sec, rate
        for label, val in d.get("items", []):
            v = self.vals.setdefault(label, {"t": val, "s": float(val), "f": 0.0})
            if val != v["t"]:
                v["t"], v["f"] = val, time.time()

    def shown(self, label):
        v = self.vals.get(label)
        return int(round(v["s"])) if v else 0

    def flash(self, label):
        v = self.vals.get(label)
        return 0.0 if not v else max(0.0, 1.0 - (time.time() - v["f"]) / 0.9)

    def t(self):
        return time.time() - self.t0

    # ---- цикл
    def _loop(self):
        for v in self.vals.values():
            d = v["t"] - v["s"]
            v["s"] = float(v["t"]) if abs(d) < 0.02 else v["s"] + d * 0.22
        try:
            if self.winfo_ismapped():
                self.draw()
                k = (time.time() - self.intro) / 0.5
                if k < 1.0:
                    e = 1 - (1 - max(0.0, k)) ** 3
                    self.move("all", (1 - e) * 90, 0)        # въезд справа, за границу Canvas обрезается
            self.after(12 if self.d.get("active") else 80, self._loop)
        except tk.TclError:
            pass

    def draw(self):
        self.delete("all")

    # ---- примитивы
    def text(self, x, y, s, size=10, bold=False, fill=T.TEXT, anchor="w", **kw):
        return self.create_text(x, y, text=s, anchor=anchor, fill=fill,
                                font=(FONT, size, "bold" if bold else "normal"), **kw)

    def label(self, x, y, s, fill=T.MUTED):
        self.text(x, y, s.upper(), 8, True, fill)

    def card(self, x0, x1, fill=CARD, outline=T.BORDER):
        _rrect(self, x0, 2, x1, self.H - 2, 14, fill=fill, outline=outline)

    def keycap(self, cx, cy, s, lit=False, size=26):
        h = size / 2
        _rrect(self, cx - h, cy - h, cx + h, cy + h, 6, fill=T.ACCENT if lit else "#23232b",
               outline=T.ACCENT_HOVER if lit else T.BORDER)
        self.text(cx, cy, s, 11, True, "#ffffff" if lit else T.MUTED, "center")

    def dot_status(self, x, y, active, status):
        col = T.OK if active else T.MUTED
        if active:
            ph = self.t() % 1.6 / 1.6
            r = 5 + 8 * ph
            self.create_oval(x - r, y - r, x + r, y + r, outline=mix(col, CARD, ph), width=2)
        self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=col, outline=col)
        self.text(x + 16, y, status, 10, True, T.TEXT if active else T.MUTED)

    def counter_card(self, x0, x1, big_label, extra, life=None):
        """Правая карточка: крупный счётчик и мелкие значения. extra - [(подпись, текст)].
        life - накопленное «за всё время» (рисуется справа от главного числа)."""
        self.card(x0, x1)
        if life is not None:
            self.text(x1 - 18, 66, f"всего {life}", 9, False, T.MUTED, "e")
        f = self.flash(big_label)
        self.create_rectangle(x0 + 1, 18, x0 + 4, self.H - 18, fill=mix(T.ACCENT, T.ACCENT_HOVER, f), outline="")
        self.label(x0 + 18, 24, big_label)
        self.text(x0 + 18, 62, str(self.shown(big_label)), 34, True,
                  mix(T.ACCENT_HOVER, "#ffffff", 1 - (1 - f) ** 2))
        w = (x1 - x0 - 24) / 2
        for i, (lab, val) in enumerate(extra):
            x, y = x0 + 18 + (i % 2) * w, 98 + (i // 2) * 32
            self.label(x, y, lab)
            self.text(x, y + 14, val, 11, True)


# ====================================================================== рыбалка
class FishHero(Hero):
    H = 168
    STEPS = ["Заброс", "Поклёвка", "Вываживание", "Улов"]
    MAP = {"Заброс": 0, "Жду поклёвку": 1, "Тяну рыбу": 2, "Улов": 3}

    def _wave(self, x0, x1, base, amp, k, speed, ph, color, bottom):
        t = self.t()
        pts = []
        for x in range(int(x0), int(x1) + 1, 6):
            pts += [x, base + amp * math.sin(x * k + t * speed + ph)]
        pts += [x1, bottom, x0, bottom]
        self.create_polygon(pts, fill=color, outline="")

    def _surface(self, x, base, amp, k, speed, ph):
        return base + amp * math.sin(x * k + self.t() * speed + ph)

    def _fish(self, cx, cy, s, color):
        self.create_oval(cx - s, cy - s * 0.5, cx + s * 0.8, cy + s * 0.5, fill=color, outline="")
        self.create_polygon(cx - s * 0.7, cy, cx - s * 1.5, cy - s * 0.55, cx - s * 1.5, cy + s * 0.55,
                            fill=color, outline="")
        self.create_oval(cx + s * 0.35, cy - s * 0.2, cx + s * 0.55, cy, fill="#0d1a21", outline="")

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 300:
            return
        d = self.d
        act = d.get("active")
        sw = W * 0.64
        x0, x1 = 0, sw
        stage = self.MAP.get(d.get("status"), -1) if act else -1
        t = self.t()
        self.card(x0, x1, fill="#0c1820", outline="#1c3743")
        # ступени цикла
        step_w = (sw - 60) / (len(self.STEPS) - 1)
        for i, name in enumerate(self.STEPS):
            cx = 30 + i * step_w
            if i:
                self.create_line(cx - step_w + 7, 24, cx - 7, 24, fill=T.OK if i <= stage else "#223742", width=2)
            col = T.ACCENT if i == stage else (T.OK if i < stage else "#223742")
            r = 6 + (1.5 * math.sin(t * 6) if i == stage else 0)
            self.create_oval(cx - r, 24 - r, cx + r, 24 + r, fill=col, outline="")
            self.text(cx, 42, name, 8, i == stage, T.TEXT if i == stage else T.MUTED, "center")
        # вода
        base = H * 0.68
        bottom = H - 9
        self._wave(14, x1 - 14, base, 3, 0.045, 1.6 if act else 0.5, 0.0, "#12303b", bottom)
        self._wave(14, x1 - 14, base + 10, 4, 0.032, -1.2 if act else -0.4, 1.7, "#0f2630", bottom)
        self._wave(14, x1 - 14, base + 20, 3, 0.05, 0.9 if act else 0.3, 3.1, "#0b1d25", bottom)
        # поплавок и леска
        bx = 64
        shake = 2.5 * math.sin(t * 26) if stage == 2 else 0
        dip = max(0, math.sin(t * 1.3) - 0.8) * 22 if stage == 1 else 0
        by = self._surface(bx, base, 3, 0.045, 1.6 if act else 0.5, 0.0) + dip
        self.create_line(bx + shake, by - 8, bx + 150, 52, fill="#3b5a68")
        if stage == 2:
            for i in range(3):
                ph = (t * 1.1 + i / 3) % 1
                rx, ry = 8 + 44 * ph, 2 + 9 * ph
                self.create_oval(bx - rx, by - ry, bx + rx, by + ry, outline=mix("#5fb8d6", "#12303b", ph))
        self.create_arc(bx - 7 + shake, by - 9, bx + 7 + shake, by + 5, start=0, extent=180, fill="#ff4256", outline="")
        self.create_arc(bx - 7 + shake, by - 9, bx + 7 + shake, by + 5, start=180, extent=180, fill="#f2f2f5", outline="")
        # центр сцены: подпись состояния или полоска вываживания
        px0, px1, py = 128, sw - 108, 74
        prog = d.get("progress")
        if stage == 2:
            p = 0.0 if prog is None else prog
            _rrect(self, px0, py - 6, px1, py + 6, 6, fill="#0a141a", outline="#1c3743")
            if p > 0.01:
                _rrect(self, px0, py - 6, px0 + max(12, (px1 - px0) * p), py + 6, 6,
                       fill=mix("#e6b800", T.OK, p), outline="")
            fx = px0 + (px1 - px0) * p
            self._fish(fx + 4, py - 20 + 2 * math.sin(t * 5), 8, "#f2f2f5")
            self.text((px0 + px1) / 2, py + 22, f"Вываживание {int(p * 100)}%", 9, True, T.TEXT, "center")
        else:
            txt = {0: "Заброс — жду зелёную зону", 1: "Жду поклёвку" + "." * (int(t * 2) % 4),
                   3: "Улов!"}.get(stage, "Рыбалка выключена" if not act else d.get("status", ""))
            self.text((px0 + px1) / 2, py, txt, 11, True, T.TEXT if act else T.MUTED, "center")
        # клавиши A/D
        held = d.get("held") if stage == 2 else None
        self.keycap(sw - 66, 78, "A", held == "A")
        self.keycap(sw - 34, 78, "D", held == "D")
        # правая карточка
        rate = f"{self.rate}"
        extra = [("Забросов", str(self.shown("Забросов"))), ("Поклёвок", str(self.shown("Поклёвок"))),
                 ("В час", rate), ("Время", fmt_time(self.sec))]
        self.counter_card(sw + 10, W, "Поймано", extra, (d.get("life") or {}).get("count"))


# ====================================================================== швейный бот
class SewHero(Hero):
    H = 140

    def __init__(self, parent):
        self.nx = 0.0
        self.last = time.time()
        super().__init__(parent)

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 300:
            return
        d, act = self.d, self.d.get("active")
        sw = W * 0.64
        now = time.time()
        dt, self.last = min(0.2, now - self.last), now
        span = sw - 70
        if act:
            self.nx = (self.nx + 95 * dt) % span
        self.card(0, sw, fill="#16121a", outline="#2d2230")
        self.dot_status(26, 24, act, d.get("status") or "Пауза")
        cy = 84

        def y_at(x):
            return cy + 10 * math.sin(x / 36.0)
        xs = list(range(36, int(sw) - 34, 8))
        base = [c for x in xs for c in (x, y_at(x))]
        self.create_line(base, fill="#3a3040", width=2, dash=(7, 7), smooth=True)
        needle = 36 + self.nx
        done = [c for x in xs if x <= needle for c in (x, y_at(x))]
        if len(done) >= 4:
            self.create_line(done, fill=T.ACCENT, width=3, dash=(11, 5), smooth=True)
        ny = y_at(needle)
        bob = 5 * math.sin(now * 14) if act else 0
        self.create_line(needle, ny - 40 + bob, needle, ny - 4 + bob, fill="#d8d8e0", width=2)
        self.create_polygon(needle - 3, ny - 4 + bob, needle + 3, ny - 4 + bob, needle, ny + 6 + bob,
                            fill="#d8d8e0", outline="")
        self.create_oval(needle - 2, ny - 34 + bob, needle + 2, ny - 30 + bob, fill="#16121a", outline="")
        self.create_line(needle, ny - 32 + bob, needle - 24, ny - 52, needle - 60, ny - 40,
                         fill=T.ACCENT_HOVER, smooth=True)
        extra = [("В час", str(self.rate)), ("Время", fmt_time(self.sec))]
        self.counter_card(sw + 10, W, "Комплектов", extra, (d.get("life") or {}).get("count"))


# ====================================================================== анти-АФК
class AfkHero(Hero):
    H = 140

    def __init__(self, parent):
        self.seen = 0
        self.pulse = 0.0
        super().__init__(parent)

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 300:
            return
        d, act = self.d, self.d.get("active")
        paused = d.get("paused")
        # кольцо обратного отсчёта
        rw = 190
        self.card(0, rw)
        cx, cy, r = rw / 2, H / 2, 46
        left, span = d.get("left", 0.0), max(1.0, d.get("span", 1.0))
        frac = 0.0 if not act else min(1.0, max(0.0, 1 - left / span))
        n = self.shown("Нажатий")
        if n != self.seen:
            self.seen, self.pulse = n, time.time()
        pf = max(0.0, 1 - (time.time() - self.pulse) / 0.8)
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=8)
        if act:
            col = mix(T.WARN if paused else T.ACCENT, "#ffffff", pf)
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-max(2, 359.9 * frac),
                            style="arc", outline=col, width=8)
            if pf:
                rr = r + 6 + 14 * (1 - pf)
                self.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=mix(T.ACCENT, CARD, 1 - pf))
        m, s = divmod(int(left), 60)
        self.text(cx, cy - 4, (f"{m}:{s:02d}" if act else "—"), 17, True, T.TEXT if act else T.MUTED, "center")
        self.text(cx, cy + 16, "пауза" if paused and act else "до нажатия", 8, False, T.MUTED, "center")
        # последние клавиши
        cw = W * 0.30
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Последние нажатия")
        rec = d.get("recent", [])
        for i, k in enumerate(rec[-7:]):
            lit = i == len(rec[-7:]) - 1 and pf > 0
            label = k if len(str(k)) <= 2 else str(k)[:3]
            self.keycap(x0 + 34 + i * 38, 66, label, lit)
        if not rec:
            self.text(x0 + 18, 66, "пока ничего", 10, False, T.MUTED)
        self.dot_status(x0 + 22, 112, act, d.get("status") or "Выключен")
        # счётчик
        self.counter_card(x1 + 10, W, "Нажатий", [("Время", fmt_time(self.sec))],
                          (d.get("life") or {}).get("count"))


# ====================================================================== чистильщик ОЗУ
class RamHero(Hero):
    H = 156

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        d, t = self.d, self.t()
        pct = d.get("pct", 0.0)
        hist = d.get("hist") or []
        hot = pct >= 90
        col = T.WARN if hot else (T.ACCENT if pct >= 75 else T.OK)
        busy = d.get("busy")
        # --- кольцо загрузки
        rw = 190
        self.card(0, rw)
        cx, cy, r = rw / 2, H / 2 - 2, 48
        shown = self.vals.setdefault("_pct", {"t": pct, "s": pct, "f": 0.0})
        shown["t"] = pct
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=9)
        self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-max(2, 3.599 * shown["s"]),
                        style="arc", outline=col, width=9)
        if busy:
            a = (t * 220) % 360
            self.create_arc(cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8, start=a, extent=70, style="arc",
                            outline=T.ACCENT_HOVER, width=3)
        self.text(cx, cy - 6, f"{int(round(shown['s']))}%", 22, True, T.TEXT, "center")
        self.text(cx, cy + 18, "ОЗУ", 8, True, T.MUTED, "center")
        # --- график
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Загрузка памяти")
        self.text(x1 - 18, 24, f"{d.get('used', 0):.1f} из {d.get('total', 0):.1f} ГБ", 9, True, T.TEXT, "e")
        gx0, gx1, gy0, gy1 = x0 + 18, x1 - 18, 44, H - 22
        for frac in (0.25, 0.5, 0.75):
            y = gy1 - (gy1 - gy0) * frac
            self.create_line(gx0, y, gx1, y, fill="#1f1f27", dash=(3, 5))
        if len(hist) >= 2:
            n = len(hist)
            lo, hi = chart_range(hist)
            pts = []
            for i, v in enumerate(hist):
                pts += [gx0 + (gx1 - gx0) * i / max(1, n - 1), gy1 - (gy1 - gy0) * (v - lo) / (hi - lo)]
            self.text(gx0 + 4, gy0 + 6, f"{hi:.0f}%", 7, False, T.MUTED)
            self.text(gx0 + 4, gy1 - 8, f"{lo:.0f}%", 7, False, T.MUTED)
            if max(hist) - min(hist) >= 1.0:                     # почти ровная линия - без заливки
                self.create_polygon(pts + [gx1, gy1, gx0, gy1], fill=mix(CARD, col, 0.2), outline="")
            self.create_line(pts, fill=col, width=2, smooth=True)
            ex, ey = pts[-2], pts[-1]
            ph = t % 1.4 / 1.4
            self.create_oval(ex - 3 - 8 * ph, ey - 3 - 8 * ph, ex + 3 + 8 * ph, ey + 3 + 8 * ph,
                             outline=mix(col, CARD, ph))
            self.create_oval(ex - 3, ey - 3, ex + 3, ey + 3, fill="#ffffff", outline="")
            self.text(gx1, gy1 + 12, "сейчас", 7, False, T.MUTED, "e")
            self.text(gx0, gy1 + 12, f"{len(hist) * 2 // 60} мин назад" if len(hist) >= 30 else "", 7, False, T.MUTED)
        else:
            self.text((gx0 + gx1) / 2, (gy0 + gy1) / 2, "Собираю данные…", 10, False, T.MUTED, "center")
        # --- освобождено
        self.card(x1 + 10, W)
        f = self.flash("Освобождено")
        self.create_rectangle(x1 + 11, 18, x1 + 14, H - 18, fill=mix(T.ACCENT, T.ACCENT_HOVER, f), outline="")
        self.label(x1 + 28, 24, "Освобождено")
        mb = self.shown("Освобождено")
        big = f"{mb / 1024:.1f} ГБ" if mb >= 1024 else f"{mb} МБ"
        self.text(x1 + 28, 62, big, 26, True, mix(T.ACCENT_HOVER, "#ffffff", 1 - (1 - f) ** 2))
        lt = d.get("last_t") or 0
        self.text(x1 + 28, 90, "последней очисткой" if lt else "очисток ещё не было", 8, False, T.MUTED)
        self.label(x1 + 28, 112, "Выделено")
        self.text(x1 + 28, 128, f"{d.get('cpct', 0):.0f}%", 11, True, T.WARN if d.get("cpct", 0) > 90 else T.TEXT)
        self.label(x1 + 28 + (W - x1 - 40) / 2, 112, "Пик")
        self.text(x1 + 28 + (W - x1 - 40) / 2, 128, f"{max(hist) if hist else 0:.0f}%", 11, True)

    def _loop(self):
        v = self.vals.get("_pct")
        if v:
            v["s"] += (v["t"] - v["s"]) * 0.12
        super()._loop()


# ====================================================================== готовка
class CookHero(Hero):
    H = 150

    def _steam(self, cx, base, col, n=3):
        """Кастрюля и поднимающийся пар (клубы растут и тают)."""
        t = self.t()
        _rrect(self, cx - 26, base - 2, cx + 26, base + 26, 7, fill="#1f1f27", outline=EDGE_C)
        self.create_line(cx - 30, base - 2, cx + 30, base - 2, fill=EDGE_C, width=3)
        self.create_oval(cx - 4, base - 9, cx + 4, base - 3, fill="#2a2a34", outline="")
        self.create_line(cx - 26, base + 8, cx - 33, base + 8, fill=EDGE_C, width=3)
        self.create_line(cx + 26, base + 8, cx + 33, base + 8, fill=EDGE_C, width=3)
        for i in range(n):
            ph = (t * 0.5 + i / n) % 1
            x = cx + (i - 1) * 14 + 5 * math.sin(t * 2 + i * 2.1)
            y = base - 12 - 44 * ph
            r = 3 + 6 * ph
            self.create_oval(x - r, y - r, x + r, y + r, outline=mix(col, CARD, ph ** 0.6), width=2)

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        d, t = self.d, self.t()
        now = time.time()
        timers = d.get("timers") or []
        act = sorted([x for x in timers if not x.get("done")], key=lambda x: x["end"])
        done = [x for x in timers if x.get("done")]
        # --- кольцо ближайшего таймера
        rw = 190
        self.card(0, rw)
        cx, cy, r = rw / 2, H / 2 - 2, 46
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=9)
        if act:
            x = act[0]
            total = max(1.0, x["end"] - x["start"])
            left = max(0.0, x["end"] - now)
            frac = 1 - left / total
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-max(2, 359.9 * frac), style="arc",
                            outline=T.ACCENT, width=9)
            m, s = divmod(int(left), 60)
            h, m = divmod(m, 60)
            self.text(cx, cy - 6, f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}", 16, True, T.TEXT, "center")
            self.text(cx, cy + 16, "до готовности", 8, False, T.MUTED, "center")
        else:
            self._steam(cx, cy + 14, T.MUTED)
            self.text(cx, H - 16, "плита свободна", 8, False, T.MUTED, "center")
        # --- список таймеров
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Таймеры")
        rows = (act + done)[:3]
        if not rows:
            self.text((x0 + x1) / 2, H / 2 + 6, "Таймеров нет: поставь на карточке блюда", 10, False, T.MUTED, "center")
        for i, x in enumerate(rows):
            y = 52 + i * 32
            ok = x.get("done")
            total = max(1.0, x["end"] - x["start"])
            frac = 1.0 if ok else min(1.0, max(0.0, 1 - (x["end"] - now) / total))
            left = max(0.0, x["end"] - now)
            name = x["name"] if len(x["name"]) <= 26 else x["name"][:25] + "…"
            self.text(x0 + 18, y, name, 9, True, T.OK if ok else T.TEXT)
            self.text(x1 - 18, y, "готово" if ok else fmt_time(left), 9, True, T.OK if ok else T.MUTED, "e")
            tr0, tr1 = x0 + 18, x1 - 18
            _rrect(self, tr0, y + 12, tr1, y + 18, 3, fill=TRACK_C, outline="")
            if frac > 0.01:
                _rrect(self, tr0, y + 12, tr0 + max(6, (tr1 - tr0) * frac), y + 18, 3,
                       fill=T.OK if ok else T.ACCENT, outline="")
        # --- счётчик
        self.card(x1 + 10, W)
        f = self.flash("Таймеров")
        self.create_rectangle(x1 + 11, 18, x1 + 14, H - 18, fill=mix(T.ACCENT, T.ACCENT_HOVER, f), outline="")
        self.label(x1 + 28, 24, "Таймеров идёт")
        self.text(x1 + 28, 62, str(self.shown("Таймеров")), 34, True, mix(T.ACCENT_HOVER, "#ffffff", 1 - (1 - f) ** 2))
        self.label(x1 + 28, 100, "Готово")
        self.text(x1 + 28, 116, str(len(done)), 11, True, T.OK if done else T.TEXT)
        nxt = act[0]["end"] - now if act else None
        self.label(x1 + 28 + (W - x1 - 40) / 2, 100, "Ближайший")
        self.text(x1 + 28 + (W - x1 - 40) / 2, 116, fmt_time(nxt) if nxt is not None else "—", 11, True)


TRACK_C = "#0b0b10"
EDGE_C = "#2d2d38"


# ====================================================================== админка
class AdminHero(Hero):
    H = 150

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        d = self.d
        by = d.get("by") or {}
        total = max(0, d.get("n_total", 0))
        # --- «бублик» по статусам ключей
        rw = 190
        self.card(0, rw)
        cx, cy, r = rw / 2, H / 2 - 2, 46
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=11)
        start = 90.0
        for name, color in (("active", T.OK), ("expired", T.WARN), ("revoked", "#6b6b78")):
            n = by.get(name, 0)
            if total and n:
                ext = 359.0 * n / total
                self.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=-max(2, ext - 3), style="arc",
                                outline=color, width=11)
                start -= ext
        self.text(cx, cy - 6, str(total), 22, True, T.TEXT, "center")
        self.text(cx, cy + 16, "ключей", 8, True, T.MUTED, "center")
        # --- устройства и скоро истекающие
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Устройства")
        used, cap = d.get("dev_used", 0), max(1, d.get("dev_cap", 0))
        tr0, tr1 = x0 + 18, x1 - 18
        _rrect(self, tr0, 40, tr1, 48, 4, fill=TRACK_C, outline="")
        if used:
            _rrect(self, tr0, 40, tr0 + max(8, (tr1 - tr0) * used / cap), 48, 4, fill=T.ACCENT, outline="")
        self.text(x1 - 18, 24, f"занято {used} из {d.get('dev_cap', 0)}", 9, True, T.TEXT, "e")
        self.label(x0 + 18, 76, "Скоро истекают (7 дней)")
        soon = d.get("soon") or []
        if not soon:
            self.text(x0 + 18, 100, "таких ключей нет", 10, False, T.MUTED)
        for i, (nm, days) in enumerate(soon[:3]):
            y = 98 + i * 18
            col = T.WARN if days <= 2 else "#ffb020"
            self.create_oval(x0 + 20, y - 3, x0 + 26, y + 3, fill=col, outline="")
            self.text(x0 + 36, y, nm if len(nm) <= 22 else nm[:21] + "…", 9, True)
            self.text(x1 - 18, y, "сегодня" if days == 0 else f"{days} дн.", 9, True, col, "e")
        # --- активные
        self.counter_card(x1 + 10, W, "Активных",
                          [("Отозвано", str(by.get("revoked", 0))), ("Истекло", str(by.get("expired", 0)))])


# ====================================================================== рынок
class MarketHero(Hero):
    H = 150

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        s = (self.d.get("market") or {})
        t = self.t()
        # --- кольцо мониторинга: сколько позиций и сколько сработало сегодня
        rw = 190
        self.card(0, rw)
        cx, cy, r = rw / 2, H / 2 - 2, 46
        n, hits = s.get("watch_n", 0), s.get("alerts_today", 0)
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=9)
        if s.get("mon_busy"):                                   # идёт проверка - бегущая дуга
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90 - (t * 240) % 360, extent=-90, style="arc",
                            outline=T.ACCENT, width=9)
        elif n and hits:
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-359.9 * min(1.0, hits / float(n)),
                            style="arc", outline=T.OK, width=9)
        self.text(cx, cy - 6, str(n), 20, True, T.TEXT, "center")
        self.text(cx, cy + 16, "в мониторинге", 8, False, T.MUTED, "center")
        nc = s.get("next_check")
        self.text(cx, H - 14, (f"сработало сегодня: {hits}" if hits else
                               (time.strftime("проверка ≈%H:%M", time.localtime(nc)) if nc else "")), 8, False,
                  T.OK if hits else T.MUTED, "center")
        # --- топ по продажам
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        head = "Топ по продажам"
        if s.get("server"):
            head += f" · {s['server']} · {s.get('cat', '')}"
        self.label(x0 + 18, 24, head)
        top = s.get("top") or []
        if s.get("error"):
            self.text((x0 + x1) / 2, H / 2 + 6, s["error"][:70], 10, False, T.WARN, "center", width=x1 - x0 - 40)
        elif s.get("busy"):
            self.text((x0 + x1) / 2, H / 2 + 6, str(s["busy"]) + "." * (int(t * 2) % 4), 11, True, T.TEXT, "center")
        elif not top:
            self.text((x0 + x1) / 2, H / 2 + 6, "Нажми «Обновить»", 10, False, T.MUTED, "center")
        mx = max([v for _, v in top] + [1])
        for i, (nm, v) in enumerate(top):
            y = 52 + i * 30
            self.text(x0 + 18, y, nm if len(nm) <= 28 else nm[:27] + "…", 9, True)
            self.text(x1 - 18, y, f"{v:,}".replace(",", " "), 9, True, T.MUTED, "e")
            _rrect(self, x0 + 18, y + 11, x1 - 18, y + 16, 2, fill="#0b0b10", outline="")
            _rrect(self, x0 + 18, y + 11, x0 + 18 + max(6, (x1 - x0 - 36) * v / mx), y + 16, 2, fill=T.ACCENT, outline="")
        # --- счётчик
        self.counter_card(x1 + 10, W, "Позиций", [("Обновлено", (s.get("updated") or "—") + (" UTC" if s.get("updated") else "")),
                                                  ("Период", f"{s.get('period')} дн." if s.get("period") else "—")])


# ====================================================================== PM по скринам
class PmHero(Hero):
    H = 150

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        s = (self.d.get("pm") or {})
        t = self.t()
        # --- кольцо: какая доля тех, кто в сети, катает вызовы
        rw = 190
        self.card(0, rw)
        cx, cy, r = rw / 2, H / 2 - 2, 46
        on, calls = s.get("online", 0), s.get("calls", 0)
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262e", width=9)
        if s.get("busy"):
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90 - (t * 240) % 360, extent=-90, style="arc",
                            outline=T.ACCENT, width=9)
        elif on and calls:
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-359.9 * min(1.0, calls / float(on)),
                            style="arc", outline=T.OK, width=9)
        self.text(cx, cy - 6, f"{calls}/{on}", 20, True, T.TEXT, "center")
        self.text(cx, cy + 16, "катают / в сети", 8, False, T.MUTED, "center")
        # --- середина: что делать
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Проверка состава")
        if s.get("busy"):
            self.text((x0 + x1) / 2, H / 2 + 6, str(s["busy"]) + "." * (int(t * 2) % 4), 11, True, T.TEXT, "center")
        elif not s.get("ok", True):
            self.text((x0 + x1) / 2, H / 2 + 6, "Распознавание Windows недоступно - см. подсказку ниже", 10, False,
                      T.WARN, "center")
        elif not s.get("n"):
            self.text((x0 + x1) / 2, H / 2 - 4, "Добавь скрины «Коллеги» и «Вызовы» (История)", 11, True, T.TEXT, "center")
            self.text((x0 + x1) / 2, H / 2 + 20, "Win+Shift+S → Ctrl+V, кнопка или папка со скринами", 9, False,
                      T.MUTED, "center")
        else:
            rows = [("Не катают - текст /f готов", s.get("ask", 0), "#ffb347"),
                    ("Катают вызовы", s.get("calls", 0), T.OK),
                    ("Изменений с прошлого снимка", s.get("changes", 0), T.ACCENT)]
            for i, (lab, v, col) in enumerate(rows):
                y = 56 + i * 28
                self.create_oval(x0 + 18, y - 4, x0 + 26, y + 4, fill=col, outline="")
                self.text(x0 + 36, y, lab, 10, True)
                self.text(x1 - 18, y, str(v), 11, True, col, "e")
        self.counter_card(x1 + 10, W, "Сотрудников", [("Скринов", str(s.get("shots", 0))),
                                                      ("В сети", str(s.get("online", 0)))])


# ====================================================================== запись для разбора
class RecHero(Hero):
    H = 150

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        s = (self.d.get("rec") or {})
        t = self.t()
        rec = bool(s.get("rec"))
        # --- индикатор REC и таймер
        rw = 190
        self.card(0, rw)
        cx, cy = rw / 2, 52
        if rec:
            ph = t % 1.2 / 1.2
            self.create_oval(cx - 22 - 12 * ph, cy - 22 - 12 * ph, cx + 22 + 12 * ph, cy + 22 + 12 * ph,
                             outline=mix("#ff3b4e", CARD, ph), width=2)
            self.create_oval(cx - 22, cy - 22, cx + 22, cy + 22, fill="#ff3b4e", outline="")
            self.text(cx, cy, "REC", 11, True, "#ffffff", "center")
        else:
            self.create_oval(cx - 22, cy - 22, cx + 22, cy + 22, fill="#23232b", outline=EDGE_R, width=2)
            self.text(cx, cy, "ГОТОВ", 8, True, T.MUTED, "center")
        sec = int(s.get("t", 0))
        self.text(cx, 108, f"{sec // 60:02d}:{sec % 60:02d}", 22, True, T.TEXT if rec else T.MUTED, "center")
        self.text(cx, 134, "длительность", 8, False, T.MUTED, "center")
        # --- параметры записи
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Запись")
        half = (x1 - x0 - 36) / 2
        cells = [("Кадров/с", f"{s.get('fps', 0):.1f}"), ("Размер", f"{s.get('mb', 0):.0f} МБ"),
                 ("События ввода", f"{s.get('events', 0)}"), ("Метки", f"{s.get('marks', 0)}")]
        for i, (lab, val) in enumerate(cells):
            x, y = x0 + 18 + (i % 2) * half, 50 + (i // 2) * 40
            self.label(x, y, lab)
            self.text(x, y + 16, val, 12, True)
        drop = s.get("dropped", 0)
        self.text(x0 + 18, H - 16, ("Потеряно кадров: %d" % drop) if drop else (
            os.path.basename(s.get("folder", "")) or "Нажми горячую клавишу или кнопку «Начать запись»"),
                  8, False, T.WARN if drop else T.MUTED)
        # --- счётчик
        self.counter_card(x1 + 10, W, "Кадров", [("Время", fmt_time(sec)), ("Размер", f"{s.get('mb', 0):.0f} МБ")])


EDGE_R = "#2d2d38"


# ====================================================================== шлифовка
class SandHero(Hero):
    H = 150
    BX0, BY0, BX1, BY1 = 573, 330, 1348, 824          # доска в координатах экрана 1080p

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        if W < 420:
            return
        d, t = self.d, self.t()
        view = d.get("view")
        act = bool(d.get("active"))
        # --- карта доски: то, что видит бот
        rw = 214
        self.card(0, rw, fill="#16130f", outline="#3a2d1e")
        bw = rw - 28
        bh = bw * (self.BY1 - self.BY0) / (self.BX1 - self.BX0)
        ox, oy = 14, (H - bh) / 2
        k = bw / (self.BX1 - self.BX0)
        _rrect(self, ox, oy, ox + bw, oy + bh, 6, fill="#c98f4f" if view else "#3a2d1e", outline="#5b4325")
        for i in range(1, 8):                          # швы между планками
            x = ox + bw * i / 8
            self.create_line(x, oy + 3, x, oy + bh - 3, fill="#a8763d" if view else "#2c2216")
        if view:
            for (sx, sy, area) in view.get("stains", []):
                r = max(3.0, math.sqrt(area / math.pi) * k)
                px, py = ox + (sx - self.BX0) * k, oy + (sy - self.BY0) * k
                self.create_oval(px - r, py - r, px + r, py + r, fill="#2b1a10", outline="")
            tg = view.get("t")
            if tg:
                px, py = ox + (tg[0] - self.BX0) * k, oy + (tg[1] - self.BY0) * k
                rr = 6 + 3 * math.sin(t * 8)
                self.create_oval(px - rr, py - rr, px + rr, py + rr, outline="#ff4d6d", width=2)
            g = view.get("g")
            if g:
                px, py = ox + (g[0] - self.BX0) * k, oy + (g[1] - self.BY0) * k
                self.create_oval(px - 45 * k, py - 45 * k, px + 45 * k, py + 45 * k, fill="#46d36b", outline="#ffffff", width=1)
        else:
            self.text(ox + bw / 2, oy + bh / 2, "жду\nмини-игру" if act else "выключено", 9, True, T.MUTED, "center")
        # --- состояние
        cw = W * 0.27
        x0, x1 = rw + 10, W - cw - 10
        self.card(x0, x1)
        self.label(x0 + 18, 24, "Шлифовка")
        st = str(d.get("status") or "")
        self.text(x0 + 18, 50, st, 12, True, T.TEXT if act else T.MUTED)
        n_left = int(d.get("left") or 0)
        cells = [("Осталось пятен", str(n_left) if view else "—"), ("Скорость", f"{d.get('vmax', 0)} px/с"),
                 ("В час", f"{self.rate}"), ("Время", fmt_time(self.sec))]
        half = (x1 - x0 - 36) / 2
        for i, (lab, val) in enumerate(cells):
            x, y = x0 + 18 + (i % 2) * half, 80 + (i // 2) * 34
            self.label(x, y, lab)
            self.text(x, y + 14, val, 11, True)
        self.counter_card(x1 + 10, W, "Досок", [("Пятен", str(self.shown("Пятен"))), ("В час", f"{self.rate}")],
                          (d.get("life") or {}).get("count"))


HEROES = {"fish": FishHero, "bot": SewHero, "afk": AfkHero, "ram": RamHero, "cook": CookHero,
          "admin": AdminHero, "market": MarketHero, "pm": PmHero, "rec": RecHero, "sand": SandHero}
