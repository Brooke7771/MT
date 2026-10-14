# -*- coding: utf-8 -*-
"""Интерфейс главного окна: боковое меню, страница «Главная» и всплывающие уведомления.
Всё рисуется примитивами на одном Canvas на каждый элемент (вложенные виджеты в Windows
рвали отрисовку). Анимации: скользящее выделение пункта, плавные переключатели,
«подъём» плиток, дыхание индикаторов, въезд уведомлений."""
import math
import time
import tkinter as tk
import tkinter.font as tkfont

import theme as T
import widgets
from widgets import mix, _rrect, ease

SIDE_W = 240
CARD = "#15151c"
EDGE = "#262630"
DIM = "#5d5d68"
LOCKED = {"bot", "fish", "afk"}


def _lerp(a, b, k):
    return a + (b - a) * k


# ====================================================================== боковое меню
class Sidebar(tk.Canvas):
    GROUPS = [("", ["home"]), ("БОТЫ", ["bot", "fish", "afk"]), ("УТИЛИТЫ", ["ram", "cook"]),
              ("СИСТЕМА", ["admin"])]
    ITEM_H, GAP, TOP = 42, 4, 98

    def __init__(self, parent, on_select, on_toggle, get_toggle, switchable):
        super().__init__(parent, width=SIDE_W, bg=T.SIDEBAR, highlightthickness=0, bd=0)
        self.on_select, self.on_toggle, self.get_toggle = on_select, on_toggle, get_toggle
        self.switchable = set(switchable)
        self.meta, self.rows = {}, []
        self.sel = None
        self.sel_y = None
        self.sel_h = 0.0
        self.hov_key = None
        self.hov, self.tg = {}, {}
        self.status = ("Нет ключа", "Доступны ОЗУ и готовка", False)
        self.t0 = time.time()
        self._fb = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)
        self._loop()

    # ---- данные
    def set_items(self, items):
        """items: [(key, значок, название)]"""
        self.meta = {k: (g, n) for k, g, n in items}
        rows, y = [], self.TOP
        for title, ks in self.GROUPS:
            ks = [k for k in ks if k in self.meta]
            if not ks:
                continue
            if title:
                rows.append(("head", title, y))
                y += 28
            for k in ks:
                rows.append(("item", k, y))
                y += self.ITEM_H + self.GAP
            y += 8
        self.rows = rows
        if self.sel not in self.meta:
            self.sel = None

    def select(self, key):
        self.sel = key
        if self.sel_y is None:
            self.sel_y = self._y(key)

    def set_status(self, title, sub, ok):
        self.status = (title, sub, ok)

    def _y(self, key):
        for kind, k, y in self.rows:
            if kind == "item" and k == key:
                return y
        return None

    # ---- мышь
    def _hit(self, e):
        for kind, k, y in self.rows:
            if kind == "item" and y <= e.y <= y + self.ITEM_H and 10 <= e.x <= SIDE_W - 10:
                return k, (k in self.switchable and e.x >= SIDE_W - 66)
        H = self.winfo_height()
        if H - 82 <= e.y <= H - 12 and 12 <= e.x <= SIDE_W - 12:
            return "#status", False
        return None, False

    def _set_hover(self, k):
        self.hov_key = k
        self.configure(cursor="hand2" if k else "")

    def _motion(self, e):
        k, _ = self._hit(e)
        if k != self.hov_key:
            self._set_hover(k)

    def _click(self, e):
        k, tog = self._hit(e)
        if not k:
            return
        if k == "#status":
            self.on_select("ram")
        elif tog:
            self.on_toggle(k)
        else:
            self.on_select(k)

    # ---- анимация и рисование
    def _loop(self):
        busy = False
        for kind, k, y in self.rows:
            if kind != "item":
                continue
            target = 1.0 if k == self.hov_key and k != self.sel else 0.0
            h = self.hov.get(k, 0.0)
            h2 = _lerp(h, target, 0.3)
            self.hov[k] = target if abs(h2 - target) < 0.01 else h2
            busy |= self.hov[k] != target
            if k in self.switchable:
                tt = 1.0 if self.get_toggle(k) else 0.0
                g = self.tg.get(k, tt)
                g2 = _lerp(g, tt, 0.3)
                self.tg[k] = tt if abs(g2 - tt) < 0.01 else g2
                busy |= self.tg[k] != tt
        if self.sel is not None:
            ty = self._y(self.sel)
            if ty is not None:
                if self.sel_y is None:
                    self.sel_y = ty
                d = ty - self.sel_y
                self.sel_y = ty if abs(d) < 0.5 else self.sel_y + d * 0.3
                busy |= self.sel_y != ty
        try:
            if self.winfo_ismapped():
                self._draw()
            self.after(16 if busy else 70, self._loop)
        except tk.TclError:
            pass

    def _draw(self):
        self.delete("all")
        W, H = SIDE_W, self.winfo_height()
        t = time.time() - self.t0
        # логотип
        _rrect(self, 18, 22, 52, 56, 10, fill=T.ACCENT, outline="")
        _rrect(self, 18, 22, 52, 39, 10, fill=mix(T.ACCENT, "#ffffff", 0.16), outline="")
        self.create_text(35, 40, text="M", fill="#ffffff", font=("Segoe UI", 16, "bold"))
        self.create_text(64, 31, text="Majestic", anchor="w", fill=T.TEXT, font=("Segoe UI", 14, "bold"))
        self.create_text(65, 50, text="T  O  O  L  S", anchor="w", fill=T.MUTED, font=("Segoe UI", 8, "bold"))
        self.create_line(18, 76, W - 18, 76, fill=EDGE)
        # выделение (скользит между пунктами)
        if self.sel is not None and self.sel_y is not None and self._y(self.sel) is not None:
            y = self.sel_y
            glow = 0.62 + 0.1 * math.sin(t * 2)
            _rrect(self, 10, y, W - 10, y + self.ITEM_H, 11, fill="#1d1017", outline=mix(T.ACCENT, T.SIDEBAR, glow))
            _rrect(self, 10, y + 10, 14, y + self.ITEM_H - 10, 2, fill=T.ACCENT, outline="")
        for kind, k, y in self.rows:
            if kind == "head":
                self.create_text(24, y + 14, text=k, anchor="w", fill=DIM, font=("Segoe UI", 8, "bold"))
                continue
            glyph, name = self.meta[k]
            on = k == self.sel
            h = self.hov.get(k, 0.0)
            if h > 0.02 and not on:
                _rrect(self, 10, y, W - 10, y + self.ITEM_H, 11, fill=mix(T.SIDEBAR, "#1a1a22", h), outline="")
            cy = y + self.ITEM_H / 2
            gx = 36 + (2 * h if not on else 3)
            self.create_text(gx, cy, text=glyph, fill=T.ACCENT_HOVER if on else mix(T.MUTED, T.TEXT, h),
                             font=("Segoe UI Symbol", 12))
            self.create_text(60 + (2 * h if not on else 3), cy, text=name, anchor="w",
                             fill="#ffffff" if on else mix(T.MUTED, T.TEXT, h), font=("Segoe UI", 11, "bold" if on else "normal"))
            if k in self.switchable:
                g = self.tg.get(k, 0.0)
                x1, x0 = W - 24, W - 24 - 36
                _rrect(self, x0, cy - 9, x1, cy + 9, 9, fill=mix("#26262e", "#1d6a3c", g),
                       outline=mix(EDGE, "#2fb865", g))
                kx = _lerp(x0 + 9, x1 - 9, g)
                self.create_oval(kx - 6, cy - 6, kx + 6, cy + 6, fill=mix("#8c8c99", "#ffffff", g), outline="")
        # карточка ключа внизу
        title, sub, ok = self.status
        y1 = H - 12
        hov = self.hov_key == "#status"
        _rrect(self, 12, y1 - 62, W - 12, y1, 12, fill="#101016" if not hov else "#14141b",
               outline=mix(EDGE, T.ACCENT, 0.5) if hov else EDGE)
        col = T.OK if ok else T.MUTED
        ph = t % 2.0 / 2.0
        if ok:
            r = 5 + 7 * ph
            self.create_oval(30 - r, y1 - 40 - r, 30 + r, y1 - 40 + r, outline=mix(col, "#101016", ph))
        self.create_oval(25, y1 - 45, 35, y1 - 35, fill=col, outline="")
        self.create_text(46, y1 - 42, text=title, anchor="w", fill=T.TEXT, font=("Segoe UI", 9, "bold"))
        self.create_text(46, y1 - 22, text=sub, anchor="w", fill=T.MUTED, font=("Segoe UI", 8))


# ====================================================================== главная страница
TILES = [
    ("bot", "✂", "Швейный бот", "Автошитьё в ателье"),
    ("fish", "⚓", "Рыбалка", "Полный цикл ловли"),
    ("afk", "♻", "Анти-АФК", "Нажатия, еда и вода"),
    ("ram", "⚡", "Чистильщик ОЗУ", "Освобождает память игры"),
    ("cook", "♨", "Готовка", "Калькулятор и таймеры"),
]
TIPS = [
    "Включай нужных ботов переключателем в меню слева: выключенный бот не реагирует на горячую клавишу.",
    "HUD поверх игры включается внизу меню: он не мешает кликам и не отвлекает бота.",
    "Чистильщик ОЗУ работает полнее от администратора: тогда чистится и кэш Windows.",
    "Рыбалку удобно запускать горячей клавишей: Numpad / по умолчанию, меняется в её разделе.",
    "Когда бот работает, его статистика видна в блоке над настройками и в HUD.",
    "Карточка ключа внизу меню покажет, что открыто и до какого числа действует доступ.",
]
BANNER = "#1a1016"


class HomePage(tk.Canvas):
    def __init__(self, parent, on_open, on_locked):
        super().__init__(parent, bg=T.BG, highlightthickness=0, bd=0)
        self.on_open, self.on_locked = on_open, on_locked
        self.access = set()
        self.res = None
        self.version = ""
        self.ram = (0.0, 0.0, 0.0)           # процент, занято ГБ, всего ГБ
        self.ram_shown = 0.0
        self.hov = None
        self.hv = {}
        self.t0 = time.time()
        self.tip_i, self.tip_t = 0, time.time()
        self._ram_t = 0.0
        self._rects = []
        self._fb = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._hover(None))
        self.bind("<Button-1>", self._click)
        self._loop()

    def set_state(self, access, res, version):
        self.access, self.res, self.version = set(access), res, version

    # ---- мышь
    def _at(self, e):
        for key, x0, y0, x1, y1 in self._rects:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                return key
        return None

    def _hover(self, k):
        self.hov = k
        self.configure(cursor="hand2" if k else "")

    def _motion(self, e):
        k = self._at(e)
        if k != self.hov:
            self._hover(k)

    def _click(self, e):
        k = self._at(e)
        if not k:
            return
        if k in LOCKED and k not in self.access:
            self.on_locked(k)
        else:
            self.on_open(k)

    # ---- цикл
    def _read_ram(self):
        try:
            import psutil
            m = psutil.virtual_memory()
            self.ram = (m.percent, (m.total - m.available) / 2 ** 30, m.total / 2 ** 30)
        except Exception:
            pass

    def _loop(self):
        now = time.time()
        if now - self._ram_t > 2.0:
            self._ram_t = now
            self._read_ram()
        self.ram_shown = _lerp(self.ram_shown, self.ram[0], 0.12)
        if now - self.tip_t > 8:
            self.tip_i, self.tip_t = (self.tip_i + 1) % len(TIPS), now
        for k in [t[0] for t in TILES]:
            tgt = 1.0 if k == self.hov else 0.0
            self.hv[k] = _lerp(self.hv.get(k, 0.0), tgt, 0.3)
        try:
            if self.winfo_ismapped():
                self._draw()
            self.after(33, self._loop)
        except tk.TclError:
            pass

    # ---- рисование
    def _status_of(self, key):
        """-> (текст, цвет, метрика)"""
        if key in LOCKED and key not in self.access:
            return "нужен ключ", T.MUTED, ""
        fn = widgets.PROVIDERS.get(key)
        if fn:
            try:
                d = fn()
            except Exception:
                d = None
            if d:
                items = d.get("items") or [("", 0)]
                unit = {"fish": "поймано", "bot": "комплектов", "afk": "нажатий"}.get(key, "")
                metric = f"{items[0][1]} {unit}"
                st = str(d.get("status", ""))
                if d.get("active"):
                    return ("Пауза" if st.startswith("Пауза") else "Работает"), \
                           ("#ffb020" if st.startswith("Пауза") else T.OK), metric
                return "Выключен", T.MUTED, metric
        return "Готов", T.OK, ""

    def _draw(self):
        self.delete("all")
        self._rects = []
        W, H = self.winfo_width(), self.winfo_height()
        if W < 300:
            return
        t = time.time() - self.t0
        # --- баннер
        bh = 138
        _rrect(self, 2, 2, W - 2, bh, 18, fill=BANNER, outline=mix(T.ACCENT, EDGE, 0.7))
        for i, (px, py, pr, sp) in enumerate(((0.18, 0.5, 46, 0.7), (0.5, 0.35, 30, 1.1), (0.72, 0.65, 38, 0.9))):
            cx = W * px + 24 * math.sin(t * sp + i)
            cy = bh * py + 8 * math.cos(t * sp * 1.3 + i)
            self.create_oval(cx - pr, cy - pr, cx + pr, cy + pr, fill="", outline=mix(BANNER, T.ACCENT, 0.16))
        name = (self.res or {}).get("name") if self.res and self.res.get("ok") else None
        self.create_text(28, 38, text="Добро пожаловать" + (f", {name}" if name else ""), anchor="w",
                         fill="#ffffff", font=("Segoe UI", 20, "bold"))
        if self.res and self.res.get("ok"):
            exp = self.res.get("expires")
            sub = "Ключ активен" + (f" до {exp[:10]}" if exp else "") + " · разделов: " + str(len(self.access & LOCKED))
        else:
            sub = "Ключ не введён · открыты очистка ОЗУ и калькулятор готовки"
        self.create_text(28, 70, text=sub, anchor="w", fill=T.MUTED, font=("Segoe UI", 10))
        self.create_text(28, 104, text=f"Majestic Tools {self.version}", anchor="w", fill=DIM, font=("Segoe UI", 9))
        # кольцо ОЗУ
        rx, ry, rr = W - 86, bh / 2 + 1, 40
        pct = self.ram_shown
        hot = pct >= 90
        col = T.WARN if hot else (T.ACCENT if pct >= 75 else T.OK)
        self.create_oval(rx - rr, ry - rr, rx + rr, ry + rr, outline="#2a1b22", width=9)
        self.create_arc(rx - rr, ry - rr, rx + rr, ry + rr, start=90, extent=-max(2, 3.6 * pct), style="arc",
                        outline=col, width=9)
        self.create_text(rx, ry - 6, text=f"{int(round(pct))}%", fill="#ffffff", font=("Segoe UI", 16, "bold"))
        self.create_text(rx, ry + 14, text="ОЗУ", fill=T.MUTED, font=("Segoe UI", 8, "bold"))
        self.create_text(rx - rr - 20, ry - 6, text=f"{self.ram[1]:.1f} из {self.ram[2]:.1f} ГБ", anchor="e",
                         fill=T.TEXT, font=("Segoe UI", 10, "bold"))
        self.create_text(rx - rr - 20, ry + 14, text="занято памяти", anchor="e", fill=T.MUTED,
                         font=("Segoe UI", 9))
        # --- плитки разделов
        self.create_text(4, bh + 28, text="РАЗДЕЛЫ", anchor="w", fill=DIM, font=("Segoe UI", 8, "bold"))
        cols = 3 if W >= 800 else 2
        gap, th = 14, 116
        tw = (W - 4 - gap * (cols - 1)) / cols
        y0 = bh + 46
        for i, (key, glyph, name, desc) in enumerate(TILES):
            r_, c_ = divmod(i, cols)
            x0 = 2 + c_ * (tw + gap)
            y = y0 + r_ * (th + gap)
            h = self.hv.get(key, 0.0)
            lift = 4 * h
            locked = key in LOCKED and key not in self.access
            text, scol, metric = self._status_of(key)
            self._rects.append((key, x0, y - lift, x0 + tw, y + th - lift))
            ac = T.MUTED if locked else T.ACCENT
            edge = mix(EDGE, T.ACCENT, 0.7 * h) if not locked else EDGE
            _rrect(self, x0, y - lift, x0 + tw, y + th - lift, 16, fill=mix(CARD, "#1b1b24", h), outline=edge)
            yy = y - lift
            _rrect(self, x0 + 16, yy + 16, x0 + 58, yy + 58, 12, fill=mix("#21151b", "#2a1820", h) if not locked else "#1b1b22",
                   outline="")
            self.create_text(x0 + 37, yy + 37, text=glyph, fill=ac if not locked else DIM, font=("Segoe UI Symbol", 17))
            self.create_text(x0 + 70, yy + 29, text=name, anchor="w", fill=T.TEXT if not locked else T.MUTED,
                             font=("Segoe UI", 11, "bold"))
            self.create_text(x0 + 70, yy + 49, text=desc, anchor="w", fill=T.MUTED, font=("Segoe UI", 8))
            # статус-плашка
            pw = self._fb.measure(text) + 26
            py = yy + th - 24
            _rrect(self, x0 + 16, py - 11, x0 + 16 + pw, py + 11, 11, fill=mix(scol, CARD, 0.84), outline=mix(scol, CARD, 0.6))
            if locked:
                _rrect(self, x0 + 24, py - 1, x0 + 32, py + 5, 1, fill=scol, outline="")
                self.create_arc(x0 + 25, py - 6, x0 + 31, py + 1, start=0, extent=180, style="arc", outline=scol)
            else:
                ph = (t % 1.6) / 1.6
                if text == "Работает":
                    rr_ = 3 + 6 * ph
                    self.create_oval(x0 + 28 - rr_, py - rr_, x0 + 28 + rr_, py + rr_, outline=mix(scol, CARD, ph))
                self.create_oval(x0 + 25, py - 3, x0 + 31, py + 3, fill=scol, outline="")
            self.create_text(x0 + 38, py, text=text, anchor="w", fill=mix(scol, "#ffffff", 0.3), font=("Segoe UI", 8, "bold"))
            if metric:
                self.create_text(x0 + tw - 16, py, text=metric, anchor="e", fill=T.TEXT, font=("Segoe UI", 9, "bold"))
        rows = (len(TILES) + cols - 1) // cols
        ty = y0 + rows * (th + gap) + 4
        # --- подсказка
        if ty + 70 < H + 40:
            _rrect(self, 2, ty, W - 2, ty + 62, 14, fill=CARD, outline=EDGE)
            self.create_text(24, ty + 31, text="✦", fill=T.ACCENT, font=("Segoe UI Symbol", 14))
            k = ease(min(1.0, (time.time() - self.tip_t) / 0.4))
            self.create_text(48, ty + 31 + (1 - k) * 14, text=TIPS[self.tip_i], anchor="w", fill=mix(CARD, T.TEXT, k),
                             font=("Segoe UI", 10), width=max(200, W - 90))


# ====================================================================== уведомления
class Toasts(tk.Canvas):
    W, H, GAP = 340, 58, 8
    KINDS = {"ok": ("#2fb865", "✓"), "warn": ("#ffb020", "!"), "err": ("#ff4d6d", "✕"), "info": ("#38c6e0", "i")}

    def __init__(self, root):
        super().__init__(root, width=self.W, height=self.H, bg=T.BG, highlightthickness=0, bd=0)
        self.root = root
        self.items = []
        self.running = False

    def push(self, text, kind="info", ttl=4.5):
        self.items = (self.items + [{"text": text, "kind": kind, "t0": time.time(), "ttl": ttl}])[-3:]
        if not self.running:
            self.running = True
            self._loop()

    def _loop(self):
        now = time.time()
        self.items = [i for i in self.items if now - i["t0"] < i["ttl"] + 0.35]
        if not self.items:
            self.running = False
            self.place_forget()
            return
        n = len(self.items)
        h = n * self.H + (n - 1) * self.GAP
        self.configure(height=h)
        self.place(relx=1.0, rely=1.0, anchor="se", x=-20, y=-20)
        tk.Misc.tkraise(self)             # у Canvas tkraise() поднимает элементы, а не само окно
        self.delete("all")
        for idx, it in enumerate(self.items):
            age = now - it["t0"]
            k = ease(min(1.0, age / 0.35))
            if age > it["ttl"]:
                k = 1 - ease(min(1.0, (age - it["ttl"]) / 0.3))
            off = (1 - k) * (self.W + 20)
            y = idx * (self.H + self.GAP)
            col, ico = self.KINDS.get(it["kind"], self.KINDS["info"])
            _rrect(self, off, y, off + self.W, y + self.H, 14, fill="#17171f", outline=mix(col, EDGE, 0.6))
            _rrect(self, off, y + 12, off + 4, y + self.H - 12, 2, fill=col, outline="")
            self.create_oval(off + 16, y + self.H / 2 - 13, off + 42, y + self.H / 2 + 13, fill=mix(col, "#17171f", 0.8),
                             outline="")
            self.create_text(off + 29, y + self.H / 2, text=ico, fill=col, font=("Segoe UI", 12, "bold"))
            frac = max(0.0, 1 - age / it["ttl"])
            self.create_text(off + 54, y + self.H / 2 - 3, text=it["text"], anchor="w", fill=T.TEXT,
                             font=("Segoe UI", 10), width=self.W - 74)
            self.create_rectangle(off + 14, y + self.H - 5, off + 14 + (self.W - 28) * frac, y + self.H - 3,
                                  fill=mix(col, "#17171f", 0.55), outline="")
        try:
            self.after(16, self._loop)
        except tk.TclError:
            pass
