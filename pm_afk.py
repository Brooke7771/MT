# -*- coding: utf-8 -*-
"""Автоматическая проверка вкладки «PM» (раньше «АФК-режим»; в обеих программах).

Пока режим включён, раз в N минут бот сам достаёт планшет (клавиша, по умолчанию стрелка вниз),
открывает EMS → «Коллеги» (выбранный отдел, прокрутка до конца списка) и «Вызовы» → «История»
(прокрутка, пока вызовы не станут старше окна «катает»), снимает и распознаёт скрины и убирает
планшет. Вкладка PM собирает из них таблицу, как из обычных скринов, а правила наблюдения
(кто + что: не берёт вызовы, «Нет на месте», зашёл/вышел, ранг/отдел/группа) дают уведомления.
Экран планшета узнаём по тексту (OCR Windows). Разобрано по записи 20261022_194728 (1920x1080)."""
import os
import re
import json
import time
import uuid

import numpy as np

import pm_ocr
import pm_track as PT

BUSY = {"on": False}                 # True, пока бот ходит по планшету (анти-АФК в это время молчит)
BASE_W, BASE_H = 1920, 1080
TABLET = (330, 70, 1600, 1010)       # где на экране планшет
BACK = (385, 130)                    # стрелка «назад» в разделе
DEPT_CUR = (1372, 188, 1440, 220)    # выбранный отдел в выпадающем списке «Коллеги» (короткое слово)
DEPT_OPEN = (1448, 203)              # открыть список
DEPT_ITEM0, DEPT_STEP, DEPT_ITEMS = 251, 48, 4   # пункты списка: центр первого, шаг, сколько видно
MENU_WORDS = {"Коллеги": (320, 420), "Учёт": (320, 420), "Управление": (320, 420), "Настройки": (320, 420),
              "Премирование": (570, 670), "Вызовы": (570, 670), "Черный": (570, 670)}
MENU_ICON = {"Коллеги": (497, 292), "Вызовы": (729, 543), "Управление": (960, 292)}   # значки в меню EMS
BAL_SEARCH = (760, 205)              # «Управление балансом»: поле поиска
BAL_TABLE = (360, 190, 1560, 975)    # строка поиска + таблица (запись 20261023_210704)
BAL_ROW0, BAL_PITCH = 315, 45        # центр первой строки таблицы, шаг строк
BAL_DATE_X, BAL_NAME_X1, BAL_UID_X = 1380, 580, (585, 660)
BAL_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*(\d{1,2}):(\d{2})")
LOOKALIKE = str.maketrans("АВЕКМНОРСТХаеорсухкмтнвђјЬ", "ABEKMHOPCTXaeopcyxkmtHBhjb")
SORT_STATUS = (1450, 269)            # заголовок «Статус в сети» - сортировка
AVAIL_TAB, HIST_TAB = (697, 130), (837, 130)   # вкладки «Доступные» / «История» в вызовах
WAIT_BTN = (830, 250, 925, 985)      # красные кнопки причины в карточках ожидающих вызовов
WAIT_ID_X = (486, 520)               # x номера гражданина в карточке («Гражданин # 186094»)
WAIT_FULL = 8                        # столько целых карточек на экране, когда список длиннее экрана
WAIT_C = (650, 620)                  # мышь для прокрутки карточек
TABLE_C = (960, 640)                 # куда ставить мышь для прокрутки таблицы
MAX_SHOTS = 30                       # предел скринов на раздел (вдруг прокрутка не кончается)

TABLET_KEYS = ["Down", "Up", "Left", "Right", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10",
               "Insert", "Home", "End", "PageUp", "PageDown"]
KEY_NAMES = {"Down": "Стрелка вниз", "Up": "Стрелка вверх", "Left": "Стрелка влево", "Right": "Стрелка вправо"}

AFK_DEFAULT = {"interval": 10, "tablet_key": "Down", "dept": "PM", "colleagues": True, "calls": True,
               "only_online": True, "waiting": True, "balance": True,
               "group": False, "idle_guard": False, "idle_sec": 60, "sound": True, "popup": True, "tg": False}

EVENTS = {"no_calls": "Не берёт вызовы", "away": "«Нет на месте» дольше", "inout": "Зашёл / вышел из игры",
          "changes": "Сменил ранг / отдел / группу", "waiting": "Вызовов в ожидании не меньше",
          "missed": "Непринятых вызовов за час не меньше"}
TIMED = ("no_calls", "away")             # у этих событий есть «минут»
COUNTED = ("waiting", "missed")          # общие по всем вызовам, число - «штук»


def default_rules():
    return [new_rule("Не катает 20 мин", "no_calls", 20),
            new_rule("Нет на месте 15 мин", "away", 15),
            new_rule("Зашёл / вышел (вайтлист)", "inout", 0, on=False, white_only=True),
            new_rule("Ранг, отдел, группа", "changes", 0),
            new_rule("Много вызовов ждут", "waiting", 5, on=False),
            new_rule("Много непринятых за час", "missed", 10, on=False)]


def new_rule(name, event, minutes, on=True, white_only=False):
    return {"id": uuid.uuid4().hex[:8], "name": name, "on": on, "event": event, "minutes": minutes,
            "who": {"white_only": white_only, "uids": "", "depts": [], "rank_min": "", "rank_max": ""},
            "popup": True, "sound": True, "tg": True}


def afk_cfg(c):
    out = dict(AFK_DEFAULT)
    out.update({k: v for k, v in (c or {}).items() if k in AFK_DEFAULT})
    return out


# ====================================================================== правила наблюдения
def _ids(s):
    return [x.strip().lower() for x in re.split(r"[,;\s]+", s or "") if x.strip()]


def who_match(uid, e, who, white):
    """Подходит ли сотрудник под «кого»: вайтлист / статики или ники / отделы / ранг от-до. Пусто - все."""
    if who.get("white_only") and not PT.in_white(uid, e, white):
        return False
    ids = _ids(who.get("uids"))
    if ids and str(uid).lower() not in ids and (e.get("name") or "").lower() not in ids \
            and not any(i in (e.get("name") or "").lower().split() for i in ids):
        return False
    if who.get("depts") and (e.get("dept") or "") not in who["depts"]:
        return False
    r = e.get("rank")
    for k, cmp in (("rank_min", lambda a, b: a >= b), ("rank_max", lambda a, b: a <= b)):
        v = str(who.get(k) or "").strip()
        if v.isdigit() and (not isinstance(r, int) or not cmp(r, int(v))):
            return False
    return True


def who_text(who):
    parts = []
    if who.get("white_only"):
        parts.append("вайтлист")
    if _ids(who.get("uids")):
        parts.append("только: " + who["uids"])
    if who.get("depts"):
        parts.append("отделы " + ", ".join(who["depts"]))
    a, b = str(who.get("rank_min") or "").strip(), str(who.get("rank_max") or "").strip()
    if a or b:
        parts.append(f"ранг {a or '…'}-{b or '…'}")
    return "; ".join(parts) or "все"


def state_path():
    return os.path.join(PT.data_dir(), "pm_afk_state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    for k in ("prev", "online_since", "away_since", "last_call", "fired", "calls_seen", "stats"):
        s.setdefault(k, {})
    s.setdefault("waiting_log", [])
    return s


def save_state(s):
    try:
        with open(state_path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
        os.replace(state_path() + ".tmp", state_path())
    except OSError:
        pass


def call_stats(snap, state, waiting, now, shift):
    """Приблизительная статистика вызовов по «Истории» всех проверок: вызов без сотрудника = не принят
    («Отменено игроком» - не дождался, «Время истекло»). Вызовы копятся без повторов (гражданин + время)."""
    seen = state["calls_seen"]
    for c in snap.get("calls", []):
        dt = PT._dt(c.get("date"))
        if not dt or not c.get("citizen"):
            continue
        key = f"{c['citizen']}|{c.get('date')}"
        acc = 1 if c.get("uid") else 0
        if key not in seen or acc > seen[key][1]:
            seen[key] = [dt.timestamp() - shift, acc]
    for k in [k for k, v in seen.items() if now - v[0] > 26 * 3600]:
        del seen[k]
    if waiting is not None:
        state["waiting_log"] = [w for w in state["waiting_log"] if now - w[0] < 24 * 3600] + [[now, waiting]]
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))

    def part(t0):
        v = [x for x in seen.values() if x[0] >= t0]
        return len(v), sum(1 for x in v if not x[1])
    h_all, h_miss = part(now - 3600)
    d_all, d_miss = part(midnight)
    wl = [w[1] for w in state["waiting_log"] if w[0] >= midnight]
    st = {"t": now, "waiting": waiting, "hour": [h_all, h_miss], "today": [d_all, d_miss],
          "waiting_avg": round(sum(wl) / len(wl), 1) if wl else None, "waiting_max": max(wl) if wl else None}
    state["stats"] = st
    return st


def stats_text(st):
    if not st:
        return ""
    out = []
    if st.get("waiting") is not None:
        out.append(f"ждут сейчас: {st['waiting']}" + (f" (сегодня в среднем {st['waiting_avg']}, макс. {st['waiting_max']})"
                                                      if st.get("waiting_avg") is not None else ""))
    for k, name in (("hour", "за час"), ("today", "сегодня")):
        a, m = st.get(k) or (0, 0)
        if a:
            out.append(f"не принято {name}: {m} из {a} ({round(100 * m / a)}%)")
    return " · ".join(out)


def evaluate(snap, rules, state, white, now=None, waiting=None):
    """Снимок новой проверки (полный, без фильтра вкладки) -> [(правило, текст)].
    state копит между проверками: прошлые статусы, с какого времени в сети / «нет на месте»,
    последний вызов каждого и уже отправленные уведомления (по одному на случай, пока он длится)."""
    now = now or time.time()
    emp = snap.get("employees", {})
    prev = state["prev"]
    ts = [PT._dt(c.get("date")) for c in snap.get("calls", [])]
    newest = max((t.timestamp() for t in ts if t), default=None)
    # время в истории - часы сервера игры; у кого другой часовой пояс на ПК - сдвиг на целые часы
    # (сдвиг узнаём, только когда свежий вызов почти ровно на целые часы от ПК, и запоминаем)
    if newest and abs(newest - now) < 15 * 3600:
        k = round((newest - now) / 3600)
        if abs(newest - now - k * 3600) < 10 * 60:
            state["tz_shift"] = k * 3600
    shift = state.get("tz_shift", 0)
    cst = call_stats(snap, state, waiting, now, shift) if snap.get("has_calls") or waiting is not None else {}
    for c, dt in zip(snap.get("calls", []), ts):       # последний вызов каждого (история шире окна)
        if c.get("uid") and dt:
            t = dt.timestamp() - shift
            if t > state["last_call"].get(c["uid"], 0):
                state["last_call"][c["uid"]] = t
    for uid, e in emp.items():
        if e["status"] in pm_ocr.ONLINE:
            state["online_since"].setdefault(uid, now)
        else:
            state["online_since"].pop(uid, None)
        if e["status"] == "away":
            state["away_since"].setdefault(uid, now)
        else:
            state["away_since"].pop(uid, None)
    out = []
    nm = lambda uid, e: f"{e.get('name') or '?'} ({uid})"
    for r in rules:
        if not r.get("on"):
            continue
        ev, mins = r["event"], max(0, int(r.get("minutes") or 0))
        if ev in COUNTED:
            v = cst.get("waiting") if ev == "waiting" else (cst.get("hour") or [0, 0])[1]
            key = r["id"]
            if v is not None and v >= mins:
                if key not in state["fired"]:
                    state["fired"][key] = now
                    out.append((r, f"вызовов в ожидании: {v}" if ev == "waiting" else
                                f"не принято вызовов за час: {v} из {cst['hour'][0]}"))
            elif v is not None:
                state["fired"].pop(key, None)
            continue
        for uid, e in emp.items():
            if not who_match(uid, e, r.get("who", {}), white):
                continue
            key = f"{r['id']}|{uid}"
            hit = None
            if ev == "no_calls" and snap.get("has_calls"):
                if e["status"] == "active":
                    since = max(state["last_call"].get(uid, 0), state["online_since"].get(uid, now))
                    m = (now - since) / 60
                    if m >= mins:
                        last = state["last_call"].get(uid)
                        hit = f"{nm(uid, e)} не берёт вызовы {int(m)} мин" + \
                              (f" (последний в {time.strftime('%H:%M', time.localtime(last))})" if last else "")
            elif ev == "away":
                if e["status"] == "away":
                    m = (now - state["away_since"].get(uid, now)) / 60
                    if m >= mins:
                        hit = f"{nm(uid, e)} «Нет на месте» уже {int(m)} мин"
            if ev in TIMED:
                if hit and key not in state["fired"]:
                    state["fired"][key] = now
                    out.append((r, hit))
                elif not hit:
                    state["fired"].pop(key, None)
                continue
            o = prev.get(uid)
            if o is None:
                continue
            if ev == "inout":
                was, is_ = o["status"] in pm_ocr.ONLINE, e["status"] in pm_ocr.ONLINE
                if is_ and not was:
                    out.append((r, f"{nm(uid, e)} зашёл в игру"))
                elif was and not is_:
                    out.append((r, f"{nm(uid, e)} вышел из игры"))
            elif ev == "changes":
                if o.get("rank") and e.get("rank") and o["rank"] != e["rank"]:
                    up = isinstance(e["rank"], int) and isinstance(o["rank"], int) and e["rank"] > o["rank"]
                    out.append((r, f"{nm(uid, e)}: ранг {o['rank']} → {e['rank']}" + (" (повысили)" if up else "")))
                if o.get("dept") and e.get("dept") and o["dept"] != e["dept"] and "?" not in (o["dept"], e["dept"]):
                    out.append((r, f"{nm(uid, e)}: отдел {o['dept']} → {e['dept']}"))
                if snap.get("has_group") and o.get("group") is not None and bool(e.get("in_group")) != o["group"]:
                    out.append((r, f"{nm(uid, e)} " + ("вошёл в группу" if e.get("in_group") else "вышел из группы")))
    state["prev"] = {uid: {"status": e["status"], "rank": e.get("rank"), "dept": e.get("dept"),
                           "group": bool(e.get("in_group")) if snap.get("has_group") else None}
                     for uid, e in emp.items()}
    return out


# ====================================================================== проход по планшету
def _norm(s):
    return s.lower().replace("ё", "е").translate(str.maketrans("aeopcxykmtbh", "аеорсхукмтвн")).strip(".,:;!«»\"'")


def _like(word, target):
    import difflib
    a, b = _norm(word), _norm(target)
    return a == b or (len(b) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75)


class Abort(Exception):
    pass


class Tablet:
    """Один проход по планшету. inp - antiafk.Input2, grab() -> картинка PIL всего экрана,
    log(текст), save(PIL) -> путь, is_game() -> игра на переднем плане."""

    def __init__(self, cfg, inp, grab, log, save, is_game, hist_min=17, stop=lambda: False, sleep=time.sleep):
        """hist_min - на сколько минут назад листать «Историю» (окно «катает» или время между проверками + запас)."""
        self.cfg, self.inp, self.grab, self.log, self.save = cfg, inp, grab, log, save
        self.is_game, self.hist_min, self.stop, self.sleep = is_game, hist_min, stop, sleep
        self.k = (1.0, 1.0)
        self.bal_fn = None               # shots -> [(uid, ник)] кого смотреть в «Управлении балансом» (в конце прохода)
        self.bal_min, self.bal_shift, self.bal_res = 30, 0, None
        self.per = {}                    # строк за один щелчок колёсика у каждого раздела (уточняется на ходу)
        self.waiting = None

    # ---- зрение
    def P(self, x, y):
        return int(x * self.k[0]), int(y * self.k[1])

    def B(self, box):
        return (int(box[0] * self.k[0]), int(box[1] * self.k[1]), int(box[2] * self.k[0]), int(box[3] * self.k[1]))

    def shot(self):
        if self.stop():
            raise Abort("автоматическая проверка выключена")
        if not self.is_game():
            raise Abort("окно игры не на переднем плане")
        img = self.grab()
        self.k = (img.width / BASE_W, img.height / BASE_H)
        return img

    def words(self, img, box=TABLET, lang="ru", scale=0.75):
        """Слова в области -> [(текст, x, y, w, h)] в координатах 1920x1080."""
        x0, y0, x1, y1 = self.B(box)
        c = img.crop((x0, y0, x1, y1))
        f = scale / self.k[0]
        c = c.resize((max(1, int(c.width * f)), max(1, int(c.height * f))))
        return [(t, (x0 + x / f) / self.k[0], (y0 + y / f) / self.k[1], w / f / self.k[0], h / f / self.k[1])
                for t, x, y, w, h in pm_ocr.ocr(c, lang)]

    @staticmethod
    def find(ws, target, y0=0, y1=2000, x0=0, x1=3000):
        for w in ws:
            if x0 <= w[1] <= x1 and y0 <= w[2] <= y1 and _like(w[0], target):
                return w
        return None

    def screen(self, img=None):
        """-> (экран, слова): home / menu / colleagues / calls / history / other / none."""
        img = img or self.shot()
        if not self.tablet_on(img):
            return "none", []
        ws = self.words(img)
        if self.find(ws, "Коллеги", y1=165, x1=600):
            return "colleagues", ws
        if self.find(ws, "Вызовы", y1=165, x1=600):
            return ("history" if self.find(ws, "Сотрудник", 245, 305) else "calls"), ws
        if self.find(ws, "Пополнить", y1=165) or self.find(ws, "транзакции", 250, 300):
            return "balance", ws
        labels = [n for n, (y0, y1) in MENU_WORDS.items() if self.find(ws, n, y0, y1)]
        if len(labels) >= 2:                                        # OCR иногда теряет одно слово
            return "menu", ws
        en = self.words(img, lang="en", scale=1.0)                  # подписи на главном экране мелкие
        if any(_norm(w[0]).startswith(_norm("emerg")) and w[2] > 160 for w in en):
            return "home", en
        return "other", ws

    def tablet_on(self, img):
        """Планшет открыт: снизу и справа его рамка - ровная тёмная полоса (на записи яркость 16, разброс 0);
        без планшета там игра (разброс 8-27)."""
        a = np.asarray(img.convert("L"))
        for box in ((600, 1004, 1300, 1022), (1588, 300, 1606, 800)):
            x0, y0, x1, y1 = self.B(box)
            r = a[y0:y1, x0:x1]
            if r.size == 0 or r.std() > 4 or not 5 < r.mean() < 40:
                return False
        return True

    def ems_icon(self, ws):
        """Значок EMS на главном экране - над подписью «Emergency Medical Services»."""
        w = next(w for w in ws if _norm(w[0]).startswith(_norm("emerg")) and w[2] > 160)
        line = [v for v in ws if abs(v[2] - w[2]) < 8 and w[1] <= v[1] < w[1] + 260]
        x1 = max(v[1] + v[3] for v in line)
        return (w[1] + x1) / 2, w[2] - 50

    def click(self, x, y, wait=0.7):
        self.inp.click(*self.P(x, y))
        self.sleep(wait)

    def wait_screen(self, want, timeout=4.0):
        t_end = time.time() + timeout
        s, ws = "none", []
        while time.time() < t_end:
            s, ws = self.screen()
            if s in want:
                return s, ws
            self.sleep(0.3)
        return s, ws

    # ---- навигация
    def open(self):
        s, ws = self.screen()
        if s != "none":
            return s, ws
        self.inp.tap(self.cfg["tablet_key"], 70)
        self.sleep(1.0)
        s, ws = self.wait_screen(("home", "menu", "colleagues", "calls", "history", "other"), 4)
        if s == "none":
            raise Abort("планшет не открылся (персонаж в машине, мёртв или клавиша планшета другая?)")
        return s, ws

    def to_menu(self, s, ws):
        for _ in range(4):
            if s == "menu":
                return ws
            if s == "home":
                self.click(*self.ems_icon(ws), 1.2)
            elif s in ("colleagues", "calls", "history", "balance", "other"):
                self.click(*BACK, 1.0)
            s, ws = self.wait_screen(("menu",), 2.5)
        if s != "menu":
            raise Abort("не нашёл меню EMS на планшете")
        return ws

    def section(self, ws, name, want):
        w = self.find(ws, name, 320, 670)
        if w is not None:
            self.click(w[1] + w[3] / 2, w[2] - 70, 1.2)
        else:                                                       # слово не прочиталось - место на записи
            self.click(*MENU_ICON[name], 1.2)
        s, ws = self.wait_screen(want, 4)
        if s not in want:
            raise Abort(f"не открылся раздел «{name}»")
        return s, ws

    def close(self):
        try:
            s, _ = self.screen()
        except Abort:
            return
        for _ in range(2):
            if s == "none":
                return
            self.inp.tap(self.cfg["tablet_key"], 70)
            self.sleep(1.0)
            s, _ = self.screen()
        if s != "none":
            self.log("Планшет не закрылся клавишей - закрой вручную")

    def short(self, img, box):
        """Короткое слово («PM», «IN»): одно OCR Windows часто не читает, а лист из 6 копий - читает
        (как отделы в таблице, pm_ocr._dept_text)."""
        return pm_ocr._dept_text([img.crop(self.B(box))] * 6, self.k[1])

    @staticmethod
    def _dept_norm(t):
        t = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", (t or "").translate(pm_ocr.LATIN)).upper()
        return "ВСЕ" if t in ("ВСЕ", "BCE", "BСЕ") or t.startswith("ВСЕОТ") or t.startswith("BCEOT") else t

    def set_dept(self, dept):
        """Фильтр «Все отделы» → нужный отдел (пусто - все отделы). -> True, если переключали."""
        want = self._dept_norm(dept) if dept else "ВСЕ"
        img = self.shot()
        if self._dept_norm(self.short(img, DEPT_CUR)) == want:
            return False
        self.click(*DEPT_OPEN, 0.8)
        for attempt in range(8):
            img = self.shot()
            for i in range(DEPT_ITEMS):
                y = DEPT_ITEM0 + DEPT_STEP * i
                if self._dept_norm(self.short(img, (1352, y - 17, 1440, y + 17))) == want:
                    self.click(1400, y, 1.0)
                    self.log(f"Отдел: {dept or 'все'}")
                    return True
            self.inp.move_to(*self.P(1440, DEPT_ITEM0 + DEPT_STEP))   # пункт ниже видимых - листаем список
            self.inp.wheel(1)
            self.sleep(0.4)
        self.click(*DEPT_OPEN, 0.5)
        raise Abort(f"в списке отделов нет «{dept or 'Все отделы'}»")

    def settle(self, kind, check=None, timeout=8.0):
        """Ждём, пока страница прогрузится: два распознавания подряд одинаковые и check(rows) - да.
        (После перехода и смены отдела игра ещё долю секунды показывает старый список.)"""
        t_end = time.time() + timeout
        prev, last = None, None
        while True:
            img = self.shot()
            res = pm_ocr.recognize(img)
            keys = [self._key(kind, r) for r in res["rows"]] if res.get("kind") == kind else None
            if keys:
                last = (img, res)
                if keys == prev and (check is None or check(res["rows"])):
                    return img, res
            if time.time() > t_end:
                if last:
                    return last
                raise Abort("страница не прогрузилась: " + ("«Коллеги»" if kind == "colleagues" else "«История» вызовов"))
            prev = keys
            self.sleep(0.6)

    @staticmethod
    def online_first(rows):
        """Список отсортирован «сначала в сети»: ни одного в сети после не в сети."""
        seen_off = False
        for r in rows:
            on = r.get("status") in pm_ocr.ONLINE
            if on and seen_off:
                return False
            seen_off = seen_off or not on
        return True

    def sort_online(self, dept_ok):
        """Щелчок по «Статус в сети»: сначала кто в сети. Второй щелчок переворачивает - проверяем по строкам."""
        img, res = self.settle("colleagues", dept_ok)
        rows = res["rows"]
        if rows and rows[0].get("status") in pm_ocr.ONLINE and self.online_first(rows):
            return img, res
        for _ in range(2):
            self.click(*SORT_STATUS, 1.0)
            img, res = self.settle("colleagues", dept_ok)
            rows = res["rows"]
            if rows and rows[0].get("status") in pm_ocr.ONLINE and self.online_first(rows):
                self.log("Сортировка: сначала кто в сети")
                return img, res
        self.log("Сортировка по статусу не встала (никого нет в сети?) - снимаю весь список")
        return img, res

    def _scroll_shots(self, kind, first=None, enough=None):
        """Скрины раздела с прокруткой вниз до конца (или пока enough(rows) не скажет «хватит»).
        first - уже прогруженный первый экран (img, res). Шаг прокрутки свой у каждого раздела и
        уточняется на каждом шаге по сдвигу строк (первая строка нового скрина - какая по счёту на
        прошлом): ~11 строк за раз, чтобы соседние скрины перекрывались; перескок - доснимаем назад."""
        if first is None:
            self.inp.move_to(*self.P(*TABLE_C))
            self.inp.wheel(-60)                                    # к началу списка
            self.sleep(0.8)
            first = self.settle(kind)
        per = self.per.get(kind)
        out, prev, notches = [], None, (max(1, int(11 / per)) if per else 1)
        for i in range(MAX_SHOTS):
            if i == 0:
                img, res = first
            else:
                img = self.shot()
                res = pm_ocr.recognize(img)
            if res.get("kind") != kind:
                raise Abort(f"на экране не {'«Коллеги»' if kind == 'colleagues' else '«История» вызовов'}")
            keys = [self._key(kind, r) for r in res["rows"]]
            if prev is not None:
                if keys == prev:                                   # прокрутка не сдвинула список - конец
                    break
                if keys and keys[0] in prev:
                    moved = prev.index(keys[0])
                    if moved > 0:
                        per = moved / notches if per is None else (per + moved / notches) / 2
                elif keys:                                         # перескочили строки - доснимаем назад
                    self.log("Прокрутка перескочила строки - доснимаю пропуск")
                    self._fill_gap(kind, prev, keys, out)
                    per = (per or 10.0) * 1.5
                if per:
                    notches = max(1, int(11 / per))
                    self.per[kind] = per
            out.append((self.save(img), res))
            prev = keys
            if enough and enough(res["rows"]):
                break
            self.inp.move_to(*self.P(*TABLE_C))
            self.inp.wheel(notches)
            self.sleep(0.8)
        return out

    def _fill_gap(self, kind, prev, keys, out):
        """Между прошлым скрином (prev) и текущим (keys) пропуск: назад по одному щелчку колёсика,
        снимая каждый шаг, пока не покажутся строки прошлого скрина; потом вперёд на столько же.
        (В «Истории» список иногда прыгает сразу на ~3 минуты вызовов - запись 20261022_210357.)"""
        back = 0
        for _ in range(12):
            self.inp.move_to(*self.P(*TABLE_C))
            self.inp.wheel(-1)
            back += 1
            self.sleep(0.6)
            img = self.shot()
            res = pm_ocr.recognize(img)
            if res.get("kind") != kind:
                break
            ks = [self._key(kind, r) for r in res["rows"]]
            if ks and ks[0] in keys and keys.index(ks[0]) == 0:   # назад не сдвинулось - дальше нельзя
                continue
            out.append((self.save(img), res))
            if any(k in prev for k in ks):                         # сошлось с прошлым скрином
                break
        self.inp.move_to(*self.P(*TABLE_C))
        self.inp.wheel(back)
        self.sleep(0.8)

    @staticmethod
    def _key(kind, r):
        if kind == "colleagues":
            return (r.get("uid"), r.get("name"))
        return (r.get("uid"), r.get("citizen"), r.get("date"))

    def cards(self, img):
        """Карточки «Доступные»: по красной кнопке причины («Смерть») справа в карточке -> центры кнопок по y
        (только целые: кнопка не срезана краем списка)."""
        import cv2
        x0, y0, x1, y1 = self.B(WAIT_BTN)
        a = np.asarray(img)[y0:y1, x0:x1].astype(np.int16)
        red = ((a[:, :, 0] > 180) & (a[:, :, 1] < 90) & (a[:, :, 2] < 90)).astype(np.uint8)
        n, _, st, _ = cv2.connectedComponentsWithStats(red)
        k = self.k[1]
        return sorted((y0 + y + h / 2) / k for x, y, w, h, _ in st[1:n]
                      if w > 50 * k and 20 * k < h < 40 * k and y > 0 and y + h < red.shape[0] - 1)

    def card_id(self, img, yb):
        """Номер гражданина в карточке («Гражданин # 186094» - над кнопкой)."""
        import cv2
        x0, y0, x1, y1 = self.B((WAIT_ID_X[0], yb - 30, WAIT_ID_X[1] + 80, yb - 4))
        c = np.asarray(img)[y0:y1, x0:x1]
        if c.size == 0:
            return ""
        f = 2.5 / self.k[1]
        c = cv2.resize(c, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        c = cv2.copyMakeBorder(c, 30, 30, 30, 30, cv2.BORDER_CONSTANT,
                               value=[int(v) for v in np.median(c.reshape(-1, 3), axis=0)])
        from PIL import Image
        t = "".join(w[0] for w in pm_ocr.ocr(Image.fromarray(c), "en"))
        return "".join(ch for ch in t if ch.isdigit())

    def count_waiting(self):
        """Вкладка «Доступные»: сколько вызовов ждут (номера граждан в карточках, с прокруткой).
        На записи 20261022_210357: 8 целых карточек на экран, 6 / 17 / 13 вызовов."""
        ids = set()
        self.inp.move_to(*self.P(*WAIT_C))
        self.inp.wheel(-30)
        self.sleep(0.6)
        for _ in range(15):
            img = self.shot()
            ys = self.cards(img)
            got = {i for i in (self.card_id(img, y) for y in ys) if len(i) >= 2}
            new = got - ids
            ids |= got
            if len(ys) < WAIT_FULL or not new:                     # всё влезло на экран / прокрутка кончилась
                break
            self.inp.move_to(*self.P(*WAIT_C))
            self.inp.wheel(3)
            self.sleep(0.7)
        return len(ids)

    # ---- «Управление балансом»: кто что делал (лечил, продавал таблетки) и когда
    def balance_rows(self, img):
        """-> (что в поиске, [{"name", "uid", "type", "date": datetime}]) с экрана «Управление балансом»."""
        from datetime import datetime
        ws = self.words(img, BAL_TABLE, lang="ru", scale=1.5)
        rows, query = {}, []
        for w in ws:
            cy = w[2] + w[4] / 2
            if cy < BAL_ROW0 - BAL_PITCH * 1.5:
                if w[1] < 1300:
                    query.append(w)
                continue
            r = round((cy - BAL_ROW0 - 7) / BAL_PITCH)
            if r >= 0:
                rows.setdefault(r, []).append(w)
        out = []
        for r in sorted(rows):
            line = sorted(rows[r], key=lambda w: w[1])
            m = BAL_DATE.search(" ".join(w[0] for w in line if w[1] >= BAL_DATE_X))
            if not m:
                continue
            d, mo, y, h, mi = (int(v) for v in m.groups())
            try:
                dt = datetime(y, mo, d, h, mi)
            except ValueError:
                continue
            uid = "".join(ch for w in line if BAL_UID_X[0] <= w[1] <= BAL_UID_X[1] for ch in w[0] if ch.isdigit())
            out.append({"name": " ".join(w[0] for w in line if w[1] < BAL_NAME_X1), "uid": uid,
                        "type": " ".join(w[0] for w in line if 700 <= w[1] < 1000), "date": dt})
        return " ".join(w[0] for w in sorted(query, key=lambda w: w[1]) if not _like(w[0], "Q")), out

    @staticmethod
    def _same_person(row, uid, name):
        if uid and row.get("uid") and row["uid"] == str(uid):
            return True
        from difflib import SequenceMatcher
        a = (row.get("name") or "").translate(LOOKALIKE).lower()
        return SequenceMatcher(None, a, (name or "").translate(LOOKALIKE).lower()).ratio() >= 0.6

    def balance(self, people, minutes=30, shift=0, manage=True):
        """people: [(uid, ник)] -> {uid: {"name", "last": "ДД.ММ.ГГГГ ЧЧ:ММ" | None, "ago": мин | None,
        "n": действий за minutes, "types": {тип: сколько}, "shot": путь}}. Время таблицы - часы сервера:
        shift - на сколько они впереди часов ПК (из АФК-проверок, tz_shift)."""
        import antiafk as AF
        import pm_ftext
        res = {}
        try:
            s, ws = self.open() if manage else self.screen()
            if s != "balance":
                ws = self.to_menu(s, ws)
                s, ws = self.section(ws, "Управление", ("balance",))
            self.settle_balance(None, None)
            for i, (uid, name) in enumerate(people):
                self.log(f"Баланс: {name} ({i + 1}/{len(people)})")
                self.click(*BAL_SEARCH, 0.25)
                self.inp.key("Ctrl", True)
                self.inp.tap("A", 40)
                self.inp.key("Ctrl", False)
                self.inp.tap("Backspace", 40)
                self.sleep(0.15)
                pm_ftext.type_text(name)
                img, rows = self.settle_balance(uid, name)
                now = time.time()
                mine = [r for r in rows if self._same_person(r, uid, name)]
                types, n, last_type = {}, 0, ""
                for r in mine:
                    ago = (now - (r["date"].timestamp() - shift)) / 60
                    if ago <= minutes:
                        n += 1
                        t = r["type"].split("#")[0].strip() or "?"
                        types[t] = types.get(t, 0) + 1
                        last_type = last_type or t                 # строки - новые сверху
                last = mine[0]["date"] if mine else None
                res[uid] = {"name": name, "last": last.strftime("%d.%m.%Y %H:%M") if last else None,
                            "last_type": last_type, "min": minutes,
                            "ago": round((now - (last.timestamp() - shift)) / 60) if last else None,
                            "n": n, "full": n >= len(mine) and len(mine) >= 14, "types": types,
                            "rows": [[r["date"].strftime("%d.%m.%Y %H:%M"), r["type"]] for r in mine],
                            "shot": self.save(img)}
            self.click(*BAL_SEARCH, 0.2)                       # поиск очистить - в следующий раз таблица полная
            self.inp.key("Ctrl", True)
            self.inp.tap("A", 40)
            self.inp.key("Ctrl", False)
            self.inp.tap("Backspace", 40)
        finally:
            if manage:
                self.close()
        return res

    def settle_balance(self, uid, name, timeout=7.0):
        """Ждём, пока таблица покажет этого человека (или пустая) и два снимка подряд одинаковые."""
        t_end = time.time() + timeout
        prev, img, rows = None, None, []
        while time.time() < t_end:
            img = self.shot()
            q, rows = self.balance_rows(img)
            key = tuple((r["name"], r["date"]) for r in rows[:4])
            ok = name is None or (rows and self._same_person(rows[0], uid, name))
            if ok and key == prev:
                return img, rows
            prev = key if ok else None
            self.sleep(0.6)
        return img, rows                                   # долго грузится или записей нет

    def run(self):
        """Полный проход -> [(путь, результат recognize)]; self.waiting - сколько вызовов ждут (или None).
        Abort - что-то пошло не так (планшет закрывается)."""
        shots = []
        self.waiting = None
        try:
            s, ws = self.open()
            if self.cfg["colleagues"]:
                ws = self.to_menu(s, ws)
                s, ws = self.section(ws, "Коллеги", ("colleagues",))
                dept = self.cfg.get("dept", "").strip()
                want = self._dept_norm(dept)
                self.settle("colleagues")                          # список прогрузился - потом фильтр
                self.set_dept(dept)

                def dept_ok(rows):
                    if not dept:
                        return True
                    ds = [self._dept_norm(r.get("dept")) for r in rows if r.get("dept") not in (None, "", "?")]
                    return bool(ds) and sum(d == want for d in ds) >= 0.8 * len(ds)
                if self.cfg.get("only_online", True):
                    first = self.sort_online(dept_ok)
                    got = self._scroll_shots("colleagues", first, lambda rows: rows and all(
                        r.get("status") not in pm_ocr.ONLINE for r in rows[-2:]))
                else:
                    got = self._scroll_shots("colleagues", self.settle("colleagues", dept_ok))
                n = len({r.get("uid") for _, res in got for r in res["rows"]})
                self.log(f"Коллеги: {len(got)} скр., {n} сотрудников")
                shots += got
                s, ws = self.screen()
            if self.cfg["calls"]:
                ws = self.to_menu(s, ws)
                s, ws = self.section(ws, "Вызовы", ("calls", "history"))
                if self.cfg.get("waiting", True):
                    if s == "history":
                        w = self.find(ws, "Доступные", y1=165)
                        self.click(*((w[1] + w[3] / 2, w[2] + w[4] / 2) if w else AVAIL_TAB), 1.2)
                        s, ws = self.wait_screen(("calls",), 4)
                    if s == "calls":
                        self.sleep(0.8)                            # карточки подгружаются
                        self.waiting = self.count_waiting()
                        self.log(f"Вызовов в ожидании: {self.waiting}")
                        s, ws = self.screen()
                if s == "calls":
                    w = self.find(ws, "История", y1=165)
                    self.click(*((w[1] + w[3] / 2, w[2] + w[4] / 2) if w else HIST_TAB), 1.2)
                    s, ws = self.wait_screen(("history",), 4)
                    if s != "history":
                        raise Abort("не открылась «История» вызовов")
                newest = {"t": None}

                def enough(rows):
                    ts = [PT._dt(r.get("date")) for r in rows]
                    ts = [t.timestamp() for t in ts if t]
                    if not ts:
                        return False
                    newest["t"] = max(newest["t"] or 0, max(ts))
                    return self.hist_min > 0 and min(ts) < newest["t"] - self.hist_min * 60
                self.inp.move_to(*self.P(*TABLE_C))
                self.inp.wheel(-60)
                self.sleep(0.8)
                got = self._scroll_shots("calls", self.settle("calls"), enough)
                self.log(f"История вызовов: {len(got)} скр.")
                shots += got
                s, ws = self.screen()
            if self.bal_fn is not None and shots:
                people = self.bal_fn(shots)
                if people:
                    self.log(f"Баланс: {len(people)} чел. без вызовов")
                    try:                                       # сбой на балансе - скрины «Коллег» и «Вызовов» остаются
                        ws = self.to_menu(s, ws)
                        s, ws = self.section(ws, "Управление", ("balance",))
                        self.bal_res = self.balance(people, self.bal_min, self.bal_shift, manage=False)
                    except Abort as ex:
                        if self.stop():
                            raise
                        self.log(f"Баланс не проверен: {ex}")
                    s, ws = self.screen()
            if s not in ("none",):
                self.to_menu(s, ws)                                 # оставляем планшет в меню EMS
        finally:
            self.close()
        return shots


# ====================================================================== таймер АФК-режима
GAME_TITLES = ("gta", "rage", "majestic", "grand theft")
KEEP_ROUNDS = 3                      # сколько последних проверок хранить скрины (pm_afk_shots)


def shots_root():
    return os.path.join(PT.data_dir(), "pm_afk_shots")


def other_bot_busy():
    """Работает другой бот (швейка, рыбалка, ...) - планшет не трогаем."""
    import importlib
    for m in ("sewing_bot", "fishing", "woodcut", "mining", "gym", "farm", "stroyka", "clicker"):
        try:
            if importlib.import_module(m).BUSY["on"]:
                return m
        except Exception:
            pass
    return None


def idle_seconds():
    """Сколько секунд не было ввода с клавиатуры/мыши (GetLastInputInfo)."""
    try:
        import ctypes

        class LII(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = LII(ctypes.sizeof(LII), 0)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
        return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 1e9


class Runner:
    """АФК-режим: свой поток, раз в interval минут - проход по планшету.
    get_cfg() -> настройки (afk_cfg), window() -> окно «катает» в минутах,
    post(сообщение) - в очередь вкладки: ("afk_status", текст) и ("afk", скрины, ошибка, время)."""

    def __init__(self, get_cfg, window, post, pick=None):
        """pick(snap) -> [(uid, ник)] - кого после прохода смотреть в «Управлении балансом» (в сети, без вызовов)."""
        import threading
        self.get_cfg, self.window, self.post, self.pick = get_cfg, window, post, pick
        self.on, self.force, self.alive = False, False, True
        self.next_t = None
        self.running = False
        self.bal_req = None              # (люди, минут) - проверка по «Управлению балансом»
        threading.Thread(target=self._loop, daemon=True).start()

    def balance_now(self, people, minutes=30):
        self.bal_req = (list(people), minutes)
        self.post(("afk_status", f"Баланс: проверю {len(people)} чел. - переключись в игру"))

    def set_on(self, on):
        self.on = bool(on)
        self.next_t = time.time() + 5 if on else None           # первая проверка - через 5 с (успеть в игру)
        self.post(("afk_status", "Автопроверка: первая через 5 с - переключись в игру" if on
                   else "Автоматическая проверка выключена"))

    def check_now(self):
        self.force = True
        self.post(("afk_status", "Проверка: переключись в игру - начну, как только она будет на экране"))

    def stop(self):
        self.alive = self.on = False

    def _wait_reason(self, cfg):
        import antiafk as AF
        if not any(t in AF.foreground_title().lower() for t in GAME_TITLES):
            return "жду, когда игра будет на переднем плане"
        b = other_bot_busy()
        if b:
            return f"жду: работает другой бот ({b})"
        if cfg["idle_guard"] and idle_seconds() < cfg["idle_sec"]:
            return f"жду: ты пользуешься клавиатурой/мышью (нужно {cfg['idle_sec']} с без ввода)"
        return None

    def _loop(self):
        last_status = None
        while self.alive:
            time.sleep(0.5)
            if self.bal_req:
                cfg = self.get_cfg()
                why = self._wait_reason(cfg)
                if why:
                    if why != last_status:
                        self.post(("afk_status", "Баланс: " + why))
                        last_status = why
                    time.sleep(1)
                    continue
                req, self.bal_req = self.bal_req, None
                self._balance(cfg, *req)
                last_status = None
                continue
            if not (self.on or self.force):
                continue
            cfg = self.get_cfg()
            if not self.force and self.next_t and time.time() < self.next_t:
                left = int(self.next_t - time.time())
                st = f"Автопроверка: следующая через {left // 60}:{left % 60:02d}"
                if st != last_status and left % 5 == 0:
                    self.post(("afk_status", st))
                    last_status = st
                continue
            why = self._wait_reason(cfg)
            if why:
                if why != last_status:
                    self.post(("afk_status", "Автопроверка: " + why))
                    last_status = why
                time.sleep(2)
                continue
            forced, self.force = self.force, False
            self._round(cfg, forced)
            last_status = None
            self.next_t = time.time() + max(1, float(cfg["interval"])) * 60

    def _balance(self, cfg, people, minutes):
        import antiafk as AF
        import mss
        from PIL import Image
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        d = os.path.join(shots_root(), "balance_" + time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(d, exist_ok=True)
        sct = mss.mss()
        n = {"i": 0}

        def grab():
            raw = sct.grab(sct.monitors[1])
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        def save(img):
            n["i"] += 1
            p = os.path.join(d, f"{n['i']:02d}.png")
            img.save(p)
            return p
        BUSY["on"] = True
        self.running = True
        err, res, t0 = "", {}, time.time()
        try:
            time.sleep(0.6)
            tab = Tablet(cfg, AF.Input2(), grab, lambda m: self.post(("afk_status", m)), save,
                         lambda: any(t in AF.foreground_title().lower() for t in GAME_TITLES),
                         stop=lambda: not self.alive)
            res = tab.balance(people, minutes, load_state().get("tz_shift", 0))
        except Abort as ex:
            err = str(ex)
        except Exception as ex:
            err = f"ошибка: {ex}"
        finally:
            BUSY["on"] = False
            self.running = False
            try:
                sct.close()
            except Exception:
                pass
        self.post(("balance", res, err, time.time() - t0, d))

    def _round(self, cfg, forced=False):
        import antiafk as AF
        import mss
        from PIL import Image
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        stamp = time.strftime("%Y%m%d_%H%M%S")
        d = os.path.join(shots_root(), stamp)
        os.makedirs(d, exist_ok=True)
        n = {"i": 0}
        sct = mss.mss()

        def grab():
            raw = sct.grab(sct.monitors[1])
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        def save(img):
            n["i"] += 1
            p = os.path.join(d, f"{n['i']:02d}.png")
            img.save(p)
            return p

        def log(m):
            self.post(("afk_status", "Автопроверка: " + m))
        BUSY["on"] = True
        self.running = True
        err, shots, t0, waiting, tab = "", [], time.time(), None, None
        try:
            time.sleep(0.6)                                        # анти-АФК успевает встать на паузу
            tab = Tablet(cfg, AF.Input2(), grab, log, save,
                         lambda: any(t in AF.foreground_title().lower() for t in GAME_TITLES),
                         hist_min=max(self.window(), float(cfg["interval"])) + 3,
                         stop=lambda: not self.alive or not (self.on or forced))
            if cfg.get("balance", True) and self.pick:
                win = self.window()

                def bal_fn(shots_):
                    res_ = [r for _, r in shots_ if r and r.get("kind")]
                    return self.pick(PT.merge(res_, win)) if res_ else []
                tab.bal_fn, tab.bal_min = bal_fn, (win if win > 0 else 10 ** 6)
                tab.bal_shift = load_state().get("tz_shift", 0)
            shots = tab.run()
            waiting = tab.waiting
        except Abort as ex:
            err = str(ex)
        except Exception as ex:
            err = f"ошибка: {ex}"
        finally:
            BUSY["on"] = False
            self.running = False
            try:
                sct.close()
            except Exception:
                pass
        self.post(("afk", shots, err, time.time() - t0, waiting, tab.bal_res if tab is not None else None))
        try:                                                       # старые проверки - удалить
            import shutil
            for old in sorted(os.listdir(shots_root()))[:-KEEP_ROUNDS]:
                shutil.rmtree(os.path.join(shots_root(), old), ignore_errors=True)
        except OSError:
            pass
