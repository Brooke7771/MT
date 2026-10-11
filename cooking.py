# -*- coding: utf-8 -*-
"""Вкладка "Готовка": таблица и калькулятор блюд, таймеры, уведомления в Telegram."""
import os
import sys
import json
import time
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request

import net

from cooking_data import (RECIPES, BUFFS, IMG_BASE, PREMIUM_MULT, PREMIUM100_MULT, MULTI_MAX,
                          MULTI_TIME, ACCEL_CUT, ACCEL_GAP_MIN)


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "cooking_config.json")     # тут лежит токен - не публикуй
TIMERS_PATH = os.path.join(app_dir(), "cooking_timers.json")
CACHE_DIR = os.path.join(app_dir(), "cache", "img")   # картинки блюд качаются при первом показе


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


# ================================================================ расчёты
def money(x):
    return f"{int(round(x)):,}".replace(",", " ")


def fmt_minutes(m):
    m = int(round(m))
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h} ч {mm} мин"
    return f"{h} ч" if h else f"{mm} мин"


def fmt_left(sec):
    sec = max(0, int(sec))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    return f"{m} мин {s:02d} с"


def price_mult(st):
    """Множитель цены: качество блюда, премиум, бонус за 100 дней премиума."""
    k = st["quality"] / 100
    if st["premium"]:
        k *= PREMIUM_MULT * (PREMIUM100_MULT if st["premium100"] else 1)
    return k


def price(r, st):
    k = price_mult(st)
    return r["std"][0] * k, r["std"][1] * k


def multi_minutes(r, batch, kitchen):
    """Время одной загрузки мультиготовки на batch готовок."""
    if batch <= 1:
        return r["minutes"]
    return r["minutes"] * batch * MULTI_TIME.get(kitchen, 1.0)


def max_accels(minutes):
    """Сколько ускорителей вообще можно применить к готовке такой длины."""
    if minutes <= 0:
        return 0
    left, n = minutes * ACCEL_CUT, 1
    while left > ACCEL_GAP_MIN:
        left = (left - ACCEL_GAP_MIN) * ACCEL_CUT
        n += 1
    return n


def accel_minutes(minutes, n):
    """Время с n ускорителями: первый сразу -15%, каждый следующий через час ещё -15% от остатка."""
    n = min(n, max_accels(minutes))
    if n <= 0:
        return minutes
    left, passed = minutes * ACCEL_CUT, 0
    for _ in range(n - 1):
        passed += ACCEL_GAP_MIN
        left = max(0.0, left - ACCEL_GAP_MIN) * ACCEL_CUT
    return passed + left


def ingredient_cost(r, costs):
    return sum(ing[1] * costs.get(ing[0], 0) for ing in r["ingredients"])


def per_hour(r, st, costs):
    lo, hi = price(r, st)
    hours = r["minutes"] / 60
    profit = r["yield"] * (lo + hi) / 2 - ingredient_cost(r, costs)
    return profit / hours, r["exp"] / hours


def calc(r, runs, batch, accels, st, costs):
    """runs загрузок по batch готовок, с accels ускорителями на каждую загрузку."""
    one = accel_minutes(multi_minutes(r, batch, st["kitchen"]), accels)
    cooks = runs * batch
    lo, hi = price(r, st)
    portions = r["yield"] * cooks
    ings = [(ing[0], ing[1] * cooks, ing[2], costs.get(ing[0])) for ing in r["ingredients"]]
    cost = sum(q * (c or 0) for _, q, _, c in ings)
    return {"portions": portions, "minutes_one": one, "minutes": one * runs,
            "exp": r["exp"] * cooks, "income": (portions * lo, portions * hi),
            "cost": cost, "ingredients": ings,
            "unknown_cost": any(c is None for *_, c in ings)}


# ================================================================ Telegram
TG_API = "https://api.telegram.org"


def tg_api(token, method, params=None, timeout=15):
    url = f"{TG_API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "MajesticTools"})
    try:
        with net.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except Exception:
            raise RuntimeError(f"Telegram ответил {e.code}")
    if not res.get("ok"):
        desc = res.get("description", "ошибка Telegram")
        if "Unauthorized" in desc:
            desc = "неверный токен бота"
        elif "chat not found" in desc:
            desc = "чат не найден - напиши боту любое сообщение"
        raise RuntimeError(desc)
    return res["result"]


def tg_find_chat(token):
    """Ищет твой чат по последнему сообщению боту. -> (chat_id, имя)."""
    updates = tg_api(token, "getUpdates")
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message")
        if msg and "chat" in msg:
            ch = msg["chat"]
            return ch["id"], ch.get("first_name") or ch.get("title") or str(ch["id"])
    raise RuntimeError("бот не видит сообщений: напиши ему в Telegram любое слово и нажми ещё раз")


def tg_send(token, chat_id, text):
    tg_api(token, "sendMessage", {"chat_id": chat_id, "text": text})


def beep():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass


# ================================================================ картинки
class ImageCache:
    """Картинки блюд и ингредиентов. Качаются с files.fletcher-wiki.com при первом показе
    и кэшируются у тебя на компьютере (в репозиторий они не кладутся)."""

    def __init__(self, ui_queue):
        try:
            from PIL import Image, ImageDraw, ImageTk   # noqa: F401
            self.pil = True
        except Exception:
            self.pil = False
        self.photos, self.waiting, self.placeholders = {}, {}, {}
        self.inflight, self.failed = set(), set()
        self.jobs = queue.Queue()
        self.ui = ui_queue
        os.makedirs(CACHE_DIR, exist_ok=True)
        if self.pil:
            for _ in range(3):
                threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def path(img):
        return os.path.join(CACHE_DIR, img.replace("/", "_"))

    def _worker(self):
        while True:
            img = self.jobs.get()
            try:
                req = urllib.request.Request(IMG_BASE + img, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                    "Referer": "https://fletcher-wiki.com/"})
                with net.urlopen(req, timeout=20) as r:
                    data = r.read()
                tmp = self.path(img) + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, self.path(img))
                self.ui.put(("img", img, True))
            except Exception:
                self.ui.put(("img", img, False))

    def _load(self, img, size):
        from PIL import Image, ImageTk
        im = Image.open(self.path(img)).convert("RGBA")
        im.thumbnail((size, size), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
        return ImageTk.PhotoImage(canvas)

    def placeholder(self, size, letter, bg):
        key = (size, letter, bg)
        if key not in self.placeholders:
            from PIL import Image, ImageDraw, ImageTk
            im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle((1, 1, size - 2, size - 2), radius=max(3, size // 6), fill=bg)
            font = None
            from PIL import ImageFont
            for name in ("segoeuib.ttf", "segoeui.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
                try:
                    font = ImageFont.truetype(name, max(8, size // 2))
                    break
                except Exception:
                    continue
            if font is not None:
                d.text((size // 2, size // 2), letter, fill="#6c6c78", anchor="mm", font=font)
            self.placeholders[key] = ImageTk.PhotoImage(im)
        return self.placeholders[key]

    def set(self, label, img, size, name, bg="#22222a"):
        if not self.pil:
            label.configure(text=(name or "?")[:1], width=2)
            return
        key = (img, size)
        if key in self.photos:
            label.configure(image=self.photos[key])
            return
        label.configure(image=self.placeholder(size, (name or "?")[:1].upper(), bg))
        if not img or img in self.failed:
            return
        if os.path.exists(self.path(img)):
            try:
                self.photos[key] = self._load(img, size)
                label.configure(image=self.photos[key])
            except Exception:
                self.failed.add(img)
            return
        self.waiting.setdefault(img, []).append((label, size))
        if img not in self.inflight:
            self.inflight.add(img)
            self.jobs.put(img)

    def arrived(self, img, ok):
        self.inflight.discard(img)
        if not ok:
            self.failed.add(img)
        for label, size in self.waiting.pop(img, []):
            if not ok:
                continue
            try:
                key = (img, size)
                if key not in self.photos:
                    self.photos[key] = self._load(img, size)
                label.configure(image=self.photos[key])
            except Exception:
                pass


# ================================================================ окно
SORTS = ["Как на сайте", "Доход в час", "Опыт в час", "Навык", "Время готовки", "Название"]
PAGE_SIZE = 12
CARD_W = 310
SPEEDUP_GAP = 3600          # следующее ускорение - через час после предыдущего
SPEEDUP_CUT = 0.15          # ускорение срезает 15% оставшегося времени


def build_cooking_tab(parent):
    import tkinter as tk
    from tkinter import ttk, simpledialog
    import theme as T

    CARD = "#16161b"
    CARD2 = "#1c1c22"
    CHIP = "#2a0f15"
    F = "Segoe UI"

    cfg = load_json(CONFIG_PATH, {})
    for k, v in {"token": "", "chat_id": None, "chat_name": "", "remind": True, "remind_min": 5,
                 "remind_speedup": True, "premium": False, "premium100": False, "quality": 100,
                 "skill": 9, "kitchen": 5, "faculty": True, "costs": {}, "label": "Плита 1"}.items():
        cfg.setdefault(k, v)
    timers = load_json(TIMERS_PATH, [])
    for t in timers:
        for k, v in {"img": None, "base": t.get("end", 0) - t.get("start", 0), "last_speedup": None,
                     "speedups": 0, "saved": 0.0, "remind_speedup": cfg["remind_speedup"],
                     "speedup_key": None}.items():
            t.setdefault(k, v)
    ui = queue.Queue()
    images = ImageCache(ui)
    alive = {"on": True}
    st = {"page": 1, "items": [], "batch": {}, "expanded": set()}

    def persist_cfg():
        save_json(CONFIG_PATH, cfg)

    def persist_timers():
        save_json(TIMERS_PATH, timers)

    def lbl(master, text="", fg=T.TEXT, bg=CARD, size=10, bold=False, **kw):
        return tk.Label(master, text=text, fg=fg, bg=bg, font=(F, size, "bold" if bold else "normal"), **kw)

    def flat_btn(master, text, cmd, accent=False, **kw):
        b = tk.Button(master, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                      bg=T.ACCENT if accent else "#24242b", fg="#ffffff" if accent else T.TEXT,
                      activebackground=T.ACCENT_HOVER if accent else "#2e2e36", activeforeground="#ffffff",
                      font=(F, 9 if not accent else 10, "bold"), padx=10, pady=5, **kw)
        return b

    def ivar(v, default):
        try:
            return int(v.get())
        except Exception:
            return default

    # ---------------------------------------------------------------- фильтры
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(1, weight=1)
    top = ttk.Frame(parent)
    top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
    search_var = tk.StringVar()
    skill_var = tk.IntVar(value=cfg["skill"])
    kitchen_var = tk.IntVar(value=cfg["kitchen"])
    faculty_var = tk.BooleanVar(value=cfg["faculty"])
    premium_var = tk.BooleanVar(value=cfg["premium"])
    prem100_var = tk.BooleanVar(value=cfg["premium100"])
    quality_var = tk.IntVar(value=cfg["quality"])
    sort_var = tk.StringVar(value=SORTS[0])

    def on_filter(*_):
        st["page"] = 1
        refresh()

    row1 = ttk.Frame(top)
    row1.pack(fill="x")
    ttk.Label(row1, text="Поиск:").pack(side="left")
    ttk.Entry(row1, textvariable=search_var, width=18).pack(side="left", padx=(4, 12))
    ttk.Label(row1, text="Навык:").pack(side="left")
    ttk.Spinbox(row1, from_=1, to=9, width=3, textvariable=skill_var, command=on_filter).pack(side="left", padx=(4, 10))
    ttk.Label(row1, text="Кухня:").pack(side="left")
    ttk.Spinbox(row1, from_=1, to=5, width=3, textvariable=kitchen_var, command=on_filter).pack(side="left", padx=(4, 10))
    ttk.Checkbutton(row1, text="Есть факультет", variable=faculty_var, command=on_filter).pack(side="left", padx=(0, 10))
    ttk.Label(row1, text="Сортировка:").pack(side="left")
    cb = ttk.Combobox(row1, textvariable=sort_var, values=SORTS, state="readonly", width=14)
    cb.pack(side="left", padx=4)
    cb.bind("<<ComboboxSelected>>", on_filter)
    search_var.trace_add("write", on_filter)
    ttk.Button(row1, text="Цены ингредиентов", command=lambda: open_costs()).pack(side="right")

    row2 = ttk.Frame(top)
    row2.pack(fill="x", pady=(6, 0))
    ttk.Label(row2, text="Качество блюда, %:").pack(side="left")
    ttk.Spinbox(row2, from_=0, to=100, increment=5, width=4, textvariable=quality_var,
                command=lambda: refresh()).pack(side="left", padx=(4, 12))
    ttk.Checkbutton(row2, text="Активный Premium (+25%)", variable=premium_var,
                    command=lambda: refresh()).pack(side="left", padx=(0, 10))
    ttk.Checkbutton(row2, text="Бонус за 100 дней Premium (+5.2%)", variable=prem100_var,
                    command=lambda: refresh()).pack(side="left")

    # ---------------------------------------------------------------- сетка карточек
    left = ttk.Frame(parent)
    left.grid(row=1, column=0, sticky="nsew")
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)
    canvas = tk.Canvas(left, bg=T.BG, highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    grid_frame = tk.Frame(canvas, bg=T.BG)
    win = canvas.create_window(0, 0, window=grid_frame, anchor="nw")
    grid_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    pager = ttk.Frame(left)
    pager.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
    cards = []

    def layout_cards(width=None):
        width = width or canvas.winfo_width()
        if width < 50:
            return
        grid_frame.update_idletasks()
        cw = max([c.winfo_reqwidth() for c in cards] + [CARD_W])
        cols = max(1, (width + 10) // (cw + 10))
        for col in range(8):
            grid_frame.columnconfigure(col, minsize=cw if col < cols else 0,
                                       uniform="card" if col < cols else "", weight=0)
        for n, c in enumerate(cards):
            c.grid(row=n // cols, column=n % cols, padx=5, pady=5, sticky="nsew")
        canvas.itemconfigure(win, width=max(width, cols * (cw + 10)))

    canvas.bind("<Configure>", lambda e: layout_cards(e.width))

    def wheel_for(cv):
        def on_wheel(e):
            cv.yview_scroll(int(-e.delta / 120) if e.delta else (1 if e.num == 5 else -1), "units")
        return on_wheel

    def bind_wheel(widget, cv):
        widget.bind("<Enter>", lambda e: (cv.bind_all("<MouseWheel>", wheel_for(cv)),
                                          cv.bind_all("<Button-4>", wheel_for(cv)),
                                          cv.bind_all("<Button-5>", wheel_for(cv))))
        widget.bind("<Leave>", lambda e: (cv.unbind_all("<MouseWheel>"), cv.unbind_all("<Button-4>"),
                                          cv.unbind_all("<Button-5>")))

    bind_wheel(left, canvas)

    # ---------------------------------------------------------------- правая панель: таймеры
    side = ttk.Frame(parent, width=390)
    side.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
    side.grid_propagate(False)
    side.rowconfigure(2, weight=1)
    side.columnconfigure(0, weight=1)
    ttk.Label(side, text="Таймеры", font=(F, 13, "bold")).grid(row=0, column=0, sticky="w")
    add_row = ttk.Frame(side)
    add_row.grid(row=1, column=0, sticky="ew", pady=(4, 6))
    label_var = tk.StringVar(value=cfg["label"])
    custom_min = tk.IntVar(value=60)
    ttk.Label(add_row, text="Метка:").pack(side="left")
    ttk.Entry(add_row, textvariable=label_var, width=10).pack(side="left", padx=(4, 8))
    ttk.Label(add_row, text="Свой:").pack(side="left")
    ttk.Spinbox(add_row, from_=1, to=5000, width=5, textvariable=custom_min).pack(side="left", padx=4)
    ttk.Label(add_row, text="мин").pack(side="left")
    ttk.Button(add_row, text="+", width=3, command=lambda: add_custom_timer()).pack(side="left", padx=4)

    tcanvas = tk.Canvas(side, bg=T.BG, highlightthickness=0, bd=0)
    tcanvas.grid(row=2, column=0, sticky="nsew")
    tlist = tk.Frame(tcanvas, bg=T.BG)
    twin = tcanvas.create_window(0, 0, window=tlist, anchor="nw")
    tlist.bind("<Configure>", lambda e: tcanvas.configure(scrollregion=tcanvas.bbox("all")))
    tcanvas.bind("<Configure>", lambda e: tcanvas.itemconfigure(twin, width=e.width))
    bind_wheel(tcanvas, tcanvas)
    timer_widgets = {}

    # ---- Telegram
    tgf = ttk.LabelFrame(side, text="Уведомления в Telegram")
    tgf.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    token_var = tk.StringVar(value=cfg["token"])
    ttk.Label(tgf, text="Токен бота:").grid(row=0, column=0, sticky="w")
    ttk.Entry(tgf, textvariable=token_var, width=26, show="•").grid(row=0, column=1, columnspan=2, sticky="ew", padx=4)
    tg_status = tk.StringVar(value=(f"Подключено: {cfg['chat_name']}" if cfg["chat_id"] else "Не подключено"))
    ttk.Button(tgf, text="Подключить", command=lambda: tg_connect()).grid(row=1, column=0, sticky="w", pady=4)
    ttk.Button(tgf, text="Тест", command=lambda: tg_test()).grid(row=1, column=1, sticky="w", padx=4)
    ttk.Label(tgf, textvariable=tg_status, style="Muted.TLabel", wraplength=340).grid(row=2, column=0, columnspan=3, sticky="w")
    remind_var = tk.BooleanVar(value=cfg["remind"])
    remind_min = tk.IntVar(value=cfg["remind_min"])
    speed_var = tk.BooleanVar(value=cfg["remind_speedup"])
    rr = ttk.Frame(tgf)
    rr.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Checkbutton(rr, text="Напоминать за", variable=remind_var, command=lambda: save_settings()).pack(side="left")
    ttk.Spinbox(rr, from_=1, to=120, width=4, textvariable=remind_min, command=lambda: save_settings()).pack(side="left", padx=4)
    ttk.Label(rr, text="мин до готовности").pack(side="left")
    ttk.Checkbutton(tgf, text="Напоминать, когда можно ускорить (раз в час)", variable=speed_var,
                    command=lambda: save_settings()).grid(row=4, column=0, columnspan=3, sticky="w")

    status_var = tk.StringVar()
    ttk.Label(parent, textvariable=status_var, style="Muted.TLabel").grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ================================================================ данные
    def save_settings():
        cfg.update(skill=min(9, max(1, ivar(skill_var, 9))), kitchen=min(5, max(1, ivar(kitchen_var, 5))),
                   faculty=faculty_var.get(), premium=premium_var.get(), premium100=prem100_var.get(),
                   quality=min(100, max(0, ivar(quality_var, 100))), remind=remind_var.get(),
                   remind_min=max(1, ivar(remind_min, 5)), remind_speedup=speed_var.get(),
                   token=token_var.get().strip(), label=label_var.get().strip() or "Плита 1")
        persist_cfg()

    def filtered():
        q = search_var.get().strip().lower()
        costs = cfg["costs"]
        items = []
        for i, r in enumerate(RECIPES):
            if q and q not in r["name"].lower() and not any(q in ing[0].lower() for ing in r["ingredients"]):
                continue
            if r["skill"] > cfg["skill"] or r["kitchen"] > cfg["kitchen"]:
                continue
            if r["faculty"] and not cfg["faculty"]:
                continue
            items.append(i)
        key = sort_var.get()
        if key == "Доход в час":
            items.sort(key=lambda i: -per_hour(RECIPES[i], cfg, costs)[0])
        elif key == "Опыт в час":
            items.sort(key=lambda i: -per_hour(RECIPES[i], cfg, costs)[1])
        elif key == "Навык":
            items.sort(key=lambda i: (RECIPES[i]["skill"], RECIPES[i]["kitchen"]))
        elif key == "Время готовки":
            items.sort(key=lambda i: RECIPES[i]["minutes"])
        elif key == "Название":
            items.sort(key=lambda i: RECIPES[i]["name"])
        return items

    # ================================================================ карточка блюда
    def section(card, title, aside=""):
        box = tk.Frame(card, bg=CARD2)
        box.pack(fill="x", padx=8, pady=(6, 0))
        head = tk.Frame(box, bg=CARD2)
        head.pack(fill="x", padx=8, pady=(6, 2))
        lbl(head, title, fg=T.MUTED, bg=CARD2, size=9, bold=True).pack(side="left")
        if aside:
            lbl(head, aside, fg=T.MUTED, bg=CARD2, size=9).pack(side="right")
        return box

    def chip(master, text, fg=T.ACCENT, bg=CHIP):
        return tk.Label(master, text=text, fg=fg, bg=bg, font=(F, 8, "bold"), padx=6, pady=2)

    def make_card(i):
        r = RECIPES[i]
        card = tk.Frame(grid_frame, bg=CARD, width=CARD_W, highlightthickness=1,
                        highlightbackground=T.BORDER, highlightcolor=T.BORDER)
        card.bind("<Enter>", lambda e: card.configure(highlightbackground=T.ACCENT))
        card.bind("<Leave>", lambda e: card.configure(highlightbackground=T.BORDER))

        # шапка: картинка, название, способы
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=8, pady=(8, 0))
        pic = tk.Label(head, bg=CARD)
        pic.pack(side="left", anchor="n")
        images.set(pic, r.get("img"), 76, r["name"])
        right = tk.Frame(head, bg=CARD)
        right.pack(side="left", fill="x", expand=True, padx=(8, 0))
        lbl(right, r["name"], size=11, bold=True, wraplength=CARD_W - 110, justify="left").pack(anchor="w")
        tags = tk.Frame(right, bg=CARD)
        tags.pack(anchor="w", pady=(4, 0))
        for n, m in enumerate(r["methods"].split(", ")):
            chip(tags, m).grid(row=n // 2, column=n % 2, padx=(0, 4), pady=2, sticky="w")

        # описание: требования, факультет, баффы
        info = tk.Frame(card, bg=CARD)
        info.pack(fill="x", padx=10, pady=(6, 0))
        lbl(info, f"Ранг {r['skill']}+  ·  кухня {r['kitchen']}+", fg=T.MUTED, size=9).pack(side="left")
        if r["faculty"]:
            chip(info, "Нужен факультет").pack(side="right")
        else:
            chip(info, "Факультет не нужен", fg=T.MUTED, bg="#202027").pack(side="right")
        for b in r["buffs"]:
            title, desc = BUFFS.get(b, (b, ""))
            lbl(card, f"✦ {title}: {desc}", fg="#d9a14a", size=8, wraplength=CARD_W - 24,
                justify="left").pack(anchor="w", padx=10, pady=(4, 0))
        if r.get("bar_anywhere"):
            lbl(card, "Можно продать любому бармену в городе", fg="#d9a14a", size=8).pack(anchor="w", padx=10, pady=(4, 0))

        # ингредиенты
        ings = r["ingredients"]
        box = section(card, "Ингредиенты", str(len(ings)))
        show = ings if i in st["expanded"] else ings[:3]
        for name, qty, src, img in show:
            row = tk.Frame(box, bg=CARD2)
            row.pack(fill="x", padx=6, pady=1)
            p = tk.Label(row, bg=CARD2)
            p.pack(side="left")
            images.set(p, img, 26, name, bg="#2a2a32")
            txt = tk.Frame(row, bg=CARD2)
            txt.pack(side="left", padx=6)
            lbl(txt, name, bg=CARD2, size=9, bold=True, wraplength=CARD_W - 120, justify="left").pack(anchor="w")
            cost = cfg["costs"].get(name)
            src_short = (src or "—").split(", ")[0] + (" и др." if src and ", " in src else "")
            sub = src_short + (f"  ·  {money(cost)} $" if cost is not None else "")
            lbl(txt, sub, fg=T.MUTED, bg=CARD2, size=8, wraplength=CARD_W - 120, justify="left").pack(anchor="w")
            lbl(row, f"x{qty}", bg=CARD2, size=10, bold=True).pack(side="right", padx=4)
        if len(ings) > 3:
            more = lbl(box, ("Свернуть ▴" if i in st["expanded"] else f"Ещё {len(ings) - 3} ▾"),
                       fg=T.MUTED, bg=CARD2, size=9, cursor="hand2")
            more.pack(pady=(2, 4))
            more.bind("<Button-1>", lambda e: toggle_expand(i))
        else:
            tk.Frame(box, bg=CARD2, height=4).pack()

        # цена
        lo, hi = price(r, cfg)
        box = section(card, "Цена в барах", "за порцию")
        k_std = cfg["quality"] / 100
        k_prem = k_std * PREMIUM_MULT * (PREMIUM100_MULT if cfg["premium100"] else 1)
        for title, k, color in (("Стандарт", k_std, T.TEXT), ("Premium", k_prem, "#ff8a98")):
            row = tk.Frame(box, bg=CARD2)
            row.pack(fill="x", padx=8, pady=1)
            lbl(row, title, fg=T.MUTED, bg=CARD2, size=9).pack(side="left")
            lbl(row, f"{money(r['std'][0] * k)} – {money(r['std'][1] * k)} $", fg=color, bg=CARD2,
                size=10, bold=True).pack(side="right")
        cost = ingredient_cost(r, cfg["costs"])
        if cost:
            lbl(box, f"Ингредиенты на готовку: {money(cost)} $", fg=T.MUTED, bg=CARD2, size=8).pack(anchor="w", padx=8)
        tk.Frame(box, bg=CARD2, height=4).pack()

        # время / мультиготовка
        mx = MULTI_MAX.get(cfg["kitchen"], 1)
        sel = min(st["batch"].get(i, 1), mx)
        box = section(card, "Время", "без ускорений")
        chips = tk.Frame(box, bg=CARD2)
        chips.pack(fill="x", padx=6, pady=(2, 6))
        for b in (1, 2, 3):
            ok = b <= mx
            on = b == sel
            c = tk.Frame(chips, bg=T.ACCENT_DIM if on else ("#24242b" if ok else "#1a1a1f"),
                         highlightthickness=1, highlightbackground=T.ACCENT if on else T.BORDER,
                         cursor="hand2" if ok else "")
            c.pack(side="left", expand=True, fill="x", padx=2)
            fg = "#ffffff" if on else (T.TEXT if ok else "#4a4a55")
            mins = multi_minutes(r, b, cfg["kitchen"])
            for txt, sz, bold in ((f"x{r['yield'] * b} шт", 9, True), (fmt_minutes(mins), 9, False),
                                  (f"+{r['exp'] * b} EXP", 8, True)):
                w = tk.Label(c, text=txt, bg=c["bg"], fg=fg if "EXP" not in txt or not ok else (T.OK if on else "#5fbf80"),
                             font=(F, sz, "bold" if bold else "normal"))
                w.pack()
                if ok:
                    w.bind("<Button-1>", lambda e, b=b: choose_batch(i, b))
            if ok:
                c.bind("<Button-1>", lambda e, b=b: choose_batch(i, b))
        if mx == 1:
            lbl(box, "Мультиготовка x2 с кухни 3 ур., x3 с кухни 5 ур.", fg=T.MUTED, bg=CARD2, size=8).pack(pady=(0, 4))

        flat_btn(card, "⏱  Поставить таймер", lambda: start_dish_timer(i), accent=True).pack(
            fill="x", padx=8, pady=8)
        return card

    def toggle_expand(i):
        st["expanded"].symmetric_difference_update({i})
        render_page(keep_scroll=True)

    def choose_batch(i, b):
        st["batch"][i] = b
        render_page(keep_scroll=True)

    def render_page(keep_scroll=False):
        y = canvas.yview()[0] if keep_scroll else 0
        for c in cards:
            c.destroy()
        cards.clear()
        items = st["items"]
        pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        st["page"] = min(st["page"], pages)
        chunk = items[(st["page"] - 1) * PAGE_SIZE: st["page"] * PAGE_SIZE]
        for i in chunk:
            cards.append(make_card(i))
        layout_cards()
        canvas.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.yview_moveto(y)
        # пагинация
        for w in pager.winfo_children():
            w.destroy()
        if pages > 1:
            def go(p):
                st["page"] = p
                render_page()
            ttk.Button(pager, text="‹", width=3, command=lambda: go(max(1, st["page"] - 1))).pack(side="left", padx=2)
            for p in range(1, pages + 1):
                ttk.Button(pager, text=str(p), width=3, style="Accent.TButton" if p == st["page"] else "TButton",
                           command=lambda p=p: go(p)).pack(side="left", padx=2)
            ttk.Button(pager, text="›", width=3, command=lambda: go(min(pages, st["page"] + 1))).pack(side="left", padx=2)
        ttk.Label(pager, text=f"Найдено блюд: {len(items)}", style="Muted.TLabel").pack(side="right", padx=6)

    def refresh():
        save_settings()
        st["items"] = filtered()
        render_page()

    # ================================================================ цены ингредиентов
    def open_costs():
        dlg = tk.Toplevel(parent)
        dlg.title("Цены ингредиентов")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        ttk.Label(dlg, text="Почём ты покупаешь ингредиенты (за 1 шт). Пустое поле - цена не указана.",
                  style="Muted.TLabel").pack(anchor="w", padx=10, pady=(10, 4))
        names = {}
        for r in RECIPES:
            for name, _, src, _ in r["ingredients"]:
                names.setdefault(name, src)
        tv = ttk.Treeview(dlg, columns=("n", "s", "p"), show="headings", height=16)
        for c, h, w in (("n", "Ингредиент", 200), ("s", "Где взять", 200), ("p", "Цена за 1", 90)):
            tv.heading(c, text=h)
            tv.column(c, width=w, anchor="e" if c == "p" else "w")
        tv.pack(fill="both", expand=True, padx=10)

        def fill():
            tv.delete(*tv.get_children())
            for n, (name, src) in enumerate(sorted(names.items())):
                c = cfg["costs"].get(name)
                tv.insert("", "end", iid=name, values=(name, src or "—", "" if c is None else money(c)),
                          tags=("odd",) if n % 2 else ())
            tv.tag_configure("odd", background=T.FIELD_ALT)
        fill()
        bottom = ttk.Frame(dlg)
        bottom.pack(fill="x", padx=10, pady=10)
        v = tk.StringVar()
        ttk.Label(bottom, text="Цена выбранного:").pack(side="left")
        e = ttk.Entry(bottom, textvariable=v, width=10)
        e.pack(side="left", padx=6)

        def on_sel(_=None):
            s_ = tv.selection()
            if s_:
                c = cfg["costs"].get(s_[0])
                v.set("" if c is None else str(c))
                e.focus_set()
        tv.bind("<<TreeviewSelect>>", on_sel)

        def save(_=None):
            s_ = tv.selection()
            if not s_:
                return
            txt = v.get().replace(" ", "").replace(",", ".")
            if txt == "":
                cfg["costs"].pop(s_[0], None)
            else:
                try:
                    cfg["costs"][s_[0]] = max(0.0, float(txt))
                except ValueError:
                    return
            persist_cfg()
            fill()
            tv.selection_set(s_[0])
            refresh()
        e.bind("<Return>", save)
        ttk.Button(bottom, text="Сохранить", style="Accent.TButton", command=save).pack(side="left")
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    # ================================================================ таймеры
    def new_id():
        return max([t["id"] for t in timers] + [0]) + 1

    def add_timer(name, img, minutes):
        now = time.time()
        save_settings()
        timers.append({"id": new_id(), "name": name, "label": cfg["label"], "img": img, "start": now,
                       "end": now + minutes * 60, "base": minutes * 60, "last_speedup": None,
                       "speedups": 0, "saved": 0.0, "remind_speedup": cfg["remind_speedup"],
                       "speedup_key": None, "reminded": False, "done": False})
        persist_timers()
        rebuild_timers()
        end = time.strftime("%H:%M", time.localtime(now + minutes * 60))
        notify(f"Таймер запущен: {name} ({cfg['label']}), готово в {end}, через {fmt_minutes(minutes)}",
               telegram=False)

    def start_dish_timer(i):
        r = RECIPES[i]
        b = min(st["batch"].get(i, 1), MULTI_MAX.get(cfg["kitchen"], 1))
        add_timer(f"{r['name']} x{r['yield'] * b}", r.get("img"), multi_minutes(r, b, cfg["kitchen"]))

    def add_custom_timer():
        add_timer("Свой таймер", None, max(1, ivar(custom_min, 60)))

    def speedup_ready_in(t, now):
        if t["last_speedup"] is None:
            return 0
        return max(0, t["last_speedup"] + SPEEDUP_GAP - now)

    def do_speedup(tid):
        t = next((x for x in timers if x["id"] == tid), None)
        now = time.time()
        if not t or t["done"] or speedup_ready_in(t, now) > 0:
            return
        cut = max(0.0, t["end"] - now) * SPEEDUP_CUT
        t["end"] -= cut
        t["saved"] += cut
        t["speedups"] += 1
        t["last_speedup"] = now
        t["reminded"] = False
        persist_timers()
        notify(f"Ускорение учтено: -{fmt_minutes(cut / 60)}, {t['name']}", telegram=False)
        update_timers()

    def correct_timer(tid):
        t = next((x for x in timers if x["id"] == tid), None)
        if not t:
            return
        left = max(0, int((t["end"] - time.time()) / 60))
        m = simpledialog.askinteger("Исправить время", "Сколько минут осталось по игре?",
                                    initialvalue=left, minvalue=0, maxvalue=100000, parent=parent)
        if m is None:
            return
        t["end"] = time.time() + m * 60
        t["done"] = m == 0 and t["done"]
        t["reminded"] = False
        persist_timers()
        rebuild_timers()

    def restart_timer(tid):
        t = next((x for x in timers if x["id"] == tid), None)
        if t:
            timers.remove(t)
            add_timer(t["name"], t.get("img"), t["base"] / 60)

    def remove_timer(tid):
        timers[:] = [t for t in timers if t["id"] != tid]
        persist_timers()
        rebuild_timers()

    def toggle_speed_remind(tid, var):
        for t in timers:
            if t["id"] == tid:
                t["remind_speedup"] = var.get()
        persist_timers()

    def rebuild_timers():
        for w in tlist.winfo_children():
            w.destroy()
        timer_widgets.clear()
        if not timers:
            lbl(tlist, "Таймеров нет. Нажми «Поставить таймер» на карточке блюда.", fg=T.MUTED, bg=T.BG,
                size=9, wraplength=340, justify="left").pack(anchor="w", pady=10)
            return
        for t in sorted(timers, key=lambda t: (t["done"], t["end"])):
            box = tk.Frame(tlist, bg=CARD, highlightthickness=1,
                           highlightbackground=T.OK if t["done"] else T.BORDER)
            box.pack(fill="x", pady=4)
            top_ = tk.Frame(box, bg=CARD)
            top_.pack(fill="x", padx=8, pady=(8, 2))
            pic = tk.Label(top_, bg=CARD)
            pic.pack(side="left")
            images.set(pic, t.get("img"), 40, t["name"])
            mid = tk.Frame(top_, bg=CARD)
            mid.pack(side="left", fill="x", expand=True, padx=8)
            lbl(mid, t["name"], size=10, bold=True, wraplength=200, justify="left").pack(anchor="w")
            lbl(mid, f"{t['label']}  ·  готово в " + time.strftime("%H:%M", time.localtime(t["end"])),
                fg=T.MUTED, size=8).pack(anchor="w")
            left_lbl = lbl(top_, "", size=12, bold=True)
            left_lbl.pack(side="right")
            bar = tk.Canvas(box, height=6, bg="#26262d", highlightthickness=0, bd=0)
            bar.pack(fill="x", padx=8, pady=(4, 2))
            fill_ = bar.create_rectangle(0, 0, 0, 6, fill=T.OK if t["done"] else T.ACCENT, width=0)
            info = lbl(box, "", fg=T.MUTED, size=8)
            info.pack(anchor="w", padx=8)
            btns = tk.Frame(box, bg=CARD)
            btns.pack(fill="x", padx=8, pady=(4, 8))
            sp = None
            if t["done"]:
                flat_btn(btns, "↻ Ещё раз", lambda tid=t["id"]: restart_timer(tid)).pack(side="left")
            else:
                sp = flat_btn(btns, "⚡ Ускорить", lambda tid=t["id"]: do_speedup(tid), accent=True)
                sp.pack(side="left")
                flat_btn(btns, "✎ Время", lambda tid=t["id"]: correct_timer(tid)).pack(side="left", padx=4)
                rv = tk.BooleanVar(value=t["remind_speedup"])
                tk.Checkbutton(btns, text="напоминать", variable=rv, bg=CARD, fg=T.MUTED, selectcolor=T.FIELD,
                               activebackground=CARD, activeforeground=T.TEXT, bd=0, highlightthickness=0,
                               font=(F, 8), command=lambda tid=t["id"], v=rv: toggle_speed_remind(tid, v)
                               ).pack(side="left", padx=4)
            flat_btn(btns, "✕", lambda tid=t["id"]: remove_timer(tid)).pack(side="right")
            timer_widgets[t["id"]] = (left_lbl, bar, fill_, info, sp)
        update_timers()

    def update_timers():
        now = time.time()
        for t in timers:
            w = timer_widgets.get(t["id"])
            if not w:
                continue
            left_lbl, bar, fill_, info, sp = w
            try:
                total = max(1.0, t["end"] - t["start"])
                frac = 1.0 if t["done"] else min(1.0, max(0.0, (now - t["start"]) / total))
                width = bar.winfo_width()
                bar.coords(fill_, 0, 0, width * frac, 6)
                if t["done"]:
                    left_lbl.configure(text="Готово ✓", fg=T.OK)
                else:
                    left_lbl.configure(text=fmt_left(t["end"] - now), fg=T.TEXT)
                parts = []
                if t["speedups"]:
                    parts.append(f"ускорений: {t['speedups']}, сэкономлено {fmt_minutes(t['saved'] / 60)}")
                if sp is not None:
                    wait = speedup_ready_in(t, now)
                    if wait > 0:
                        sp.configure(text=f"⚡ через {fmt_left(wait)}", state="disabled",
                                     bg="#24242b", disabledforeground=T.MUTED)
                    else:
                        sp.configure(text="⚡ Ускорить (-15%)", state="normal", bg=T.ACCENT)
                info.configure(text="  ·  ".join(parts))
            except tk.TclError:
                pass

    def notify(text, telegram=True):
        status_var.set(time.strftime("%H:%M ") + "".join(ch for ch in text if ord(ch) < 0x10000).strip())
        if telegram and cfg["token"] and cfg["chat_id"]:
            def work():
                try:
                    tg_send(cfg["token"], cfg["chat_id"], text)
                except Exception as ex:
                    ui.put(("status", f"Telegram: {ex}"))
            threading.Thread(target=work, daemon=True).start()

    def check_timers(first=False):
        now = time.time()
        changed = rebuild = False
        for t in timers:
            if t["done"]:
                continue
            if now >= t["end"]:
                t["done"] = True
                changed = rebuild = True
                late = " (пока программа была выключена)" if first and now - t["end"] > 60 else ""
                notify(f"🍳 Готово: {t['name']} ({t['label']}){late}")
                beep()
                continue
            if cfg["remind"] and not t["reminded"] and t["end"] - now <= cfg["remind_min"] * 60 \
                    and t["end"] - t["start"] > cfg["remind_min"] * 60:
                t["reminded"] = True
                changed = True
                notify(f"⏰ Через {cfg['remind_min']} мин будет готово: {t['name']} ({t['label']})")
            key = t["last_speedup"] or t["start"]
            if t["remind_speedup"] and now >= key + SPEEDUP_GAP and t["speedup_key"] != key \
                    and t["end"] - now > 60:
                t["speedup_key"] = key
                changed = True
                notify(f"⚡ Можно ускорить: {t['name']} ({t['label']})")
                beep()
        if changed:
            persist_timers()
        if rebuild:
            rebuild_timers()

    # ================================================================ Telegram
    def tg_connect():
        save_settings()
        token = cfg["token"]
        if not token:
            tg_status.set("Вставь токен от @BotFather")
            return
        tg_status.set("Ищу твой чат...")

        def work():
            try:
                chat_id, name = tg_find_chat(token)
                tg_send(token, chat_id, "✅ Majestic Tools подключён. Сюда будут приходить уведомления о готовке.")
                ui.put(("chat", (chat_id, name)))
            except Exception as ex:
                ui.put(("tg", f"Не получилось: {ex}"))
        threading.Thread(target=work, daemon=True).start()

    def tg_test():
        save_settings()
        if not (cfg["token"] and cfg["chat_id"]):
            tg_status.set("Сначала нажми «Подключить»")
            return

        def work():
            try:
                tg_send(cfg["token"], cfg["chat_id"], "🔔 Тестовое уведомление от Majestic Tools")
                ui.put(("tg", f"Тест отправлен: {cfg['chat_name']}"))
            except Exception as ex:
                ui.put(("tg", f"Ошибка: {ex}"))
        threading.Thread(target=work, daemon=True).start()

    # ================================================================ цикл
    def pump():
        if not alive["on"]:
            return
        try:
            while True:
                item = ui.get_nowait()
                kind = item[0]
                if kind == "img":
                    images.arrived(item[1], item[2])
                elif kind == "chat":
                    cfg["chat_id"], cfg["chat_name"] = item[1]
                    persist_cfg()
                    tg_status.set(f"Подключено: {item[1][1]}")
                elif kind == "tg":
                    tg_status.set(item[1])
                elif kind == "status":
                    status_var.set(item[1])
        except queue.Empty:
            pass
        except tk.TclError:
            return
        parent.after(150, pump)

    def tick():
        if not alive["on"]:
            return
        try:
            check_timers()
            update_timers()
        except tk.TclError:
            return
        parent.after(1000, tick)

    def stop():
        alive["on"] = False
        persist_timers()

    refresh()
    check_timers(first=True)
    rebuild_timers()
    parent.after(150, pump)
    parent.after(1000, tick)
    return stop


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    root.title("Готовка")
    try:
        import theme
        theme.apply(root)
    except Exception:
        pass
    stop = build_cooking_tab(root)
    root.mainloop()
    stop()
