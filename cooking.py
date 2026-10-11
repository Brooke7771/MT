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

from cooking_data import (RECIPES, PREMIUM_MULT, PREMIUM100_MULT, MULTI_MAX, MULTI_TIME,
                          ACCEL_CUT, ACCEL_GAP_MIN)


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "cooking_config.json")     # тут лежит токен - не публикуй
TIMERS_PATH = os.path.join(app_dir(), "cooking_timers.json")


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
    return sum(q * costs.get(n, 0) for n, q, _ in r["ingredients"])


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
    ings = [(n, q * cooks, src, costs.get(n)) for n, q, src in r["ingredients"]]
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


# ================================================================ окно
SORTS = ["Как на сайте", "Доход в час", "Опыт в час", "Навык", "Время готовки", "Название"]
BATCH_NAMES = {1: "x1 (обычная)", 2: "x2 (мультиготовка)", 3: "x3 (мультиготовка)"}


def build_cooking_tab(parent):
    import tkinter as tk
    from tkinter import ttk

    cfg = load_json(CONFIG_PATH, {})
    for k, v in {"token": "", "chat_id": None, "chat_name": "", "remind": True, "remind_min": 5,
                 "premium": False, "premium100": False, "quality": 100, "skill": 9,
                 "kitchen": 5, "faculty": True, "costs": {}}.items():
        cfg.setdefault(k, v)
    timers = load_json(TIMERS_PATH, [])
    ui = queue.Queue()
    alive = {"on": True}
    pad = {"padx": 6, "pady": 3}

    def persist_cfg():
        save_json(CONFIG_PATH, cfg)

    def persist_timers():
        save_json(TIMERS_PATH, timers)

    # ---------------------------------------------------------------- фильтры и модификаторы
    top = ttk.Frame(parent)
    top.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
    search_var = tk.StringVar()
    skill_var = tk.IntVar(value=cfg["skill"])
    kitchen_var = tk.IntVar(value=cfg["kitchen"])
    faculty_var = tk.BooleanVar(value=cfg["faculty"])
    premium_var = tk.BooleanVar(value=cfg["premium"])
    prem100_var = tk.BooleanVar(value=cfg["premium100"])
    quality_var = tk.IntVar(value=cfg["quality"])
    sort_var = tk.StringVar(value=SORTS[0])

    def on_change(*_):
        refresh_list()
        show_detail()

    row1 = ttk.Frame(top)
    row1.pack(fill="x")
    ttk.Label(row1, text="Поиск:").pack(side="left")
    ttk.Entry(row1, textvariable=search_var, width=16).pack(side="left", padx=(2, 8))
    ttk.Label(row1, text="Мой навык:").pack(side="left")
    tk.Spinbox(row1, from_=1, to=9, width=3, textvariable=skill_var, command=on_change).pack(side="left", padx=(2, 8))
    ttk.Label(row1, text="Кухня:").pack(side="left")
    tk.Spinbox(row1, from_=1, to=5, width=3, textvariable=kitchen_var, command=on_change).pack(side="left", padx=(2, 8))
    ttk.Checkbutton(row1, text="Есть факультет", variable=faculty_var, command=on_change).pack(side="left", padx=(0, 8))
    ttk.Label(row1, text="Сортировка:").pack(side="left")
    cb = ttk.Combobox(row1, textvariable=sort_var, values=SORTS, state="readonly", width=14)
    cb.pack(side="left", padx=2)
    cb.bind("<<ComboboxSelected>>", lambda e: refresh_list())
    search_var.trace_add("write", lambda *a: refresh_list())

    row2 = ttk.Frame(top)
    row2.pack(fill="x", pady=(3, 0))
    ttk.Label(row2, text="Качество блюда, %:").pack(side="left")
    tk.Spinbox(row2, from_=0, to=100, increment=5, width=4, textvariable=quality_var,
               command=on_change).pack(side="left", padx=(2, 10))
    ttk.Checkbutton(row2, text="Активный Premium (+25%)", variable=premium_var, command=on_change).pack(side="left", padx=(0, 8))
    ttk.Checkbutton(row2, text="Бонус за 100 дней Premium (+5.2%)", variable=prem100_var,
                    command=on_change).pack(side="left")

    # ---------------------------------------------------------------- таблица блюд
    cols = ("name", "skill", "out", "time", "exp", "price", "perh", "exph")
    heads = ("Блюдо", "Навык/кухня", "Выход", "Время", "Опыт", "Цена порции", "Доход/ч", "Опыт/ч")
    widths = (230, 95, 55, 95, 50, 120, 75, 65)
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=14, selectmode="browse")
    for c, h, w in zip(cols, heads, widths):
        tree.heading(c, text=h)
        tree.column(c, width=w, anchor="w" if c == "name" else "e")
    tree.grid(row=1, column=0, sticky="nsew", **pad)

    # ---------------------------------------------------------------- карточка блюда
    card = ttk.LabelFrame(parent, text="Блюдо")
    card.grid(row=1, column=1, sticky="nsew", **pad)
    title_var = tk.StringVar(value="Выбери блюдо в таблице")
    ttk.Label(card, textvariable=title_var, font=("Segoe UI", 11, "bold"),
              wraplength=360).grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    info_var = tk.StringVar()
    ttk.Label(card, textvariable=info_var, wraplength=360, justify="left").grid(
        row=1, column=0, columnspan=4, sticky="w", **pad)

    runs_var = tk.IntVar(value=1)
    batch_var = tk.StringVar(value=BATCH_NAMES[1])
    accel_var = tk.IntVar(value=0)
    ttk.Label(card, text="Загрузок подряд:").grid(row=2, column=0, sticky="w", **pad)
    tk.Spinbox(card, from_=1, to=99, width=4, textvariable=runs_var,
               command=lambda: show_detail()).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(card, text="Ускорителей:").grid(row=2, column=2, sticky="e", **pad)
    accel_spin = tk.Spinbox(card, from_=0, to=50, width=4, textvariable=accel_var,
                            command=lambda: show_detail())
    accel_spin.grid(row=2, column=3, sticky="w", **pad)
    ttk.Label(card, text="Режим:").grid(row=3, column=0, sticky="w", **pad)
    batch_cb = ttk.Combobox(card, textvariable=batch_var, state="readonly", width=20,
                            values=[BATCH_NAMES[1]])
    batch_cb.grid(row=3, column=1, columnspan=3, sticky="w", **pad)
    batch_cb.bind("<<ComboboxSelected>>", lambda e: show_detail())

    icols = ("ing", "qty", "src", "cost")
    itree = ttk.Treeview(card, columns=icols, show="headings", height=6, selectmode="browse")
    for c, h, w in zip(icols, ("Ингредиент", "Нужно", "Где взять", "Цена за 1"), (150, 60, 115, 70)):
        itree.heading(c, text=h)
        itree.column(c, width=w, anchor="w" if c in ("ing", "src") else "e")
    itree.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    cost_var = tk.StringVar()
    ttk.Label(card, text="Цена за 1 шт выбранного:").grid(row=5, column=0, columnspan=2, sticky="w", **pad)
    ttk.Entry(card, textvariable=cost_var, width=9).grid(row=5, column=2, sticky="w", **pad)
    ttk.Button(card, text="Сохранить цену", command=lambda: set_cost()).grid(row=5, column=3, sticky="e", **pad)
    itree.bind("<<TreeviewSelect>>", lambda e: pick_ing())

    totals_var = tk.StringVar()
    ttk.Label(card, textvariable=totals_var, justify="left", font=("Consolas", 9)).grid(
        row=6, column=0, columnspan=4, sticky="w", **pad)
    label_var = tk.StringVar(value="Плита 1")
    ttk.Label(card, text="Метка:").grid(row=7, column=0, sticky="w", **pad)
    ttk.Entry(card, textvariable=label_var, width=14).grid(row=7, column=1, columnspan=2, sticky="w", **pad)
    ttk.Button(card, text="Поставить таймер", command=lambda: add_timer_from_dish()).grid(
        row=7, column=3, sticky="e", **pad)

    # ---------------------------------------------------------------- таймеры
    tframe = ttk.LabelFrame(parent, text="Таймеры")
    tframe.grid(row=2, column=0, sticky="nsew", **pad)
    tcols = ("dish", "label", "end", "left")
    ttree = ttk.Treeview(tframe, columns=tcols, show="headings", height=5, selectmode="browse")
    for c, h, w in zip(tcols, ("Что готовится", "Метка", "Готово в", "Осталось"), (300, 110, 120, 130)):
        ttree.heading(c, text=h)
        ttree.column(c, width=w, anchor="w")
    ttree.grid(row=0, column=0, columnspan=6, sticky="ew", **pad)
    custom_name = tk.StringVar(value="Свой таймер")
    custom_min = tk.IntVar(value=60)
    ttk.Entry(tframe, textvariable=custom_name, width=18).grid(row=1, column=0, sticky="w", **pad)
    tk.Spinbox(tframe, from_=1, to=5000, width=5, textvariable=custom_min).grid(row=1, column=1, sticky="w")
    ttk.Label(tframe, text="мин").grid(row=1, column=2, sticky="w")
    ttk.Button(tframe, text="Добавить", command=lambda: add_custom_timer()).grid(row=1, column=3, **pad)
    ttk.Button(tframe, text="Удалить", command=lambda: delete_timer()).grid(row=1, column=4, **pad)
    ttk.Button(tframe, text="Убрать готовые", command=lambda: clear_done()).grid(row=1, column=5, **pad)

    # ---------------------------------------------------------------- Telegram
    tgf = ttk.LabelFrame(parent, text="Уведомления в Telegram")
    tgf.grid(row=2, column=1, sticky="nsew", **pad)
    token_var = tk.StringVar(value=cfg["token"])
    ttk.Label(tgf, text="Токен бота:").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(tgf, textvariable=token_var, width=30, show="•").grid(row=0, column=1, columnspan=3, sticky="w", **pad)
    tg_status = tk.StringVar(value=(f"Подключено: {cfg['chat_name']}" if cfg["chat_id"] else "Не подключено"))
    ttk.Button(tgf, text="Подключить", command=lambda: tg_connect()).grid(row=1, column=0, sticky="w", **pad)
    ttk.Button(tgf, text="Тест", command=lambda: tg_test()).grid(row=1, column=1, sticky="w", **pad)
    ttk.Label(tgf, textvariable=tg_status, wraplength=320).grid(row=2, column=0, columnspan=4, sticky="w", **pad)
    remind_var = tk.BooleanVar(value=cfg["remind"])
    remind_min = tk.IntVar(value=cfg["remind_min"])
    ttk.Checkbutton(tgf, text="Напоминать за", variable=remind_var,
                    command=lambda: save_settings()).grid(row=3, column=0, sticky="w", **pad)
    tk.Spinbox(tgf, from_=1, to=120, width=4, textvariable=remind_min,
               command=lambda: save_settings()).grid(row=3, column=1, sticky="w")
    ttk.Label(tgf, text="мин до готовности").grid(row=3, column=2, sticky="w")
    status_var = tk.StringVar()
    ttk.Label(parent, textvariable=status_var, foreground="#555").grid(
        row=3, column=0, columnspan=2, sticky="w", **pad)

    # ================================================================ логика
    def ivar(v, default):
        try:
            return int(v.get())
        except Exception:
            return default

    def save_settings():
        cfg.update(skill=min(9, max(1, ivar(skill_var, 9))), kitchen=min(5, max(1, ivar(kitchen_var, 5))),
                   faculty=faculty_var.get(), premium=premium_var.get(), premium100=prem100_var.get(),
                   quality=min(100, max(0, ivar(quality_var, 100))), remind=remind_var.get(),
                   remind_min=max(1, ivar(remind_min, 5)), token=token_var.get().strip())
        persist_cfg()

    def refresh_list():
        save_settings()
        q = search_var.get().strip().lower()
        costs = cfg["costs"]
        items = []
        for i, r in enumerate(RECIPES):
            if q and q not in r["name"].lower() and not any(q in n.lower() for n, _, _ in r["ingredients"]):
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
        sel = selected_recipe()
        tree.delete(*tree.get_children())
        for i in items:
            r = RECIPES[i]
            lo, hi = price(r, cfg)
            ph, eh = per_hour(r, cfg, costs)
            name = r["name"] + ("  [факультет]" if r["faculty"] else "")
            tree.insert("", "end", iid=str(i), values=(
                name, f"{r['skill']} / {r['kitchen']}", f"x{r['yield']}", fmt_minutes(r["minutes"]),
                r["exp"], f"{money(lo)}–{money(hi)}", money(ph), f"{eh:.1f}"))
        if sel is not None and tree.exists(str(sel)):
            tree.selection_set(str(sel))
        status_var.set(f"Блюд: {len(items)} из {len(RECIPES)}. Доход в час - по средней цене "
                       f"минус ингредиенты, если ты указал их цены.")

    def selected_recipe():
        s = tree.selection()
        return int(s[0]) if s else None

    def current_batch():
        for b, name in BATCH_NAMES.items():
            if batch_var.get() == name:
                return b
        return 1

    def show_detail():
        i = selected_recipe()
        if i is None:
            return
        save_settings()
        r = RECIPES[i]
        mx = MULTI_MAX.get(cfg["kitchen"], 1)
        batch_cb.configure(values=[BATCH_NAMES[b] for b in range(1, mx + 1)])
        batch = min(current_batch(), mx)
        batch_var.set(BATCH_NAMES[batch])
        runs = max(1, ivar(runs_var, 1))
        max_acc = max_accels(multi_minutes(r, batch, cfg["kitchen"]))
        accel_spin.configure(to=max_acc)
        accels = min(max(0, ivar(accel_var, 0)), max_acc)
        if ivar(accel_var, 0) != accels:
            accel_var.set(accels)
        c = calc(r, runs, batch, accels, cfg, cfg["costs"])
        title_var.set(r["name"])
        extra = f" Бафф: {', '.join(r['buffs'])}." if r["buffs"] else ""
        info_var.set(f"{r['methods']}. Навык {r['skill']}, кухня {r['kitchen']}, "
                     f"{'нужен факультет кулинарии' if r['faculty'] else 'факультет не нужен'}.{extra}")
        sel_ing = itree.selection()
        itree.delete(*itree.get_children())
        for n, q, src, cost in c["ingredients"]:
            itree.insert("", "end", iid=n, values=(n, f"x{q}", src or "-", "-" if cost is None else money(cost)))
        for s_ in sel_ing:
            if itree.exists(s_):
                itree.selection_set(s_)
        lo, hi = c["income"]
        lines = [f"Порций: {c['portions']}   Опыт: +{c['exp']}",
                 f"Время: {fmt_minutes(c['minutes'])}" +
                 (f" ({runs} x {fmt_minutes(c['minutes_one'])})" if runs > 1 else "") +
                 (f", ускорителей до {max_acc}" if max_acc and not accels else ""),
                 f"Выручка: {money(lo)} – {money(hi)} $"]
        if c["cost"]:
            lines.append(f"Ингредиенты: {money(c['cost'])} $" +
                         (" (не все цены указаны)" if c["unknown_cost"] else ""))
            lines.append(f"Прибыль: {money(lo - c['cost'])} – {money(hi - c['cost'])} $")
        totals_var.set("\n".join(lines))

    def pick_ing():
        s_ = itree.selection()
        if s_:
            v = cfg["costs"].get(s_[0])
            cost_var.set("" if v is None else str(v))

    def set_cost():
        s_ = itree.selection()
        if not s_:
            status_var.set("Выбери ингредиент в списке, потом впиши цену")
            return
        txt = cost_var.get().replace(" ", "").replace(",", ".")
        if txt == "":
            cfg["costs"].pop(s_[0], None)
        else:
            try:
                cfg["costs"][s_[0]] = max(0.0, float(txt))
            except ValueError:
                status_var.set("Цена - это число")
                return
        persist_cfg()
        refresh_list()
        show_detail()

    tree.bind("<<TreeviewSelect>>", lambda e: show_detail())

    # ---------------------------------------------------------------- таймеры
    def new_id():
        return max([t["id"] for t in timers] + [0]) + 1

    def add_timer(name, label, minutes):
        now = time.time()
        timers.append({"id": new_id(), "name": name, "label": label, "start": now,
                       "end": now + minutes * 60, "reminded": False, "done": False})
        persist_timers()
        redraw_timers()
        end = time.strftime("%H:%M", time.localtime(now + minutes * 60))
        notify(f"Таймер запущен: {name} ({label}), будет готово в {end} "
               f"(через {fmt_minutes(minutes)})", telegram=False)

    def add_timer_from_dish():
        i = selected_recipe()
        if i is None:
            status_var.set("Сначала выбери блюдо в таблице")
            return
        r = RECIPES[i]
        batch = current_batch()
        accels = max(0, ivar(accel_var, 0))
        c = calc(r, 1, batch, accels, cfg, cfg["costs"])      # таймер - на одну загрузку
        add_timer(f"{r['name']} x{c['portions']}", label_var.get().strip() or "-", c["minutes_one"])

    def add_custom_timer():
        add_timer(custom_name.get().strip() or "Таймер", "-", max(1, ivar(custom_min, 60)))

    def delete_timer():
        s_ = ttree.selection()
        if not s_:
            return
        tid = int(s_[0])
        timers[:] = [t for t in timers if t["id"] != tid]
        persist_timers()
        redraw_timers()

    def clear_done():
        timers[:] = [t for t in timers if not t["done"]]
        persist_timers()
        redraw_timers()

    def redraw_timers():
        sel = ttree.selection()
        ttree.delete(*ttree.get_children())
        now = time.time()
        for t in sorted(timers, key=lambda t: t["end"]):
            day = "" if time.localtime(t["end"]).tm_yday == time.localtime(now).tm_yday else \
                time.strftime("%d.%m ", time.localtime(t["end"]))
            left = "Готово ✓" if t["done"] else fmt_left(t["end"] - now)
            ttree.insert("", "end", iid=str(t["id"]), values=(
                t["name"], t["label"], day + time.strftime("%H:%M", time.localtime(t["end"])), left))
        for s_ in sel:
            if ttree.exists(s_):
                ttree.selection_set(s_)

    def notify(text, telegram=True):
        status_var.set(time.strftime("%H:%M ") + text)
        if telegram and cfg["token"] and cfg["chat_id"]:
            def work():
                try:
                    tg_send(cfg["token"], cfg["chat_id"], text)
                except Exception as ex:
                    ui.put(("status", f"Telegram: {ex}"))
            threading.Thread(target=work, daemon=True).start()

    def check_timers(first=False):
        now = time.time()
        changed = False
        for t in timers:
            if t["done"]:
                continue
            if now >= t["end"]:
                t["done"] = True
                changed = True
                late = " (пока программа была выключена)" if first and now - t["end"] > 60 else ""
                notify(f"🍳 Готово: {t['name']} ({t['label']}){late}")
                beep()
            elif cfg["remind"] and not t["reminded"] and t["end"] - now <= cfg["remind_min"] * 60 \
                    and t["end"] - t["start"] > cfg["remind_min"] * 60:
                t["reminded"] = True
                changed = True
                notify(f"⏰ Через {cfg['remind_min']} мин будет готово: {t['name']} ({t['label']})")
        if changed:
            persist_timers()

    # ---------------------------------------------------------------- Telegram
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

    # ---------------------------------------------------------------- цикл
    def tick():
        if not alive["on"]:
            return
        try:
            while True:
                kind, val = ui.get_nowait()
                if kind == "chat":
                    cfg["chat_id"], cfg["chat_name"] = val
                    persist_cfg()
                    tg_status.set(f"Подключено: {val[1]}")
                elif kind == "tg":
                    tg_status.set(val)
                elif kind == "status":
                    status_var.set(val)
        except queue.Empty:
            pass
        try:
            check_timers()
            redraw_timers()
        except tk.TclError:
            return
        parent.after(1000, tick)

    def stop():
        alive["on"] = False
        persist_timers()

    refresh_list()
    check_timers(first=True)
    redraw_timers()
    parent.after(1000, tick)
    return stop


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    root.title("Готовка")
    stop = build_cooking_tab(root)
    root.mainloop()
    stop()
