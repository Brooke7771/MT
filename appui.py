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
from widgets import mix, _rrect, ease, chart_range

SIDE_W = 240
CARD = "#15151c"
EDGE = "#262630"
DIM = "#5d5d68"
LOCKED = {"bot", "fish", "afk", "wood", "mine", "gym", "farm", "build"}


def _lerp(a, b, k):
    return a + (b - a) * k


# ====================================================================== боковое меню
class Sidebar(tk.Canvas):
    GROUPS = [("", ["home"]), ("БОТЫ", ["bot", "fish", "wood", "mine", "gym", "farm", "build", "afk", "test"]), ("УТИЛИТЫ", ["ram", "cook", "market", "pm"]),
              ("СИСТЕМА", ["rec", "admin"])]
    ITEM_H, GAP, TOP = 42, 4, 86
    KEY_H = 92                  # карточка ключа внизу (с отступами)

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
        self.bind("<Configure>", lambda e: self._layout())
        self.bind("<MouseWheel>", self._wheel)
        self.ih = self.ITEM_H
        self.scroll, self.max_scroll = 0.0, 0.0
        self._loop()

    # ---- данные
    def set_items(self, items):
        """items: [(key, значок, название)]"""
        self.meta = {k: (g, n) for k, g, n in items}
        self._layout()
        if self.sel not in self.meta:
            self.sel = None

    def _layout(self):
        """Раскладка пунктов; если по высоте не влезает (до карточки ключа внизу) - пункты плотнее."""
        groups = [(t, [k for k in ks if k in self.meta]) for t, ks in self.GROUPS]
        groups = [(t, ks) for t, ks in groups if ks]
        n_items = sum(len(ks) for _, ks in groups)
        n_heads = sum(1 for t, _ in groups if t)
        H = self.winfo_height()
        room = (H if H > 50 else 2000) - self.KEY_H - self.TOP
        ih, gap, head, sep = self.ITEM_H, self.GAP, 28, 8
        need = lambda: n_items * (ih + gap) + n_heads * head + len(groups) * sep
        while need() > room and ih > 28:
            ih -= 1
            head = max(20, head - 1)
            sep = max(2, sep - 1)
            gap = 2 if ih < 38 else gap
        # всё равно не влезает (маленькое окно) - пункты листаются колёсиком
        self.max_scroll = float(max(0, need() - room))
        self.scroll = min(self.scroll, self.max_scroll)
        self.ih = ih
        rows, y = [], self.TOP
        for title, ks in groups:
            if title:
                rows.append(("head", title, y))
                y += head
            for k in ks:
                rows.append(("item", k, y))
                y += ih + gap
            y += sep
        self.rows = rows

    def select(self, key):
        self.sel = key
        if self.sel_y is None:
            self.sel_y = self._y(key)

    def set_status(self, title, sub, ok):
        self.status = (title, sub, ok)

    def _y(self, key):
        for kind, k, y in self.rows:
            if kind == "item" and k == key:
                return y - self.scroll
        return None

    def _wheel(self, e):
        if self.max_scroll <= 0:
            return
        self.scroll = min(self.max_scroll, max(0.0, self.scroll - e.delta / 120 * 40))
        self.sel_y = self._y(self.sel) if self.sel is not None else None
        self._draw()

    def _visible(self, y):
        """Пункт (верх y на экране) целиком виден: не под логотипом и не под карточкой ключа."""
        return self.TOP - 4 <= y and y + self.ih <= self.winfo_height() - self.KEY_H + 4

    # ---- мышь
    def _hit(self, e):
        for kind, k, y in self.rows:
            y -= self.scroll
            if kind == "item" and self._visible(y) and y <= e.y <= y + self.ih and 10 <= e.x <= SIDE_W - 10:
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
            self.after(8 if busy else 70, self._loop)
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
        self.create_line(18, 72, W - 18, 72, fill=EDGE)
        # выделение (скользит между пунктами)
        if self.sel is not None and self.sel_y is not None and self._y(self.sel) is not None \
                and self._visible(self.sel_y):
            y = self.sel_y
            glow = 0.62 + 0.1 * math.sin(t * 2)
            _rrect(self, 10, y, W - 10, y + self.ih, 11, fill="#1d1017", outline=mix(T.ACCENT, T.SIDEBAR, glow))
            _rrect(self, 10, y + 10, 14, y + self.ih - 10, 2, fill=T.ACCENT, outline="")
        for kind, k, y in self.rows:
            y -= self.scroll
            if kind == "head":
                if self._visible(y):
                    self.create_text(24, y + 12, text=k, anchor="w", fill=DIM, font=("Segoe UI", 8, "bold"))
                continue
            if not self._visible(y):
                continue
            glyph, name = self.meta[k]
            on = k == self.sel
            h = self.hov.get(k, 0.0)
            if h > 0.02 and not on:
                _rrect(self, 10, y, W - 10, y + self.ih, 11, fill=mix(T.SIDEBAR, "#1a1a22", h), outline="")
            cy = y + self.ih / 2
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
        # пункты не влезли - подсказка, что меню листается
        if self.max_scroll > 0:
            if self.scroll < self.max_scroll - 1:
                self.create_text(W / 2, H - self.KEY_H + 6, text="⌄", fill=DIM, font=("Segoe UI", 10, "bold"))
            if self.scroll > 1:
                self.create_text(W / 2, self.TOP - 6, text="⌃", fill=DIM, font=("Segoe UI", 10, "bold"))
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
    ("wood", "♣", "Лесоруб", "Рубка и ветки сами"),
    ("mine", "⛏", "Шахта", "Добыча и руда сами"),
    ("gym", "✊", "Качалка", "Упражнения сами"),
    ("farm", "❀", "Ферма", "Апельсины сами"),
    ("build", "⚒", "Стройка", "Шлифовка, лифт, молоток"),
    ("afk", "♻", "Анти-АФК", "Нажатия, еда и вода"),
    ("ram", "⚡", "Чистильщик ОЗУ", "Освобождает память игры"),
    ("cook", "♨", "Готовка", "Калькулятор и таймеры"),
    ("market", "⚖", "Рынок", "Цены по серверам, где выгодно"),
    ("pm", "☰", "PM", "Таблица по скринам планшета"),
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
    """Главная: баннер, статистика за всё время, плитки разделов, лента событий, подсказка.
    Без ключа закрытые боты не показываются вовсе - вместо них плитка «Ввести ключ»."""

    def __init__(self, parent, on_open, on_locked):
        super().__init__(parent, bg=T.BG, highlightthickness=0, bd=0)
        self.on_open, self.on_locked = on_open, on_locked
        self.access = set()
        self.res = None
        self.version = ""
        self.ram = (0.0, 0.0, 0.0)           # процент, занято ГБ, всего ГБ
        self.ram_shown = 0.0
        self.hist = []                       # загрузка ОЗУ каждые 2 с (для графика в баннере)
        self.hov = None
        self.hv = {}
        self.sv = {}                         # показанные (докручиваемые) числа статистики
        self.t0 = time.time()
        self.tip_i, self.tip_t = 0, time.time()
        self._ram_t = 0.0
        self._rects = []
        self.intro = time.time()
        self.active = False                  # страницы лежат стопкой: рисуем, только когда «Главная» сверху
        self.scroll, self.scroll_t = 0.0, 0.0
        self.content_h = 0
        self._fb = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._hover(None))
        self.bind("<Button-1>", self._click)
        self.bind("<MouseWheel>", self._wheel)
        self._loop()

    def replay(self):
        """Блоки заново «выезжают» по очереди (зовётся при открытии страницы)."""
        self.intro = time.time()

    def _k(self, i):
        return ease(max(0.0, min(1.0, (time.time() - self.intro - 0.07 * i) / 0.45)))

    def set_state(self, access, res, version):
        self.access, self.res, self.version = set(access), res, version

    # ---- мышь
    def _at(self, e):
        y = e.y + self.scroll
        for key, x0, y0, x1, y1 in self._rects:
            if x0 <= e.x <= x1 and y0 <= y <= y1:
                return key
        return None

    def _hover(self, k):
        self.hov = k
        self.configure(cursor="hand2" if k else "")

    def _motion(self, e):
        k = self._at(e)
        if k != self.hov:
            self._hover(k)

    def _wheel(self, e):
        room = max(0, self.content_h - self.winfo_height() + 8)
        self.scroll_t = max(0.0, min(float(room), self.scroll_t - e.delta / 120.0 * 70))

    def _click(self, e):
        k = self._at(e)
        if not k:
            return
        if k == "#key" or (k in LOCKED and k not in self.access):
            self.on_locked(k)
        else:
            self.on_open(k)

    # ---- цикл
    def _read_ram(self):
        try:
            import psutil
            m = psutil.virtual_memory()
            self.ram = (m.percent, (m.total - m.available) / 2 ** 30, m.total / 2 ** 30)
            self.hist.append(m.percent)
            del self.hist[:-60]
        except Exception:
            pass

    def _loop(self):
        now = time.time()
        busy = False
        if now - self._ram_t > 2.0:
            self._ram_t = now
            self._read_ram()
        self.ram_shown = _lerp(self.ram_shown, self.ram[0], 0.12)
        if now - self.tip_t > 9:
            self.tip_i, self.tip_t = (self.tip_i + 1) % len(TIPS), now
        keys = [t[0] for t in TILES] + ["#key"]
        for k in keys:
            tgt = 1.0 if k == self.hov else 0.0
            v = _lerp(self.hv.get(k, 0.0), tgt, 0.3)
            self.hv[k] = tgt if abs(v - tgt) < 0.01 else v
            busy |= self.hv[k] != tgt
        d = self.scroll_t - self.scroll
        self.scroll = self.scroll_t if abs(d) < 0.5 else self.scroll + d * 0.3
        busy |= self.scroll != self.scroll_t
        busy |= now - self.intro < 1.2
        try:
            if not self.active:
                self.after(150, self._loop)
                return
            if self.winfo_ismapped():
                self._draw()
            self.after(8 if busy else 30, self._loop)
        except tk.TclError:
            pass

    # ---- данные плиток
    def _status_of(self, key):
        """-> (текст, цвет, метрика)"""
        fn = widgets.PROVIDERS.get(key)
        d = None
        if fn:
            try:
                d = fn()
            except Exception:
                d = None
        if d:
            lab = d.get("label")
            if lab:
                col = {"ok": T.OK, "warn": "#ffb020"}.get(lab[1], T.MUTED)
                if key == "ram":
                    return lab[0], col, f"{d.get('pct', 0):.0f}% занято"
                if key == "cook":
                    n = (d.get("items") or [("", 0)])[0][1]
                    return lab[0], col, (f"{n} идёт" if n else "")
                if key == "pm":
                    m = d.get("pm") or {}
                    return lab[0], col, (f"{m.get('online', 0)} в сети" if m.get("n") else "")
                if key == "market":
                    m = d.get("market") or {}
                    if m.get("alerts_today"):
                        return lab[0], col, f"сработало: {m['alerts_today']}"
                    if m.get("watch_n"):
                        return lab[0], col, f"{m['watch_n']} в мониторинге"
                    n = (d.get("items") or [("", 0)])[0][1]
                    return lab[0], col, (f"{n} позиций" if n else "")
                return lab[0], col, ""
            items = d.get("items") or [("", 0)]
            unit = {"fish": "поймано", "bot": "комплектов", "afk": "нажатий", "sand": "досок", "wood": "деревьев", "mine": "камней", "gym": "подходов", "farm": "деревьев",
                    "build": "досок"}.get(key, "")
            metric = f"{items[0][1]} {unit}"
            st = str(d.get("status", ""))
            if d.get("active"):
                return ("Пауза" if st.startswith("Пауза") else "Работает"), \
                       ("#ffb020" if st.startswith("Пауза") else T.OK), metric
            return "Выключен", T.MUTED, metric
        return "Готов", T.OK, ""

    # ---- рисование
    def _apply_sections(self, secs, W):
        """«Выезд» блоков: всё, что целиком лежит в полосе блока, получает тег и сдвигается ОДНИМ вызовом
        (по одному элементу - сотни вызовов на кадр, было заметно медленно)."""
        live = [(n, i, y0, y1) for n, (i, y0, y1) in enumerate(secs) if self._k(i) < 1.0]
        for n, i, y0, y1 in live:                      # сначала метим всё, потом двигаем
            self.addtag_enclosed(f"sec{n}", -10, y0 - 8, W + 10, y1 + 8)
        for n, i, y0, y1 in live:
            k = self._k(i)
            if k < 0.02:
                self.delete(f"sec{n}")
            else:
                self.move(f"sec{n}", 0, (1 - k) * 22)

    def _draw(self):
        self.delete("all")
        self._rects = []
        W, H = self.winfo_width(), self.winfo_height()
        if W < 300:
            return
        t = time.time() - self.t0
        locked_ok = self.access & LOCKED
        sections = []                                 # (номер, верх, низ) блоков для анимации выезда
        # ---------------- баннер
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
            sub = "Ключ активен" + (f" до {exp[:10]}" if exp else "") + f" · открыто ботов: {len(locked_ok)}"
        else:
            sub = "Ключ не введён · открыты очистка ОЗУ и калькулятор готовки"
        self.create_text(28, 70, text=sub, anchor="w", fill=T.MUTED, font=("Segoe UI", 10))
        self.create_text(28, 104, text=f"Majestic Tools {self.version}", anchor="w", fill=DIM, font=("Segoe UI", 9))
        rx, ry, rr = W - 86, bh / 2 + 1, 40
        pct = self.ram_shown
        col = T.WARN if pct >= 90 else (T.ACCENT if pct >= 75 else T.OK)
        self.create_oval(rx - rr, ry - rr, rx + rr, ry + rr, outline="#2a1b22", width=9)
        self.create_arc(rx - rr, ry - rr, rx + rr, ry + rr, start=90, extent=-max(2, 3.6 * pct), style="arc",
                        outline=col, width=9)
        self.create_text(rx, ry - 6, text=f"{int(round(pct))}%", fill="#ffffff", font=("Segoe UI", 16, "bold"))
        self.create_text(rx, ry + 14, text="ОЗУ", fill=T.MUTED, font=("Segoe UI", 8, "bold"))
        tx = rx - rr - 20
        self.create_text(tx, ry - 6, text=f"{self.ram[1]:.1f} из {self.ram[2]:.1f} ГБ", anchor="e",
                         fill=T.TEXT, font=("Segoe UI", 10, "bold"))
        self.create_text(tx, ry + 14, text="занято памяти", anchor="e", fill=T.MUTED, font=("Segoe UI", 9))
        if W >= 900 and len(self.hist) >= 2:                 # график загрузки за последние минуты
            gx1, gx0, gy0, gy1 = tx - 130, tx - 130 - 240, ry - 30, ry + 30
            n = len(self.hist)
            lo, hi = chart_range(self.hist)
            pts = []
            for i, v in enumerate(self.hist):
                pts += [gx0 + (gx1 - gx0) * i / max(1, n - 1), gy1 - (gy1 - gy0) * (v - lo) / (hi - lo)]
            if max(self.hist) - min(self.hist) >= 1.0:           # почти ровная линия - без заливки
                self.create_polygon(pts + [gx1, gy1, gx0, gy1], fill=mix(BANNER, col, 0.2), outline="")
            self.create_line(pts, fill=col, width=2, smooth=True)
            self.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3, fill="#ffffff", outline="")
        sections.append((0, 0, bh + 4))
        y = bh + 18
        # ---------------- статистика за всё время
        try:
            import stats
        except Exception:
            stats = None
        order = [k for k in ("fish", "bot", "wood", "mine", "gym", "farm", "afk") if k in locked_ok]
        if "build" in locked_ok:
            order += ["sand", "lift", "hammer"]
        if stats and order:
            ys = y
            self.create_text(4, y + 8, text="ЗА ВСЁ ВРЕМЯ", anchor="w", fill=DIM, font=("Segoe UI", 8, "bold"))
            y += 24
            labels = {"fish": "Поймано рыб", "bot": "Комплектов", "afk": "Нажатий", "sand": "Досок отшлифовано", "wood": "Деревьев", "mine": "Камней", "gym": "Подходов", "farm": "Деревьев",
                      "lift": "Подъёмов лифта", "hammer": "Досок прибито"}
            cells = [(k, labels[k], stats.get(k)["count"]) for k in order]
            secs = sum(stats.get(k)["secs"] for k in order)
            cells.append(("#time", "Время работы", secs))
            gap = 14
            cw = (W - 4 - gap * (len(cells) - 1)) / len(cells)
            for i, (k, lab, val) in enumerate(cells):
                x0 = 2 + i * (cw + gap)
                cur = self.sv.get(k, 0.0)
                cur = val if abs(val - cur) < 0.6 else cur + (val - cur) * 0.12
                self.sv[k] = cur
                _rrect(self, x0, y, x0 + cw, y + 78, 14, fill=CARD, outline=EDGE)
                self.create_rectangle(x0 + 1, y + 16, x0 + 4, y + 62, fill=T.ACCENT, outline="")
                self.create_text(x0 + 20, y + 20, text=lab.upper(), anchor="w", fill=T.MUTED, font=("Segoe UI", 8, "bold"))
                if k == "#time":
                    s = int(cur)
                    txt = f"{s // 3600} ч {s // 60 % 60} мин" if s >= 3600 else f"{s // 60} мин"
                else:
                    txt = f"{int(round(cur)):,}".replace(",", " ")
                self.create_text(x0 + 20, y + 49, text=txt, anchor="w", fill="#ffffff", font=("Segoe UI", 22, "bold"))
            y += 78
            sections.append((1, ys, y))
            y += 12
        # ---------------- плитки разделов
        ys = y
        self.create_text(4, y + 14, text="РАЗДЕЛЫ", anchor="w", fill=DIM, font=("Segoe UI", 8, "bold"))
        y += 32
        tiles = [t for t in TILES if t[0] not in LOCKED or t[0] in self.access]
        if not locked_ok:
            tiles = tiles + [("#key", "key", "Ввести ключ", "Откроет ботов: шитьё, рыбалка, анти-АФК")]
        cols = 3 if W >= 800 else 2
        gap, th = 14, 116
        tw = (W - 4 - gap * (cols - 1)) / cols
        y0 = y
        for i, (key, glyph, name, desc) in enumerate(tiles):
            r_, c_ = divmod(i, cols)
            x0 = 2 + c_ * (tw + gap)
            ty = y0 + r_ * (th + gap)
            h = self.hv.get(key, 0.0)
            lift = 4 * h
            cta = key == "#key"
            text, scol, metric = ("нужен ключ", T.ACCENT, "") if cta else self._status_of(key)
            self._rects.append((key, x0, ty - lift, x0 + tw, ty + th - lift))
            yy = ty - lift
            _rrect(self, x0, yy, x0 + tw, yy + th, 16, fill=mix(CARD, "#1d1018" if cta else "#1b1b24", h),
                   outline=mix(mix(EDGE, T.ACCENT, 0.5 if cta else 0.0), T.ACCENT, 0.7 * h))
            _rrect(self, x0 + 16, yy + 16, x0 + 58, yy + 58, 12, fill=mix("#21151b", "#2a1820", h), outline="")
            if cta:                                                  # значок ключа примитивами
                cx, cy = x0 + 31, yy + 37
                self.create_oval(cx - 7, cy - 7, cx + 3, cy + 3, outline=T.ACCENT, width=2)
                self.create_line(cx + 2, cy + 2, cx + 14, cy + 14, fill=T.ACCENT, width=2)
                self.create_line(cx + 9, cy + 9, cx + 12, cy + 6, fill=T.ACCENT, width=2)
                self.create_line(cx + 13, cy + 13, cx + 16, cy + 10, fill=T.ACCENT, width=2)
            else:
                self.create_text(x0 + 37, yy + 37, text=glyph, fill=T.ACCENT, font=("Segoe UI Symbol", 17))
            self.create_text(x0 + 70, yy + 29, text=name, anchor="w", fill=T.TEXT, font=("Segoe UI", 11, "bold"))
            self.create_text(x0 + 70, yy + 49, text=desc, anchor="w", fill=T.MUTED, font=("Segoe UI", 8))
            pw = self._fb.measure(text) + 26
            py = yy + th - 24
            _rrect(self, x0 + 16, py - 11, x0 + 16 + pw, py + 11, 11, fill=mix(scol, CARD, 0.84),
                   outline=mix(scol, CARD, 0.6))
            ph = (t % 1.6) / 1.6
            if text == "Работает":
                rr_ = 3 + 6 * ph
                self.create_oval(x0 + 28 - rr_, py - rr_, x0 + 28 + rr_, py + rr_, outline=mix(scol, CARD, ph))
            self.create_oval(x0 + 25, py - 3, x0 + 31, py + 3, fill=scol, outline="")
            self.create_text(x0 + 38, py, text=text, anchor="w", fill=mix(scol, "#ffffff", 0.3), font=("Segoe UI", 8, "bold"))
            if metric:
                self.create_text(x0 + tw - 16, py, text=metric, anchor="e", fill=T.TEXT, font=("Segoe UI", 9, "bold"))
        rows = (len(tiles) + cols - 1) // cols
        y = y0 + rows * (th + gap)
        sections.append((2, ys, y))
        # ---------------- лента событий
        ys = y
        evs = list(reversed(stats.EVENTS))[:5] if stats else []
        eh = 52 + max(1, len(evs)) * 26
        _rrect(self, 2, y, W - 2, y + eh, 16, fill=CARD, outline=EDGE)
        self.create_text(20, y + 22, text="ПОСЛЕДНИЕ СОБЫТИЯ", anchor="w", fill=T.MUTED, font=("Segoe UI", 8, "bold"))
        if not evs:
            self.create_text(20, y + 56, text="Пока тихо: запуски ботов, смена ключа и обновления появятся здесь.",
                             anchor="w", fill=DIM, font=("Segoe UI", 10))
        for i, (ts, text, kind) in enumerate(evs):
            ey = y + 52 + i * 26
            col = {"ok": T.OK, "warn": "#ffb020", "err": T.WARN}.get(kind, "#38c6e0")
            self.create_oval(22, ey - 3, 28, ey + 3, fill=col, outline="")
            self.create_text(42, ey, text=time.strftime("%H:%M", time.localtime(ts)), anchor="w", fill=T.MUTED,
                             font=("Consolas", 9))
            self.create_text(92, ey, text=text, anchor="w", fill=T.TEXT, font=("Segoe UI", 10))
        y += eh
        sections.append((3, ys, y))
        y += 14
        # ---------------- подсказка
        ys = y
        _rrect(self, 2, y, W - 2, y + 62, 14, fill=CARD, outline=EDGE)
        self.create_text(24, y + 31, text="✦", fill=T.ACCENT, font=("Segoe UI Symbol", 14))
        k = ease(min(1.0, (time.time() - self.tip_t) / 0.4))
        self.create_text(48, y + 31 + (1 - k) * 14, text=TIPS[self.tip_i], anchor="w", fill=mix(CARD, T.TEXT, k),
                         font=("Segoe UI", 10), width=max(200, W - 90))
        y += 62
        sections.append((4, ys, y))
        self._apply_sections(sections, W)
        self.content_h = y + 10
        # ---------------- прокрутка
        if self.scroll:
            self.move("all", 0, -self.scroll)
        if self.content_h > H:
            frac = H / self.content_h
            top = (H - 8) * (self.scroll / max(1, self.content_h - H)) * (1 - frac)
            _rrect(self, W - 6, 4 + top, W - 2, 4 + top + (H - 8) * frac, 2, fill="#2c2c36", outline="")


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
            self.after(8, self._loop)
        except tk.TclError:
            pass
