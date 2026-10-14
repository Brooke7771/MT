# -*- coding: utf-8 -*-
"""Рынок: цены Маркетплейса Majestic RP по серверам через ОФИЦИАЛЬНОЕ API (id.majestic-rp.ru/api).
У каждого пользователя свой персональный ключ (создаётся на сайте, хранится только на этом компьютере).
Лимит API - 5 запросов в 60 секунд: запросы идут через очередь, ответы кэшируются на диск.
Один запрос = вся статистика одного сервера по одной категории."""
import os
import sys
import json
import time
import queue
import threading
import urllib.request
import urllib.error
from collections import deque

import net

HOST = "https://api.majestic-files.net"
LIMIT, WINDOW = 5, 60.0              # официальный лимит: 5 запросов / 60 секунд
TTL = 30 * 60                        # кэш считается свежим 30 минут (данные на сервере обновляются реже)

SERVERS = [("ru1", "New York"), ("ru2", "Detroit"), ("ru3", "Chicago"), ("ru4", "San Francisco"),
           ("ru5", "Atlanta"), ("ru6", "San Diego"), ("ru7", "Los Angeles"), ("ru8", "Miami"),
           ("ru9", "Las Vegas"), ("ru10", "Washington"), ("ru11", "Dallas"), ("ru12", "Boston"),
           ("ru13", "Houston"), ("ru14", "Seattle"), ("ru15", "Phoenix"), ("ru16", "Denver"),
           ("ru17", "Portland"), ("ru18", "Orlando"), ("ru19", "Memphis")]
# (путь, название, ключ списка в ответе, поле названия)
CATS = [("items", "Предметы", "itemStatistics", "itemName"),
        ("vehicles", "Транспорт", "vehicleStatistics", "modelName"),
        ("clothes", "Одежда", "clothesStatistics", "itemName"),
        ("houses", "Дома", "houseStatistics", "itemName"),
        ("apartments", "Квартиры", "apartmentStatistics", "itemName"),
        ("warehouses", "Склады", "warehouseStatistics", "itemName"),
        ("offices", "Офисы", "officeStatistics", "itemName")]
CAT_BY = {c[0]: c for c in CATS}
SERVER_NAME = dict(SERVERS)

# состояние для блока над страницей (heroes.MarketHero читает через widgets.register)
STATE = {"has_key": False, "server": "", "cat": "", "n": 0, "top": [], "updated": "", "period": 0,
         "left": LIMIT, "reset": 0.0, "busy": "", "error": ""}


def data_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


class ApiError(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


class Api:
    def __init__(self):
        self.key_path = os.path.join(data_dir(), "majestic_api_key.txt")
        self.cache_dir = os.path.join(data_dir(), "market_cache")
        self._slots = deque()
        self._lock = threading.Lock()
        self._mem = {}

    # ---- ключ
    def key(self):
        try:
            with open(self.key_path, encoding="utf-8-sig") as f:
                return f.read().strip()
        except OSError:
            return ""

    def set_key(self, key):
        key = key.strip()
        with open(self.key_path, "w", encoding="utf-8") as f:
            f.write(key)

    # ---- лимит запросов
    def _purge(self, now):
        while self._slots and now - self._slots[0] >= WINDOW:
            self._slots.popleft()

    def remaining(self):
        with self._lock:
            self._purge(time.time())
            return LIMIT - len(self._slots)

    def reset_in(self):
        with self._lock:
            now = time.time()
            self._purge(now)
            return max(0.0, self._slots[0] + WINDOW - now) if self._slots else 0.0

    def _take_slot(self, cancel):
        """Ждёт свободный «слот» лимита (5 запросов в минуту). cancel - threading.Event."""
        while True:
            with self._lock:
                now = time.time()
                self._purge(now)
                if len(self._slots) < LIMIT:
                    self._slots.append(now)
                    return
                wait = self._slots[0] + WINDOW - now
            if cancel is not None and cancel.is_set():
                raise ApiError("cancel", "Отменено")
            time.sleep(min(0.5, max(0.05, wait)))

    # ---- кэш
    def _cache_path(self, cat, server):
        return os.path.join(self.cache_dir, f"{cat}_{server}.json")

    def cached(self, cat, server):
        """-> (время, данные) или None. Из памяти, иначе с диска."""
        k = (cat, server)
        if k in self._mem:
            return self._mem[k]
        try:
            with open(self._cache_path(cat, server), encoding="utf-8") as f:
                d = json.load(f)
            self._mem[k] = (d["ts"], d["data"])
            return self._mem[k]
        except Exception:
            return None

    def _store(self, cat, server, data):
        ts = time.time()
        self._mem[(cat, server)] = (ts, data)
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._cache_path(cat, server), "w", encoding="utf-8") as f:
                json.dump({"ts": ts, "data": data}, f, ensure_ascii=False)
        except OSError:
            pass

    # ---- запрос
    def fetch(self, cat, server, force=False, cancel=None):
        """Статистика категории на сервере (из кэша, если он свежий). -> dict result из ответа API."""
        c = self.cached(cat, server)
        if c and not force and time.time() - c[0] < TTL:
            return c[1]
        key = self.key()
        if not key:
            raise ApiError("nokey", "Не введён ключ API")
        self._take_slot(cancel)
        req = urllib.request.Request(
            f"{HOST}/v1/ext/marketplace/{cat}/{server}",
            headers={"X-API-KEY": key, "X-LANGUAGE": "ru", "Accept": "application/json", "User-Agent": "MajesticTools/1.0"})
        try:
            with net.urlopen(req, timeout=40) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ApiError("auth", "Неверный ключ API (401). Проверь ключ на id.majestic-rp.ru/api")
            if e.code == 403:
                raise ApiError("access", "У ключа нет доступа к этому методу (403)")
            if e.code == 429:
                with self._lock:                       # сервер считает, что лимит исчерпан: ждём окно целиком
                    now = time.time()
                    self._slots.clear()
                    self._slots.extend([now] * LIMIT)
                raise ApiError("limit", "Лимит запросов (429): подожди минуту и повтори")
            raise ApiError("server", f"Ошибка сервера API ({e.code})")
        except Exception as ex:
            if c:                                      # нет связи - отдаём устаревшее, лучше чем ничего
                return c[1]
            raise ApiError("net", f"Нет связи с API: {ex}")
        res = j.get("result", j) if isinstance(j, dict) else {}
        if not isinstance(res, dict) or CAT_BY[cat][2] not in res:
            raise ApiError("server", "Неожиданный ответ API")
        self._store(cat, server, res)
        return res


def rows_of(cat, data):
    """Нормализует ответ API в список словарей: key, name, total, sold, avg, min, max."""
    lst = (data or {}).get(CAT_BY[cat][2]) or []
    namef = CAT_BY[cat][3]
    out = []
    for r in lst:
        if cat == "clothes":
            k = f"{r.get('component')}:{r.get('drawable')}:{r.get('texture')}:{r.get('gender')}:{r.get('isProp')}"
        else:
            k = str(r.get("itemId", r.get("model", r.get("houseId", r.get("apartmentId", r.get("warehouseId",
                                                                                              r.get("officeId", r.get(namef))))))))
        out.append({"key": k, "name": str(r.get(namef) or k), "total": int(r.get("totalCount") or 0),
                    "sold": int(r.get("soldCount") or 0), "avg": int(r.get("averagePrice") or 0),
                    "min": int(r.get("minPrice") or 0), "max": int(r.get("maxPrice") or 0)})
    return out


def money(n):
    return f"{int(n):,}".replace(",", " ") + " $"


def num(n):
    return f"{int(n):,}".replace(",", " ")


def compare_servers(api, cat, min_sold=30, min_price=100, servers=None):
    """По кэшу всех серверов: где дешевле купить и дороже продать. -> список словарей по убыванию разницы."""
    per = {}
    names = {}
    for sid, _ in (servers or SERVERS):
        c = api.cached(cat, sid)
        if not c:
            continue
        for r in rows_of(cat, c[1]):
            if r["sold"] >= min_sold and r["avg"] >= min_price:
                per.setdefault(r["key"], {})[sid] = r["avg"]
                names[r["key"]] = r["name"]
    out = []
    for k, d in per.items():
        if len(d) < 3:
            continue
        lo = min(d, key=d.get)
        hi = max(d, key=d.get)
        if d[lo] <= 0:
            continue
        out.append({"key": k, "name": names[k], "lo": lo, "lo_p": d[lo], "hi": hi, "hi_p": d[hi],
                    "pct": (d[hi] - d[lo]) * 100.0 / d[lo], "n": len(d)})
    out.sort(key=lambda x: -x["pct"])
    return out


# ====================================================================== интерфейс
def build_market_tab(parent):
    import webbrowser
    import tkinter as tk
    from tkinter import ttk, messagebox
    import theme as T
    import widgets

    api = Api()
    q = queue.Queue()
    st = {"rows": [], "data": None, "cat": "items", "server": "ru19", "alive": True, "cancel": None, "busy": False}

    def sname(sid):
        return f"{sid.upper()} · {SERVER_NAME.get(sid, '')}"

    # ---------------- ключ
    keybox = ttk.LabelFrame(parent, text="Ключ API Majestic")
    keybox.pack(fill="x", padx=8, pady=(0, 6))
    key_var = tk.StringVar(value=api.key())
    key_msg = tk.StringVar()
    ke = ttk.Entry(keybox, textvariable=key_var, width=44, show="•")
    ke.pack(side="left", padx=(8, 4), pady=6)
    try:
        import licenseui
        licenseui.enable_clipboard(ke)
    except Exception:
        pass

    def save_key():
        k = key_var.get().strip()
        if not k:
            key_msg.set("Введи ключ")
            return
        api.set_key(k)
        key_var.set(k)
        STATE["has_key"] = True
        key_msg.set("Ключ сохранён. Нажми «Обновить»")
        load(force=False)

    def paste_key():
        try:
            key_var.set(ke.clipboard_get().strip())
        except tk.TclError:
            pass
    ttk.Button(keybox, text="Вставить", command=paste_key).pack(side="left", padx=2)
    ttk.Button(keybox, text="Сохранить", style="Accent.TButton", command=save_key).pack(side="left", padx=2)
    ttk.Button(keybox, text="Получить ключ", command=lambda: webbrowser.open("https://id.majestic-rp.ru/api")
               ).pack(side="left", padx=2)
    ttk.Label(keybox, textvariable=key_msg, style="Muted.TLabel").pack(side="left", padx=8)
    STATE["has_key"] = bool(api.key())
    if not STATE["has_key"]:
        key_msg.set("Ключ создаётся бесплатно на id.majestic-rp.ru/api (раздел «Ключ доступа»)")

    # ---------------- управление
    bar = ttk.Frame(parent)
    bar.pack(fill="x", padx=8, pady=2)
    cat_var = tk.StringVar(value="Предметы")
    srv_var = tk.StringVar(value=sname("ru19"))
    find_var = tk.StringVar()
    sort_var = tk.StringVar(value="Продано")
    desc_var = tk.BooleanVar(value=True)
    nz_var = tk.BooleanVar(value=True)
    ttk.Label(bar, text="Категория:").pack(side="left")
    cb_cat = ttk.Combobox(bar, textvariable=cat_var, values=[c[1] for c in CATS], width=11, state="readonly")
    cb_cat.pack(side="left", padx=(4, 10))
    ttk.Label(bar, text="Сервер:").pack(side="left")
    cb_srv = ttk.Combobox(bar, textvariable=srv_var, values=[sname(s) for s, _ in SERVERS], width=20, state="readonly")
    cb_srv.pack(side="left", padx=(4, 10))
    ttk.Label(bar, text="Поиск:").pack(side="left")
    en_find = ttk.Entry(bar, textvariable=find_var, width=18)
    en_find.pack(side="left", padx=(4, 10))
    ttk.Button(bar, text="Обновить", style="Accent.TButton", command=lambda: load(force=True)).pack(side="left", padx=2)
    ttk.Button(bar, text="Сравнить серверы", command=lambda: open_compare()).pack(side="left", padx=2)

    bar2 = ttk.Frame(parent)
    bar2.pack(fill="x", padx=8, pady=(2, 4))
    ttk.Label(bar2, text="Сортировка:").pack(side="left")
    SORTS = {"Название": "name", "Средняя цена": "avg", "Мин. цена": "min", "Макс. цена": "max",
             "Продано": "sold", "В продаже": "total"}
    ttk.Combobox(bar2, textvariable=sort_var, values=list(SORTS), width=13, state="readonly").pack(side="left", padx=4)
    ttk.Checkbutton(bar2, text="по убыванию", variable=desc_var, command=lambda: view()).pack(side="left", padx=8)
    ttk.Checkbutton(bar2, text="только с продажами", variable=nz_var, command=lambda: view()).pack(side="left", padx=4)
    status = tk.StringVar(value="")
    ttk.Label(bar2, textvariable=status, style="Muted.TLabel").pack(side="left", padx=12)

    foot = ttk.Label(parent, text="Данные: официальное API Majestic RP (id.majestic-rp.ru/api). "
                                  "Двойной щелчок по строке - цены по всем серверам.", style="Muted.TLabel")
    foot.pack(side="bottom", anchor="w", padx=10, pady=(2, 4))      # до таблицы, иначе её expand выдавит подпись

    # ---------------- таблица
    box = ttk.Frame(parent)
    box.pack(fill="both", expand=True, padx=8, pady=2)
    cols = ("name", "avg", "min", "max", "sold", "total")
    tv = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse", height=14)
    for c, h, w, a in (("name", "Название", 280, "w"), ("avg", "Средняя", 110, "e"), ("min", "Мин.", 100, "e"),
                       ("max", "Макс.", 100, "e"), ("sold", "Продано", 110, "e"), ("total", "В продаже", 120, "e")):
        tv.heading(c, text=h, command=lambda c=c: sort_by(c))
        tv.column(c, width=w, anchor=a)
    sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tv.tag_configure("odd", background=T.FIELD_ALT)

    SORT_COL = {"name": "Название", "avg": "Средняя цена", "min": "Мин. цена", "max": "Макс. цена",
                "sold": "Продано", "total": "В продаже"}

    def sort_by(col):
        if SORTS.get(sort_var.get()) == col:
            desc_var.set(not desc_var.get())
        else:
            sort_var.set(SORT_COL[col])
            desc_var.set(col != "name")
        view()

    def cur_cat():
        return next((c[0] for c in CATS if c[1] == cat_var.get()), "items")

    def cur_srv():
        return srv_var.get().split(" ")[0].lower()

    # ---------------- загрузка
    def set_busy(text):
        STATE["busy"] = text
        st["busy"] = bool(text)

    def load(force=False):
        if st["busy"]:
            return
        if not api.key():
            status.set("Сначала введи ключ API")
            return
        cat, srv = cur_cat(), cur_srv()
        st["cat"], st["server"] = cat, srv
        c = api.cached(cat, srv)
        fresh = c and time.time() - c[0] < TTL
        set_busy("Загрузка…" if (force or not fresh) else "")
        status.set("Загружаю…" if (force or not fresh) else "")

        def work():
            try:
                q.put(("data", cat, srv, api.fetch(cat, srv, force=force)))
            except ApiError as ex:
                q.put(("err", ex.kind, str(ex)))
            except Exception as ex:
                q.put(("err", "server", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    def view():
        data = st["data"]
        if data is None:
            return
        cat = st["cat"]
        rows = st["rows"]
        s = find_var.get().strip().lower()
        if s:
            rows = [r for r in rows if s in r["name"].lower()]
        if nz_var.get():
            rows = [r for r in rows if r["sold"] > 0]
        f = SORTS.get(sort_var.get(), "sold")
        rows = sorted(rows, key=lambda r: r[f], reverse=bool(desc_var.get()) if f != "name" else bool(desc_var.get()))
        tv.delete(*tv.get_children())
        shown = rows[:1500]
        for i, r in enumerate(shown):
            tv.insert("", "end", iid=f"{i}", values=(r["name"], money(r["avg"]), money(r["min"]), money(r["max"]),
                                                      num(r["sold"]), num(r["total"])),
                      tags=("odd",) if i % 2 else ())
        st["shown"] = shown
        lu = data.get("lastUpdated", "")
        status.set(f"{sname(st['server'])} · {CAT_BY[cat][1]} · показано {len(shown)} из {len(rows)}"
                   + (f" · обновлено {lu[:16].replace('T', ' ')} UTC" if lu else "")
                   + (f" · период {data.get('periodDays')} дн." if data.get("periodDays") else ""))
        top = sorted(st["rows"], key=lambda r: -r["sold"])[:3]
        STATE.update(server=sname(st["server"]), cat=CAT_BY[cat][1], n=len(st["rows"]),
                     top=[(r["name"], r["sold"]) for r in top], updated=lu[11:16], period=data.get("periodDays") or 0,
                     error="")

    find_var.trace_add("write", lambda *a: view())
    sort_var.trace_add("write", lambda *a: view())
    cb_cat.bind("<<ComboboxSelected>>", lambda e: load())
    cb_srv.bind("<<ComboboxSelected>>", lambda e: load())

    # ---------------- окно «цены по серверам» для строки
    def open_detail(event=None):
        sel = tv.selection()
        if not sel:
            return
        r = st["shown"][int(sel[0])]
        cat = st["cat"]
        dlg = tk.Toplevel(parent)
        dlg.title(r["name"])
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.geometry("640x560")
        ttk.Label(dlg, text=r["name"], font=T.FONT_TITLE).pack(anchor="w", padx=14, pady=(12, 0))
        info = tk.StringVar()
        ttk.Label(dlg, textvariable=info, style="Muted.TLabel").pack(anchor="w", padx=14)
        t2 = ttk.Treeview(dlg, columns=("srv", "avg", "min", "max", "sold"), show="headings", height=18)
        for c, h, w, a in (("srv", "Сервер", 180, "w"), ("avg", "Средняя", 110, "e"), ("min", "Мин.", 100, "e"),
                           ("max", "Макс.", 100, "e"), ("sold", "Продано", 100, "e")):
            t2.heading(c, text=h)
            t2.column(c, width=w, anchor=a)
        t2.tag_configure("buy", foreground=T.OK)
        t2.tag_configure("sell", foreground="#ff8a8a")
        t2.pack(fill="both", expand=True, padx=14, pady=8)
        pr = tk.StringVar()
        ttk.Label(dlg, textvariable=pr, style="Muted.TLabel").pack(anchor="w", padx=14)
        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=14, pady=(4, 12))

        def fill():
            per = []
            for sid, _ in SERVERS:
                c = api.cached(cat, sid)
                if not c:
                    continue
                for x in rows_of(cat, c[1]):
                    if x["key"] == r["key"]:
                        per.append((sid, x))
                        break
            t2.delete(*t2.get_children())
            paid = [p for p in per if p[1]["avg"] > 1 and p[1]["sold"] > 0]
            lo = min(paid, key=lambda p: p[1]["avg"])[0] if paid else None
            hi = max(paid, key=lambda p: p[1]["avg"])[0] if paid else None
            for sid, x in sorted(per, key=lambda p: p[1]["avg"] if p[1]["avg"] > 0 else 1e18):
                tag = ("buy",) if sid == lo and lo != hi else (("sell",) if sid == hi and lo != hi else ())
                t2.insert("", "end", values=(sname(sid) + ("  ← купить" if tag == ("buy",) else
                                                           "  ← продать" if tag == ("sell",) else ""),
                                             money(x["avg"]), money(x["min"]), money(x["max"]), num(x["sold"])), tags=tag)
            miss = len(SERVERS) - len(per)
            info.set(f"Серверов с данными: {len(per)} из {len(SERVERS)}"
                     + (f". Дешевле всего на {sname(lo)}, дороже на {sname(hi)}" if lo and hi and lo != hi else ""))
            return miss

        miss = fill()
        cancel = {"ev": None}

        def more():
            if st["busy"] or not api.key():
                return
            ev = threading.Event()
            cancel["ev"] = ev
            set_busy("Догружаю серверы…")

            def work():
                for sid, _ in SERVERS:
                    if ev.is_set():
                        break
                    c = api.cached(cat, sid)
                    if c and time.time() - c[0] < TTL:
                        continue
                    q.put(("progress", f"Загружаю {sname(sid)}… (лимит API: 5 запросов в минуту)"))
                    try:
                        api.fetch(cat, sid, cancel=ev)
                    except ApiError as ex:
                        if ex.kind != "cancel":
                            q.put(("err", ex.kind, str(ex)))
                        break
                q.put(("done_more", None))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(btns, text="Догрузить все серверы", style="Accent.TButton", command=more).pack(side="left")
        ttk.Button(btns, text="Остановить", command=lambda: cancel["ev"] and cancel["ev"].set()).pack(side="left", padx=6)
        ttk.Button(btns, text="Закрыть", command=dlg.destroy).pack(side="right")
        st["detail"] = (dlg, fill, pr)

        def closed(e):
            if e.widget is dlg:
                if cancel["ev"]:
                    cancel["ev"].set()
                st.pop("detail", None)
        dlg.bind("<Destroy>", closed)

    tv.bind("<Double-1>", open_detail)
    tv.bind("<Return>", open_detail)

    # ---------------- окно «сравнить серверы»
    def open_compare():
        cat = cur_cat()
        dlg = tk.Toplevel(parent)
        dlg.title(f"Сравнение серверов: {CAT_BY[cat][1]}")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.geometry("900x620")
        ttk.Label(dlg, text="Где выгодно купить и продать", font=T.FONT_TITLE).pack(anchor="w", padx=14, pady=(12, 0))
        ttk.Label(dlg, text="Один и тот же товар на разных серверах стоит по-разному. Для сравнения нужны данные "
                            "всех 19 серверов: из-за лимита API (5 запросов в минуту) первая загрузка занимает ~4 минуты, "
                            "потом всё берётся из кэша.", style="Muted.TLabel", wraplength=860,
                  justify="left").pack(anchor="w", padx=14, pady=(2, 6))
        ctrl = ttk.Frame(dlg)
        ctrl.pack(fill="x", padx=14)
        ms_var, mp_var = tk.IntVar(value=30), tk.IntVar(value=100)
        ttk.Label(ctrl, text="Минимум продаж:").pack(side="left")
        ttk.Spinbox(ctrl, from_=0, to=100000, width=7, textvariable=ms_var).pack(side="left", padx=(4, 12))
        ttk.Label(ctrl, text="Мин. цена, $:").pack(side="left")
        ttk.Spinbox(ctrl, from_=0, to=10 ** 9, width=10, textvariable=mp_var).pack(side="left", padx=(4, 12))
        msg = tk.StringVar()
        t3 = ttk.Treeview(dlg, columns=("name", "buy", "sell", "pct"), show="headings", height=18)
        for c, h, w, a in (("name", "Название", 260, "w"), ("buy", "Купить", 190, "w"), ("sell", "Продать", 190, "w"),
                           ("pct", "Разница", 90, "e")):
            t3.heading(c, text=h)
            t3.column(c, width=w, anchor=a)
        t3.tag_configure("odd", background=T.FIELD_ALT)
        res = {"rows": []}

        def show():
            try:
                rows = compare_servers(api, cat, max(0, ms_var.get()), max(0, mp_var.get()))
            except tk.TclError:
                return
            res["rows"] = rows[:400]
            t3.delete(*t3.get_children())
            for i, x in enumerate(res["rows"]):
                t3.insert("", "end", values=(x["name"], f"{SERVER_NAME[x['lo']]} · {money(x['lo_p'])}",
                                             f"{SERVER_NAME[x['hi']]} · {money(x['hi_p'])}", f"+{x['pct']:.0f}%"),
                          tags=("odd",) if i % 2 else ())
            have = sum(1 for sid, _ in SERVERS if api.cached(cat, sid))
            msg.set(f"Серверов в кэше: {have} из {len(SERVERS)} · найдено позиций: {len(rows)}")
        ttk.Button(ctrl, text="Применить", command=show).pack(side="left", padx=2)
        ttk.Label(ctrl, textvariable=msg, style="Muted.TLabel").pack(side="left", padx=10)
        t3.pack(fill="both", expand=True, padx=14, pady=8)
        prog = tk.StringVar()
        ttk.Label(dlg, textvariable=prog, style="Muted.TLabel").pack(anchor="w", padx=14)
        cancel = {"ev": None}

        def start():
            if st["busy"] or not api.key():
                prog.set("Нужен ключ API" if not api.key() else "Подожди: идёт другая загрузка")
                return
            ev = threading.Event()
            cancel["ev"] = ev
            set_busy("Сравнение серверов…")

            def work():
                for sid, _ in SERVERS:
                    if ev.is_set():
                        break
                    c = api.cached(cat, sid)
                    if c and time.time() - c[0] < TTL:
                        continue
                    q.put(("progress", f"Загружаю {sname(sid)}…"))
                    try:
                        api.fetch(cat, sid, cancel=ev)
                    except ApiError as ex:
                        if ex.kind != "cancel":
                            q.put(("err", ex.kind, str(ex)))
                        break
                q.put(("done_more", None))
            threading.Thread(target=work, daemon=True).start()
        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=14, pady=(4, 12))
        ttk.Button(btns, text="Загрузить недостающие серверы", style="Accent.TButton", command=start).pack(side="left")
        ttk.Button(btns, text="Остановить", command=lambda: cancel["ev"] and cancel["ev"].set()).pack(side="left", padx=6)
        ttk.Button(btns, text="Закрыть", command=dlg.destroy).pack(side="right")
        st["compare"] = (dlg, show, prog)
        show()

        def closed(e):
            if e.widget is dlg:
                if cancel["ev"]:
                    cancel["ev"].set()
                st.pop("compare", None)
        dlg.bind("<Destroy>", closed)

    # ---------------- обмен с потоками
    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "data":
                    _, cat, srv, data = m
                    set_busy("")
                    if (cat, srv) == (st["cat"], st["server"]):
                        st["data"] = data
                        st["rows"] = rows_of(cat, data)
                        view()
                elif m[0] == "err":
                    set_busy("")
                    status.set(m[2])
                    STATE["error"] = m[2]
                    if m[1] in ("auth", "nokey"):
                        key_msg.set(m[2])
                elif m[0] == "progress":
                    for k in ("detail", "compare"):
                        if k in st:
                            try:
                                st[k][2].set(m[1])
                            except tk.TclError:
                                pass
                elif m[0] == "done_more":
                    set_busy("")
                    for k in ("detail", "compare"):
                        if k in st:
                            try:
                                st[k][1]()
                                st[k][2].set("Готово")
                            except tk.TclError:
                                pass
        except queue.Empty:
            pass
        except tk.TclError:
            return
        STATE["left"], STATE["reset"] = api.remaining(), api.reset_in()
        if st["alive"]:
            parent.after(150, pump)

    def provider():
        s = dict(STATE)
        return {"active": False, "label": ("Нужен ключ" if not s["has_key"] else
                                           ("Загрузка…" if s["busy"] else "Готов"), "warn" if not s["has_key"] else "ok"),
                "status": s["busy"] or "Рынок", "market": s,
                "items": [("Позиций", s["n"])]}
    widgets.register("market", provider)

    def stop():
        st["alive"] = False
        c = st.get("cancel")
        if c:
            c.set()

    parent.after(100, pump)
    if api.key():
        parent.after(400, lambda: load())          # свежий кэш подхватится без запроса
    return stop
