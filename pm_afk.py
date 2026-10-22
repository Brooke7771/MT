# -*- coding: utf-8 -*-
"""АФК-режим вкладки «PM» (только в основной программе Majestic Tools, в Majestic PM его нет).

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
MENU_ICON = {"Коллеги": (497, 292), "Вызовы": (729, 543)}   # значки в меню EMS (если подпись не прочиталась)
TABLE_C = (960, 640)                 # куда ставить мышь для прокрутки таблицы
MAX_SHOTS = 30                       # предел скринов на раздел (вдруг прокрутка не кончается)

TABLET_KEYS = ["Down", "Up", "Left", "Right", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10",
               "Insert", "Home", "End", "PageUp", "PageDown"]
KEY_NAMES = {"Down": "Стрелка вниз", "Up": "Стрелка вверх", "Left": "Стрелка влево", "Right": "Стрелка вправо"}

AFK_DEFAULT = {"interval": 10, "tablet_key": "Down", "dept": "PM", "colleagues": True, "calls": True,
               "group": False, "idle_guard": False, "idle_sec": 60, "sound": True, "popup": True, "tg": False}

EVENTS = {"no_calls": "Не берёт вызовы", "away": "«Нет на месте» дольше", "inout": "Зашёл / вышел из игры",
          "changes": "Сменил ранг / отдел / группу"}
TIMED = ("no_calls", "away")             # у этих событий есть «минут»


def default_rules():
    return [new_rule("Не катает 20 мин", "no_calls", 20),
            new_rule("Нет на месте 15 мин", "away", 15),
            new_rule("Зашёл / вышел (вайтлист)", "inout", 0, on=False, white_only=True),
            new_rule("Ранг, отдел, группа", "changes", 0)]


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
    for k in ("prev", "online_since", "away_since", "last_call", "fired"):
        s.setdefault(k, {})
    return s


def save_state(s):
    try:
        with open(state_path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
        os.replace(state_path() + ".tmp", state_path())
    except OSError:
        pass


def evaluate(snap, rules, state, white, now=None):
    """Снимок новой проверки (полный, без фильтра вкладки) -> [(правило, текст)].
    state копит между проверками: прошлые статусы, с какого времени в сети / «нет на месте»,
    последний вызов каждого и уже отправленные уведомления (по одному на случай, пока он длится)."""
    now = now or time.time()
    emp = snap.get("employees", {})
    prev = state["prev"]
    ts = [PT._dt(c.get("date")) for c in snap.get("calls", [])]
    newest = max((t.timestamp() for t in ts if t), default=None)
    # время в истории - часы сервера игры; у кого другой часовой пояс на ПК - сдвиг на целые часы
    shift = round((newest - now) / 3600) * 3600 if newest and abs(newest - now) < 15 * 3600 else 0
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

    def __init__(self, cfg, inp, grab, log, save, is_game, window_min=15, stop=lambda: False, sleep=time.sleep):
        self.cfg, self.inp, self.grab, self.log, self.save = cfg, inp, grab, log, save
        self.is_game, self.window_min, self.stop, self.sleep = is_game, window_min, stop, sleep
        self.k = (1.0, 1.0)
        self.per = None                  # строк за один щелчок колёсика (узнаём на первой прокрутке)

    # ---- зрение
    def P(self, x, y):
        return int(x * self.k[0]), int(y * self.k[1])

    def B(self, box):
        return (int(box[0] * self.k[0]), int(box[1] * self.k[1]), int(box[2] * self.k[0]), int(box[3] * self.k[1]))

    def shot(self):
        if self.stop():
            raise Abort("АФК-режим выключен")
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
            elif s in ("colleagues", "calls", "history", "other"):
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
        """Фильтр «Все отделы» → нужный отдел (пусто - все отделы)."""
        want = self._dept_norm(dept) if dept else "ВСЕ"
        img = self.shot()
        if self._dept_norm(self.short(img, DEPT_CUR)) == want:
            return
        self.click(*DEPT_OPEN, 0.8)
        for attempt in range(8):
            img = self.shot()
            for i in range(DEPT_ITEMS):
                y = DEPT_ITEM0 + DEPT_STEP * i
                if self._dept_norm(self.short(img, (1352, y - 17, 1440, y + 17))) == want:
                    self.click(1400, y, 1.0)
                    self.log(f"Отдел: {dept or 'все'}")
                    return
            self.inp.move_to(*self.P(1440, DEPT_ITEM0 + DEPT_STEP))   # пункт ниже видимых - листаем список
            self.inp.wheel(1)
            self.sleep(0.4)
        self.click(*DEPT_OPEN, 0.5)
        raise Abort(f"в списке отделов нет «{dept or 'Все отделы'}»")

    def _scroll_shots(self, kind, enough=None):
        """Скрины раздела с прокруткой вниз до конца (или пока enough(rows) не скажет «хватит»).
        Шаг прокрутки подбирается по сдвигу строк (первая строка нового скрина - какая по счёту
        на прошлом): ~11 строк за раз, чтобы соседние скрины перекрывались."""
        self.inp.move_to(*self.P(*TABLE_C))
        self.inp.wheel(-60)                                        # к началу списка
        self.sleep(0.8)
        out, prev, notches = [], None, (max(1, int(11 / self.per)) if self.per else 1)
        for _ in range(MAX_SHOTS):
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
                    if moved > 0 and self.per is None:
                        self.per = moved / notches
                elif keys:                                         # перескочили строки - доснимаем назад
                    self.log("Прокрутка перескочила строки - доснимаю и уменьшаю шаг")
                    self.inp.move_to(*self.P(*TABLE_C))
                    self.inp.wheel(-max(1, notches // 2))
                    self.sleep(0.7)
                    back = self.shot()
                    bres = pm_ocr.recognize(back)
                    if bres.get("kind") == kind:
                        out.append((self.save(back), bres))
                    self.inp.wheel(max(1, notches // 2))
                    self.sleep(0.7)
                    self.per = (self.per or 15.0) * 1.5
                if self.per:
                    notches = max(1, int(11 / self.per))
            out.append((self.save(img), res))
            prev = keys
            if enough and enough(res["rows"]):
                break
            self.inp.move_to(*self.P(*TABLE_C))
            self.inp.wheel(notches)
            self.sleep(0.7)
        return out

    @staticmethod
    def _key(kind, r):
        if kind == "colleagues":
            return (r.get("uid"), r.get("name"))
        return (r.get("uid"), r.get("citizen"), r.get("date"))

    def run(self):
        """Полный проход -> [(путь, результат recognize)]. Abort - что-то пошло не так (планшет закрывается)."""
        shots = []
        try:
            s, ws = self.open()
            if self.cfg["colleagues"]:
                ws = self.to_menu(s, ws)
                s, ws = self.section(ws, "Коллеги", ("colleagues",))
                self.set_dept(self.cfg.get("dept", "").strip())
                got = self._scroll_shots("colleagues")
                n = len({r.get("uid") for _, res in got for r in res["rows"]})
                self.log(f"Коллеги: {len(got)} скр., {n} сотрудников")
                shots += got
                s, ws = self.screen()
            if self.cfg["calls"]:
                ws = self.to_menu(s, ws)
                s, ws = self.section(ws, "Вызовы", ("calls", "history"))
                if s == "calls":
                    w = self.find(ws, "История", y1=165)
                    if w is None:
                        raise Abort("нет вкладки «История» в вызовах")
                    self.click(w[1] + w[3] / 2, w[2] + w[4] / 2, 1.2)
                newest = {"t": None}

                def enough(rows):
                    ts = [PT._dt(r.get("date")) for r in rows]
                    ts = [t.timestamp() for t in ts if t]
                    if not ts:
                        return False
                    newest["t"] = max(newest["t"] or 0, max(ts))
                    return self.window_min > 0 and min(ts) < newest["t"] - (self.window_min + 2) * 60
                got = self._scroll_shots("calls", enough)
                self.log(f"История вызовов: {len(got)} скр.")
                shots += got
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
    for m in ("sewing_bot", "fishing", "woodcut", "mining", "gym", "farm", "sanding", "clicker"):
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

    def __init__(self, get_cfg, window, post):
        import threading
        self.get_cfg, self.window, self.post = get_cfg, window, post
        self.on, self.force, self.alive = False, False, True
        self.next_t = None
        self.running = False
        threading.Thread(target=self._loop, daemon=True).start()

    def set_on(self, on):
        self.on = bool(on)
        self.next_t = time.time() + 5 if on else None           # первая проверка - через 5 с (успеть в игру)
        self.post(("afk_status", "АФК-режим: первая проверка через 5 с - переключись в игру" if on
                   else "АФК-режим выключен"))

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
            if not (self.on or self.force):
                continue
            cfg = self.get_cfg()
            if not self.force and self.next_t and time.time() < self.next_t:
                left = int(self.next_t - time.time())
                st = f"АФК-режим: следующая проверка через {left // 60}:{left % 60:02d}"
                if st != last_status and left % 5 == 0:
                    self.post(("afk_status", st))
                    last_status = st
                continue
            why = self._wait_reason(cfg)
            if why:
                if why != last_status:
                    self.post(("afk_status", "АФК-режим: " + why))
                    last_status = why
                time.sleep(2)
                continue
            forced, self.force = self.force, False
            self._round(cfg, forced)
            last_status = None
            self.next_t = time.time() + max(1, float(cfg["interval"])) * 60

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
            self.post(("afk_status", "АФК: " + m))
        BUSY["on"] = True
        self.running = True
        err, shots, t0 = "", [], time.time()
        try:
            time.sleep(0.6)                                        # анти-АФК успевает встать на паузу
            tab = Tablet(cfg, AF.Input2(), grab, log, save,
                         lambda: any(t in AF.foreground_title().lower() for t in GAME_TITLES),
                         window_min=self.window(), stop=lambda: not self.alive or not (self.on or forced))
            shots = tab.run()
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
        self.post(("afk", shots, err, time.time() - t0))
        try:                                                       # старые проверки - удалить
            import shutil
            for old in sorted(os.listdir(shots_root()))[:-KEEP_ROUNDS]:
                shutil.rmtree(os.path.join(shots_root(), old), ignore_errors=True)
        except OSError:
            pass
