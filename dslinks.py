# -*- coding: utf-8 -*-
"""Ссылки на сообщения Discord из результатов поиска (по записи 20261031_210625).

Владелец ищет в канале свой статик (и упоминания @), и по каждому найденному сообщению делает
правый щелчок -> «Скопировать ссылку на сообщение» -> вставляет в блокнот. Здесь это делает бот:
находит окно Discord, на открытых результатах поиска по очереди щёлкает правой кнопкой по
каждой карточке, находит в меню пункт со словом «ссылку» (OCR Windows, как во вкладке PM),
жмёт его и читает ссылку из буфера; листает результаты колёсиком и страницы кнопкой «Далее».
Дата сообщения - прямо в ссылке (номер сообщения = время отправки), по ней фильтр «с ... по ...».
Поиск в Discord настраивает сам пользователь: вводить запросы за него ненадёжно (подсказки
поиска перехватывают ввод). Остановить - пошевелить мышью.

Только основная программа: окно открывается кнопкой во вкладке PM (build_pm_tab(afk=True))."""
import re
import sys
import time
import ctypes
import threading
import datetime as dt

import numpy as np
import cv2

LINK_RE = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")
DISCORD_EPOCH = 1420070400000
SAFE_GAP = 14            # px: пункт «Удалить сообщение» стоит прямо под нужным - щёлкаем строго по строке «ссылку»


def msg_time(url_or_id):
    """Время отправки сообщения (секунды Unix) по его номеру (snowflake) или ссылке."""
    m = LINK_RE.search(str(url_or_id))
    mid = int(m.group(3)) if m else int(url_or_id)
    return ((mid >> 22) + DISCORD_EPOCH) / 1000.0


def parse_date(s, end=False):
    """«31.10.2026» -> секунды Unix начала (или конца, end=True) дня; пусто/ошибка -> None."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            if end:
                d += dt.timedelta(days=1)
            return d.timestamp() - (0.001 if end else 0)
        except ValueError:
            pass
    return None


def in_range(url, t0, t1):
    t = msg_time(url)
    return (t0 is None or t >= t0) and (t1 is None or t <= t1)


def sort_links(urls):
    return sorted(set(urls), key=lambda u: int(LINK_RE.search(u).group(3)))


def query_hint(static, d0, d1):
    """Строка для поиска Discord: after/before не включают сам день - сдвигаем на день."""
    q = [static.strip()] if static.strip() else []
    a, b = parse_date(d0), parse_date(d1, end=True)
    if a:
        q.append("after:" + (dt.datetime.fromtimestamp(a) - dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    if b:
        q.append("before:" + (dt.datetime.fromtimestamp(b) + dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    return " ".join(q)


# ====================================================================== окно Discord
def find_discord():
    """-> hwnd самого большого видимого окна Discord или None."""
    if sys.platform != "win32":
        return None
    import psutil
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            name = psutil.Process(pid.value).name().lower()
        except Exception:
            name = ""
        if name.startswith("discord"):
            r = window_rect(hwnd)
            if r and (r[2] - r[0]) > 400 and (r[3] - r[1]) > 300:
                found.append(((r[2] - r[0]) * (r[3] - r[1]), hwnd))
        return True
    u.EnumWindows(cb, 0)
    return max(found)[1] if found else None


def window_rect(hwnd):
    import ctypes.wintypes as W
    r = W.RECT()
    try:                                          # видимая рамка окна, без невидимых полей Windows 10/11
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    if not ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def activate(hwnd):
    u = ctypes.windll.user32
    if u.IsIconic(ctypes.c_void_p(hwnd)):
        u.ShowWindow(ctypes.c_void_p(hwnd), 9)            # SW_RESTORE
    u.keybd_event(0x12, 0, 0, 0)                          # Alt: Windows разрешает SetForegroundWindow
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(ctypes.c_void_p(hwnd))


def cursor():
    import ctypes.wintypes as W
    p = W.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


# ====================================================================== зрение
def ocr_words(img, k=1.0):
    """Слова кадра BGR: [(текст, x, y, w, h)] в координатах кадра."""
    import pm_ocr
    from PIL import Image
    im = img if k == 1.0 else cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    words = pm_ocr.ocr(Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB)), "ru")
    return [(s, x / k, y / k, w / k, h / k) for s, x, y, w, h in words]


def _low(s):
    return s.lower().replace("ё", "е")


def layout(img, words):
    """Панель результатов поиска: шапка «N результатов», кнопки «Назад»/«Далее», границы, карточки.
    -> dict или None (результатов поиска не видно)."""
    H, W = img.shape[:2]
    head = [w for w in words if _low(w[0]).startswith("результат")]
    if not head:
        return None
    hx, hy = head[0][1], head[0][2]
    total = None
    for s, x, y, w, h in words:
        if abs(y - hy) < 8 and x < hx and s.isdigit():
            total, hx = int(s), min(hx, x)
    nxt = [w for w in words if _low(w[0]).startswith("далее") and w[2] > hy + 100]
    prv = [w for w in words if _low(w[0]).startswith("назад") and w[2] > hy + 100]
    top = int(hy + 30)
    bottom = int((nxt[0][2] if nxt else prv[0][2] if prv else H) - 22)
    x_col = int(hx + 6)
    return {"hx": hx, "hy": hy, "total": total, "next": nxt[0] if nxt else None, "prev": prv[0] if prv else None,
            "top": top, "bottom": bottom, "x_col": x_col, "cards": find_cards(img, hx, top, bottom, x_col)}


def find_cards(img, hx, top, bottom, x_col):
    """Карточки результатов: [[y0, y1, x_правый]]. Цвет панели - слева от карточек, карточка - самый
    частый другой цвет в столбце x_col."""
    H, W = img.shape[:2]
    pane = np.median(img[top:bottom, max(0, int(hx) - 8)].astype(int), axis=0)
    col = img[top:bottom, min(W - 1, x_col)].astype(int)
    diff_pane = np.abs(col - pane).sum(1)
    cand = col[diff_pane > 12]
    cards = []
    if len(cand):
        vals, cnt = np.unique(cand // 4, axis=0, return_counts=True)
        card = vals[cnt.argmax()] * 4 + 2
        on = (np.abs(col - card).sum(1) <= 14) & (diff_pane > 12)
        s = None
        for i, v in enumerate(list(on) + [False]):
            if v and s is None:
                s = i
            elif not v and s is not None:
                if i - s > 30:
                    cards.append([top + s, top + i])
                s = None
        # правый край карточки - самый правый столбец, где больше половины строк карточки не цвета панели
        for c in cards:
            blk = img[c[0]:c[1], x_col:].astype(int)
            frac = (np.abs(blk - pane).sum(2) > 10).mean(axis=0)
            off = np.nonzero(np.convolve(frac <= 0.5, np.ones(6), "valid") >= 6)[0]      # 6 столбцов подряд - панель
            c.append(int(x_col + (off[0] if len(off) else min(300, W - x_col - 20))))
    return cards


def menu_link_item(words):
    """Пункт «Скопировать ссылку на сообщение» в открытом меню -> (x, y) центра строки или None."""
    for s, x, y, w, h in words:
        if _low(s).startswith("ссылк"):
            line = [q for q in words if abs(q[2] - y) < h * 0.6 and -260 < q[1] - x < 260]
            cp = [q for q in line if "копир" in _low(q[0]) and q[1] < x]
            if cp and any("сообщ" in _low(q[0]) for q in line):
                return int(x + w / 2), int(y + h / 2)
    return None


def shift_y(a, b):
    """На сколько px содержимое панели уехало вверх между кадрами a и b (серые)."""
    h = a.shape[0]
    if h < 200:
        return 0
    bh = h // 3
    band = a[h - bh - 10:h - 10]                 # нижняя треть: после прокрутки вниз она уезжает вверх
    if band.std() < 3:
        return 0
    res = cv2.matchTemplate(b, band, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < 0.8:
        return None
    return (h - bh - 10) - loc[1]


# ====================================================================== сбор
class Stopped(Exception):
    pass


class Collector:
    """Рабочий поток: собрать ссылки со всех страниц открытого поиска."""

    def __init__(self, log, progress, t0=None, t1=None, inp=None, grab=None):
        self.log, self.progress = log, progress
        self.t0, self.t1 = t0, t1
        self.inp = inp
        self.grab_fn = grab
        self.stop = threading.Event()
        self.links = {}
        self.skipped = 0
        self.mouse = None
        self.rect = None
        self.notches = 1
        self.per = None          # px прокрутки на один щелчок колёсика

    # ---- ввод/снимки
    def check(self):
        if self.stop.is_set():
            raise Stopped()
        if self.mouse is not None:
            x, y = cursor()
            if abs(x - self.mouse[0]) + abs(y - self.mouse[1]) > 40:
                self.stop.set()
                raise Stopped()

    def move(self, x, y):
        self.inp.move_to(x, y)
        self.mouse = (int(x), int(y))

    def click(self, x, y, right=False):
        self.check()
        self.move(x, y)
        time.sleep(0.06)
        self.inp.click(x, y, right=right)

    def shot(self):
        if self.grab_fn:
            return self.grab_fn()
        import mss
        if not hasattr(self, "_sct"):
            self._sct = mss.mss()
        l, t, r, b = self.rect
        im = self._sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        return np.ascontiguousarray(np.asarray(im)[:, :, :3])

    def scr(self, x, y):
        """Координаты кадра -> экран."""
        return self.rect[0] + x, self.rect[1] + y

    # ---- одна карточка
    def copy_link(self, img, card):
        import pm_ftext as FT
        y0, y1, xr = card
        x_col = self.L["x_col"]
        top, bottom = max(y0, self.L["top"]), min(y1, self.L["bottom"])
        mid = (top + bottom) // 2
        pts = [(x_col - 2, mid), (xr - 12, min(bottom - 6, top + 26)), (x_col - 2, min(bottom - 8, top + 14))]
        for px, py in pts:
            FT.set_clip("")
            before = img
            self.click(*self.scr(px, py), right=True)
            time.sleep(0.3)
            im2 = self.shot()
            item = menu_link_item(ocr_words(im2))
            if item is None:
                time.sleep(0.25)                      # меню ещё «проявляется»
                im2 = self.shot()
                item = menu_link_item(ocr_words(im2))
            if item is None:
                # открылось что-то другое (меню пользователя/ссылки) - закрыть; ничего не открылось - Esc не жмём
                if np.abs(im2.astype(int) - before.astype(int)).mean() > 1.5:
                    self.inp.tap("Esc", 40)
                    time.sleep(0.2)
                continue
            self.click(*self.scr(*item))
            for _ in range(10):
                time.sleep(0.08)
                m = LINK_RE.search(FT.get_clip() or "")
                if m:
                    return m.group(0)
            return None
        return None

    # ---- весь поиск
    def run(self, hwnd):
        if hwnd:
            activate(hwnd)
            time.sleep(0.6)
            self.rect = window_rect(hwnd)
        self.mouse = None
        page_no = 0
        while True:
            page_no += 1
            first_new = self._page(page_no)
            if first_new == "stop":
                break
            nxt = self.L["next"]
            if not nxt:
                break
            sig = self._sig(self.shot())
            self.click(*self.scr(nxt[1] + nxt[3] / 2, nxt[2] + nxt[4] / 2))
            ok = False
            for _ in range(40):
                time.sleep(0.15)
                self.check()
                if self._sig(self.shot()) != sig:
                    ok = True
                    break
            if not ok:
                break                                   # «Далее» не листает - это последняя страница
            time.sleep(0.6)
        return sort_links(self.links.values())

    def _sig(self, img):
        L = self.L
        a = cv2.cvtColor(img[L["top"]:L["bottom"], int(L["hx"]):], cv2.COLOR_BGR2GRAY)
        return cv2.resize(a, (40, 40)).tobytes()

    def _page(self, page_no):
        """Все карточки текущей страницы (с прокруткой). -> "stop", если дальше смотреть не нужно."""
        done = []                      # отработанные карточки: [y0, y1] в координатах текущего кадра
        page_links = []
        stall = 0
        while True:
            self.check()
            img = self.shot()
            L = layout(img, ocr_words(img))
            if L is None:
                raise RuntimeError("не вижу результатов поиска Discord («N результатов») - открой поиск")
            self.L = L
            last_pass = stall >= 1
            todo = []
            for c in L["cards"]:
                if any(abs((c[0] + c[1]) / 2 - (d0 + d1) / 2) < 25 or (d0 - 5 <= c[0] and c[1] <= d1 + 5)
                       for d0, d1 in done):
                    continue
                full = c[0] > L["top"] + 2 and c[1] < L["bottom"] - 2
                tall = c[1] - c[0] > (L["bottom"] - L["top"]) * 0.6 and c[0] > L["top"] + 2
                cut_top = c[0] <= L["top"] + 2 and c[1] - L["top"] > 40     # листаем только вниз - целиком уже не покажется
                if full or tall or cut_top or last_pass:
                    todo.append(c)
            for c in todo:
                url = self.copy_link(img, c)
                done.append([c[0], c[1]])
                if url:
                    page_links.append(url)
                    mid = LINK_RE.search(url).group(3)
                    if in_range(url, self.t0, self.t1):
                        self.links[mid] = url
                    else:
                        self.skipped += 1
                self.progress(page_no, len(self.links), self.skipped, L["total"])
                img = self.shot()                       # после меню кадр мог чуть сдвинуться
            if last_pass:
                break
            # прокрутка вниз
            cx, cy = (L["x_col"] + L["cards"][0][2]) // 2 if L["cards"] else L["x_col"] + 150, (L["top"] + L["bottom"]) // 2
            a = cv2.cvtColor(img[L["top"]:L["bottom"], L["x_col"]:], cv2.COLOR_BGR2GRAY)
            self.check()
            self.move(*self.scr(cx, cy))
            time.sleep(0.05)
            self.inp.wheel(self.notches)
            time.sleep(0.45)
            img2 = self.shot()
            b = cv2.cvtColor(img2[L["top"]:L["bottom"], L["x_col"]:], cv2.COLOR_BGR2GRAY)
            if np.abs(a.astype(int) - b.astype(int)).mean() < 0.8:
                stall += 1                              # список не сдвинулся - низ страницы
                continue
            stall = 0
            dy = shift_y(a, b)
            # сверка: верхние края прежних карточек должны уехать ровно на dy
            new = find_cards(img2, L["hx"], L["top"], L["bottom"], L["x_col"])
            olds = [c[0] for c in L["cards"] if c[0] > L["top"] + 2]
            news = [c[0] for c in new if c[0] > L["top"] + 2]
            ok = dy is not None and 0 <= dy < (L["bottom"] - L["top"]) and (
                not olds or not news or any(abs(o - dy - n) <= 4 for o in olds for n in news))
            if not ok:
                done = []                               # не сошлось - лучше переснять лишний раз, чем пропустить
                continue
            if dy >= 3:
                # шаг колёсика - примерно на 40% панели; px на щелчок учим только по полным прокруткам
                # (у низа страницы список сдвигается меньше - по такому шаг раздуло бы до 10 щелчков)
                per = dy / self.notches
                if self.per is None or abs(per - self.per) < 0.35 * self.per:
                    self.per = per if self.per is None else 0.7 * self.per + 0.3 * per
                    want = int(min(6, max(1, round(0.4 * (L["bottom"] - L["top"]) / self.per))))
                    self.notches += (want > self.notches) - (want < self.notches)
                for d in done:
                    d[0] -= dy
                    d[1] -= dy
                done = [d for d in done if d[1] > L["top"] - 400]
        # список отсортирован «сначала новые» и вся страница старше начала периода - дальше смотреть незачем
        if page_links and self.t0 is not None:
            ts = [msg_time(u) for u in page_links]
            if ts[0] >= ts[-1] and max(ts) < self.t0:
                self.log("Дальше сообщения старше начала периода - остановился")
                return "stop"
        return None


# ====================================================================== окно
def open_window(parent, cfg, save_cfg):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import theme as T
    d = cfg.setdefault("ds_links", {"static": "", "from": "", "to": ""})
    w = tk.Toplevel(parent)
    w.title("Ссылки из Discord")
    w.configure(bg=T.BG)
    try:
        T.dark_titlebar(w)
    except Exception:
        pass
    pad = {"padx": 8, "pady": 4}
    top = ttk.LabelFrame(w, text="Что собрать")
    top.pack(fill="x", padx=10, pady=(10, 4))
    st_var, f_var, t_var = tk.StringVar(value=d["static"]), tk.StringVar(value=d["from"]), tk.StringVar(value=d["to"])
    ttk.Label(top, text="Статик:").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(top, textvariable=st_var, width=12).grid(row=0, column=1, sticky="w", **pad)
    ttk.Label(top, text="Даты с:").grid(row=0, column=2, sticky="e", **pad)
    ttk.Entry(top, textvariable=f_var, width=12).grid(row=0, column=3, sticky="w", **pad)
    ttk.Label(top, text="по:").grid(row=0, column=4, sticky="e", **pad)
    ttk.Entry(top, textvariable=t_var, width=12).grid(row=0, column=5, sticky="w", **pad)
    ttk.Label(top, text="ДД.ММ.ГГГГ, пусто - без ограничения", style="Muted.TLabel").grid(row=0, column=6, sticky="w", **pad)
    q_var = tk.StringVar()
    qr = ttk.Frame(top)
    qr.grid(row=1, column=0, columnspan=7, sticky="w", **pad)
    ttk.Label(qr, text="Запрос для поиска Discord:").pack(side="left")
    ttk.Entry(qr, textvariable=q_var, width=44, state="readonly").pack(side="left", padx=6)
    ttk.Button(qr, text="Копировать", command=lambda: copy_text(q_var.get(), "Запрос скопирован")).pack(side="left")
    ttk.Label(top, justify="left", wraplength=760, style="Muted.TLabel", text=(
        "1) В Discord открой нужный канал, вставь запрос в поиск (Ctrl+V) и нажми Enter.\n"
        "2) Нажми «Собрать» - бот сам пройдёт все страницы результатов и скопирует ссылку каждого сообщения.\n"
        "3) Упоминания @: в поиске Discord выбери «mentions:» (упоминает) и себя, Enter - и снова «Собрать». "
        "Ссылки добавятся к списку без повторов.\n"
        "Остановить - просто пошевели мышью. Пока бот работает, мышь и клавиатуру не трогай.")).grid(
        row=2, column=0, columnspan=7, sticky="w", **pad)
    bar = ttk.Frame(w)
    bar.pack(fill="x", padx=10, pady=4)
    run_btn = ttk.Button(bar, text="Собрать", style="Accent.TButton", command=lambda: start())
    run_btn.pack(side="left")
    ttk.Button(bar, text="Очистить список", command=lambda: clear()).pack(side="left", padx=6)
    ttk.Button(bar, text="Копировать все", command=lambda: copy_text("\n".join(cur_links()), "Ссылки скопированы")).pack(side="right")
    ttk.Button(bar, text="Сохранить .txt…", command=lambda: save_txt()).pack(side="right", padx=6)
    stat = tk.StringVar(value="Список пуст")
    ttk.Label(w, textvariable=stat, style="Status.TLabel").pack(anchor="w", padx=12)
    box = tk.Text(w, height=16, width=96, font=("Consolas", 9), bg=T.FIELD, fg=T.TEXT, insertbackground=T.TEXT,
                  relief="flat", highlightthickness=1, highlightbackground=T.BORDER)
    box.pack(fill="both", expand=True, padx=10, pady=(4, 10))
    links = {u for u in d.get("last", []) if LINK_RE.search(u)}
    state = {"col": None, "q": None}

    def store():
        d.update(static=st_var.get().strip(), **{"from": f_var.get().strip(), "to": t_var.get().strip()})
        save_cfg(cfg)
        q_var.set(query_hint(st_var.get(), f_var.get(), t_var.get()))

    def cur_links():
        t0, t1 = parse_date(f_var.get()), parse_date(t_var.get(), end=True)
        return [u for u in sort_links(links) if in_range(u, t0, t1)]

    def show(extra=""):
        box.delete("1.0", "end")
        cl = cur_links()
        box.insert("1.0", "\n".join(cl))
        stat.set((f"Ссылок: {len(cl)}" if cl else "Список пуст") + (f"  ·  {extra}" if extra else ""))

    def copy_text(text, what):
        w.clipboard_clear()
        w.clipboard_append(text)
        stat.set(what)

    def clear():
        links.clear()
        d["last"] = []
        save_cfg(cfg)
        show()

    def save_txt():
        cl = cur_links()
        if not cl:
            stat.set("Нечего сохранять")
            return
        name = "ссылки_" + (st_var.get().strip() or "discord") + \
               (f"_{f_var.get().strip()}-{t_var.get().strip()}" if f_var.get().strip() or t_var.get().strip() else "") + ".txt"
        p = filedialog.asksaveasfilename(parent=w, defaultextension=".txt", initialfile=name.replace("/", "."),
                                         filetypes=[("Текст", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(cl) + "\n")
            stat.set(f"Сохранено: {p}")

    def start():
        if state["col"] is not None:
            state["col"].stop.set()
            return
        store()
        hwnd = find_discord()
        if not hwnd:
            messagebox.showwarning("Discord", "Не нашёл окно Discord - открой его (приложение, не браузер).", parent=w)
            return
        import queue
        import antiafk as AF
        q = queue.Queue()
        state["q"] = q
        col = Collector(lambda m: q.put(("log", m)), lambda p, n, s, tot: q.put(("prog", (p, n, s, tot))),
                        parse_date(f_var.get()), parse_date(t_var.get(), end=True), AF.Input2())
        state["col"] = col
        run_btn.configure(text="Стоп")
        for x in (parent.winfo_toplevel(), w):           # свернуть: окна программы не должны закрывать Discord
            try:
                x.iconify()
            except tk.TclError:
                pass

        def work():
            try:
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            try:
                col.run(hwnd)
                q.put(("done", "Готово"))
            except Stopped:
                q.put(("done", "Остановлено"))
            except Exception as ex:
                q.put(("done", f"Ошибка: {ex}"))
        threading.Thread(target=work, daemon=True).start()
        poll()

    def poll():
        col, q = state["col"], state["q"]
        try:
            while True:
                kind, v = q.get_nowait()
                if kind == "prog":
                    p, n, s, tot = v
                    links.update(col.links.values())
                    stat.set(f"Страница {p}  ·  ссылок {len(links)}" + (f"  ·  вне дат {s}" if s else "") +
                             (f"  ·  в поиске {tot}" if tot else ""))
                elif kind == "log":
                    stat.set(v)
                elif kind == "done":
                    links.update(col.links.values())
                    d["last"] = sort_links(links)
                    save_cfg(cfg)
                    state["col"] = None
                    run_btn.configure(text="Собрать")
                    for x in (parent.winfo_toplevel(), w):
                        try:
                            x.deiconify()
                        except tk.TclError:
                            pass
                    w.lift()
                    show(f"{v}: добавлено {len(col.links)}" + (f", вне дат {col.skipped}" if col.skipped else ""))
                    return
        except Exception:
            pass
        w.after(200, poll)

    for var in (st_var, f_var, t_var):
        var.trace_add("write", lambda *a: (q_var.set(query_hint(st_var.get(), f_var.get(), t_var.get()))))
    q_var.set(query_hint(st_var.get(), f_var.get(), t_var.get()))
    show()

    def close():
        if state["col"] is not None:
            state["col"].stop.set()
        store()
        w.destroy()
    w.protocol("WM_DELETE_WINDOW", close)
    return w
