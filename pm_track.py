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
    for r in results:
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
        employees[uid] = {"num": e.get("num"), "name": e["name"], "rank": e["rank"], "status": e["status"],
                          "offline_since": e.get("offline_since"), "on_calls": uid in on_calls,
                          "calls": on_calls.get(uid, {}).get("n", 0), "doubt": list(e["doubt"])}
    outsiders = {u: v for u, v in on_calls.items() if u not in employees}
    return {"taken_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "employees": employees,
            "complete": complete, "has_calls": bool(uniq), "calls_newest": newest.strftime("%d.%m.%Y %H:%M") if newest else None,
            "window_min": window_min, "outsiders": outsiders, "calls": uniq, "unknown": unknown}


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
            out.append(("вышел", f"{nm(uid)}" + (f" - не в сети с {e['offline_since']}" if e.get("offline_since") else "")))
        elif o["status"] != e["status"] and on_now:
            out.append(("статус", f"{nm(uid)}: {STATUS_NAME.get(o['status'])} → {STATUS_NAME.get(e['status'])}"))
        if o.get("name") and e.get("name") and o["name"] != e["name"]:
            out.append(("ник", f"{uid}: {o['name']} → {e['name']}"))
        if o.get("rank") and e.get("rank") and o["rank"] != e["rank"]:
            out.append(("ранг", f"{nm(uid)}: {'повысили' if e['rank'] > o['rank'] else 'понизили'} {o['rank']} → {e['rank']}"))
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
               ("ник", "Сменили ник"), ("ранг", "Ранг"), ("катает", "Начали катать вызовы"),
               ("не катает", "Перестали катать вызовы")]


# ====================================================================== вывод
def status_text(e):
    if e["status"] == "offline":
        return "Не в сети" + (f" с {e['offline_since']}" if e.get("offline_since") else "")
    return STATUS_NAME.get(e["status"], "?")


def sorted_emp(snap):
    return sorted(snap["employees"].items(), key=lambda kv: (kv[1].get("num") or 9999, kv[1]["name"]))


def table_text(snap):
    lines = ["№\tНик\tСтатик\tРанг\tСтатус\tВызовы"]
    for i, (uid, e) in enumerate(sorted_emp(snap), 1):
        lines.append(f"{e.get('num') or i}\t{e['name']}\t{uid}\t{e.get('rank') or ''}\t{status_text(e)}\t"
                     f"{'катает' if e.get('on_calls') else ''}")
    return "\n".join(lines)


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
