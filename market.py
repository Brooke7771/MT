# -*- coding: utf-8 -*-
"""Рынок: цены Маркетплейса Majestic RP по серверам (официальное API, id.majestic-rp.ru/api).
Данные API обновляются раз в сутки, поэтому их собирает один раз в сутки GitHub Actions в репозитории
mt-market (ключ API владельца - только в секрете того репозитория) и раздаёт всем через релиз «data».
Свой ключ необязателен: он нужен, только если общий источник недоступен. Лимит API - 5 запросов в 60 с."""
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
SHARED = os.environ.get("MT_MARKET_URL", "https://github.com/Brooke7771/mt-market/releases/download/data")
INDEX_TTL = 10 * 60                  # как часто спрашивать общий источник, не появились ли новые данные

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
        self._shared = {"index": None, "index_ts": 0.0, "got": {}, "err": ""}
        self._slock = threading.Lock()

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

    # ---- общий источник (собранные раз в сутки данные всех серверов)
    def _get(self, name, timeout=40):
        url = f"{SHARED}/{name}"
        if url.startswith("file:"):
            with open(url[5:], "rb") as f:
                return f.read()
        req = urllib.request.Request(url, headers={"User-Agent": "MajesticTools/1.0"})
        with net.urlopen(req, timeout=timeout) as r:
            return r.read()

    def shared_index(self, force=False):
        """index.json общего источника (спрашиваем не чаще раза в 10 минут). -> dict или None."""
        s = self._shared
        now = time.time()
        if s["index"] is not None and now - s["index_ts"] < (60 if force else INDEX_TTL):
            return s["index"]
        if s["index"] is None and now - s["index_ts"] < 60:
            return None                                    # только что не получилось - не долбим
        s["index_ts"] = now
        try:
            s["index"] = json.loads(self._get("index.json", 20).decode("utf-8"))
            s["err"] = ""
        except Exception as ex:
            s["err"] = str(ex)
        return s["index"]

    def _shared_fetch(self, cat, server, force=False):
        """Категория из общего источника (один файл = все серверы). -> данные сервера или None."""
        import gzip
        with self._slock:
            idx = self.shared_index(force)
            if not idx or cat not in (idx.get("cats") or {}):
                return None
            gen = idx.get("generated", 0)
            c = self.cached(cat, server)
            want = (idx["cats"][cat] or {}).get(server, "")
            if c and want and (c[1] or {}).get("lastUpdated", "") >= want:
                if time.time() - c[0] > TTL:
                    self._store(cat, server, c[1])         # то же самое: просто отмечаем «проверено»
                return c[1]
            if self._shared["got"].get(cat) != gen:
                try:
                    d = json.loads(gzip.decompress(self._get(f"{cat}.json.gz")).decode("utf-8"))
                except Exception as ex:
                    self._shared["err"] = str(ex)
                    return None
                for sid, res in (d.get("servers") or {}).items():
                    if isinstance(res, dict) and CAT_BY[cat][2] in res:
                        self._store(cat, sid, res)
                self._shared["got"][cat] = gen
            c = self._mem.get((cat, server))
            return c[1] if c else None

    def shared_history(self, cat, server):
        """Снимки по дням из общего источника: {"days": {...}} или None."""
        import gzip
        if self.shared_index() is None:
            return None
        try:
            return json.loads(gzip.decompress(self._get(f"hist_{cat}_{server}.json.gz")).decode("utf-8"))
        except Exception:
            return None

    def shared_ok(self):
        return self._shared["index"] is not None

    def ready(self):
        """Можно ли получить данные: общий источник или свой ключ."""
        return bool(self.key()) or self.shared_index() is not None

    # ---- запрос
    def fetch(self, cat, server, force=False, cancel=None):
        """Статистика категории на сервере: кэш, если свежий; иначе общий источник; иначе своим ключом."""
        c = self.cached(cat, server)
        if c and not force and time.time() - c[0] < TTL:
            return c[1]
        res = self._shared_fetch(cat, server, force)
        if res is not None:
            return res
        key = self.key()
        if not key:
            if c:
                return c[1]
            raise ApiError("net", "Общий источник цен сейчас недоступен (нет интернета?). Можно вставить свой ключ API")
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
HOOKS = {"toast": None, "open": None}       # главное окно подставляет: всплывашка в окне и «открыть Рынок»


def build_market_tab(parent):
    import csv
    import webbrowser
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    import theme as T
    import widgets
    import market_watch as W

    api = Api()
    store = W.Store()
    store.remote = api.shared_history
    ui = store.settings.setdefault("ui", {})
    q = queue.Queue()
    st = {"rows": [], "data": None, "cat": ui.get("cat", "items"), "server": ui.get("server", "ru19"),
          "alive": True, "cancel": None, "busy": False, "shown": [], "popups": []}

    def sname(sid):
        return f"{sid.upper()} · {SERVER_NAME.get(sid, '')}"

    def save_ui(**kw):
        ui.update(kw)
        store.save()

    # ================================================================ ключ API (сохраняется сам)
    keybox = ttk.LabelFrame(parent, text="Свой ключ API Majestic (необязательно)")
    keybox.pack(fill="x", padx=8, pady=(0, 4))
    key_var = tk.StringVar(value=api.key())
    key_msg = tk.StringVar()
    ke = ttk.Entry(keybox, textvariable=key_var, width=40, show="•")
    ke.pack(side="left", padx=(8, 4), pady=6)
    try:
        import licenseui
        licenseui.enable_clipboard(ke)
    except Exception:
        pass
    shown_key = {"on": False}

    def toggle_show():
        shown_key["on"] = not shown_key["on"]
        ke.configure(show="" if shown_key["on"] else "•")
        eye.configure(text="Скрыть" if shown_key["on"] else "Показать")

    def autosave(*a):
        k = key_var.get().strip()
        if k and k != api.key() and len(k) >= 16:
            api.set_key(k)
            STATE["has_key"] = True
            key_msg.set("Ключ сохранён: вводить заново не нужно")
            monitor.check_now()
    key_var.trace_add("write", lambda *a: parent.after(600, autosave))

    def paste_key():
        try:
            key_var.set(ke.clipboard_get().strip())
        except tk.TclError:
            pass

    def check_key():
        k = key_var.get().strip()
        if not k:
            key_msg.set("Вставь ключ")
            return
        api.set_key(k)
        key_msg.set("Проверяю ключ…")

        def work():
            try:
                api.fetch("offices", "ru1", force=True)
                q.put(("keymsg", "Ключ работает ✓"))
            except ApiError as ex:
                q.put(("keymsg", "Неверный ключ" if ex.kind == "auth" else str(ex)))
        threading.Thread(target=work, daemon=True).start()
    ttk.Button(keybox, text="Вставить", command=paste_key).pack(side="left", padx=2)
    eye = ttk.Button(keybox, text="Показать", command=toggle_show)
    eye.pack(side="left", padx=2)
    ttk.Button(keybox, text="Проверить ключ", style="Accent.TButton", command=check_key).pack(side="left", padx=2)
    ttk.Button(keybox, text="Получить ключ", command=lambda: webbrowser.open("https://id.majestic-rp.ru/api")
               ).pack(side="left", padx=2)
    ttk.Label(keybox, textvariable=key_msg, style="Muted.TLabel").pack(side="left", padx=8)
    STATE["has_key"] = True             # цены идут из общего источника, свой ключ - запасной вариант
    key_msg.set("Свой ключ сохранён (запасной вариант)" if api.key() else
                "Не нужен: цены приходят сами раз в сутки")

    nb = ttk.Notebook(parent)
    nb.pack(fill="both", expand=True, padx=6, pady=2)
    tabs = {}
    for k_, t_ in (("prices", "Цены"), ("watch", "Мониторинг"), ("deals", "Выгода"), ("trends", "Тренды"),
                   ("calc", "Калькулятор"), ("notify", "Уведомления")):
        f = ttk.Frame(nb)
        nb.add(f, text=t_)
        tabs[k_] = f

    def show_tab(k_):
        nb.select(tabs[k_])

    # ================================================================ вкладка «Цены»
    P = tabs["prices"]
    bar = ttk.Frame(P)
    bar.pack(fill="x", padx=4, pady=(6, 2))
    cat_var = tk.StringVar(value=CAT_BY.get(st["cat"], CATS[0])[1])
    srv_var = tk.StringVar(value=sname(st["server"]))
    find_var = tk.StringVar()
    sort_var = tk.StringVar(value=ui.get("sort", "Продано"))
    desc_var = tk.BooleanVar(value=ui.get("desc", True))
    nz_var = tk.BooleanVar(value=ui.get("nz", True))
    allcat_var = tk.BooleanVar(value=False)
    ttk.Label(bar, text="Категория:").pack(side="left")
    cb_cat = ttk.Combobox(bar, textvariable=cat_var, values=[c[1] for c in CATS], width=11, state="readonly")
    cb_cat.pack(side="left", padx=(4, 10))
    ttk.Label(bar, text="Сервер:").pack(side="left")
    cb_srv = ttk.Combobox(bar, textvariable=srv_var, values=[sname(s) for s, _ in SERVERS], width=20, state="readonly")
    cb_srv.pack(side="left", padx=(4, 10))
    ttk.Label(bar, text="Поиск:").pack(side="left")
    ttk.Entry(bar, textvariable=find_var, width=18).pack(side="left", padx=(4, 6))
    ttk.Checkbutton(bar, text="во всех категориях", variable=allcat_var, command=lambda: all_categories()).pack(side="left")
    ttk.Button(bar, text="Обновить", style="Accent.TButton", command=lambda: load(force=True)).pack(side="left", padx=(10, 2))

    bar2 = ttk.Frame(P)
    bar2.pack(fill="x", padx=4, pady=(2, 4))
    SORTS = {"Название": "name", "Средняя цена": "avg", "Мин. цена": "min", "Макс. цена": "max",
             "Продано": "sold", "В продаже": "total"}
    ttk.Label(bar2, text="Сортировка:").pack(side="left")
    ttk.Combobox(bar2, textvariable=sort_var, values=list(SORTS), width=13, state="readonly").pack(side="left", padx=4)
    ttk.Checkbutton(bar2, text="по убыванию", variable=desc_var, command=lambda: view()).pack(side="left", padx=6)
    ttk.Checkbutton(bar2, text="только с продажами", variable=nz_var, command=lambda: view()).pack(side="left", padx=4)
    ttk.Button(bar2, text="В мониторинг…", command=lambda: add_from_table()).pack(side="left", padx=(12, 2))
    ttk.Button(bar2, text="Сравнить серверы", command=lambda: open_compare()).pack(side="left", padx=2)
    ttk.Button(bar2, text="Экспорт CSV", command=lambda: export_prices()).pack(side="left", padx=2)
    status = tk.StringVar(value="")
    ttk.Label(P, textvariable=status, style="Muted.TLabel").pack(anchor="w", padx=6)
    ttk.Label(P, text="Данные: официальное API Majestic RP (статистика за 30 дней), общий сбор раз в сутки (~05:40 по Киеву). "
                      "Двойной щелчок - цены по серверам, правый клик - меню.", style="Muted.TLabel").pack(
        side="bottom", anchor="w", padx=6, pady=(2, 4))
    box = ttk.Frame(P)
    box.pack(fill="both", expand=True, padx=4, pady=2)
    cols = ("name", "avg", "min", "max", "sold", "total")
    tv = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse", height=12)
    for c, h, w, a in (("name", "Название", 300, "w"), ("avg", "Средняя", 110, "e"), ("min", "Мин.", 100, "e"),
                       ("max", "Макс.", 100, "e"), ("sold", "Продано", 110, "e"), ("total", "В продаже", 110, "e")):
        tv.heading(c, text=h, command=lambda c=c: sort_by(c))
        tv.column(c, width=w, anchor=a)
    sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tv.tag_configure("odd", background=T.FIELD_ALT)
    tv.tag_configure("watched", foreground="#ffb347")
    SORT_COL = {v: k for k, v in SORTS.items()}

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

    def set_busy(text):
        STATE["busy"] = text
        st["busy"] = bool(text)

    def load(force=False):
        if st["busy"]:
            return
        autosave()
        allcat_var.set(False)
        cat, srv = cur_cat(), cur_srv()
        st["cat"], st["server"] = cat, srv
        save_ui(cat=cat, server=srv)
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

    def all_categories():
        """Поиск сразу по всем категориям сервера: недостающие категории догружаются по лимиту."""
        if not allcat_var.get():
            load()
            return
        if st["busy"]:
            allcat_var.set(False)
            return
        srv = cur_srv()
        set_busy("Загружаю все категории…")

        def work():
            rows = []
            for cat, title, *_ in CATS:
                try:
                    q.put(("progress_status", f"Загружаю {title}…"))
                    d = api.fetch(cat, srv)
                    store.record(cat, srv, d)
                    for r in rows_of(cat, d):
                        r["cat"] = cat
                        rows.append(r)
                except ApiError as ex:
                    q.put(("err", ex.kind, str(ex)))
                    break
            q.put(("allcat", srv, rows))
        threading.Thread(target=work, daemon=True).start()

    def watched_keys():
        with store.lock:
            return {(it["cat"], str(it["key"])) for it in store.items}

    def view():
        if st["data"] is None and not st["rows"]:
            return
        rows = st["rows"]
        s = find_var.get().strip().lower()
        if s:
            rows = [r for r in rows if s in r["name"].lower()]
        if nz_var.get():
            rows = [r for r in rows if r["sold"] > 0]
        f = SORTS.get(sort_var.get(), "sold")
        rows = sorted(rows, key=lambda r: r[f], reverse=bool(desc_var.get()))
        save_ui(sort=sort_var.get(), desc=bool(desc_var.get()), nz=bool(nz_var.get()))
        tv.delete(*tv.get_children())
        shown = rows[:1500]
        wk = watched_keys()
        for i, r in enumerate(shown):
            cat = r.get("cat", st["cat"])
            name = r["name"] + (f"  ({CAT_BY[cat][1]})" if r.get("cat") else "")
            tags = ["odd"] if i % 2 else []
            if (cat, r["key"]) in wk:
                tags.append("watched")
                name = "★ " + name
            tv.insert("", "end", iid=f"{i}", values=(name, money(r["avg"]), money(r["min"]), money(r["max"]),
                                                      num(r["sold"]), num(r["total"])), tags=tags)
        st["shown"] = shown
        data = st["data"] or {}
        lu = data.get("lastUpdated", "")
        status.set(f"{sname(st['server'])} · {'все категории' if allcat_var.get() else CAT_BY[st['cat']][1]} · показано "
                   f"{len(shown)} из {len(rows)}" + (f" · обновлено {lu[:16].replace('T', ' ')} UTC" if lu else ""))
        top = sorted(st["rows"], key=lambda r: -r["sold"])[:3]
        STATE.update(server=sname(st["server"]), cat=CAT_BY[st["cat"]][1], n=len(st["rows"]),
                     top=[(r["name"], r["sold"]) for r in top], updated=lu[11:16], period=data.get("periodDays") or 0,
                     error="")

    find_var.trace_add("write", lambda *a: view())
    sort_var.trace_add("write", lambda *a: view())
    cb_cat.bind("<<ComboboxSelected>>", lambda e: load())
    cb_srv.bind("<<ComboboxSelected>>", lambda e: load())

    def selected_row():
        sel = tv.selection()
        return st["shown"][int(sel[0])] if sel else None

    def export_prices():
        if not st["shown"]:
            messagebox.showinfo("Экспорт", "Таблица пуста", parent=parent)
            return
        path = filedialog.asksaveasfilename(parent=parent, defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")],
                                            initialfile=f"рынок_{st['server']}_{time.strftime('%Y%m%d')}.csv")
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Название", "Категория", "Средняя", "Мин", "Макс", "Продано", "В продаже"])
            for r in st["shown"]:
                w.writerow([r["name"], CAT_BY[r.get("cat", st["cat"])][1], r["avg"], r["min"], r["max"], r["sold"], r["total"]])
        status.set(f"Сохранено: {path}")

    menu = tk.Menu(tv, tearoff=0)
    menu.add_command(label="Добавить в мониторинг…", command=lambda: add_from_table())
    menu.add_command(label="Цены по всем серверам", command=lambda: open_detail())
    menu.add_command(label="График цены", command=lambda: (lambda r: r and open_chart(r.get("cat", st["cat"]), st["server"], r["key"], r["name"]))(selected_row()))
    menu.add_command(label="Открыть в калькуляторе", command=lambda: (lambda r: r and calc_fill(r["avg"], r["avg"]))(selected_row()))
    menu.add_command(label="Копировать название", command=lambda: (lambda r: r and (parent.clipboard_clear(), parent.clipboard_append(r["name"])))(selected_row()))

    def popup_menu(e):
        iid = tv.identify_row(e.y)
        if iid:
            tv.selection_set(iid)
            menu.tk_popup(e.x_root, e.y_root)
    tv.bind("<Button-3>", popup_menu)

    # ---------------- окно «цены по серверам»
    def open_detail(event=None):
        r = selected_row()
        if not r:
            return
        cat = r.get("cat", st["cat"])
        dlg = tk.Toplevel(parent)
        dlg.title(r["name"])
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.geometry("660x580")
        ttk.Label(dlg, text=r["name"], font=T.FONT_TITLE).pack(anchor="w", padx=14, pady=(12, 0))
        info = tk.StringVar()
        ttk.Label(dlg, textvariable=info, style="Muted.TLabel").pack(anchor="w", padx=14)
        t2 = ttk.Treeview(dlg, columns=("srv", "avg", "min", "max", "sold"), show="headings", height=17)
        for c, h, w, a in (("srv", "Сервер", 200, "w"), ("avg", "Средняя", 110, "e"), ("min", "Мин.", 100, "e"),
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
            info.set(f"Серверов с данными: {len(per)} из {len(SERVERS)}"
                     + (f". Дешевле всего на {sname(lo)}, дороже на {sname(hi)}" if lo and hi and lo != hi else ""))
        fill()
        cancel = {"ev": None}

        def more():
            if st["busy"]:
                return
            ev = threading.Event()
            cancel["ev"] = ev
            set_busy("Догружаю серверы…")
            threading.Thread(target=load_servers, args=(cat, ev), daemon=True).start()
        ttk.Button(btns, text="Догрузить все серверы", style="Accent.TButton", command=more).pack(side="left")
        ttk.Button(btns, text="Остановить", command=lambda: cancel["ev"] and cancel["ev"].set()).pack(side="left", padx=6)
        ttk.Button(btns, text="В мониторинг…", command=lambda: watch_dialog(cat=cat, key=r["key"], name=r["name"],
                                                                           price=r["avg"])).pack(side="left", padx=6)
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

    def load_servers(cat, ev):
        """Догрузить все серверы категории (по лимиту API), прогресс - в открытые окна."""
        for sid, _ in SERVERS:
            if ev.is_set():
                break
            c = api.cached(cat, sid)
            if c and time.time() - c[0] < TTL:
                continue
            q.put(("progress", f"Загружаю {sname(sid)}…"))
            try:
                d = api.fetch(cat, sid, cancel=ev)
                store.record(cat, sid, d)
            except ApiError as ex:
                if ex.kind != "cancel":
                    q.put(("err", ex.kind, str(ex)))
                break
        q.put(("done_more", None))

    # ---------------- окно «сравнить серверы» (старое, для быстрого взгляда)
    def open_compare():
        show_tab("deals")
        deals_cat.set(CAT_BY[cur_cat()][1])
        deals_show()

    # ================================================================ вкладка «Мониторинг»
    Wt = tabs["watch"]
    wbar = ttk.Frame(Wt)
    wbar.pack(fill="x", padx=4, pady=(6, 2))
    for text, cmd, sty in (("Добавить…", lambda: watch_dialog(), "Accent.TButton"), ("Изменить…", lambda: edit_watch(), None),
                           ("Удалить", lambda: remove_watch(), None), ("Вкл / выкл", lambda: toggle_watch(), None),
                           ("График…", lambda: chart_watch(), None), ("Проверить сейчас", lambda: check_now(), None),
                           ("Экспорт CSV", lambda: export_watch(), None)):
        b = ttk.Button(wbar, text=text, command=cmd, **({"style": sty} if sty else {}))
        b.pack(side="left", padx=2)
    mon_var = tk.StringVar()
    ttk.Label(Wt, textvariable=mon_var, style="Muted.TLabel").pack(anchor="w", padx=6, pady=(2, 2))
    wbox = ttk.Frame(Wt)
    wbox.pack(fill="both", expand=True, padx=4, pady=2)
    wcols = ("name", "cat", "srv", "cond", "now", "ch", "state")
    wt = ttk.Treeview(wbox, columns=wcols, show="headings", selectmode="browse", height=12)
    for c, h, w, a in (("name", "Товар", 220, "w"), ("cat", "Категория", 90, "w"), ("srv", "Серверы", 110, "w"),
                       ("cond", "Условие", 230, "w"), ("now", "Сейчас", 110, "e"), ("ch", "За сутки", 80, "e"),
                       ("state", "Статус", 170, "w")):
        wt.heading(c, text=h)
        wt.column(c, width=w, anchor=a)
    wsb = ttk.Scrollbar(wbox, orient="vertical", command=wt.yview)
    wt.configure(yscrollcommand=wsb.set)
    wt.pack(side="left", fill="both", expand=True)
    wsb.pack(side="right", fill="y")
    wt.tag_configure("hit", foreground=T.OK)
    wt.tag_configure("off", foreground=T.MUTED)
    wt.tag_configure("odd", background=T.FIELD_ALT)
    ttk.Label(Wt, text="Проверка идёт сама: после ежедневного обновления API, при запуске программы и по кнопке. "
                       "Уведомление приходит один раз, когда условие выполнилось.", style="Muted.TLabel").pack(
        anchor="w", padx=6, pady=(2, 6))

    def srv_text(servers):
        if not servers or "*" in servers:
            return "все серверы"
        return ", ".join(s.upper() for s in servers[:4]) + (f" +{len(servers) - 4}" if len(servers) > 4 else "")

    def watch_refresh():
        sel = wt.selection()
        wt.delete(*wt.get_children())
        with store.lock:
            items = list(store.items)
        for i, it in enumerate(items):
            last = it.get("last") or {}
            vals = [v for s, v in last.items() if s in W.expand(it.get("servers")) and v.get("val") is not None]
            now_txt, ch_txt = "—", "—"
            if vals:
                if it["cond"] in ("avg_le", "min_le"):
                    best = min(vals, key=lambda v: v["val"])
                elif it["cond"] == "avg_ge":
                    best = max(vals, key=lambda v: v["val"])
                else:
                    best = max(vals, key=lambda v: abs(v["val"] or 0))
                now_txt = W.fmt_value(it["cond"], best["val"])
                chs = [v["ch"] for v in vals if v.get("ch") is not None]
                if chs:
                    ch_txt = f"{max(chs, key=abs):+g}%"
            hits = [s.upper() for s, v in (it.get("state") or {}).items() if v]
            if not it.get("enabled"):
                state, tag = "выключено", "off"
            elif it.get("snooze", 0) > time.time():
                state, tag = "отложено до " + time.strftime("%d.%m %H:%M", time.localtime(it["snooze"])), "off"
            elif hits:
                state, tag = "✓ выполнено: " + ", ".join(hits[:3]) + ("…" if len(hits) > 3 else ""), "hit"
            elif vals:
                state, tag = "ждёт", ""
            else:
                state, tag = "нет данных (проверка ещё не было)", ""
            tags = [t for t in (tag, "odd" if i % 2 else "") if t]
            wt.insert("", "end", iid=it["id"], tags=tags, values=(
                it["name"], CAT_BY[it["cat"]][1], srv_text(it.get("servers")),
                f"{W.COND_NAME[it['cond']]} {W.fmt_value(it['cond'], it['value'])}", now_txt, ch_txt, state))
        if sel and wt.exists(sel[0]):
            wt.selection_set(sel[0])
        s = monitor.status
        lc = time.strftime("%d.%m %H:%M", time.localtime(s["last_check"])) if s["last_check"] else "ещё не было"
        nc = time.strftime("%d.%m %H:%M", time.localtime(s["next_check"]))
        mon_var.set(f"В мониторинге: {len(items)}  ·  последняя проверка: {lc}  ·  следующая: ≈{nc} (после обновления API)"
                    f"  ·  сработало сегодня: {store.alerts_today()}" + (f"  ·  {s['busy']}" if s["busy"] else "")
                    + (f"  ·  ⚠ {s['error']}" if s["error"] else ""))

    def sel_watch():
        s = wt.selection()
        return store.get(s[0]) if s else None

    def watch_dialog(item=None, cat=None, key=None, name=None, price=None):
        """Добавить/изменить позицию мониторинга."""
        dlg = tk.Toplevel(parent)
        dlg.title("Мониторинг: " + ("изменить" if item else "добавить"))
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.grab_set()
        cat = item["cat"] if item else (cat or cur_cat())
        v_cat = tk.StringVar(value=CAT_BY[cat][1])
        v_name = tk.StringVar(value=item["name"] if item else (name or ""))
        chosen = {"key": item["key"] if item else (str(key) if key is not None else None),
                  "name": item["name"] if item else name}
        pad = {"padx": 12, "pady": 4}
        ttk.Label(dlg, text="Категория:").grid(row=0, column=0, sticky="w", **pad)
        cbc = ttk.Combobox(dlg, textvariable=v_cat, values=[c[1] for c in CATS], width=14, state="readonly")
        cbc.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(dlg, text="Товар (начни вводить):").grid(row=1, column=0, sticky="w", **pad)
        en = ttk.Entry(dlg, textvariable=v_name, width=40)
        en.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        lb = tk.Listbox(dlg, height=6, width=52, bg=T.FIELD, fg=T.TEXT, selectbackground=T.ACCENT, relief="flat",
                        highlightthickness=1, highlightbackground=T.BORDER, activestyle="none")
        lb.grid(row=2, column=1, columnspan=2, sticky="w", padx=12)
        hint = tk.StringVar()
        ttk.Label(dlg, textvariable=hint, style="Muted.TLabel").grid(row=3, column=1, columnspan=2, sticky="w", padx=12)
        found = []

        def catalog(c):
            """Названия категории из любого кэша (сначала текущий сервер)."""
            for sid in [cur_srv()] + [s for s, _ in SERVERS]:
                cc = api.cached(c, sid)
                if cc:
                    return {r["key"]: r for r in rows_of(c, cc[1])}
            return {}

        def search(*a):
            c = next((x[0] for x in CATS if x[1] == v_cat.get()), "items")
            cat_rows = catalog(c)
            s = v_name.get().strip().lower()
            lb.delete(0, "end")
            found.clear()
            if not cat_rows:
                hint.set("Нет данных по категории: открой её во вкладке «Цены», тогда появятся названия")
                return
            res = sorted((r for r in cat_rows.values() if s in r["name"].lower()), key=lambda r: -r["sold"])[:60]
            for r in res:
                found.append(r)
                lb.insert("end", f"{r['name']}   ·   ср. {money(r['avg'])}")
            hint.set(f"Найдено: {len(res)}" + (" (показаны первые 60)" if len(res) == 60 else "") +
                     ("" if chosen["key"] is None else f"   ·   выбрано: {chosen['name']}"))

        def pick(e=None):
            s = lb.curselection()
            if not s:
                return
            r = found[s[0]]
            chosen.update(key=r["key"], name=r["name"])
            v_name.set(r["name"])
            if not v_val.get().strip() or v_val.get() == "0":
                v_val.set(str(max(1, int(r["avg"] * 0.9))) if r["avg"] else "")
            search()
        lb.bind("<<ListboxSelect>>", pick)
        v_name.trace_add("write", lambda *a: search())
        cbc.bind("<<ComboboxSelected>>", lambda e: (chosen.update(key=None, name=None), search()))

        ttk.Label(dlg, text="Серверы:").grid(row=4, column=0, sticky="nw", **pad)
        sf = ttk.Frame(dlg)
        sf.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        v_all = tk.BooleanVar(value=(not item) and False or bool(item and "*" in item.get("servers", [])))
        servers_init = set(item.get("servers", [])) if item else {cur_srv()}
        sv = {}
        ttk.Checkbutton(sf, text="все серверы", variable=v_all, command=lambda: sync_all()).grid(row=0, column=0, columnspan=5, sticky="w")
        for i, (sid, sn) in enumerate(SERVERS):
            sv[sid] = tk.BooleanVar(value=sid in servers_init)
            ttk.Checkbutton(sf, text=sid.upper(), variable=sv[sid]).grid(row=1 + i // 7, column=i % 7, sticky="w", padx=2)

        def sync_all():
            for v in sv.values():
                v.set(v_all.get())

        ttk.Label(dlg, text="Условие:").grid(row=5, column=0, sticky="w", **pad)
        v_cond = tk.StringVar(value=W.COND_NAME[item["cond"]] if item else W.CONDS[0][1])
        ttk.Combobox(dlg, textvariable=v_cond, values=[c[1] for c in W.CONDS], width=32, state="readonly").grid(
            row=5, column=1, sticky="w", **pad)
        v_val = tk.StringVar(value=(f"{item['value']:g}" if item else (str(max(1, int(price * 0.9))) if price else "")))
        ttk.Label(dlg, text="Значение ($ или %):").grid(row=6, column=0, sticky="w", **pad)
        ttk.Entry(dlg, textvariable=v_val, width=14).grid(row=6, column=1, sticky="w", **pad)
        v_note = tk.StringVar(value=item.get("note", "") if item else "")
        ttk.Label(dlg, text="Заметка:").grid(row=7, column=0, sticky="w", **pad)
        ttk.Entry(dlg, textvariable=v_note, width=40).grid(row=7, column=1, columnspan=2, sticky="w", **pad)
        msg = tk.StringVar()
        ttk.Label(dlg, textvariable=msg, style="Warn.TLabel").grid(row=8, column=1, columnspan=2, sticky="w", padx=12)

        def ok():
            c = next((x[0] for x in CATS if x[1] == v_cat.get()), "items")
            if chosen["key"] is None:
                rows = catalog(c)
                exact = [r for r in rows.values() if r["name"].lower() == v_name.get().strip().lower()]
                if exact:
                    chosen.update(key=exact[0]["key"], name=exact[0]["name"])
            if chosen["key"] is None:
                msg.set("Выбери товар из списка")
                return
            try:
                val = float(v_val.get().replace(" ", "").replace(",", ".").replace("$", "").replace("%", ""))
            except ValueError:
                msg.set("Значение - число")
                return
            servers = ["*"] if v_all.get() else [s for s, v in sv.items() if v.get()]
            if not servers:
                msg.set("Отметь хотя бы один сервер")
                return
            cond = next(k for k, n in W.CONDS if n == v_cond.get())
            if item:
                store.update(item["id"], cat=c, key=chosen["key"], name=chosen["name"], servers=servers, cond=cond,
                             value=val, note=v_note.get().strip())
            else:
                store.add(c, chosen["key"], chosen["name"], servers, cond, val, v_note.get().strip())
            dlg.destroy()
            watch_refresh()
            view()
            monitor.check_now()
            show_tab("watch")
        ttk.Button(dlg, text="Сохранить", style="Accent.TButton", command=ok).grid(
            row=9, column=0, columnspan=3, sticky="ew", padx=12, pady=12)
        search()
        dlg.update_idletasks()                       # по центру окна программы
        top = parent.winfo_toplevel()
        dlg.geometry(f"+{top.winfo_rootx() + max(0, (top.winfo_width() - dlg.winfo_reqwidth()) // 2)}"
                     f"+{top.winfo_rooty() + max(0, (top.winfo_height() - dlg.winfo_reqheight()) // 3)}")
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    def add_from_table():
        r = selected_row()
        if not r:
            watch_dialog()
            return
        watch_dialog(cat=r.get("cat", st["cat"]), key=r["key"], name=r["name"], price=r["avg"])

    def edit_watch():
        it = sel_watch()
        if it:
            watch_dialog(item=it)

    wt.bind("<Double-1>", lambda e: edit_watch())

    def remove_watch():
        it = sel_watch()
        if it and messagebox.askyesno("Удалить", f"Убрать «{it['name']}» из мониторинга?", parent=parent):
            store.remove(it["id"])
            watch_refresh()
            view()

    def toggle_watch():
        it = sel_watch()
        if it:
            store.update(it["id"], enabled=not it.get("enabled"), snooze=0, state={})
            watch_refresh()
            monitor.check_now()

    def check_now():
        monitor.check_now()
        mon_var.set("Проверяю…")

    def chart_watch():
        it = sel_watch()
        if it:
            srv = next((s for s in W.expand(it.get("servers")) if store.series(it["cat"], s, it["key"])), W.expand(it.get("servers"))[0])
            open_chart(it["cat"], srv, it["key"], it["name"], it)

    def export_watch():
        path = filedialog.asksaveasfilename(parent=parent, defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")],
                                            initialfile=f"мониторинг_{time.strftime('%Y%m%d')}.csv")
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Товар", "Категория", "Сервер", "Дата", "Средняя", "Мин", "Макс", "Продано", "В продаже"])
            with store.lock:
                items = list(store.items)
            for it in items:
                for srv in W.expand(it.get("servers")):
                    for d, a, mn, mx, sold, tot in store.series(it["cat"], srv, it["key"]):
                        w.writerow([it["name"], CAT_BY[it["cat"]][1], srv.upper(), d, a, mn, mx, sold, tot])
        mon_var.set(f"Сохранено: {path}")

    # ---------------- график цены по дням (по снимкам)
    def open_chart(cat, srv, key, name, item=None):
        dlg = tk.Toplevel(parent)
        dlg.title(f"График: {name}")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.geometry("760x470")
        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(top, text=name, font=T.FONT_TITLE).pack(side="left")
        v_srv = tk.StringVar(value=sname(srv))
        srvs = W.expand(item.get("servers")) if item else [s for s, _ in SERVERS]
        cb = ttk.Combobox(top, textvariable=v_srv, values=[sname(s) for s in srvs], width=20, state="readonly")
        cb.pack(side="right")
        cv = tk.Canvas(dlg, bg="#14141a", highlightthickness=0, height=360)
        cv.pack(fill="both", expand=True, padx=12, pady=8)
        note = tk.StringVar()
        ttk.Label(dlg, textvariable=note, style="Muted.TLabel").pack(anchor="w", padx=12, pady=(0, 10))

        def draw(e=None):
            cv.delete("all")
            s = v_srv.get().split(" ")[0].lower()
            ser = store.series(cat, s, key)
            W_, H_ = cv.winfo_width(), cv.winfo_height()
            if len(ser) < 2:
                cv.create_text(W_ / 2, H_ / 2, fill=T.MUTED, font=("Segoe UI", 11),
                               text="Нужны данные минимум за 2 дня.\nСнимки копятся сами каждый день, пока программа открыта.",
                               justify="center")
                note.set(f"Снимков: {len(ser)}")
                return
            x0, x1, y0, y1 = 60, W_ - 20, 20, H_ - 34
            vals = [v for row in ser for v in (row[1], row[2], row[3]) if v > 0]
            lo, hi = min(vals), max(vals)
            if hi - lo < 1:
                hi = lo + 1
            if item and item["cond"] in ("avg_le", "min_le", "avg_ge"):
                lo, hi = min(lo, item["value"]), max(hi, item["value"])
            pad_ = (hi - lo) * 0.08
            lo, hi = lo - pad_, hi + pad_

            def X(i):
                return x0 + (x1 - x0) * i / max(1, len(ser) - 1)

            def Y(v):
                return y1 - (y1 - y0) * (v - lo) / (hi - lo)
            for k in range(5):
                v = lo + (hi - lo) * k / 4
                cv.create_line(x0, Y(v), x1, Y(v), fill="#24242c")
                cv.create_text(x0 - 6, Y(v), text=f"{int(v):,}".replace(",", " "), anchor="e", fill=T.MUTED, font=("Segoe UI", 8))
            for idx, col, w in ((3, "#5a3a3a", 1), (2, "#2f5a3a", 1), (1, T.ACCENT, 3)):
                pts = [c for i, row in enumerate(ser) for c in (X(i), Y(row[idx]))]
                cv.create_line(pts, fill=col, width=w, smooth=False)
            for i, row in enumerate(ser):
                cv.create_oval(X(i) - 3, Y(row[1]) - 3, X(i) + 3, Y(row[1]) + 3, fill="#ffffff", outline="")
                if i in (0, len(ser) - 1) or len(ser) <= 10:
                    cv.create_text(X(i), y1 + 14, text=row[0][5:], fill=T.MUTED, font=("Segoe UI", 8))
            if item and item["cond"] in ("avg_le", "min_le", "avg_ge"):
                cv.create_line(x0, Y(item["value"]), x1, Y(item["value"]), fill="#ffb347", dash=(6, 4))
                cv.create_text(x1, Y(item["value"]) - 8, text="цель", anchor="e", fill="#ffb347", font=("Segoe UI", 8, "bold"))
            first, last = ser[0][1], ser[-1][1]
            ch = (last - first) * 100.0 / first if first else 0
            note.set(f"Красная - средняя, зелёная - минимальная, тёмно-красная - максимальная. Дней: {len(ser)}. "
                     f"Средняя: {money(first)} → {money(last)} ({ch:+.1f}%)")
        cv.bind("<Configure>", draw)
        cb.bind("<<ComboboxSelected>>", draw)

    # ================================================================ вкладка «Выгода»
    D = tabs["deals"]
    dbar = ttk.Frame(D)
    dbar.pack(fill="x", padx=4, pady=(6, 2))
    s_ = store.settings
    deals_cat = tk.StringVar(value=CAT_BY.get((s_.get("arb_cats") or ["items"])[0], CATS[0])[1])
    v_sold, v_price, v_pct = tk.StringVar(value=str(s_.get("arb_sold", 50))), tk.StringVar(value=str(s_.get("arb_price", 1000))), tk.StringVar(value=str(s_.get("arb_pct", 30)))
    v_fee = tk.StringVar(value=f"{s_.get('fee', 3.0):g}")
    v_arb = tk.BooleanVar(value=bool(s_.get("arb")))
    ttk.Label(dbar, text="Категория:").pack(side="left")
    ttk.Combobox(dbar, textvariable=deals_cat, values=[c[1] for c in CATS], width=11, state="readonly").pack(side="left", padx=(4, 10))
    for lab, var, w in (("Мин. продаж:", v_sold, 7), ("Мин. цена, $:", v_price, 9), ("Мин. выгода, %:", v_pct, 5), ("Комиссия, %:", v_fee, 4)):
        ttk.Label(dbar, text=lab).pack(side="left")
        ttk.Entry(dbar, textvariable=var, width=w).pack(side="left", padx=(4, 10))
    dbar2 = ttk.Frame(D)
    dbar2.pack(fill="x", padx=4, pady=(2, 4))
    ttk.Button(dbar2, text="Показать", style="Accent.TButton", command=lambda: deals_show()).pack(side="left", padx=2)
    ttk.Button(dbar2, text="Загрузить все серверы", command=lambda: deals_load()).pack(side="left", padx=2)
    ttk.Checkbutton(dbar2, text="Каждый день искать сам и присылать уведомление", variable=v_arb,
                    command=lambda: deals_save()).pack(side="left", padx=12)
    deals_msg = tk.StringVar()
    ttk.Label(D, textvariable=deals_msg, style="Muted.TLabel").pack(anchor="w", padx=6)
    dt_ = ttk.Treeview(D, columns=("name", "buy", "sell", "net", "pct"), show="headings", height=12)
    for c, h, w, a in (("name", "Товар", 250, "w"), ("buy", "Купить", 200, "w"), ("sell", "Продать", 200, "w"),
                       ("net", "Чистыми с 1 шт.", 130, "e"), ("pct", "Выгода", 80, "e")):
        dt_.heading(c, text=h)
        dt_.column(c, width=w, anchor=a)
    dt_.tag_configure("odd", background=T.FIELD_ALT)
    dt_.pack(fill="both", expand=True, padx=4, pady=4)
    ttk.Label(D, text="Выгода считается после комиссии маркетплейса при продаже. Средние цены - за 30 дней, это ориентир, "
                      "а не гарантия: проверь цену в игре. Двойной щелчок - в калькулятор.", style="Muted.TLabel").pack(
        anchor="w", padx=6, pady=(0, 6))
    dres = {"rows": []}

    def num_of(var, d):
        try:
            return float(var.get().replace(" ", "").replace(",", "."))
        except ValueError:
            return d

    def deals_save():
        c = next((x[0] for x in CATS if x[1] == deals_cat.get()), "items")
        store.settings.update(arb=bool(v_arb.get()), arb_cats=[c], arb_sold=int(num_of(v_sold, 50)),
                              arb_price=int(num_of(v_price, 1000)), arb_pct=num_of(v_pct, 30), fee=num_of(v_fee, 3.0))
        store.save()
        if v_arb.get():
            monitor.check_now()

    def deals_show():
        deals_save()
        c = next((x[0] for x in CATS if x[1] == deals_cat.get()), "items")
        rows = W.deals(api, c, num_of(v_fee, 3.0), int(num_of(v_sold, 50)), int(num_of(v_price, 1000)))
        rows = [r for r in rows if r["net_pct"] >= num_of(v_pct, 0)][:400]
        dres["rows"] = rows
        dt_.delete(*dt_.get_children())
        for i, x in enumerate(rows):
            dt_.insert("", "end", iid=str(i), tags=("odd",) if i % 2 else (), values=(
                x["name"], f"{x['lo'].upper()} · {SERVER_NAME[x['lo']]} · {money(x['lo_p'])}",
                f"{x['hi'].upper()} · {SERVER_NAME[x['hi']]} · {money(x['hi_p'])}", money(x["net"]), f"+{x['net_pct']:.0f}%"))
        have = sum(1 for s, _ in SERVERS if api.cached(c, s))
        deals_msg.set(f"Серверов в кэше: {have} из {len(SERVERS)} · найдено сделок: {len(rows)}"
                      + ("" if have == len(SERVERS) else " · нажми «Загрузить все серверы»"))

    def deals_load():
        if st["busy"]:
            deals_msg.set("Подожди: идёт другая загрузка")
            return
        c = next((x[0] for x in CATS if x[1] == deals_cat.get()), "items")
        ev = threading.Event()
        set_busy("Загружаю серверы…")
        st["deals_loading"] = True
        threading.Thread(target=load_servers, args=(c, ev), daemon=True).start()

    dt_.bind("<Double-1>", lambda e: (lambda s: s and calc_fill(dres["rows"][int(s[0])]["lo_p"], dres["rows"][int(s[0])]["hi_p"]))(dt_.selection()))

    # ================================================================ вкладка «Тренды»
    R = tabs["trends"]
    rbar = ttk.Frame(R)
    rbar.pack(fill="x", padx=4, pady=(6, 2))
    tr_cat = tk.StringVar(value=CAT_BY.get(st["cat"], CATS[0])[1])
    tr_srv = tk.StringVar(value=sname(st["server"]))
    tr_per = tk.StringVar(value="за сутки")
    tr_sold = tk.StringVar(value="20")
    ttk.Label(rbar, text="Категория:").pack(side="left")
    ttk.Combobox(rbar, textvariable=tr_cat, values=[c[1] for c in CATS], width=11, state="readonly").pack(side="left", padx=(4, 10))
    ttk.Label(rbar, text="Сервер:").pack(side="left")
    ttk.Combobox(rbar, textvariable=tr_srv, values=[sname(s) for s, _ in SERVERS], width=20, state="readonly").pack(side="left", padx=(4, 10))
    ttk.Combobox(rbar, textvariable=tr_per, values=["за сутки", "за неделю"], width=10, state="readonly").pack(side="left", padx=(0, 10))
    ttk.Label(rbar, text="Мин. продаж:").pack(side="left")
    ttk.Entry(rbar, textvariable=tr_sold, width=6).pack(side="left", padx=(4, 10))
    ttk.Button(rbar, text="Показать", style="Accent.TButton", command=lambda: trends_show()).pack(side="left")
    tr_msg = tk.StringVar()
    ttk.Label(R, textvariable=tr_msg, style="Muted.TLabel").pack(anchor="w", padx=6)
    two = ttk.Frame(R)
    two.pack(fill="both", expand=True, padx=4, pady=4)
    tr_tables = {}
    for side, title in (("down", "Подешевело"), ("up", "Подорожало")):
        fr = ttk.LabelFrame(two, text=title)
        fr.pack(side="left", fill="both", expand=True, padx=3)
        t_ = ttk.Treeview(fr, columns=("name", "was", "now", "pct"), show="headings", height=14)
        for c, h, w, a in (("name", "Товар", 200, "w"), ("was", "Было", 90, "e"), ("now", "Стало", 90, "e"), ("pct", "%", 60, "e")):
            t_.heading(c, text=h)
            t_.column(c, width=w, anchor=a)
        t_.tag_configure("odd", background=T.FIELD_ALT)
        t_.pack(fill="both", expand=True, padx=4, pady=4)
        tr_tables[side] = t_

    def trends_show():
        c = next((x[0] for x in CATS if x[1] == tr_cat.get()), "items")
        s = tr_srv.get().split(" ")[0].lower()
        cc = api.cached(c, s)
        if cc:
            store.record(c, s, cc[1])
        d0, d1, rows = W.trends(store, c, s, 1 if tr_per.get() == "за сутки" else 7, int(num_of(tr_sold, 0)))
        for t_ in tr_tables.values():
            t_.delete(*t_.get_children())
        if not d0:
            tr_msg.set(f"Снимков по {CAT_BY[c][1]}, {s.upper()}: {len(store.days(c, s))}. Нужно минимум 2 дня - они копятся "
                       "раз в сутки; общий источник хранит до 35 дней, так что обычно они уже есть.")
            return
        names = {}
        if cc:
            names = {r["key"]: r["name"] for r in rows_of(c, cc[1])}
        down = sorted([r for r in rows if r["pct"] < 0], key=lambda r: r["pct"])[:60]
        up = sorted([r for r in rows if r["pct"] > 0], key=lambda r: -r["pct"])[:60]
        for side, lst in (("down", down), ("up", up)):
            for i, r in enumerate(lst):
                tr_tables[side].insert("", "end", tags=("odd",) if i % 2 else (), values=(
                    names.get(r["key"], r["key"]), money(r["was"]), money(r["now"]), f"{r['pct']:+.1f}%"))
        tr_msg.set(f"Сравнение {d0} → {d1} · товаров: {len(rows)}")

    # ================================================================ вкладка «Калькулятор»
    C = tabs["calc"]
    cv_buy, cv_sell, cv_qty, cv_fee = tk.StringVar(value="1000"), tk.StringVar(value="1300"), tk.StringVar(value="1"), tk.StringVar(value=f"{s_.get('fee', 3.0):g}")
    cf = ttk.Frame(C)
    cf.pack(anchor="nw", padx=16, pady=14)
    for i, (lab, var) in enumerate((("Цена покупки, $:", cv_buy), ("Цена продажи, $:", cv_sell), ("Количество, шт.:", cv_qty),
                                    ("Комиссия маркетплейса, %:", cv_fee))):
        ttk.Label(cf, text=lab).grid(row=i, column=0, sticky="w", pady=4)
        ttk.Entry(cf, textvariable=var, width=14).grid(row=i, column=1, sticky="w", padx=8, pady=4)
    res_var = tk.StringVar()
    ttk.Label(cf, textvariable=res_var, font=("Segoe UI", 12), justify="left").grid(row=0, column=2, rowspan=5, sticky="nw", padx=30)

    def calc(*a):
        b, s, n, f = num_of(cv_buy, 0), num_of(cv_sell, 0), max(1, num_of(cv_qty, 1)), num_of(cv_fee, 3.0)
        got = W.price_after_fee(s, f)
        prof = got - b
        be = b / (1 - f / 100.0) if f < 100 else 0
        res_var.set(f"Получишь с продажи 1 шт.: {money(got)}\nПрибыль с 1 шт.: {money(prof)}\n"
                    f"Прибыль за {int(n)} шт.: {money(prof * n)}\nМаржа: {(prof * 100.0 / b if b else 0):+.1f}%\n"
                    f"Продавать не дешевле: {money(be)} (в ноль)")
    for v in (cv_buy, cv_sell, cv_qty, cv_fee):
        v.trace_add("write", calc)
    calc()

    def calc_fill(buy, sell):
        cv_buy.set(str(int(buy)))
        cv_sell.set(str(int(sell)))
        show_tab("calc")

    # ================================================================ вкладка «Уведомления»
    N = tabs["notify"]
    nf = ttk.Frame(N)
    nf.pack(fill="x", padx=12, pady=10)
    n_popup, n_sound, n_tg = tk.BooleanVar(value=s_.get("popup", True)), tk.BooleanVar(value=s_.get("sound", True)), tk.BooleanVar(value=s_.get("tg", True))
    n_sname = tk.StringVar(value=s_.get("sound_name", "Уведомление"))
    n_quiet = tk.BooleanVar(value=s_.get("quiet", False))
    n_qf, n_qt = tk.StringVar(value=s_.get("quiet_from", "23:00")), tk.StringVar(value=s_.get("quiet_to", "08:00"))
    n_start = tk.BooleanVar(value=s_.get("check_on_start", True))

    def nsave(*a):
        store.settings.update(popup=bool(n_popup.get()), sound=bool(n_sound.get()), tg=bool(n_tg.get()),
                              sound_name=n_sname.get(), quiet=bool(n_quiet.get()), quiet_from=n_qf.get().strip(),
                              quiet_to=n_qt.get().strip(), check_on_start=bool(n_start.get()))
        store.save()
    ttk.Checkbutton(nf, text="Всплывающее окно поверх всех окон", variable=n_popup, command=nsave).grid(row=0, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Checkbutton(nf, text="Звук", variable=n_sound, command=nsave).grid(row=1, column=0, sticky="w", pady=3)
    cbs = ttk.Combobox(nf, textvariable=n_sname, values=list(W.SOUNDS), width=14, state="readonly")
    cbs.grid(row=1, column=1, sticky="w", padx=6)
    cbs.bind("<<ComboboxSelected>>", lambda e: (nsave(), W.play_sound(n_sname.get())))
    ttk.Button(nf, text="Послушать", command=lambda: W.play_sound(n_sname.get())).grid(row=1, column=2, sticky="w", padx=6)
    ttk.Checkbutton(nf, text="Telegram (бот из раздела «Готовка»)", variable=n_tg, command=nsave).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
    tg_state = tk.StringVar()
    ttk.Label(nf, textvariable=tg_state, style="Muted.TLabel").grid(row=2, column=2, sticky="w", padx=6)
    ttk.Button(nf, text="Тест в Telegram", command=lambda: tg_test()).grid(row=2, column=3, sticky="w", padx=6)
    qf = ttk.Frame(nf)
    qf.grid(row=3, column=0, columnspan=4, sticky="w", pady=3)
    ttk.Checkbutton(qf, text="Тихие часы: без окна и звука (Telegram приходит) с", variable=n_quiet, command=nsave).pack(side="left")
    ttk.Entry(qf, textvariable=n_qf, width=6).pack(side="left", padx=4)
    ttk.Label(qf, text="до").pack(side="left")
    ttk.Entry(qf, textvariable=n_qt, width=6).pack(side="left", padx=4)
    for v in (n_qf, n_qt):
        v.trace_add("write", nsave)
    ttk.Checkbutton(nf, text="Проверять мониторинг при запуске программы", variable=n_start, command=nsave).grid(row=4, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Button(nf, text="Показать тестовое уведомление", style="Accent.TButton", command=lambda: notify({
        "t": time.time(), "kind": "test", "item_id": None, "title": "Тест уведомления",
        "short": "Так будет выглядеть уведомление о цене", "text": "Красный горбыль (Предметы)\nУсловие: Средняя цена ≤ 100 $\n"
        "RU19 · Memphis: средняя 95 $, мин. 80 $, за сутки -5%"}, test=True)).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 4))
    ttk.Label(N, text="Журнал уведомлений", style="Big.TLabel").pack(anchor="w", padx=12, pady=(6, 2))
    jt = ttk.Treeview(N, columns=("t", "kind", "text"), show="headings", height=9)
    for c, h, w in (("t", "Время", 120), ("kind", "Тип", 110), ("text", "Событие", 640)):
        jt.heading(c, text=h)
        jt.column(c, width=w, anchor="w")
    jt.tag_configure("odd", background=T.FIELD_ALT)
    jt.pack(fill="both", expand=True, padx=12, pady=(0, 4))
    ttk.Button(N, text="Очистить журнал", command=lambda: (store.alerts.clear(), store.add_alert({"t": time.time(), "title": "", "short": "Журнал очищен", "kind": "info"}), journal_refresh())).pack(anchor="w", padx=12, pady=(0, 8))
    KIND = {"watch": "Цена", "arb": "Выгода", "test": "Тест", "info": ""}

    def journal_refresh():
        jt.delete(*jt.get_children())
        with store.lock:
            al = list(reversed(store.alerts))[:300]
        for i, a in enumerate(al):
            jt.insert("", "end", tags=("odd",) if i % 2 else (), values=(
                time.strftime("%d.%m %H:%M", time.localtime(a.get("t") or 0)), KIND.get(a.get("kind"), ""), a.get("short") or ""))

    def tg_test():
        def work():
            ok, m = W.telegram("✅ Majestic Tools: тест уведомлений рынка")
            q.put(("tgmsg", ("Telegram: сообщение отправлено ✓" if ok else m)))
        threading.Thread(target=work, daemon=True).start()
        tg_state.set("Отправляю…")

    # ================================================================ уведомления
    def popup(ev):
        """Окно поверх всех окон в правом нижнем углу, само закрывается через минуту."""
        win = tk.Toplevel(parent)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=T.ACCENT)
        body = tk.Frame(win, bg="#15151c")
        body.pack(fill="both", expand=True, padx=(4, 1), pady=1)
        col = T.OK if ev.get("kind") == "watch" else ("#38c6e0" if ev.get("kind") == "arb" else "#ffb347")
        tk.Label(body, text=("● " + (ev.get("title") or "Рынок")), bg="#15151c", fg=col, font=("Segoe UI", 12, "bold"),
                 anchor="w").pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(body, text=ev.get("text") or ev.get("short") or "", bg="#15151c", fg=T.TEXT, font=("Segoe UI", 10),
                 justify="left", anchor="w", wraplength=380).pack(fill="x", padx=14, pady=(0, 8))
        bb = tk.Frame(body, bg="#15151c")
        bb.pack(fill="x", padx=10, pady=(0, 12))

        def btn(text, cmd, accent=False):
            b = tk.Label(bb, text=text, bg=T.ACCENT if accent else "#23232b", fg="#ffffff", font=("Segoe UI", 9, "bold"),
                         padx=10, pady=5, cursor="hand2")
            b.pack(side="left", padx=4)
            b.bind("<Button-1>", lambda e: (cmd(), close()))

        def close():
            try:
                win.destroy()
            except tk.TclError:
                pass
            if win in st["popups"]:
                st["popups"].remove(win)

        def open_():
            if HOOKS.get("open"):
                HOOKS["open"]()
            show_tab("deals" if ev.get("kind") == "arb" else "watch")
        btn("Открыть", open_, True)
        if ev.get("kind") == "watch" and ev.get("item_id"):
            btn("Отложить на сутки", lambda: (store.update(ev["item_id"], snooze=time.time() + 86400), watch_refresh()))
            btn("Выключить", lambda: (store.update(ev["item_id"], enabled=False), watch_refresh()))
        btn("Закрыть", lambda: None)
        win.update_idletasks()
        w, h = max(420, win.winfo_reqwidth()), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        k = len(st["popups"])
        win.geometry(f"{w}x{h}+{sw - w - 20}+{max(10, sh - h - 70 - k * (h + 10))}")
        st["popups"].append(win)
        win.after(60000, close)

    def notify(ev, test=False):
        s = store.settings
        quiet = W.in_quiet(s) and not test
        try:
            import stats
            stats.event(f"Рынок: {ev.get('short')}", "ok")
        except Exception:
            pass
        if HOOKS.get("toast"):
            try:
                HOOKS["toast"](f"Рынок: {ev.get('short')}", "ok", 6.0)
            except Exception:
                pass
        if s.get("popup", True) and not quiet:
            popup(ev)
        if s.get("sound", True) and not quiet:
            W.play_sound(s.get("sound_name", "Уведомление"))
        if s.get("tg", True) and not test:
            threading.Thread(target=W.telegram, args=("📈 Majestic Tools · " + (ev.get("title") or "Рынок") + "\n" + (ev.get("text") or ev.get("short") or ""),),
                             daemon=True).start()
        if test:
            store.add_alert(ev)
        journal_refresh()
        watch_refresh()

    monitor = W.Monitor(api, store, lambda ev: q.put(("event", ev)))

    # ================================================================ обмен с потоками
    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "data":
                    _, cat, srv, data = m
                    set_busy("")
                    STATE["error"] = ""
                    store.record(cat, srv, data)
                    if (cat, srv) == (st["cat"], st["server"]) and not allcat_var.get():
                        st["data"] = data
                        st["rows"] = rows_of(cat, data)
                        view()
                elif m[0] == "allcat":
                    _, srv, rows = m
                    set_busy("")
                    st["rows"], st["data"] = rows, api.cached(st["cat"], srv)[1] if api.cached(st["cat"], srv) else {}
                    view()
                elif m[0] == "err":
                    set_busy("")
                    status.set(m[2])
                    STATE["error"] = m[2]
                    if m[1] in ("auth", "nokey"):
                        key_msg.set(m[2])
                elif m[0] == "progress":
                    deals_msg.set(m[1])
                    if "detail" in st:
                        try:
                            st["detail"][2].set(m[1])
                        except tk.TclError:
                            pass
                elif m[0] == "progress_status":
                    status.set(m[1])
                elif m[0] == "done_more":
                    set_busy("")
                    if "detail" in st:
                        try:
                            st["detail"][1]()
                            st["detail"][2].set("Готово")
                        except tk.TclError:
                            pass
                    if st.pop("deals_loading", None):
                        deals_show()
                elif m[0] == "keymsg":
                    key_msg.set(m[1])
                    STATE["has_key"] = bool(api.key())
                elif m[0] == "tgmsg":
                    tg_state.set(m[1])
                elif m[0] == "event":
                    notify(m[1])
        except queue.Empty:
            pass
        except tk.TclError:
            return
        STATE["left"], STATE["reset"] = api.remaining(), api.reset_in()
        ms = monitor.status
        with store.lock:
            STATE.update(watch_n=len(store.items), alerts_today=store.alerts_today(), next_check=ms["next_check"],
                         mon_busy=ms["busy"])
        if st["alive"]:
            st["tick"] = st.get("tick", 0) + 1
            if st["tick"] % 25 == 0:                    # раз в ~5 с обновляем таблицу мониторинга
                watch_refresh()
                tg_state.set(tg_state.get() if "Отправ" in tg_state.get() or "✓" in tg_state.get()
                             else ("бот подключён" if W.telegram_ready() else "бот не подключён (настрой в «Готовке»)"))
            parent.after(200, pump)

    def provider():
        s = dict(STATE)
        n_alerts = s.get("alerts_today", 0)
        return {"active": False, "label": (f"Сработало: {n_alerts}" if n_alerts else
                                           ("Загрузка…" if s["busy"] else ("Нет связи" if s.get("error") else "Готов")),
                                           "warn" if s.get("error") else "ok"),
                "status": s["busy"] or "Рынок", "market": s,
                "items": [("Позиций", s["n"])]}
    widgets.register("market", provider)

    def stop():
        st["alive"] = False
        monitor.stop()
        c = st.get("cancel")
        if c:
            c.set()

    tg_state.set("бот подключён" if W.telegram_ready() else "бот не подключён (настрой в «Готовке»)")
    def on_tab(e=None):
        k_ = next((k for k, f in tabs.items() if str(f) == nb.select()), "prices")
        if k_ == "deals" and not dres["rows"]:
            deals_show()
        elif k_ == "trends" and not tr_tables["up"].get_children() and not tr_tables["down"].get_children():
            trends_show()
        save_ui(tab=k_)
    nb.bind("<<NotebookTabChanged>>", on_tab)
    if ui.get("tab") in tabs:
        nb.select(tabs[ui["tab"]])
    watch_refresh()
    journal_refresh()
    monitor.start()
    parent.after(100, pump)
    parent.after(400, lambda: load())              # свежий кэш подхватится без запроса
    return stop
