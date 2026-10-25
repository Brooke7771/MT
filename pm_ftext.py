# -*- coding: utf-8 -*-
"""Вкладка «Тексты /f» в PM: очередь сообщений по сотрудникам, которые в сети и не катают вызовы.
  - свои шаблоны с подстановками ({name}, {first}, {uid}, {rank}, {dept}, {status}, {calls}, {num}, {time});
  - отдельный шаблон для «Нет на месте»; кого включать в список; свои строки вручную;
  - отправка в чат игры: открыть чат (клавиша), вставить текст (Ctrl+V или печатью), Enter -
    по кнопке «Отправить следующее», горячей клавишей или прогоном всего списка раз в N секунд;
  - в списке видно, что уйдёт следующим и что уже отправлено; копирование всего / текущего / выбранного.
Отправляет только когда окно игры на переднем плане (иначе прогон ждёт)."""
import time
import ctypes
import threading
from datetime import datetime

import pm_track as PT

DEFAULT_TEMPLATES = [
    {"name": "Выйти на связь", "text": "/f {name}, выйдите на связь: где вы сейчас находитесь и чем заняты?"},
    {"name": "Взять вызов", "text": "/f {name}, возьмите, пожалуйста, вызов - на вас никто не катает."},
    {"name": "Нет на месте", "text": "/f {name}, вы отмечены «Нет на месте». Вы на связи?"},
    {"name": "Коротко", "text": "/f {first}, на связь"},
    {"name": "Отчёт о работе", "text": "/f {name}, прошу предоставить отчёт о проделанной работе за последние 30 минут: в спец. связь, в личные сообщения или во флуд нашего отдела с тегом {me}."},
]
REPORT_TPL = {"name": "Отчёт о работе", "text": "/f {name}, прошу предоставить отчёт о проделанной работе за последние 30 минут: в спец. связь, в личные сообщения или во флуд нашего отдела с тегом {me}."}
PLACEHOLDERS = [("{name}", "ник полностью"), ("{first}", "имя (первое слово ника)"), ("{uid}", "статик"),
                ("{rank}", "ранг"), ("{dept}", "отдел"), ("{status}", "статус («Сейчас активен» ...)"),
                ("{calls}", "вызовов на скринах"), ("{num}", "номер в очереди"), ("{time}", "время сейчас ЧЧ:ММ"),
                ("{me}", "твой ник (поле «Мой ник»)")]
CHAT_KEYS = {"T": 0x14, "Y": 0x15, "U": 0x16, "Enter": 0x1C, "F6": 0x40, "`": 0x29}
HOTKEYS = {0: "выкл", 0x77: "F8", 0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x2D: "Insert", 0x24: "Home",
           0x23: "End", 0x21: "PageUp", 0x22: "PageDown", 0x13: "Pause", 0x6E: "Numpad .", 0x6F: "Numpad /"}
GAME_TITLES = ("gta", "rage", "majestic", "grand theft")
SC_CTRL, SC_V, SC_ENTER = 0x1D, 0x2F, 0x1C


def fill_cfg(cfg):
    if not cfg.get("f_templates"):
        cfg["f_templates"] = [dict(t) for t in DEFAULT_TEMPLATES]
    cfg.setdefault("f_tpl", 0)
    cfg.setdefault("f_tpl_away", -1)            # -1 - тот же, что для «Сейчас активен»
    cfg.setdefault("f_who", {"active": True, "away": True})
    cfg.setdefault("f_interval", 5)
    cfg.setdefault("f_chat_key", "T")
    cfg.setdefault("f_method", "paste")
    cfg.setdefault("f_hotkey", 0)
    cfg.setdefault("f_restore_clip", True)
    cfg.setdefault("f_me", "")
    cfg.setdefault("f_skip_working", True)
    if int(cfg.get("f_tpl_v", 1)) < 2:              # шаблон «Отчёт о работе» появился в pm-v1.6.0
        if not any("отчёт" in t.get("text", "").lower() for t in cfg["f_templates"]):
            cfg["f_templates"].append(dict(REPORT_TPL))
        cfg["f_tpl_v"] = 2
    return cfg


def render(tpl, e, uid, num, me=""):
    name = e.get("name") or ""
    vals = {"name": name, "first": name.split()[0] if name.split() else name, "uid": uid or "",
            "rank": e.get("rank") or "", "dept": e.get("dept") or "", "status": PT.status_text(e),
            "calls": e.get("calls") or 0, "num": num, "time": datetime.now().strftime("%H:%M"), "me": me}
    out = tpl
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ====================================================================== ввод в игру
class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _U(ctypes.Union):
    _fields_ = [("ki", _KI), ("mi", _MI)]


class _IN(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send(scan=0, up=False, uni=None):
    ki = _KI(0, uni if uni is not None else scan,
             (0x0004 if uni is not None else 0x0008) | (0x0002 if up else 0), 0, None)
    inp = _IN(1, _U(ki=ki))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def tap(scan, hold=0.03):
    _send(scan)
    time.sleep(hold)
    _send(scan, up=True)


def type_text(text):
    for ch in text:
        for unit in (ch.encode("utf-16-le")[i:i + 2] for i in range(0, len(ch.encode("utf-16-le")), 2)):
            code = int.from_bytes(unit, "little")
            _send(uni=code)
            _send(uni=code, up=True)
        time.sleep(0.004)


def _clip_api():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = (ctypes.c_void_p,)
    k.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    return u, k


def get_clip():
    u, k = _clip_api()
    if not u.OpenClipboard(None):
        return None
    try:
        h = u.GetClipboardData(13)
        if not h:
            return None
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def set_clip(text):
    u, k = _clip_api()
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):
        if u.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        return bool(u.SetClipboardData(13, h))
    finally:
        u.CloseClipboard()


def game_foreground():
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        return any(t in b.value.lower() for t in GAME_TITLES)
    except Exception:
        return False


def send_chat(text, chat_key="T", method="paste", restore=True):
    """Открыть чат, ввести текст, Enter. -> "" или причина, почему не отправлено."""
    if not game_foreground():
        return "окно игры не активно"
    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    old = get_clip() if (method == "paste" and restore) else None
    tap(CHAT_KEYS.get(chat_key, 0x14))
    time.sleep(0.28)                           # чат открывается не сразу
    if method == "paste":
        if not set_clip(text):
            tap(0x01)                          # Esc - закрыть чат
            return "буфер обмена занят"
        _send(SC_CTRL)
        time.sleep(0.02)
        tap(SC_V)
        time.sleep(0.02)
        _send(SC_CTRL, up=True)
    else:
        type_text(text)
    time.sleep(0.15)
    tap(SC_ENTER)
    if old is not None:
        time.sleep(0.25)
        set_clip(old)
    return ""


# ====================================================================== вкладка
def build_f_tab(frame, cfg, save_cfg, msg):
    """-> объект с refresh(snap), copy_all(), stop()."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    import theme as T

    fill_cfg(cfg)
    st = {"snap": None, "rows": [], "cur": 0, "run": False, "busy": False, "next_t": 0.0, "status": {},
          "extra": [], "override": {}, "alive": True, "hk_prev": False}

    # ---- верх: шаблон и кого писать
    top = ttk.Frame(frame)
    top.pack(fill="x", pady=(4, 2))
    ttk.Label(top, text="Шаблон:").pack(side="left")
    tpl_var = tk.StringVar()
    tpl_cb = ttk.Combobox(top, textvariable=tpl_var, state="readonly", width=24)
    tpl_cb.pack(side="left", padx=4)
    ttk.Label(top, text="для «Нет на месте»:").pack(side="left", padx=(8, 0))
    away_var = tk.StringVar()
    away_cb = ttk.Combobox(top, textvariable=away_var, state="readonly", width=20)
    away_cb.pack(side="left", padx=4)
    ttk.Button(top, text="Шаблоны…", command=lambda: edit_templates()).pack(side="left", padx=4)
    top2 = ttk.Frame(frame)
    top2.pack(fill="x", pady=(0, 2))
    who_a = tk.BooleanVar(value=cfg["f_who"].get("active", True))
    who_w = tk.BooleanVar(value=cfg["f_who"].get("away", True))
    ttk.Label(top2, text="Кому:").pack(side="left", padx=(0, 2))
    ttk.Checkbutton(top2, text="Сейчас активен", variable=who_a, command=lambda: who_changed()).pack(side="left")
    ttk.Checkbutton(top2, text="Нет на месте", variable=who_w, command=lambda: who_changed()).pack(side="left", padx=4)
    ttk.Button(top2, text="+ Своя строка", command=lambda: add_line()).pack(side="right")
    sel_btn = ttk.Button(top2, text="Шаблон выбранным ▾", command=lambda: tpl_popup(widget=sel_btn))
    sel_btn.pack(side="right", padx=4)
    top3 = ttk.Frame(frame)
    top3.pack(fill="x", pady=(0, 2))
    skip_var = tk.BooleanVar(value=cfg["f_skip_working"])
    ttk.Checkbutton(top3, text="Не писать тем, кто работал (проверка по балансу)", variable=skip_var,
                    command=lambda: (cfg.update(f_skip_working=bool(skip_var.get())), save(), rebuild())).pack(side="left")
    me_var = tk.StringVar(value=cfg["f_me"])
    ttk.Entry(top3, textvariable=me_var, width=18).pack(side="right")
    ttk.Label(top3, text="Мой ник (для {me}):").pack(side="right", padx=(8, 4))
    me_var.trace_add("write", lambda *a: (cfg.update(f_me=me_var.get().strip()), save_cfg(cfg), rebuild()))

    # ---- очередь
    mid = ttk.Frame(frame)
    mid.pack(fill="both", expand=True, pady=2)
    cols = ("n", "who", "status", "text", "sent")
    tv = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
    for c, h, w, a, s in (("n", "№", 36, "e", False), ("who", "Кому", 150, "w", False), ("status", "Статус", 110, "w", False),
                          ("text", "Текст", 300, "w", True), ("sent", "Отправка", 96, "w", False)):
        tv.heading(c, text=h)
        tv.column(c, width=w, anchor=a, stretch=s)
    sb = ttk.Scrollbar(mid, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tv.tag_configure("odd", background=T.FIELD_ALT)
    tv.tag_configure("cur", background=T.ACCENT, foreground="#ffffff")
    tv.tag_configure("sent", foreground=T.MUTED)
    tv.tag_configure("skip", foreground="#8a7a55")
    tv.tag_configure("err", foreground="#e06c6c")

    nxt_var = tk.StringVar()
    nxt = tk.Label(frame, textvariable=nxt_var, bg=T.CARD, fg=T.TEXT, anchor="w", justify="left",
                   font=("Segoe UI", 10, "bold"), padx=10, pady=6, wraplength=900)
    nxt.pack(fill="x", pady=(4, 2))
    nxt.bind("<Configure>", lambda e: nxt.configure(wraplength=max(200, e.width - 24)))

    # ---- управление отправкой
    c1 = ttk.Frame(frame)
    c1.pack(fill="x", pady=2)
    ttk.Button(c1, text="➤ Отправить следующее", style="Accent.TButton", command=lambda: send_next()).pack(side="left")
    ttk.Button(c1, text="Пропустить", command=lambda: skip()).pack(side="left", padx=4)
    ttk.Button(c1, text="⟲ С начала", command=lambda: restart()).pack(side="left")
    ttk.Button(c1, text="Отсюда", command=lambda: from_selected()).pack(side="left", padx=4)
    c1b = ttk.Frame(frame)
    c1b.pack(fill="x", pady=2)
    run_btn = ttk.Button(c1b, text="▶ Прогнать весь список", command=lambda: toggle_run())
    run_btn.pack(side="left", padx=(0, 6))
    ttk.Label(c1b, text="каждые").pack(side="left")
    int_var = tk.StringVar(value=str(cfg["f_interval"]))
    ttk.Spinbox(c1b, from_=2, to=600, width=5, textvariable=int_var).pack(side="left", padx=3)
    ttk.Label(c1b, text="с").pack(side="left")
    run_var = tk.StringVar()
    ttk.Label(c1b, textvariable=run_var, style="Muted.TLabel").pack(side="left", padx=10)

    ttk.Button(c1, text="⚙ Отправка…", command=lambda: send_settings()).pack(side="right")
    ttk.Button(c1b, text="всё", command=lambda: copy_all()).pack(side="right")
    ttk.Button(c1b, text="выбранные", command=lambda: copy_sel()).pack(side="right", padx=4)
    ttk.Button(c1b, text="следующее", command=lambda: copy_cur()).pack(side="right")
    ttk.Label(c1b, text="Копировать:").pack(side="right", padx=(0, 4))
    key_var = tk.StringVar(value=cfg["f_chat_key"])
    meth_var = tk.StringVar(value={"paste": "вставкой (Ctrl+V)", "type": "печатью"}[cfg["f_method"]])
    hk_var = tk.StringVar(value=HOTKEYS.get(cfg["f_hotkey"], "выкл"))

    def send_settings():
        d = tk.Toplevel(frame)
        d.title("Отправка в чат")
        d.configure(bg=T.BG)
        d.transient(frame.winfo_toplevel())
        g = ttk.Frame(d)
        g.pack(padx=14, pady=12)
        for i, (lab, var, vals, w) in enumerate((("Чат открывается клавишей:", key_var, list(CHAT_KEYS), 8),
                                                 ("Ввод текста:", meth_var, ["вставкой (Ctrl+V)", "печатью"], 18),
                                                 ("«Отправить следующее» клавишей:", hk_var, list(HOTKEYS.values()), 10))):
            ttk.Label(g, text=lab).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Combobox(g, textvariable=var, values=vals, state="readonly", width=w).grid(row=i, column=1, sticky="w", padx=8)
        ttk.Label(g, text="Если чат открывается, а текст не появляется - выбери «печатью». Горячей клавишей удобно "
                          "слать прямо из игры.", style="Muted.TLabel", wraplength=380, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(d, text="Готово", style="Accent.TButton", command=d.destroy).pack(anchor="e", padx=14, pady=(0, 12))
        try:
            T.dark_titlebar(d)
        except Exception:
            pass

    info_var = tk.StringVar()
    info_lbl = ttk.Label(frame, textvariable=info_var, style="Muted.TLabel", wraplength=600, justify="left")
    info_lbl.pack(fill="x", pady=(2, 0))
    info_lbl.bind("<Configure>", lambda e: info_lbl.configure(wraplength=max(200, e.width - 10)))
    # низ - прижат к низу и упакован раньше очереди: когда места мало (в основной программе выше блок
    # автоматической проверки), сжимается очередь, а не кнопки
    for w, pady in ((info_lbl, (2, 0)), (c1b, 2), (c1, 2), (nxt, (4, 2))):
        w.pack_forget()
        w.pack(side="bottom", fill="x", pady=pady, before=mid)
    tv.configure(height=4)

    # ================================================================ данные
    def tpl_names():
        return [t["name"] for t in cfg["f_templates"]]

    def sync_tpl_boxes():
        names = tpl_names()
        tpl_cb["values"] = names
        away_cb["values"] = ["тот же"] + names
        cfg["f_tpl"] = min(max(0, int(cfg["f_tpl"])), len(names) - 1)
        tpl_var.set(names[cfg["f_tpl"]])
        a = int(cfg["f_tpl_away"])
        away_var.set(names[a] if 0 <= a < len(names) else "тот же")

    def rows_of(snap):
        rows = []
        tpl = cfg["f_templates"][cfg["f_tpl"]]["text"]
        a = int(cfg["f_tpl_away"])
        tpl_away = cfg["f_templates"][a]["text"] if 0 <= a < len(cfg["f_templates"]) else tpl
        if snap:
            for uid, e in PT.sorted_emp(snap):
                s = e.get("status")
                if e.get("on_calls") or s not in ("active", "away"):
                    continue
                if (s == "active" and not cfg["f_who"].get("active", True)) or \
                        (s == "away" and not cfg["f_who"].get("away", True)):
                    continue
                b = e.get("bal") or {}
                if b.get("n") and cfg.get("f_skip_working", True):
                    continue
                key = "u:" + (uid or e.get("name", ""))
                text = st["override"].get(key) or render(tpl_away if s == "away" else tpl, e, uid, len(rows) + 1,
                                                         cfg.get("f_me", ""))
                status = PT.status_text(e)
                if b:
                    status += f" · баланс: {b['n']} действ." if b.get("n") else " · баланс: пусто"
                rows.append({"key": key, "who": e.get("name") or "?", "status": status, "text": text, "e": e, "uid": uid})
        for i, t in enumerate(st["extra"]):
            key = f"x:{i}"
            rows.append({"key": key, "who": "своя строка", "status": "", "text": st["override"].get(key) or t})
        return rows

    def redraw():
        sel = tv.selection()
        tv.delete(*tv.get_children())
        for i, r in enumerate(st["rows"]):
            s = st["status"].get(r["key"]) or ""
            tags = []
            if i == st["cur"] and i < len(st["rows"]):
                tags.append("cur")
            elif s.startswith("✓"):
                tags.append("sent")
            elif s.startswith("пропущено"):
                tags.append("skip")
            elif s.startswith("✗"):
                tags.append("err")
            elif i % 2:
                tags.append("odd")
            tv.insert("", "end", iid=str(i), tags=tags, values=(i + 1, r["who"], r["status"], r["text"], s))
        for s_ in sel:
            if tv.exists(s_):
                tv.selection_add(s_)
        if st["rows"] and st["cur"] < len(st["rows"]):
            tv.see(str(st["cur"]))
            r = st["rows"][st["cur"]]
            nxt_var.set(f"Следующее ({st['cur'] + 1} из {len(st['rows'])}):  {r['text']}")
        elif st["rows"]:
            nxt_var.set(f"Список пройден: отправлено {sum(1 for v in st['status'].values() if v.startswith('✓'))} "
                        f"из {len(st['rows'])}. «С начала» - пройти заново.")
        else:
            nxt_var.set("Писать некому: все в сети катают вызовы (или нет скринов «Коллеги»).")

    def rebuild(keep_cur=True):
        old_key = st["rows"][st["cur"]]["key"] if keep_cur and st["cur"] < len(st["rows"]) else None
        st["rows"] = rows_of(st["snap"])
        keys = [r["key"] for r in st["rows"]]
        if old_key in keys:
            st["cur"] = keys.index(old_key)
        else:                                  # первый ещё не отправленный
            st["cur"] = next((i for i, r in enumerate(st["rows"]) if not st["status"].get(r["key"])), len(st["rows"]))
        redraw()
        snap = st["snap"]
        info = []
        if snap and not snap.get("has_calls"):
            info.append("⚠ Нет скринов «Вызовы» (История): считаю, что вызовы не катает никто.")
        if snap:
            yes = [e["name"] for u, e in PT.sorted_emp(snap) if e.get("status") in ("active", "away") and e.get("on_calls")]
            if yes:
                info.append("Катают вызовы: " + ", ".join(yes))
        info_var.set("   ".join(info))

    def refresh(snap):
        changed = st["snap"] is None or (snap or {}).get("employees", {}).keys() != (st["snap"] or {}).get("employees", {}).keys()
        st["snap"] = snap
        if changed and not st["run"]:
            st["status"].clear()
            st["override"].clear()
        rebuild(keep_cur=not changed)

    # ================================================================ действия
    def copy_text(text, what):
        if not text:
            msg("Нечего копировать")
            return
        frame.clipboard_clear()
        frame.clipboard_append(text)
        msg(f"Скопировано: {what} ✓")

    def copy_all():
        copy_text("\n".join(r["text"] for r in st["rows"]), f"{len(st['rows'])} строк")

    def copy_cur():
        if st["cur"] < len(st["rows"]):
            copy_text(st["rows"][st["cur"]]["text"], "следующее сообщение")

    def copy_sel():
        idx = sorted(int(i) for i in tv.selection())
        copy_text("\n".join(st["rows"][i]["text"] for i in idx), f"{len(idx)} строк")

    def mark(i, text):
        if 0 <= i < len(st["rows"]):
            st["status"][st["rows"][i]["key"]] = text

    def advance():
        st["cur"] += 1
        while st["cur"] < len(st["rows"]) and (st["status"].get(st["rows"][st["cur"]]["key"]) or "").startswith("✓"):
            st["cur"] += 1

    def send_next(auto=False):
        if st["busy"]:
            return
        if st["cur"] >= len(st["rows"]):
            msg("Список пройден - «С начала», чтобы пройти заново")
            stop_run()
            return
        i = st["cur"]
        text = st["rows"][i]["text"]
        st["busy"] = True
        save()

        def work():
            err = send_chat(text, cfg["f_chat_key"], cfg["f_method"], cfg["f_restore_clip"])
            frame.after(0, lambda: done(i, err, auto))
        threading.Thread(target=work, daemon=True).start()

    def done(i, err, auto):
        st["busy"] = False
        if not st["alive"]:
            return
        if err:
            if auto and err == "окно игры не активно":
                run_var.set("Прогон ждёт: переключись в игру")
                st["next_t"] = time.time() + 1.0
                return
            mark(i, "✗ " + err)
            msg(f"Не отправлено: {err}")
            redraw()
            return
        mark(i, "✓ " + time.strftime("%H:%M:%S"))
        advance()
        redraw()
        if st["cur"] >= len(st["rows"]):
            msg("Все сообщения отправлены ✓")
            stop_run()

    def skip():
        if st["cur"] < len(st["rows"]):
            mark(st["cur"], "пропущено")
            advance()
            redraw()

    def restart():
        st["status"].clear()
        st["cur"] = 0
        redraw()

    def from_selected():
        sel = tv.selection()
        if sel:
            st["cur"] = int(sel[0])
            redraw()

    def interval():
        try:
            return max(2, min(600, int(int_var.get())))
        except (ValueError, tk.TclError):
            return 5

    def toggle_run():
        if st["run"]:
            stop_run()
            return
        if st["cur"] >= len(st["rows"]):
            msg("Нечего отправлять: «С начала», чтобы пройти список заново")
            return
        if not messagebox.askyesno("Прогнать список", f"Отправить в чат игры {len(st['rows']) - st['cur']} сообщ. "
                                   f"по одному раз в {interval()} с?\n\nПереключись в игру: пока окно игры не "
                                   f"активно, прогон ждёт. Остановить - этой же кнопкой.", parent=frame):
            return
        st["run"] = True
        st["next_t"] = time.time() + 2.0       # успеть переключиться в игру
        run_btn.configure(text="■ Стоп")
        msg("Прогон запущен: переключись в игру")

    def stop_run():
        st["run"] = False
        run_btn.configure(text="▶ Прогнать весь список")
        run_var.set("")

    def add_line():
        d = tk.Toplevel(frame)
        d.title("Своя строка")
        d.configure(bg=T.BG)
        d.transient(frame.winfo_toplevel())
        ttk.Label(d, text="Текст (уйдёт в чат как есть, например «/f Всем PM: сбор у больницы»):").pack(anchor="w", padx=10, pady=(10, 4))
        v = tk.StringVar(value="/f ")
        e = ttk.Entry(d, textvariable=v, width=80)
        e.pack(fill="x", padx=10)
        e.focus_set()

        def ok(*a):
            t = v.get().strip()
            if t:
                st["extra"].append(t)
                rebuild()
            d.destroy()
        e.bind("<Return>", ok)
        ttk.Button(d, text="Добавить", style="Accent.TButton", command=ok).pack(anchor="e", padx=10, pady=10)

    def edit_row(event=None):
        sel = tv.selection()
        if not sel:
            return
        i = int(sel[0])
        r = st["rows"][i]
        d = tk.Toplevel(frame)
        d.title(f"Текст для: {r['who']}")
        d.configure(bg=T.BG)
        d.transient(frame.winfo_toplevel())
        v = tk.StringVar(value=r["text"])
        e = ttk.Entry(d, textvariable=v, width=90)
        e.pack(fill="x", padx=10, pady=10)
        e.focus_set()

        def ok(*a):
            st["override"][r["key"]] = v.get().strip()
            rebuild()
            d.destroy()

        def remove():
            if r["key"].startswith("x:"):
                del st["extra"][int(r["key"][2:])]
                st["override"] = {k: v_ for k, v_ in st["override"].items() if not k.startswith("x:")}
            else:
                st["status"][r["key"]] = "пропущено"
            rebuild()
            d.destroy()
        e.bind("<Return>", ok)
        bf = ttk.Frame(d)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bf, text="Сохранить", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(bf, text="Убрать из очереди" if r["key"].startswith("x:") else "Не писать ему",
                   command=remove).pack(side="right", padx=6)
    tv.bind("<Double-1>", edit_row)

    tpl_menu = tk.Menu(tv, tearoff=0, bg=T.PANEL, fg=T.TEXT, activebackground=T.ACCENT, activeforeground="#ffffff", bd=0)

    def apply_tpl(j):
        """Шаблон j - выбранным строкам (только на этот раз: в очереди меняется их текст)."""
        t = cfg["f_templates"][j]["text"]
        for iid in tv.selection():
            r = st["rows"][int(iid)]
            if r.get("e") is not None:
                st["override"][r["key"]] = render(t, r["e"], r.get("uid"), int(iid) + 1, cfg.get("f_me", ""))
        rebuild()

    def tpl_popup(event=None, widget=None):
        if event is not None:
            iid = tv.identify_row(event.y)
            if iid and iid not in tv.selection():
                tv.selection_set(iid)
        if not tv.selection():
            msg("Выдели строки (Ctrl/Shift + щелчок), потом выбери шаблон")
            return
        tpl_menu.delete(0, "end")
        tpl_menu.add_command(label=f"Шаблон для выбранных ({len(tv.selection())}):", state="disabled")
        for j, t in enumerate(cfg["f_templates"]):
            tpl_menu.add_command(label=t["name"], command=lambda j=j: apply_tpl(j))
        tpl_menu.add_separator()
        tpl_menu.add_command(label="Вернуть основной", command=lambda: (
            [st["override"].pop(st["rows"][int(i)]["key"], None) for i in tv.selection()], rebuild()))
        if event is not None:
            tpl_menu.tk_popup(event.x_root, event.y_root)
        else:
            w = widget
            tpl_menu.tk_popup(w.winfo_rootx(), w.winfo_rooty() + w.winfo_height())
    tv.bind("<Button-3>", tpl_popup)

    def edit_templates():
        d = tk.Toplevel(frame)
        d.title("Шаблоны /f")
        d.configure(bg=T.BG)
        d.transient(frame.winfo_toplevel())
        d.geometry("820x460")
        left = ttk.Frame(d)
        left.pack(side="left", fill="y", padx=10, pady=10)
        lb = tk.Listbox(left, width=26, bg=T.FIELD, fg=T.TEXT, selectbackground=T.ACCENT, relief="flat",
                        highlightthickness=0, activestyle="none", exportselection=False, font=("Segoe UI", 10))
        lb.pack(fill="y", expand=True)
        right = ttk.Frame(d)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)
        ttk.Label(right, text="Название:").pack(anchor="w")
        nm = tk.StringVar()
        ttk.Entry(right, textvariable=nm).pack(fill="x")
        ttk.Label(right, text="Текст (команда целиком: /f, /r, /d ...):").pack(anchor="w", pady=(8, 0))
        tx = tk.Text(right, height=4, bg=T.FIELD, fg=T.TEXT, insertbackground=T.TEXT, relief="flat", wrap="word",
                     font=("Segoe UI", 10), padx=6, pady=4, highlightthickness=0)
        tx.pack(fill="x")
        prev = tk.StringVar()
        ttk.Label(right, textvariable=prev, style="Muted.TLabel", wraplength=520, justify="left").pack(anchor="w", pady=(4, 0))
        ph = ttk.Frame(right)
        ph.pack(fill="x", pady=(8, 0))
        ttk.Label(ph, text="Подстановки (щелчок - вставить):").grid(row=0, column=0, columnspan=3, sticky="w")
        for j, (p, desc) in enumerate(PLACEHOLDERS):
            b = ttk.Button(ph, text=p, width=9, command=lambda p=p: (tx.insert("insert", p), upd_prev()))
            b.grid(row=1 + j // 3, column=(j % 3) * 2, sticky="w", pady=1)
            ttk.Label(ph, text=desc, style="Muted.TLabel").grid(row=1 + j // 3, column=(j % 3) * 2 + 1, sticky="w", padx=(2, 10))
        sel = {"i": int(cfg["f_tpl"])}

        def sample():
            if st["snap"]:
                for uid, e in PT.sorted_emp(st["snap"]):
                    if e.get("status") in ("active", "away"):
                        return e, uid
            return {"name": "Daniel Trouble", "rank": 5, "dept": "PM", "status": "active", "calls": 0}, "12345"

        def upd_prev(*a):
            e, uid = sample()
            prev.set("Пример: " + render(tx.get("1.0", "end").strip(), e, uid, 1, cfg.get("f_me") or "Твой Ник"))
        tx.bind("<KeyRelease>", upd_prev)

        def load_list():
            lb.delete(0, "end")
            for j, t in enumerate(cfg["f_templates"]):
                lb.insert("end", ("★ " if j == int(cfg["f_tpl"]) else "   ") + t["name"])

        def show(j):
            if not 0 <= j < len(cfg["f_templates"]):
                return
            sel["i"] = j
            lb.selection_clear(0, "end")
            lb.selection_set(j)
            nm.set(cfg["f_templates"][j]["name"])
            tx.delete("1.0", "end")
            tx.insert("1.0", cfg["f_templates"][j]["text"])
            upd_prev()
        lb.bind("<<ListboxSelect>>", lambda e: lb.curselection() and show(lb.curselection()[0]))

        def store():
            j = sel["i"]
            if 0 <= j < len(cfg["f_templates"]):
                cfg["f_templates"][j] = {"name": nm.get().strip() or f"Шаблон {j + 1}", "text": tx.get("1.0", "end").strip()}

        def save_it():
            store()
            load_list()
            show(sel["i"])
            after_change()

        def add():
            store()
            cfg["f_templates"].append({"name": f"Шаблон {len(cfg['f_templates']) + 1}", "text": "/f {name}, "})
            load_list()
            show(len(cfg["f_templates"]) - 1)
            after_change()

        def delete():
            if len(cfg["f_templates"]) <= 1:
                return
            j = sel["i"]
            del cfg["f_templates"][j]
            for k in ("f_tpl", "f_tpl_away"):
                if int(cfg[k]) == j:
                    cfg[k] = 0 if k == "f_tpl" else -1
                elif int(cfg[k]) > j:
                    cfg[k] = int(cfg[k]) - 1
            load_list()
            show(max(0, j - 1))
            after_change()

        def make_main():
            store()
            cfg["f_tpl"] = sel["i"]
            load_list()
            show(sel["i"])
            after_change()

        def reset_defaults():
            if messagebox.askyesno("Шаблоны", "Вернуть шаблоны по умолчанию? Свои шаблоны пропадут.", parent=d):
                cfg["f_templates"] = [dict(t) for t in DEFAULT_TEMPLATES]
                cfg["f_tpl"], cfg["f_tpl_away"] = 0, -1
                load_list()
                show(0)
                after_change()
        bf = ttk.Frame(right)
        bf.pack(fill="x", side="bottom")
        ttk.Button(bf, text="Сохранить", style="Accent.TButton", command=save_it).pack(side="right")
        ttk.Button(bf, text="★ Основной", command=make_main).pack(side="right", padx=4)
        ttk.Button(bf, text="Удалить", command=delete).pack(side="right")
        ttk.Button(bf, text="+ Новый", command=add).pack(side="right", padx=4)
        ttk.Button(bf, text="По умолчанию", command=reset_defaults).pack(side="left")
        load_list()
        show(sel["i"])

    def after_change():
        st["override"] = {k: v for k, v in st["override"].items() if k.startswith("x:")}
        sync_tpl_boxes()
        save()
        rebuild()

    def who_changed():
        cfg["f_who"] = {"active": bool(who_a.get()), "away": bool(who_w.get())}
        save()
        rebuild()

    def tpl_changed(e=None):
        names = tpl_names()
        if tpl_var.get() in names:
            cfg["f_tpl"] = names.index(tpl_var.get())
        cfg["f_tpl_away"] = names.index(away_var.get()) if away_var.get() in names else -1
        st["override"] = {k: v for k, v in st["override"].items() if k.startswith("x:")}
        save()
        rebuild()
    tpl_cb.bind("<<ComboboxSelected>>", tpl_changed)
    away_cb.bind("<<ComboboxSelected>>", tpl_changed)

    def save():
        cfg["f_interval"] = interval()
        cfg["f_chat_key"] = key_var.get() if key_var.get() in CHAT_KEYS else "T"
        cfg["f_method"] = "type" if meth_var.get() == "печатью" else "paste"
        cfg["f_hotkey"] = next((k for k, v in HOTKEYS.items() if v == hk_var.get()), 0)
        save_cfg(cfg)
    for v in (int_var, key_var, meth_var, hk_var):
        v.trace_add("write", lambda *a: save())

    # ================================================================ таймер: прогон и горячая клавиша
    def tick():
        if not st["alive"]:
            return
        try:
            now = time.time()
            if st["run"] and not st["busy"]:
                left = st["next_t"] - now
                if left <= 0:
                    st["next_t"] = now + interval()
                    run_var.set("Отправляю…")
                    send_next(auto=True)
                else:
                    run_var.set(f"Прогон: следующее через {left:.0f} с")
            vk = cfg.get("f_hotkey") or 0
            if vk:
                down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not st["hk_prev"] and not st["busy"]:
                    send_next()
                st["hk_prev"] = down
            frame.after(100, tick)
        except tk.TclError:
            pass

    sync_tpl_boxes()
    redraw()
    frame.after(300, tick)

    class Api:
        pass
    api = Api()
    api.refresh = refresh
    api.copy_all = copy_all

    def stop():
        st["alive"] = False
    api.stop = stop
    return api
