# -*- coding: utf-8 -*-
"""Мониторинг рынка: список отслеживания, ежедневные снимки цен, условия и уведомления.

Как устроены данные API (проверено): статистика за последние 30 дней (средняя/мин/макс цена, продано,
в продаже), обновляется РАЗ В СУТКИ около 02:10 UTC. Живых лотов нет. Поэтому:
  - проверка идёт после ежедневного обновления (+ при запуске программы и по кнопке), а не «каждую минуту»;
  - один запрос = вся категория на одном сервере, поэтому все отслеживаемые товары одного сервера и категории
    проверяются одним запросом; фон оставляет хотя бы 1 запрос из 5/мин для ручного просмотра;
  - каждый день сохраняется снимок (для графиков, «изменилось за сутки» и трендов);
  - уведомление приходит один раз при выполнении условия; повтор - только если условие сначала перестало
    выполняться, а потом снова выполнилось."""
import os
import json
import time
import threading
import datetime as dt

import market as M

UPDATE_UTC = (2, 15)            # примерно когда API выкладывает свежие цифры (02:10 UTC + запас)
RETRY_SEC = 15 * 60             # если API ещё не обновилось - пробуем снова не чаще, чем раз в 15 минут
KEEP_DAYS = 35                  # сколько дней хранить снимки

CONDS = [("avg_le", "Средняя цена ≤"), ("min_le", "Мин. цена ≤"), ("avg_ge", "Средняя цена ≥"),
         ("drop_pct", "Средняя упала за сутки на, %"), ("rise_pct", "Средняя выросла за сутки на, %"),
         ("sold_up", "Продажи выросли за сутки на, %")]
COND_NAME = dict(CONDS)
PCT_CONDS = {"drop_pct", "rise_pct", "sold_up"}

DEFAULT_SETTINGS = {
    "popup": True, "sound": True, "sound_name": "Уведомление", "tg": True,
    "quiet": False, "quiet_from": "23:00", "quiet_to": "08:00",
    "arb": False, "arb_cats": ["items"], "arb_pct": 30, "arb_sold": 50, "arb_price": 1000,
    "fee": 3.0, "check_on_start": True, "ui": {},
}


def _path(name):
    return os.path.join(M.data_dir(), name)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def parse_utc(s):
    """'2026-10-17T02:11:23.691Z' -> epoch секунд (UTC) или 0."""
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    except Exception:
        return 0.0


def expected_update(now=None):
    """Момент последнего ожидаемого обновления API (epoch): сегодня в UPDATE_UTC, если уже прошло, иначе вчера."""
    now = time.time() if now is None else now
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    t = d.replace(hour=UPDATE_UTC[0], minute=UPDATE_UTC[1], second=0, microsecond=0)
    if d < t:
        t -= dt.timedelta(days=1)
    return t.timestamp()


def next_update(now=None):
    return expected_update(now) + 86400


def expand(servers):
    return [s for s, _ in M.SERVERS] if (not servers or "*" in servers) else list(servers)


def fmt_value(cond, v):
    return f"{v:g}%" if cond in PCT_CONDS else M.money(v)


# ====================================================================== хранилище
class Store:
    """Список отслеживания, настройки, журнал срабатываний и ежедневные снимки. Потокобезопасно."""

    def __init__(self):
        self.lock = threading.RLock()
        d = _load(_path("market_watch.json"), {})
        self.items = d.get("items", [])
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(d.get("settings", {}))
        self.arb_seen = d.get("arb_seen", {})
        self.alerts = _load(_path("market_alerts.json"), [])
        self._hist = {}
        self.remote = None              # (cat, srv) -> {"days": ...}: общие снимки, подмешиваются раз за запуск
        self._remote_done = set()

    def save(self):
        with self.lock:
            _save(_path("market_watch.json"), {"items": self.items, "settings": self.settings, "arb_seen": self.arb_seen})

    # ---- позиции
    def add(self, cat, key, name, servers, cond, value, note="", enabled=True):
        with self.lock:
            it = {"id": f"{int(time.time() * 1000)}{len(self.items)}", "cat": cat, "key": str(key), "name": name,
                  "servers": list(servers) or ["*"], "cond": cond, "value": float(value), "note": note,
                  "enabled": bool(enabled), "state": {}, "last": {}, "snooze": 0, "created": time.time()}
            self.items.append(it)
            self.save()
            return it

    def update(self, item_id, **kw):
        with self.lock:
            for it in self.items:
                if it["id"] == item_id:
                    if any(k in kw for k in ("cond", "value", "servers", "key")):
                        it["state"] = {}            # условие поменялось - заново ждём первое срабатывание
                    it.update(kw)
            self.save()

    def remove(self, item_id):
        with self.lock:
            self.items = [it for it in self.items if it["id"] != item_id]
            self.save()

    def get(self, item_id):
        with self.lock:
            return next((it for it in self.items if it["id"] == item_id), None)

    # ---- журнал
    def add_alert(self, ev):
        with self.lock:
            self.alerts.append({k: ev.get(k) for k in ("t", "title", "short", "kind", "item_id")})
            del self.alerts[:-300]
            _save(_path("market_alerts.json"), self.alerts)

    def alerts_today(self):
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        with self.lock:
            return sum(1 for a in self.alerts if (a.get("t") or 0) >= start)

    # ---- снимки
    def _hfile(self, cat, srv):
        return os.path.join(M.data_dir(), "market_history", f"{cat}_{srv}.json")

    def _hload(self, cat, srv):
        k = (cat, srv)
        if k not in self._hist:
            self._hist[k] = _load(self._hfile(cat, srv), {"days": {}})
        return self._hist[k]

    def warm(self, cat, srv):
        """Подмешать общие снимки (скачиваются вне блокировки, один раз за запуск)."""
        k = (cat, srv)
        if not self.remote or k in self._remote_done:
            return
        self._remote_done.add(k)
        r = self.remote(cat, srv)
        if not r or not r.get("days"):
            return
        with self.lock:
            h = self._hload(cat, srv)
            new = {d: v for d, v in r["days"].items() if d not in h["days"]}
            if not new:
                return
            h["days"].update(new)
            for d in sorted(h["days"])[:-KEEP_DAYS]:
                del h["days"][d]
            os.makedirs(os.path.dirname(self._hfile(cat, srv)), exist_ok=True)
            _save(self._hfile(cat, srv), h)

    def record(self, cat, srv, data):
        """Сохраняет снимок дня (по lastUpdated) для всей категории на сервере."""
        day = (data or {}).get("lastUpdated", "")[:10]
        if not day:
            return
        with self.lock:
            h = self._hload(cat, srv)
            if day in h["days"]:
                return
            h["days"][day] = {r["key"]: [r["avg"], r["min"], r["max"], r["sold"], r["total"]] for r in M.rows_of(cat, data)}
            for d in sorted(h["days"])[:-KEEP_DAYS]:
                del h["days"][d]
            os.makedirs(os.path.dirname(self._hfile(cat, srv)), exist_ok=True)
            _save(self._hfile(cat, srv), h)

    def days(self, cat, srv):
        self.warm(cat, srv)
        with self.lock:
            return sorted(self._hload(cat, srv)["days"])

    def series(self, cat, srv, key):
        """[(день, avg, min, max, sold, total)] по возрастанию даты."""
        self.warm(cat, srv)
        with self.lock:
            h = self._hload(cat, srv)["days"]
            return [(d, *h[d][str(key)]) for d in sorted(h) if str(key) in h[d]]

    def prev_row(self, cat, srv, key, day):
        """Строка предыдущего доступного дня (до day) как словарь, или None."""
        with self.lock:
            h = self._hload(cat, srv)["days"]
            for d in sorted(h, reverse=True):
                if d < day and str(key) in h[d]:
                    a = h[d][str(key)]
                    return {"day": d, "avg": a[0], "min": a[1], "max": a[2], "sold": a[3], "total": a[4]}
        return None


# ====================================================================== условия
def evaluate(cond, value, row, prev):
    """-> (выполнено, значение_сейчас). row/prev - словари с avg/min/max/sold/total; prev может быть None."""
    v = float(value)
    if cond == "avg_le":
        return (row["avg"] > 0 and row["avg"] <= v), row["avg"]
    if cond == "min_le":
        return (row["min"] > 0 and row["min"] <= v), row["min"]
    if cond == "avg_ge":
        return (v > 0 and row["avg"] >= v), row["avg"]
    if prev is None:
        return False, None
    if cond in ("drop_pct", "rise_pct"):
        if prev["avg"] <= 0 or row["avg"] <= 0:
            return False, None
        ch = (row["avg"] - prev["avg"]) * 100.0 / prev["avg"]
        return ((-ch >= v) if cond == "drop_pct" else (ch >= v)), round(ch, 1)
    if cond == "sold_up":
        if prev["sold"] <= 0:
            return False, None
        ch = (row["sold"] - prev["sold"]) * 100.0 / prev["sold"]
        return ch >= v, round(ch, 1)
    return False, None


def deals(api, cat, fee_pct, min_sold, min_price):
    """Где купить и где продать с учётом комиссии продажи. -> список по убыванию чистой выгоды в %."""
    out = []
    for x in M.compare_servers(api, cat, min_sold, min_price):
        net = x["hi_p"] * (1 - fee_pct / 100.0) - x["lo_p"]
        if net <= 0:
            continue
        x = dict(x, net=net, net_pct=net * 100.0 / x["lo_p"])
        out.append(x)
    out.sort(key=lambda r: -r["net_pct"])
    return out


def trends(store, cat, srv, days_back=1, min_sold=10):
    """Изменение средней цены за days_back дней по снимкам. -> (дата_от, дата_до, список) или (None, None, [])."""
    ds = store.days(cat, srv)
    if len(ds) < 2:
        return None, None, []
    d1 = ds[-1]
    d0 = ds[max(0, len(ds) - 1 - days_back)]
    with store.lock:
        h = store._hload(cat, srv)["days"]
        a, b = h[d0], h[d1]
    names = {}
    out = []
    for k, now in b.items():
        was = a.get(k)
        if not was or was[0] <= 0 or now[0] <= 0 or now[3] < min_sold:
            continue
        out.append({"key": k, "was": was[0], "now": now[0], "pct": (now[0] - was[0]) * 100.0 / was[0], "sold": now[3]})
    return d0, d1, out


def price_after_fee(sell, fee_pct):
    return sell * (1 - fee_pct / 100.0)


# ====================================================================== фоновая проверка
class Monitor:
    """Раз в минуту смотрит, не пора ли обновить данные отслеживаемых групп (категория, сервер); после
    ежедневного обновления API скачивает их, сохраняет снимок, проверяет условия и зовёт notify(event)."""

    def __init__(self, api, store, notify):
        self.api, self.store, self.notify = api, store, notify
        self.stop_ev = threading.Event()
        self.wake = threading.Event()
        self.force = False
        self.attempt = {}
        self.status = {"busy": "", "error": "", "last_check": 0.0, "next_check": next_update(), "checked": 0}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self.wake.set()

    def check_now(self):
        self.force = True
        self.wake.set()

    def groups(self):
        s = self.store.settings
        g = set()
        with self.store.lock:
            for it in self.store.items:
                if it.get("enabled"):
                    for srv in expand(it.get("servers")):
                        g.add((it["cat"], srv))
        if s.get("arb"):
            for cat in s.get("arb_cats") or []:
                for srv, _ in M.SERVERS:
                    g.add((cat, srv))
        return sorted(g)

    def stale(self, cat, srv, now):
        c = self.api.cached(cat, srv)
        if not c:
            return True
        lu = parse_utc((c[1] or {}).get("lastUpdated", ""))
        return lu < expected_update(now) - 600

    def _run(self):
        first = True
        while not self.stop_ev.is_set():
            force, self.force = self.force, False
            try:
                if self.api.ready() and (force or not first or self.store.settings.get("check_on_start", True)):
                    self.cycle(force)
            except Exception as ex:
                self.status["error"] = f"Мониторинг: {ex}"
            first = False
            self.wake.wait(60)
            self.wake.clear()

    def cycle(self, force=False):
        now = time.time()
        fetched = 0
        for cat, srv in self.groups():
            if self.stop_ev.is_set():
                return
            if not self.stale(cat, srv, now):
                continue
            if not force and now - self.attempt.get((cat, srv), 0) < RETRY_SEC:
                continue
            if not force and self.api.remaining() <= 1:     # оставляем запрос для ручного просмотра
                break
            self.attempt[(cat, srv)] = now
            self.status["busy"] = f"Мониторинг: загружаю {M.CAT_BY[cat][1]}, {srv.upper()}"
            try:
                data = self.api.fetch(cat, srv, force=True, cancel=self.stop_ev)
                self.store.record(cat, srv, data)
                fetched += 1
                self.status["error"] = ""
            except M.ApiError as ex:
                self.status["error"] = str(ex)
                if ex.kind in ("auth", "nokey", "cancel", "limit"):
                    break
        self.status["busy"] = ""
        for cat, srv in self.groups():              # общие снимки заранее, вне блокировки (для «за сутки»)
            self.store.warm(cat, srv)
        # снимки дня из уже скачанного кэша (если кэш свежий, но снимка ещё не было)
        for cat, srv in self.groups():
            c = self.api.cached(cat, srv)
            if c:
                self.store.record(cat, srv, c[1])
        self.evaluate_all()
        if self.store.settings.get("arb"):
            self.arbitrage()
        self.status["last_check"] = time.time()
        self.status["next_check"] = next_update()
        self.status["checked"] = fetched

    def evaluate_all(self):
        events = []
        idx = {}
        st = self.store
        now = time.time()
        with st.lock:
            for it in st.items:
                if not it.get("enabled"):
                    continue
                hits = []
                for srv in expand(it.get("servers")):
                    k = (it["cat"], srv)
                    if k not in idx:
                        c = self.api.cached(*k)
                        if not c:
                            idx[k] = None
                        else:
                            idx[k] = ({r["key"]: r for r in M.rows_of(it["cat"], c[1])}, (c[1].get("lastUpdated") or "")[:10])
                    if not idx[k]:
                        continue
                    rows, day = idx[k]
                    row = rows.get(str(it["key"]))
                    if not row:
                        continue
                    prev = st.prev_row(it["cat"], srv, it["key"], day)
                    ok, val = evaluate(it["cond"], it["value"], row, prev)
                    was = it.setdefault("state", {}).get(srv, False)
                    it["state"][srv] = bool(ok)
                    ch = None
                    if prev and prev["avg"] > 0 and row["avg"] > 0:
                        ch = round((row["avg"] - prev["avg"]) * 100.0 / prev["avg"], 1)
                    it.setdefault("last", {})[srv] = {"val": val, "avg": row["avg"], "min": row["min"], "day": day,
                                                      "ch": ch, "ok": bool(ok)}
                    if ok and not was and it.get("snooze", 0) < now:
                        hits.append((srv, row, val, ch))
                if hits:
                    events.append(self._event(it, hits))
            st.save()
        for ev in events:
            st.add_alert(ev)
            self.notify(ev)

    def _event(self, it, hits):
        cat = M.CAT_BY[it["cat"]][1]
        cond = f"{COND_NAME[it['cond']]} {fmt_value(it['cond'], it['value'])}"
        lines = []
        for srv, row, val, ch in hits[:6]:
            s = f"{srv.upper()} · {M.SERVER_NAME.get(srv, '')}: средняя {M.money(row['avg'])}, мин. {M.money(row['min'])}"
            if ch is not None:
                s += f", за сутки {ch:+g}%"
            lines.append(s)
        if len(hits) > 6:
            lines.append(f"… и ещё серверов: {len(hits) - 6}")
        text = f"{it['name']} ({cat})\nУсловие: {cond}\n" + "\n".join(lines)
        if it.get("note"):
            text += f"\nЗаметка: {it['note']}"
        best = hits[0]
        return {"t": time.time(), "kind": "watch", "item_id": it["id"], "title": "Цена достигнута",
                "short": f"{it['name']}: {COND_NAME[it['cond']]} {fmt_value(it['cond'], it['value'])} "
                         f"({best[0].upper()}, сейчас {fmt_value(it['cond'], best[2]) if best[2] is not None else '—'})",
                "text": text, "cat": it["cat"], "key": it["key"]}

    def arbitrage(self):
        s = self.store.settings
        today = time.strftime("%Y-%m-%d")
        seen = self.store.arb_seen.setdefault(today, [])
        for d in list(self.store.arb_seen):
            if d != today:
                del self.store.arb_seen[d]
        events = []
        for cat in s.get("arb_cats") or []:
            have = [srv for srv, _ in M.SERVERS if self.api.cached(cat, srv) and not self.stale(cat, srv, time.time())]
            if len(have) < len(M.SERVERS):
                continue                                    # ждём, пока будут свежие данные всех серверов
            for x in deals(self.api, cat, float(s.get("fee", 3.0)), int(s.get("arb_sold", 50)), int(s.get("arb_price", 1000))):
                if x["net_pct"] < float(s.get("arb_pct", 30)):
                    break
                tag = f"{cat}|{x['key']}|{x['lo']}|{x['hi']}"
                if tag in seen:
                    continue
                seen.append(tag)
                events.append({"t": time.time(), "kind": "arb", "item_id": None, "title": "Выгодная сделка",
                               "short": f"{x['name']}: купить {x['lo'].upper()} {M.money(x['lo_p'])} → продать "
                                        f"{x['hi'].upper()} {M.money(x['hi_p'])}, чистыми +{x['net_pct']:.0f}%",
                               "text": f"{x['name']} ({M.CAT_BY[cat][1]})\nКупить: {x['lo'].upper()} · "
                                       f"{M.SERVER_NAME[x['lo']]} - {M.money(x['lo_p'])}\nПродать: {x['hi'].upper()} · "
                                       f"{M.SERVER_NAME[x['hi']]} - {M.money(x['hi_p'])}\nПосле комиссии {s.get('fee', 3.0):g}%: "
                                       f"+{M.money(x['net'])} с штуки (+{x['net_pct']:.0f}%)",
                               "cat": cat, "key": x["key"]})
                if len(events) >= 3:
                    break
        self.store.save()
        for ev in events:
            self.store.add_alert(ev)
            self.notify(ev)


def in_quiet(settings, now=None):
    if not settings.get("quiet"):
        return False
    t = time.localtime(now)
    cur = t.tm_hour * 60 + t.tm_min

    def mins(s):
        try:
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 0
    a, b = mins(settings.get("quiet_from", "23:00")), mins(settings.get("quiet_to", "08:00"))
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


SOUNDS = {"Уведомление": "SystemNotification", "Восклицание": "SystemExclamation", "Звёздочка": "SystemAsterisk",
          "Вопрос": "SystemQuestion", "Сигнал": "beep"}


def play_sound(name):
    try:
        import winsound
        alias = SOUNDS.get(name, "SystemNotification")
        if alias == "beep":
            def b():
                for f in (880, 1175, 1568):
                    winsound.Beep(f, 140)
            threading.Thread(target=b, daemon=True).start()
        else:
            winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass


def telegram(text):
    """Отправка через бота из раздела «Готовка» (токен и чат там). -> (ok, сообщение)."""
    try:
        import cooking
        c = cooking.load_json(cooking.CONFIG_PATH, {})
        if not c.get("token") or not c.get("chat_id"):
            return False, "Telegram не настроен: подключи бота в разделе «Готовка» → «Уведомления в Telegram»"
        cooking.tg_send(c["token"], c["chat_id"], text)
        return True, "отправлено"
    except Exception as ex:
        return False, f"Telegram: {ex}"


def telegram_ready():
    try:
        import cooking
        c = cooking.load_json(cooking.CONFIG_PATH, {})
        return bool(c.get("token") and c.get("chat_id"))
    except Exception:
        return False
