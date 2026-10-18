# -*- coding: utf-8 -*-
"""PM: склейка распознанных скринов, снимки на диске, сравнение снимков, таблица и тексты /f.
Ключ сотрудника везде - статик (UID): ник может меняться, статик нет."""
import os
import sys
import json
import time
import glob
from datetime import datetime, timedelta

from pm_ocr import STATUS_NAME, ONLINE, ASK

F_TEXT = "/f {name}, выйдите на связь: где вы сейчас находитесь и чем заняты?"


def data_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def snap_dir():
    return os.path.join(data_dir(), "pm_snapshots")


def _dt(s):
    try:
        return datetime.strptime(s, "%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return None


# ====================================================================== склейка
def merge(results, window_min=15):
    """results - список ответов pm_ocr.recognize. -> снимок (dict, как на диске) + служебные поля.
    window_min - кто «катает вызовы»: был в истории за столько минут до самого свежего вызова (0 - все)."""
    emp, calls = {}, []
    unknown = []
    group, group_count, has_group = [], None, False
    for r in results:
        if r.get("kind") == "group":
            has_group = True
            group.extend(r["rows"])
            if r.get("count"):
                group_count = max(group_count or 0, r["count"])
        if r.get("kind") == "colleagues":
            rows = r["rows"]
            for i, row in enumerate(rows):                       # пропущенный номер - по соседям
                if row["num"] is None and i > 0 and rows[i - 1]["num"] is not None:
                    row["num"] = rows[i - 1]["num"] + 1
            for row in rows:
                if not row["uid"]:
                    unknown.append(row)
                    continue
                old = emp.get(row["uid"])
                if old is None or len(row["doubt"]) < len(old["doubt"]):
                    emp[row["uid"]] = dict(row)
        elif r.get("kind") == "calls":
            calls.extend(r["rows"])
    # вызовы: без повторов (один и тот же вызов на соседних скринах)
    seen, uniq = set(), []
    for c in calls:
        k = (c["uid"], c["citizen"], c["date"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    dates = [d for d in (_dt(c["date"]) for c in uniq) if d]
    newest = max(dates) if dates else None
    since = newest - timedelta(minutes=window_min) if newest and window_min > 0 else None
    on_calls = {}
    for c in uniq:
        if not c["uid"]:
            continue
        d = _dt(c["date"])
        if since and d and d < since:
            continue
        on_calls.setdefault(c["uid"], {"name": c["employee"], "n": 0})["n"] += 1
    # список отсортирован «сначала в сети»: все в сети видны, если номера 1..k без дыр и есть строка k+1 «не в сети»
    by_num = {e["num"]: e for e in emp.values() if e.get("num")}
    k = max([n for n, e in by_num.items() if e["status"] in ONLINE] or [0])
    complete = k > 0 and all(n in by_num for n in range(1, k + 1)) and \
        (k + 1) in by_num and by_num[k + 1]["status"] == "offline"
    employees = {}
    for uid, e in emp.items():
        employees[uid] = {"num": e.get("num"), "name": e["name"], "rank": e["rank"], "dept": e.get("dept") or "",
                          "status": e["status"],
                          "offline_since": e.get("offline_since"), "on_calls": uid in on_calls,
                          "calls": on_calls.get(uid, {}).get("n", 0), "doubt": list(e["doubt"])}
    outsiders = {u: v for u, v in on_calls.items() if u not in employees}
    members = group_members(group)
    in_group, group_out = match_group(members, employees)
    for uid, e in employees.items():
        e["in_group"] = uid in in_group
    return {"taken_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "employees": employees,
            "complete": complete, "has_calls": bool(uniq), "calls_newest": newest.strftime("%d.%m.%Y %H:%M") if newest else None,
            "window_min": window_min, "outsiders": outsiders, "calls": uniq, "unknown": unknown,
            "has_group": has_group, "group": members, "group_count": group_count, "group_outsiders": group_out}


# ====================================================================== группа в телефоне
def _nk(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalpha())


def group_members(rows):
    """Склейка списков группы с нескольких скринов: без повторов; «Имя» без фамилии (срезано прокруткой)
    выбрасывается, если есть полная запись с тем же именем."""
    full, seen = [], set()
    for r in rows:
        if r.get("last") and _nk(r["name"]) not in seen:
            seen.add(_nk(r["name"]))
            full.append(r["name"])
    firsts = {_nk(n.split()[0]) for n in full}
    for r in rows:
        if not r.get("last") and _nk(r.get("first")) not in firsts and _nk(r["name"]) not in seen:
            seen.add(_nk(r["name"]))
            full.append(r["name"])
    return full


def match_group(members, employees):
    """-> (статики сотрудников из группы, имена из группы, которых нет среди сотрудников).
    Сверка по нику с допуском: OCR может спутать пару букв (l/I, 0/O)."""
    from difflib import SequenceMatcher
    names = {uid: _nk(e.get("name")) for uid, e in employees.items()}
    firsts = {uid: _nk((e.get("name") or "").split(" ")[0]) for uid, e in employees.items()}
    found, out = set(), []
    for m in members:
        key, parts = _nk(m), m.split()
        best, score = None, 0.0
        for uid, nk in names.items():
            if len(parts) < 2:                                   # только имя: годится, если оно единственное
                sc = 1.0 if firsts[uid] == key and list(firsts.values()).count(key) == 1 else 0.0
            else:
                sc = SequenceMatcher(None, key, nk).ratio()
            if sc > score:
                best, score = uid, sc
        if best is not None and score >= 0.84:
            found.add(best)
        else:
            out.append(m)
    return found, out


# ====================================================================== отметки вручную (брифинг, комментарий)
def _marks_path():
    return os.path.join(data_dir(), "pm_marks.json")


def load_marks():
    try:
        with open(_marks_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_marks(marks):
    marks = {u: m for u, m in marks.items() if m.get("brief") or m.get("comment")}
    tmp = _marks_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(marks, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _marks_path())


BRIEF_NAME = {"": "", "yes": "есть", "no": "нет"}


# ====================================================================== фильтр и вайтлист
STATUS_ORDER = ("active", "away", "arena", "jail", "offline", "left")
FILTER_DEFAULT = {"on": False, "status": list(STATUS_ORDER), "rank_min": "", "rank_max": "",
                  "calls": "all", "group": "all", "brief": "all", "comment": "all", "name": "", "off_hours": "",
                  "depts": [],
                  "white_always": True, "white_only": False}


def filter_cfg(f):
    """Настройки фильтра с недостающими полями по умолчанию (старый pm_config.json)."""
    out = dict(FILTER_DEFAULT)
    out.update({k: v for k, v in (f or {}).items() if k in FILTER_DEFAULT})
    return out


def _int(s):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def in_white(uid, e, white):
    """white - [{"uid", "name"}]: совпадение по статику, а без статика - по нику (без учёта регистра)."""
    nk = (e.get("name") or "").strip().lower()
    for w in white or []:
        if w.get("uid") and str(w["uid"]) == str(uid):
            return True
        if not w.get("uid") and w.get("name") and w["name"].strip().lower() == nk:
            return True
    return False


def passes(uid, e, f, white, now=None):
    """Проходит ли сотрудник фильтр f (FILTER_DEFAULT) с учётом вайтлиста."""
    w = in_white(uid, e, white)
    if f.get("white_only"):
        return w
    if w and f.get("white_always"):
        return True
    st = e.get("status") or "unknown"
    if st in STATUS_ORDER and st not in f["status"]:
        return False
    if f["depts"] and (e.get("dept") or "") not in f["depts"]:     # пусто - все отделы
        return False
    lo, hi, r = _int(f["rank_min"]), _int(f["rank_max"]), e.get("rank")
    if (lo is not None or hi is not None) and r is None:
        return False
    if lo is not None and r < lo or hi is not None and r > hi:
        return False
    if f["calls"] == "yes" and not e.get("on_calls") or f["calls"] == "no" and e.get("on_calls"):
        return False
    if f["group"] == "yes" and not e.get("in_group") or f["group"] == "no" and e.get("in_group"):
        return False
    b = e.get("brief") or ""
    if f["brief"] != "all" and b != {"yes": "yes", "no": "no", "none": ""}[f["brief"]]:
        return False
    c = bool((e.get("comment") or "").strip())
    if f["comment"] == "yes" and not c or f["comment"] == "no" and c:
        return False
    if f["name"].strip():
        q = f["name"].strip().lower()
        if q not in (e.get("name") or "").lower() and q not in str(uid):
            return False
    h = _int(f["off_hours"])
    if h is not None and st == "offline":
        d = _dt(e.get("offline_since"))
        if d and ((now or datetime.now()) - d).total_seconds() > h * 3600:
            return False
    return True


def filtered(snap, f, white):
    """Снимок только с теми, кто проходит фильтр (если он включён). Поле hidden - сколько скрыто."""
    if not snap:
        return snap
    f = filter_cfg(f)
    if not f["on"]:
        return dict(snap, hidden=0)
    emp = {u: e for u, e in snap["employees"].items() if passes(u, e, f, white)}
    return dict(snap, employees=emp, hidden=len(snap["employees"]) - len(emp))


# ====================================================================== порядок списка между проверками
ROSTER_KEYS = ("name", "rank", "dept", "status", "offline_since", "num")


def _roster_path():
    return os.path.join(data_dir(), "pm_roster.json")


def load_roster():
    """{"order": [статики по порядку], "emp": {статик: последние данные}} - порядок первой проверки + кто добавился."""
    try:
        with open(_roster_path(), encoding="utf-8") as f:
            r = json.load(f)
        if isinstance(r.get("order"), list) and isinstance(r.get("emp"), dict):
            return r
    except (OSError, ValueError, AttributeError):
        pass
    return {"order": [], "emp": {}}


def save_roster(r):
    tmp = _roster_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _roster_path())


def reset_roster():
    try:
        os.remove(_roster_path())
    except OSError:
        pass


def apply_roster(snap, base):
    """Порядок строк как в прошлой проверке (base). Кого нет на скринах - «Вышел» (кто и так был не в сети -
    остаётся как был); новые - ниже, по номеру в игре. Без скринов «Коллеги» ничего не трогаем."""
    emp = snap["employees"]
    snap["roster_new"], snap["roster_gone"] = [], []
    if not emp:
        return
    order = [u for u in base.get("order", []) if u in emp or u in base.get("emp", {})]
    known = set(order)
    new = [u for u, e in sorted(emp.items(), key=lambda kv: (kv[1].get("num") or 9999, kv[1]["name"])) if u not in known]
    for u in order:
        if u in emp:
            continue
        o = base["emp"][u]
        e = {k: o.get(k) for k in ROSTER_KEYS}
        e.update(on_calls=False, calls=0, doubt=[], in_group=False, gone=True)
        if e["status"] in ONLINE or not e["status"]:
            e["status"], e["offline_since"] = "left", None
            snap["roster_gone"].append(u)
        emp[u] = e
    if order:
        snap["roster_new"] = new
    for i, u in enumerate(order + new):
        emp[u]["order"] = i


def roster_of(snap):
    order = [u for u, _ in sorted_emp(snap)]
    return {"order": order, "emp": {u: {k: snap["employees"][u].get(k) for k in ROSTER_KEYS} for u in order},
            "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


# ====================================================================== снимки
def save(snap):
    os.makedirs(snap_dir(), exist_ok=True)
    keep = {k: v for k, v in snap.items() if k not in ("calls", "unknown")}
    path = os.path.join(snap_dir(), datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def snapshots():
    """[(путь, подпись)] от новых к старым."""
    out = []
    for p in sorted(glob.glob(os.path.join(snap_dir(), "*.json")), reverse=True):
        b = os.path.basename(p)[:-5]
        try:
            lab = datetime.strptime(b, "%Y%m%d_%H%M%S").strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            lab = b
        out.append((p, lab))
    return out


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ====================================================================== сравнение
def diff(old, new):
    """-> список (группа, текст). Группы: зашёл, вышел, пропал, статус, ник, ранг, катает, не катает, чужие."""
    out = []
    if not old:
        return out
    oe, ne = old.get("employees", {}), new.get("employees", {})

    def nm(uid):
        e = ne.get(uid) or oe.get(uid) or {}
        return f"{e.get('name') or '?'} ({uid})"
    for uid, e in ne.items():
        o = oe.get(uid)
        on_now = e["status"] in ONLINE
        if o is None or (o["status"] not in ONLINE and on_now):
            if on_now or o is None:
                out.append(("зашёл", f"{nm(uid)} - {STATUS_NAME.get(e['status'], e['status'])}"))
            continue
        if o["status"] in ONLINE and not on_now:
            out.append(("вышел", f"{nm(uid)}" + (f" - не в сети с {e['offline_since']}" if e.get("offline_since") else
                                                 " - нет на скринах" if e["status"] == "left" else "")))
        elif o["status"] != e["status"] and on_now:
            out.append(("статус", f"{nm(uid)}: {STATUS_NAME.get(o['status'])} → {STATUS_NAME.get(e['status'])}"))
        if o.get("name") and e.get("name") and o["name"] != e["name"]:
            out.append(("ник", f"{uid}: {o['name']} → {e['name']}"))
        if o.get("rank") and e.get("rank") and o["rank"] != e["rank"]:
            out.append(("ранг", f"{nm(uid)}: {'повысили' if e['rank'] > o['rank'] else 'понизили'} {o['rank']} → {e['rank']}"))
        if o.get("dept") and e.get("dept") and o["dept"] != e["dept"] and not e.get("gone"):
            out.append(("отдел", f"{nm(uid)}: {o['dept']} → {e['dept']}"))
        if old.get("has_group") and new.get("has_group"):
            if e.get("in_group") and not o.get("in_group"):
                out.append(("в группу", f"{nm(uid)} вошёл в группу"))
            elif o.get("in_group") and not e.get("in_group"):
                out.append(("из группы", f"{nm(uid)} вышел из группы"))
        if old.get("has_calls") and new.get("has_calls") and on_now:
            if e.get("on_calls") and not o.get("on_calls"):
                out.append(("катает", f"{nm(uid)} начал катать вызовы"))
            elif o.get("on_calls") and not e.get("on_calls"):
                out.append(("не катает", f"{nm(uid)} перестал катать вызовы"))
    for uid, o in oe.items():
        if uid in ne or o["status"] not in ONLINE:
            continue
        if new.get("complete"):
            out.append(("вышел", f"{nm(uid)} - нет среди в сети"))
        else:
            out.append(("пропал", f"{nm(uid)} - нет на скринах (список обрезан? нужен ещё скрин)"))
    return out


GROUP_TITLE = [("зашёл", "Зашли"), ("вышел", "Вышли"), ("пропал", "Пропали"), ("статус", "Сменили статус"),
               ("ник", "Сменили ник"), ("ранг", "Ранг"), ("отдел", "Сменили отдел"), ("катает", "Начали катать вызовы"),
               ("не катает", "Перестали катать вызовы"), ("в группу", "Вошли в группу"),
               ("из группы", "Вышли из группы")]


# ====================================================================== вывод
def status_text(e):
    if e["status"] == "offline":
        return "Не в сети" + (f" с {e['offline_since']}" if e.get("offline_since") else "")
    return STATUS_NAME.get(e["status"], "?")


def sorted_emp(snap):
    return sorted(snap["employees"].items(), key=lambda kv: (kv[1].get("order", 0), kv[1].get("num") or 9999,
                                                             kv[1]["name"]))


def table_text(snap):
    lines = ["№\tНик\tСтатик\tРанг\tОтдел\tСтатус\tВызовы\tГруппа\tБрифинг\tКомментарий"]
    for i, (uid, e) in enumerate(sorted_emp(snap), 1):
        com = (e.get("comment") or "").replace("\t", " ").replace("\n", " ")
        lines.append(f"{i}\t{e['name']}\t{uid}\t{e.get('rank') or ''}\t{e.get('dept') or ''}\t{status_text(e)}\t"
                     f"{'катает' if e.get('on_calls') else ''}\t{group_text(snap, e)}\t"
                     f"{BRIEF_NAME.get(e.get('brief') or '', '')}\t{com}")
    return "\n".join(lines)


def group_text(snap, e):
    if not snap.get("has_group"):
        return ""
    return "в группе" if e.get("in_group") else "нет"


def changes_text(snap, old, old_label=""):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    head = f"Проверка {now}" + (f" (сравнение со снимком {old_label})" if old_label else "")
    if not old:
        return head + "\nПрошлого снимка нет: это первая проверка. Нажми «Сохранить снимок» - следующая сравнится с ним."
    d = diff(old, snap)
    parts = [head]
    for g, title in GROUP_TITLE:
        items = [t for gg, t in d if gg == g]
        if items:
            parts.append(f"\n{title} ({len(items)}):\n" + "\n".join("• " + t for t in items))
    if snap.get("outsiders"):
        parts.append(f"\nВ вызовах, но нет в списке PM ({len(snap['outsiders'])}):\n" +
                     "\n".join(f"• {v['name'] or '?'} ({u}) - вызовов: {v['n']}" for u, v in snap["outsiders"].items()))
    if snap.get("group_outsiders"):
        parts.append(f"\nВ группе, но нет в списке PM ({len(snap['group_outsiders'])}):\n" +
                     "\n".join(f"• {n}" for n in snap["group_outsiders"]))
    if len(parts) == 1:
        parts.append("\nИзменений нет.")
    return "\n".join(parts)


def f_text(snap):
    online = [(u, e) for u, e in sorted_emp(snap) if e["status"] in ASK]
    no = [e["name"] for u, e in online if not e.get("on_calls")]
    yes = [e["name"] for u, e in online if e.get("on_calls")]
    out = []
    if not snap.get("has_calls"):
        out.append("⚠ Нет скринов «Вызовы» (История): считаю, что вызовы не катает никто.\n")
    out += [F_TEXT.format(name=n) for n in no]
    if yes:
        out.append("\nКатают вызовы: " + ", ".join(yes))
    return "\n".join(out)
