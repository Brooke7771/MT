# -*- coding: utf-8 -*-
"""Живые элементы интерфейса: плавные переходы цвета, карточки статистики ботов.
Всё рисуется примитивами на одном Canvas (вложенные виджеты в Windows рвали отрисовку).
Боты регистрируют провайдер статистики: widgets.register("fish", fn), где fn() возвращает
{"active": bool, "status": str, "items": [(подпись, число)], "rate_of": подпись или None}."""
import time
import tkinter as tk

import theme as T

PROVIDERS = {}


def register(key, fn):
    PROVIDERS[key] = fn


# ---------------------------------------------------------------- цвет и анимация
def _rgb(c):
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def mix(a, b, t):
    """Цвет между a (t=0) и b (t=1)."""
    t = max(0.0, min(1.0, t))
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def ease(t):
    return 1 - (1 - t) ** 3


_tokens = {}


def tween(widget, key, fn, ms=160):
    """Запускает fn(t) для t от 0 до 1 за ms миллисекунд. Новый вызов с тем же key отменяет старый."""
    token = object()
    _tokens[key] = token
    start = time.time()

    def step():
        if _tokens.get(key) is not token:
            return
        t = min(1.0, (time.time() - start) * 1000.0 / ms)
        try:
            fn(ease(t))
        except tk.TclError:
            return
        if t < 1.0:
            widget.after(16, step)
    step()


def fmt_time(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _rrect(cv, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


# ---------------------------------------------------------------- карточки статистики
CARD = "#17171d"


class StatsBar(tk.Canvas):
    """Полоса карточек: слева статус с «дышащим» индикатором, дальше счётчики.
    Числа плавно докручиваются, карточка со свежим изменением на миг вспыхивает."""

    def __init__(self, parent):
        super().__init__(parent, height=92, bg=T.BG, highlightthickness=0, bd=0)
        self.status, self.active = "Выключен", False
        self.items = []                       # [{"label", "target", "shown", "flash"}]
        self._t0 = time.time()
        self.bind("<Configure>", lambda e: self._draw())
        self._loop()

    def set_data(self, status, active, items):
        self.status, self.active = status, active
        if [i["label"] for i in self.items] != [lab for lab, _ in items]:
            self.items = [{"label": lab, "target": val, "shown": val if isinstance(val, str) else 0.0,
                           "flash": 0.0} for lab, val in items]
            for it in self.items:                     # при смене страницы числа не крутим с нуля
                if not isinstance(it["target"], str):
                    it["shown"] = float(it["target"])
        else:
            for it, (_, val) in zip(self.items, items):
                if val != it["target"]:
                    if not isinstance(val, str) and not isinstance(it["target"], str):
                        it["flash"] = time.time()
                    it["target"] = val

    def _animating(self):
        for it in self.items:
            if not isinstance(it["target"], str) and abs(it["target"] - it["shown"]) > 0.01:
                return True
            if time.time() - it["flash"] < 0.9:
                return True
        return False

    def _loop(self):
        busy = self.active or self._animating()
        for it in self.items:
            if not isinstance(it["target"], str):
                d = it["target"] - it["shown"]
                it["shown"] = it["target"] if abs(d) < 0.02 else it["shown"] + d * 0.22
        if busy or getattr(self, "_dirty", True):
            self._dirty = False
            try:
                self._draw()
            except tk.TclError:
                return
        try:
            self.after(33 if busy else 250, self._loop)
        except tk.TclError:
            pass

    def _draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50:
            return
        gap, n = 10, len(self.items) + 1
        weights = [1.6] + [1.0] * (n - 1)
        unit = (W - gap * (n - 1)) / sum(weights)
        x = 0.0
        now = time.time()
        for i in range(n):
            w = unit * weights[i]
            x0, x1 = x, x + w
            x += w + gap
            if i == 0:
                _rrect(self, x0, 2, x1, H - 2, 12, fill=CARD, outline=T.BORDER)
                ph = (now - self._t0) % 1.6 / 1.6
                cx, cy = x0 + 26, H / 2
                col = T.OK if self.active else T.MUTED
                if self.active:
                    r = 6 + 9 * ph
                    ring = mix(col, CARD, ph)
                    self.create_oval(cx - r, cy - r, cx + r, cy + r, outline=ring, width=2)
                self.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=col, outline=col)
                self.create_text(x0 + 46, cy - 12, text="СТАТУС", anchor="w", fill=T.MUTED,
                                 font=("Segoe UI", 8, "bold"))
                self.create_text(x0 + 46, cy + 8, text=self.status, anchor="w", fill=T.TEXT if self.active else T.MUTED,
                                 font=("Segoe UI", 12, "bold"), width=max(60, w - 56))
                continue
            it = self.items[i - 1]
            f = min(1.0, (now - it["flash"]) / 0.9)
            _rrect(self, x0, 2, x1, H - 2, 12, fill=CARD, outline=mix(T.ACCENT, T.BORDER, f))
            self.create_text(x0 + 16, 24, text=it["label"].upper(), anchor="w", fill=T.MUTED,
                             font=("Segoe UI", 8, "bold"))
            v = it["target"] if isinstance(it["target"], str) else str(int(round(it["shown"])))
            self.create_text(x0 + 16, 56, text=v, anchor="w", fill=mix(T.ACCENT_HOVER, "#ffffff", 1 - (1 - f) ** 2),
                             font=("Segoe UI", 22, "bold"))


class Dashboard:
    """Считает время работы каждого бота и показывает карточки для текущей страницы."""

    def __init__(self, root, parent, before):
        self.root, self.shown = root, None
        self.parent, self.before = parent, before
        self.views = {}                 # key -> свой блок страницы (у каждого бота он особый)
        self.cur = None
        self.sess = {}                  # key -> [секунды, последний тик]
        self.last = {}
        self._tick()

    def _view(self, key):
        if key not in self.views:
            try:
                import heroes
                cls = heroes.HEROES.get(key)
            except Exception:
                cls = None
            self.views[key] = cls(self.parent) if cls else StatsBar(self.parent)
        return self.views[key]

    def show(self, key):
        self.shown = key
        if self.cur is not None:
            self.cur.pack_forget()
            self.cur = None
        if key in PROVIDERS:
            self.cur = self._view(key)
            self.cur.pack(fill="x", padx=22, pady=(0, 10), before=self.before)
            self._push(key)

    def _push(self, key):
        d = self.last.get(key)
        if not d:
            return
        sec = self.sess.get(key, [0.0, None])[0]
        items = list(d["items"])
        rate = d.get("rate_of")
        rate_val = 0
        if rate:
            val = dict(items).get(rate, 0)
            rate_val = int(val / (sec / 3600.0)) if sec >= 60 else 0
        view = self._view(key)
        if isinstance(view, StatsBar):
            if rate:
                items.append(("В час", rate_val))
            items.append(("Время", fmt_time(sec)))
            view.set_data(d["status"], d["active"], items)
        else:
            view.set_data(d, sec, rate_val)

    def _tick(self):
        now = time.time()
        for key, fn in list(PROVIDERS.items()):
            try:
                d = fn()
            except Exception:
                continue
            s = self.sess.setdefault(key, [0.0, None])
            if d.get("active") and s[1] is not None:
                s[0] += now - s[1]
            s[1] = now
            self.last[key] = d
        if self.shown in PROVIDERS:
            self._push(self.shown)
        try:
            self.root.after(500, self._tick)
        except tk.TclError:
            pass
