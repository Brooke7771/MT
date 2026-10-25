# -*- coding: utf-8 -*-
"""PM: история по каждому сотруднику (копится с каждой проверкой) и окно его статистики.
Что копится (pm_history.json в папке данных, ключ - статик):
  checks - время каждой проверки состава («Коллеги»); pts - в каких проверках человек был в сети и с каким статусом,
  катал ли вызовы; rk - смены ранга/отдела; calls - принятые им вызовы из «Истории»; bal - строки «Управления балансом».
Время вызовов и баланса - часы сервера (как в планшете), в расчётах переводится в часы ПК сдвигом tz_shift."""
import os
import json
import time
from collections import Counter
from datetime import datetime, timedelta

import pm_track as PT
from pm_ocr import STATUS_NAME, ONLINE

KEEP_DAYS = 60              # дольше не храним
SAME_CHECK = 5 * 60         # скрины «Коллеги» ближе этого - одна проверка
GAP_MAX = 35 * 60           # между проверками дольше - не считаем, что был в сети всё это время

SHORT = {"active": "активен", "away": "нет на месте", "arena": "на арене", "jail": "в тюрьме"}
_H = {"h": None}


def path():
    return os.path.join(PT.data_dir(), "pm_history.json")


def load():
    if _H["h"] is None:
        try:
            with open(path(), encoding="utf-8") as f:
                h = json.load(f)
        except (OSError, ValueError):
            h = {}
        h.setdefault("checks", [])
        h.setdefault("people", {})
        _H["h"] = h
    return _H["h"]


def save():
    h = load()
    try:
        with open(path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(path() + ".tmp", path())
    except OSError:
        pass


def tz_shift():
    """На сколько секунд часы сервера впереди часов ПК (узнаёт автоматическая проверка)."""
    try:
        with open(os.path.join(PT.data_dir(), "pm_afk_state.json"), encoding="utf-8") as f:
            return float(json.load(f).get("tz_shift", 0))
    except (OSError, ValueError, TypeError):
        return 0.0


def _person(h, uid, name=""):
    p = h["people"].setdefault(uid, {"name": name, "pts": [], "rk": [], "calls": {}, "bal": {}})
    if name:
        p["name"] = name
    return p


def _prune(h, now):
    t0 = now - KEEP_DAYS * 86400
    h["checks"] = [t for t in h["checks"] if t >= t0]
    for p in h["people"].values():
        p["pts"] = [x for x in p["pts"] if x[0] >= t0]
        for k in ("calls", "bal"):
            p[k] = {kk: v for kk, v in p[k].items() if (_ts(v[0]) or now) >= t0 - 2 * 86400}


def _ts(date_s, shift=0.0):
    d = PT._dt(date_s)
    return d.timestamp() - shift if d else None


def record_snap(snap, check_t=None):
    """Проверка состава (снимок вкладки PM) -> история. Пересборка той же проверки (правки, новые скрины тех же
    минут) перезаписывает её точку, а не добавляет новую."""
    if not snap:
        return
    h = load()
    changed = False
    now = time.time()
    emp = snap.get("employees") or {}
    real = {u: e for u, e in emp.items() if e.get("status") not in ("left", "unknown")}
    if real:
        t = round(check_t or now)
        if h["checks"] and abs(t - h["checks"][-1]) < SAME_CHECK:
            t = h["checks"][-1]
        else:
            h["checks"].append(t)
            changed = True
        for uid, e in real.items():
            p = _person(h, uid, e.get("name", ""))
            on = e.get("status") in ONLINE
            pt = [t, e.get("status"), 1 if e.get("on_calls") else 0, int(e.get("calls") or 0)]
            if p["pts"] and p["pts"][-1][0] == t:
                if not on:
                    p["pts"].pop()
                    changed = True
                elif p["pts"][-1] != pt:
                    p["pts"][-1] = pt
                    changed = True
            elif on:
                p["pts"].append(pt)
                changed = True
            rk = [e.get("rank") or "", e.get("dept") or ""]
            if rk != ["", ""] and (not p["rk"] or p["rk"][-1][1:] != rk):
                p["rk"].append([t] + rk)
                changed = True
    for c in snap.get("calls") or []:
        uid = c.get("uid")
        if not uid or not c.get("date"):
            continue
        p = _person(h, uid, "" if uid in h["people"] else (c.get("employee") or ""))
        k = f"{c.get('citizen') or ''}|{c['date']}"
        v = [c["date"], c.get("citizen") or "", c.get("reason") or "", c.get("status") or "", c.get("desc") or ""]
        if p["calls"].get(k) != v:
            p["calls"][k] = v
            changed = True
    if changed:
        _prune(h, now)
        save()


def record_balance(res):
    """Итог проверки по балансу {uid: {"name", "rows": [[дата, тип], ...]}} -> история."""
    h = load()
    changed = False
    for uid, b in (res or {}).items():
        rows = b.get("rows") or []
        if not rows:
            continue
        p = _person(h, uid, b.get("name", ""))
        for d, typ in rows:
            k = f"{d}|{typ}"
            if k not in p["bal"]:
                p["bal"][k] = [d, typ]
                changed = True
    if changed:
        save()


# ====================================================================== расчёт
PERIODS = ("Сегодня", "Вчера", "7 дней", "30 дней", "Всё время")


def period_bounds(name, now=None):
    now = now or time.time()
    mid = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "Сегодня":
        return mid.timestamp(), now
    if name == "Вчера":
        return (mid - timedelta(days=1)).timestamp(), mid.timestamp()
    if name == "7 дней":
        return (mid - timedelta(days=6)).timestamp(), now
    if name == "30 дней":
        return (mid - timedelta(days=29)).timestamp(), now
    return 0.0, now


def _online(h, p, t0, t1):
    """Время в сети по проверкам: от проверки, где был в сети, до следующей (не дольше GAP_MAX)."""
    checks = h["checks"]
    nxt = {a: b for a, b in zip(checks, checks[1:])}
    by_st, calls_s, n = Counter(), 0.0, 0
    for t, stt, oc, _ in p["pts"]:
        if not (t0 <= t < t1):
            continue
        n += 1
        b = nxt.get(t)
        if b is None or b - t > GAP_MAX:
            continue
        d = min(b, t1) - t
        by_st[stt] += d
        if oc:
            calls_s += d
    return by_st, calls_s, n


def _calls_of(p, t0, t1, shift):
    out = []
    for v in p["calls"].values():
        t = _ts(v[0], shift)
        if t is not None and t0 <= t < t1:
            out.append((t, v))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _bal_of(p, t0, t1, shift):
    out = []
    for d, typ in p["bal"].values():
        t = _ts(d, shift)
        if t is not None and t0 <= t < t1:
            out.append((t, d, typ))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def bal_kind(typ):
    t = (typ or "").split("#")[0].strip()
    return t or "?"


def summary(uid, period, now=None):
    now = now or time.time()
    h = load()
    shift = tz_shift()
    t0, t1 = period_bounds(period, now)
    p = h["people"].get(uid) or {"name": "", "pts": [], "rk": [], "calls": {}, "bal": {}}
    by_st, calls_s, n_on = _online(h, p, t0, t1)
    calls = _calls_of(p, t0, t1, shift)
    bal = _bal_of(p, t0, t1, shift)
    gaps = [(a[0] - b[0]) / 60 for a, b in zip(calls, calls[1:]) if 0 < a[0] - b[0] < 2 * 3600]
    gaps.sort()
    # место в команде по вызовам за тот же период (среди тех, кто был в сети или принимал вызовы)
    team = []
    for u, q in h["people"].items():
        nc = len(_calls_of(q, t0, t1, shift))
        if nc or any(t0 <= x[0] < t1 for x in q["pts"]):
            team.append((nc, u))
    mine = len(calls)
    place = 1 + sum(1 for nc, _ in team if nc > mine) if mine else None      # равные делят место
    checks = [t for t in h["checks"] if t0 <= t < t1]
    all_t = [x[0] for x in p["pts"]] + [t for t, _ in _calls_of(p, 0, now + 86400, shift)]
    return {"uid": uid, "name": p.get("name", ""), "t0": t0, "t1": t1, "period": period,
            "online": sum(by_st.values()), "by_status": dict(by_st), "calls_time": calls_s,
            "checks": len(checks), "checks_on": n_on,
            "calls": calls, "reasons": Counter(v[2] or "?" for _, v in calls),
            "call_status": Counter(v[3] or "?" for _, v in calls),
            "bal": bal, "bal_types": Counter(bal_kind(x[2]) for x in bal),
            "gap_med": gaps[len(gaps) // 2] if gaps else None,
            "place": place, "team": len(team),
            "rk": p["rk"], "pts": [x for x in p["pts"] if t0 <= x[0] < t1],
            "since": min(h["checks"]) if h["checks"] else None, "first_seen": min(all_t) if all_t else None,
            "last_call": calls[0] if calls else None, "last_bal": bal[0] if bal else None}


def hm(sec):
    m = int(round(sec / 60))
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч {m % 60:02d} мин"


def _ago(t, now):
    m = int((now - t) / 60)
    if m < 60:
        return f"{max(m, 0)} мин назад"
    if m < 48 * 60:
        return f"{m // 60} ч назад"
    return f"{m // 1440} дн. назад"


def lines(s, now=None):
    """Сводка текстом (окно «Обзор» и «Копировать»)."""
    now = now or time.time()
    out = []
    on = s["online"]
    if on:
        parts = [f"{SHORT.get(k, k)} {hm(v)}" for k, v in sorted(s["by_status"].items(), key=lambda kv: -kv[1])]
        out.append(f"В сети: ~{hm(on)} ({', '.join(parts)})")
        out.append(f"Катал вызовы: ~{hm(s['calls_time'])} ({round(100 * s['calls_time'] / on)}% времени в сети)")
    elif s["checks_on"]:
        out.append("В сети: только в последней проверке (время считается от проверки до следующей)")
    elif s["checks"]:
        out.append("В сети: ни в одной проверке за период")
    out.append(f"В сети в проверках: {s['checks_on']} из {s['checks']}" if s["checks"] else "Проверок за период нет")
    nc = len(s["calls"])
    if nc:
        rate = f", {nc / (on / 3600):.1f} в час в сети" if on >= 600 else ""
        out.append(f"Вызовов принято: {nc}{rate}")
        if s["gap_med"] is not None:
            out.append(f"Обычный перерыв между вызовами: ~{round(s['gap_med'])} мин")
        out.append("Причины: " + ", ".join(f"{k} {v}" for k, v in s["reasons"].most_common(6)))
        out.append("Статусы вызовов: " + ", ".join(f"{k} {v}" for k, v in s["call_status"].most_common(6)))
        t, v = s["last_call"]
        out.append(f"Последний вызов: {v[0]} ({_ago(t, now)}), гражданин {v[1] or '?'}, {v[2] or '?'}")
    else:
        out.append("Вызовов принято: 0")
    if s["place"]:
        out.append(f"Место по вызовам: {s['place']} из {s['team']}")
    nb = len(s["bal"])
    if nb:
        out.append(f"Баланс: {nb} действий - " + ", ".join(f"{k} {v}" for k, v in s["bal_types"].most_common(6)))
        t, d, typ = s["last_bal"]
        out.append(f"Последнее по балансу: {typ} - {d} ({_ago(t, now)})")
    else:
        out.append("Баланс: действий нет (или его не проверяли)")
    if len(s["rk"]) > 1:
        out.append("Ранг/отдел: " + " → ".join(f"{r or '?'} {d}".strip() for _, r, d in s["rk"][-5:]))
    return out


# ====================================================================== окно
def open_window(parent, uid, emp=None, marks=None, shot=None):
    import tkinter as tk
    from tkinter import ttk
    import theme as T
    emp = emp or {}
    marks = marks or {}
    d = tk.Toplevel(parent)
    d.title(f"Статистика - {emp.get('name') or uid}")
    d.configure(bg=T.BG)
    d.geometry("980x700")
    d.minsize(760, 520)
    try:
        T.dark_titlebar(d)
    except Exception:
        pass

    head = ttk.Frame(d)
    head.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(head, text=emp.get("name") or (load()["people"].get(uid) or {}).get("name") or uid, bg=T.BG, fg=T.TEXT,
             font=("Segoe UI", 16, "bold")).pack(side="left")
    info = [f"статик {uid}"]
    if emp.get("rank"):
        info.append(f"ранг {emp['rank']}")
    if emp.get("dept"):
        info.append(emp["dept"])
    if emp.get("status"):
        info.append(PT.status_text(emp))
    if emp.get("in_group"):
        info.append("в группе")
    if marks.get("brief"):
        info.append("брифинг: " + PT.BRIEF_NAME.get(marks["brief"], ""))
    if marks.get("comment"):
        info.append("«" + marks["comment"] + "»")
    tk.Label(head, text="  ·  ".join(info), bg=T.BG, fg=T.MUTED, font=("Segoe UI", 10)).pack(side="left", padx=12, pady=(6, 0))
    per_var = tk.StringVar(value="7 дней")
    cb = ttk.Combobox(head, textvariable=per_var, values=PERIODS, state="readonly", width=11)
    cb.pack(side="right")
    ttk.Label(head, text="Период:").pack(side="right", padx=6)

    tiles = tk.Canvas(d, bg=T.BG, height=86, highlightthickness=0)
    tiles.pack(fill="x", padx=14, pady=4)

    nb = ttk.Notebook(d)
    nb.pack(fill="both", expand=True, padx=14, pady=4)
    f_over, f_calls, f_bal, f_chk = (ttk.Frame(nb) for _ in range(4))
    for f, t in ((f_over, "Обзор"), (f_calls, "Вызовы"), (f_bal, "Баланс"), (f_chk, "Проверки")):
        nb.add(f, text=t)

    chart = tk.Canvas(f_over, bg=T.CARD, height=210, highlightthickness=0)
    chart.pack(fill="x", pady=(6, 4))
    txt = tk.Text(f_over, bg=T.FIELD, fg=T.TEXT, relief="flat", wrap="word", font=("Segoe UI", 10),
                  padx=10, pady=8, highlightthickness=0, height=10)
    txt.pack(fill="both", expand=True, pady=(0, 4))

    def table(f, cols):
        tv = ttk.Treeview(f, columns=[c[0] for c in cols], show="headings")
        for c, hdr, w, a in cols:
            tv.heading(c, text=hdr)
            tv.column(c, width=w, anchor=a, stretch=c == cols[-1][0] or w >= 200)
        sb = ttk.Scrollbar(f, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True, pady=6)
        sb.pack(side="right", fill="y", pady=6)
        tv.tag_configure("odd", background=T.FIELD_ALT)
        return tv
    tv_c = table(f_calls, (("date", "Дата (сервер)", 130, "w"), ("cit", "Гражданин", 90, "e"), ("reason", "Причина", 150, "w"),
                           ("status", "Статус", 140, "w"), ("desc", "Описание", 300, "w")))
    tv_b = table(f_bal, (("date", "Дата (сервер)", 130, "w"), ("type", "Действие", 500, "w")))
    tv_k = table(f_chk, (("t", "Проверка", 130, "w"), ("st", "Статус", 150, "w"), ("work", "Вызовы", 300, "w")))

    bot = ttk.Frame(d)
    bot.pack(fill="x", padx=14, pady=(4, 12))
    hint = ttk.Label(bot, text="", style="Muted.TLabel")
    hint.pack(side="left")
    ttk.Button(bot, text="Закрыть", command=d.destroy).pack(side="right")
    if shot and os.path.exists(shot):
        ttk.Button(bot, text="Скрин баланса", command=lambda: os.startfile(shot)).pack(side="right", padx=6)
    ttk.Button(bot, text="Копировать сводку", style="Accent.TButton", command=lambda: copy()).pack(side="right")
    S = {"s": None}

    def copy():
        s = S["s"]
        if not s:
            return
        head_ = f"{s['name'] or emp.get('name', '')} ({uid}) - {s['period'].lower()}"
        d.clipboard_clear()
        d.clipboard_append("\n".join([head_] + lines(s)))
        hint.configure(text="Сводка скопирована")

    def draw_tiles(s):
        tiles.delete("all")
        w = max(tiles.winfo_width(), 700)
        on = s["online"]
        items = [("ВЫЗОВОВ", str(len(s["calls"])), T.OK),
                 ("БАЛАНС, ДЕЙСТВИЙ", str(len(s["bal"])), "#4fd1c5"),
                 ("В СЕТИ", hm(on) if on else "-", T.TEXT),
                 ("ВЫЗОВОВ В ЧАС", f"{len(s['calls']) / (on / 3600):.1f}" if on >= 600 else "-", T.TEXT),
                 ("МЕСТО ПО ВЫЗОВАМ", f"{s['place']} из {s['team']}" if s["place"] else "-", T.ACCENT)]
        gap = 10
        tw = (w - gap * (len(items) - 1)) / len(items)
        for i, (lab, val, col) in enumerate(items):
            x = i * (tw + gap)
            tiles.create_rectangle(x, 2, x + tw, 84, fill=T.CARD, outline=T.CARD_EDGE)
            tiles.create_text(x + 14, 20, text=lab, anchor="w", fill=T.MUTED, font=("Segoe UI", 8, "bold"))
            tiles.create_text(x + 14, 54, text=val, anchor="w", fill=col, font=("Segoe UI", 18, "bold"))

    def draw_chart(s):
        """Столбики: вызовы (зелёные) и действия баланса (бирюзовые) по дням, за один день - по часам."""
        chart.delete("all")
        w = max(chart.winfo_width(), 700)
        hgt = 210
        one_day = s["t1"] - s["t0"] <= 86400 + 1
        if one_day:
            keys = list(range(24))
            key = lambda t: datetime.fromtimestamp(t).hour
            labs = [f"{k}" for k in keys]
            title = "по часам"
        else:
            t0 = s["t0"] or min([t for t, _ in s["calls"]] + [x[0] for x in s["bal"]] + [s["t1"] - 6 * 86400])
            days = []
            dd = datetime.fromtimestamp(max(t0, s["t1"] - 30 * 86400)).date()
            end = datetime.fromtimestamp(s["t1"]).date()
            while dd <= end:
                days.append(dd)
                dd += timedelta(days=1)
            keys = days
            key = lambda t: datetime.fromtimestamp(t).date()
            labs = [k.strftime("%d.%m") for k in keys]
            title = "по дням"
        c = Counter(key(t) for t, _ in s["calls"])
        b = Counter(key(x[0]) for x in s["bal"])
        top = max([c[k] + b[k] for k in keys] + [1])
        chart.create_text(12, 14, text=f"Вызовы и баланс {title}", anchor="w", fill=T.MUTED, font=("Segoe UI", 9, "bold"))
        chart.create_rectangle(w - 210, 9, w - 200, 19, fill=T.OK, outline="")
        chart.create_text(w - 195, 14, text="вызовы", anchor="w", fill=T.MUTED, font=("Segoe UI", 8))
        chart.create_rectangle(w - 130, 9, w - 120, 19, fill="#4fd1c5", outline="")
        chart.create_text(w - 115, 14, text="баланс", anchor="w", fill=T.MUTED, font=("Segoe UI", 8))
        x0, y0, y1 = 30, 34, hgt - 24
        slot = (w - x0 - 12) / max(len(keys), 1)
        bw = max(3, min(28, slot * 0.7))
        step = max(1, int(36 / slot) + 1)
        for i, k in enumerate(keys):
            x = x0 + i * slot + (slot - bw) / 2
            hc = (y1 - y0) * c[k] / top
            hb = (y1 - y0) * b[k] / top
            if c[k]:
                chart.create_rectangle(x, y1 - hc, x + bw, y1, fill=T.OK, outline="")
            if b[k]:
                chart.create_rectangle(x, y1 - hc - hb, x + bw, y1 - hc, fill="#4fd1c5", outline="")
            if c[k] + b[k]:
                chart.create_text(x + bw / 2, y1 - hc - hb - 7, text=str(c[k] + b[k]), fill=T.TEXT, font=("Segoe UI", 7))
            if i % step == 0:
                chart.create_text(x + bw / 2, y1 + 11, text=labs[i], fill=T.MUTED, font=("Segoe UI", 7))
        chart.create_line(x0, y1, w - 12, y1, fill=T.BORDER)

    def refresh(e=None):
        s = summary(uid, per_var.get())
        S["s"] = s
        draw_tiles(s)
        draw_chart(s)
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", "\n".join(lines(s)))
        if not s["checks"] and not s["calls"] and not s["bal"]:
            txt.insert("end", "\n\nЗа этот период данных нет. Статистика копится с версии 1.55: с каждой проверкой "
                              "(скрины «Коллеги» и «Вызовы» или автоматическая проверка) и проверкой по балансу.")
        txt.configure(state="disabled")
        for tv in (tv_c, tv_b, tv_k):
            tv.delete(*tv.get_children())
        for i, (t, v) in enumerate(s["calls"]):
            tv_c.insert("", "end", values=(v[0], v[1], v[2], v[3], v[4]), tags=("odd",) if i % 2 else ())
        for i, (t, dt_, typ) in enumerate(s["bal"]):
            tv_b.insert("", "end", values=(dt_, typ), tags=("odd",) if i % 2 else ())
        for i, (t, stt, oc, n) in enumerate(reversed(s["pts"])):
            tv_k.insert("", "end", values=(datetime.fromtimestamp(t).strftime("%d.%m.%Y %H:%M"),
                                           STATUS_NAME.get(stt, stt), f"катал ({n})" if oc else ""),
                        tags=("odd",) if i % 2 else ())
        nb.tab(f_calls, text=f"Вызовы ({len(s['calls'])})")
        nb.tab(f_bal, text=f"Баланс ({len(s['bal'])})")
        nb.tab(f_chk, text=f"Проверки ({len(s['pts'])})")
        since = s["since"]
        hint.configure(text=f"Данные копятся с {datetime.fromtimestamp(since).strftime('%d.%m.%Y %H:%M')}" if since else
                       "Данных пока нет")
    cb.bind("<<ComboboxSelected>>", refresh)
    d.bind("<Escape>", lambda e: d.destroy())
    d.after(60, refresh)
    tiles.bind("<Configure>", lambda e: S["s"] and draw_tiles(S["s"]))
    chart.bind("<Configure>", lambda e: S["s"] and draw_chart(S["s"]))
    return d
