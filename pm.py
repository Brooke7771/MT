# -*- coding: utf-8 -*-
"""Вкладка «PM»: таблица сотрудников по скриншотам планшета («Коллеги» и «Вызовы» → «История»).
Скрины: кнопкой, из буфера (Win+Shift+S → Ctrl+V) или из папки, за которой следим.
Распознавание - pm_ocr (OCR Windows, бесплатно, без интернета), снимки и сравнение - pm_track.
Игру не трогает: только читает готовые картинки."""
import os
import time
import json
import queue
import threading

import pm_ocr
import pm_track as PT

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
NEW_ROUND = 5 * 60          # скрин «Коллеги» позже прошлых на столько секунд - новая проверка
STATE = {"n": 0, "online": 0, "calls": 0, "ask": 0, "changes": 0, "busy": "", "shots": 0, "ok": True}


def _cfg_path():
    return os.path.join(PT.data_dir(), "pm_config.json")


def load_cfg():
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(c):
    try:
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def build_pm_tab(parent):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import theme as T
    import widgets

    cfg = load_cfg()
    q = queue.Queue()
    work = queue.Queue()
    st = {"alive": True, "shots": [], "snap": None, "old": None, "old_label": "", "watch_seen": set(),
          "watch_t": 0.0, "edits": {}, "marks": PT.load_marks(), "base": PT.load_roster()}
    st["roster"] = st["base"]
    ok, ocr_msg = pm_ocr.available()
    STATE["ok"] = ok

    # ================================================================ верхняя панель
    bar = ttk.Frame(parent)
    bar.pack(fill="x", padx=8, pady=(0, 4))
    ttk.Button(bar, text="Добавить скрины…", style="Accent.TButton", command=lambda: add_files()).pack(side="left")
    ttk.Button(bar, text="Вставить из буфера (Ctrl+V)", command=lambda: paste()).pack(side="left", padx=4)
    ttk.Button(bar, text="Очистить", command=lambda: clear()).pack(side="left", padx=4)
    ttk.Button(bar, text="Новый порядок", command=lambda: new_order()).pack(side="left", padx=4)
    watch_var = tk.BooleanVar(value=bool(cfg.get("watch_on")))
    folder = {"path": cfg.get("watch_dir", "")}
    ttk.Checkbutton(bar, text="Следить за папкой:", variable=watch_var, command=lambda: watch_toggle()).pack(
        side="left", padx=(16, 4))
    folder_lbl = ttk.Label(bar, text=folder["path"] or "не выбрана", style="Muted.TLabel")
    folder_lbl.pack(side="left")
    ttk.Button(bar, text="Выбрать…", command=lambda: pick_folder()).pack(side="left", padx=4)
    msg = tk.StringVar(value=("Добавь скрины «Коллеги», «Вызовы» (История) и группы в телефоне. Можно несколько: они склеятся."
                              if ok else "⚠ " + ocr_msg))
    ttk.Label(parent, textvariable=msg, style="Muted.TLabel" if ok else "Warn.TLabel").pack(anchor="w", padx=10)

    body = ttk.Frame(parent)
    body.pack(fill="both", expand=True, padx=8, pady=4)
    # ---- слева: скрины
    left = ttk.LabelFrame(body, text="Скрины")
    left.pack(side="left", fill="y", padx=(0, 6))
    lb = tk.Listbox(left, width=34, bg=T.FIELD, fg=T.TEXT, selectbackground=T.ACCENT, relief="flat",
                    highlightthickness=0, activestyle="none", font=("Segoe UI", 9))
    lb.pack(fill="both", expand=True, padx=6, pady=6)
    ttk.Button(left, text="Убрать выбранный", command=lambda: remove_shot()).pack(fill="x", padx=6, pady=(0, 6))

    # ---- справа: результат
    nb = ttk.Notebook(body)
    nb.pack(side="left", fill="both", expand=True)
    tabs = {}
    for k_, t_ in (("emp", "Сотрудники"), ("calls", "Вызовы"), ("diff", "Изменения"), ("f", "Тексты /f")):
        f = ttk.Frame(nb)
        nb.add(f, text=t_)
        tabs[k_] = f

    cols = ("num", "name", "uid", "rank", "status", "calls", "group", "brief", "comment")
    et = ttk.Treeview(tabs["emp"], columns=cols, show="headings", selectmode="browse")
    for c, h, w, a in (("num", "№", 44, "e"), ("name", "Ник", 190, "w"), ("uid", "Статик", 76, "e"),
                       ("rank", "Ранг", 50, "e"), ("status", "Статус", 180, "w"), ("calls", "Вызовы", 86, "w"),
                       ("group", "Группа", 72, "center"), ("brief", "Брифинг", 76, "center"),
                       ("comment", "Комментарий", 200, "w")):
        et.heading(c, text=h)
        et.column(c, width=w, anchor=a)
    esb = ttk.Scrollbar(tabs["emp"], orient="vertical", command=et.yview)
    et.configure(yscrollcommand=esb.set)
    et.pack(side="left", fill="both", expand=True, pady=4)
    esb.pack(side="right", fill="y", pady=4)
    et.tag_configure("odd", background=T.FIELD_ALT)
    et.tag_configure("doubt", foreground="#ffb347")
    et.tag_configure("off", foreground=T.MUTED)
    et.tag_configure("calls", foreground=T.OK)
    et.tag_configure("left", foreground="#e06c6c")

    ct = ttk.Treeview(tabs["calls"], columns=("emp", "uid", "cit", "reason", "status", "date"), show="headings")
    for c, h, w, a in (("emp", "Сотрудник", 200, "w"), ("uid", "UID", 80, "e"), ("cit", "Гражданин", 90, "e"),
                       ("reason", "Причина", 120, "w"), ("status", "Статус", 110, "w"), ("date", "Дата", 140, "w")):
        ct.heading(c, text=h)
        ct.column(c, width=w, anchor=a)
    ct.tag_configure("odd", background=T.FIELD_ALT)
    ct.tag_configure("pm", foreground=T.OK)
    ct.pack(fill="both", expand=True, pady=4)

    def text_box(parent_):
        t = tk.Text(parent_, bg=T.FIELD, fg=T.TEXT, insertbackground=T.TEXT, relief="flat", wrap="word",
                    font=("Segoe UI", 10), padx=10, pady=8, highlightthickness=0)
        t.pack(fill="both", expand=True, pady=4)
        return t
    diff_txt = text_box(tabs["diff"])
    f_txt = text_box(tabs["f"])

    # ---- низ: действия
    bot = ttk.Frame(parent)
    bot.pack(fill="x", padx=8, pady=(2, 8))
    win_var = tk.StringVar(value=str(cfg.get("window", 15)))
    ttk.Label(bot, text="Катает - вызов за последние").pack(side="left")
    ttk.Entry(bot, textvariable=win_var, width=4).pack(side="left", padx=4)
    ttk.Label(bot, text="мин. (0 - все на скринах)").pack(side="left")
    for text, cmd in (("Копировать таблицу", lambda: copy(PT.table_text(st["snap"]) if st["snap"] else "")),
                      ("Копировать изменения", lambda: copy(diff_txt.get("1.0", "end").strip())),
                      ("Копировать /f", lambda: copy(f_txt.get("1.0", "end").strip()))):
        ttk.Button(bot, text=text, command=cmd).pack(side="left", padx=(10, 0))
    ttk.Button(bot, text="Сохранить снимок", style="Accent.TButton", command=lambda: save_snap()).pack(side="right")
    ttk.Button(bot, text="Сбросить брифинг", command=lambda: reset_brief()).pack(side="right", padx=(0, 6))
    base_var = tk.StringVar()
    base_cb = ttk.Combobox(bot, textvariable=base_var, width=22, state="readonly")
    base_cb.pack(side="right", padx=6)
    ttk.Label(bot, text="Сравнить со снимком:").pack(side="right")

    # ================================================================ скрины
    def add_paths(paths):
        for p in paths:
            if not p.lower().endswith(IMG_EXT) or any(s["path"] == p for s in st["shots"]):
                continue
            s = {"path": p, "name": os.path.basename(p), "res": None, "state": "в очереди", "t": time.time()}
            st["shots"].append(s)
            work.put(s)
        shots_refresh()

    def add_files():
        ps = filedialog.askopenfilenames(parent=parent, title="Скрины планшета",
                                         filetypes=[("Картинки", "*.png *.jpg *.jpeg *.bmp")],
                                         initialdir=folder["path"] or None)
        add_paths(list(ps))

    def paste(e=None):
        try:
            from PIL import ImageGrab
            got = ImageGrab.grabclipboard()
        except Exception as ex:
            msg.set(f"Буфер: {ex}")
            return
        if isinstance(got, list):
            add_paths(got)
        elif got is not None:
            d = os.path.join(PT.data_dir(), "pm_clipboard")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, time.strftime("%Y%m%d_%H%M%S") + f"_{len(st['shots'])}.png")
            got.save(p)
            add_paths([p])
        else:
            msg.set("В буфере нет картинки: сделай скрин (Win+Shift+S) и нажми ещё раз")
    parent.winfo_toplevel().bind("<Control-v>", lambda e: paste() if parent.winfo_ismapped() else None, add="+")
    parent.winfo_toplevel().bind("<Control-V>", lambda e: paste() if parent.winfo_ismapped() else None, add="+")

    def clear():
        st["shots"].clear()
        st["edits"].clear()
        st["base"] = st["roster"]                                      # следующая проверка - в том же порядке
        shots_refresh()
        rebuild()

    def new_order():
        if not st["roster"].get("order"):
            return
        if messagebox.askyesno("Порядок списка", "Забыть порядок прошлых проверок? Таблица выстроится заново "
                               "по текущим скринам, «Вышел» у пропавших исчезнет.", parent=parent):
            PT.reset_roster()
            st["base"] = st["roster"] = {"order": [], "emp": {}}
            rebuild()

    def new_round(s):
        """Пришёл скрин «Коллеги» через NEW_ROUND после прошлых - это новая проверка: старые скрины убираем
        (порядок строк остаётся от прошлой)."""
        prev = [x for x in st["shots"] if x is not s and x["res"] and x["res"].get("kind") == "colleagues"]
        if s["res"].get("kind") != "colleagues" or not prev or s["t"] - max(x["t"] for x in prev) < NEW_ROUND:
            return
        st["shots"][:] = [x for x in st["shots"] if x["t"] > s["t"] - NEW_ROUND]
        st["edits"].clear()
        st["base"] = st["roster"]

    def remove_shot():
        s = lb.curselection()
        if s:
            del st["shots"][s[0]]
            shots_refresh()
            rebuild()

    KIND = {"colleagues": "Коллеги", "calls": "Вызовы", "group": "Группа"}

    def shots_refresh():
        lb.delete(0, "end")
        for s in st["shots"]:
            r = s["res"]
            if r is None:
                lab = f"⏳ {s['name']}"
            elif r.get("kind"):
                lab = f"✓ {KIND[r['kind']]}: {len(r['rows'])} стр. · {s['name']}"
            else:
                lab = f"⚠ {s['name']}: {r.get('error', '')}"
            lb.insert("end", lab)
            if r is not None and not r.get("kind"):
                lb.itemconfigure("end", foreground="#ffb347")
        STATE["shots"] = len(st["shots"])

    # ---- папка
    def pick_folder():
        d = filedialog.askdirectory(parent=parent, title="Папка, куда сохраняются скрины", initialdir=folder["path"] or None)
        if d:
            folder["path"] = d
            folder_lbl.configure(text=d)
            cfg["watch_dir"] = d
            save_cfg(cfg)
            if watch_var.get():
                watch_toggle()

    def watch_toggle():
        cfg["watch_on"] = bool(watch_var.get())
        save_cfg(cfg)
        if watch_var.get():
            if not folder["path"]:
                pick_folder()
                if not folder["path"]:
                    watch_var.set(False)
                    return
            st["watch_seen"] = set(_list_imgs(folder["path"]))       # старые файлы не берём
            st["watch_t"] = time.time()
            msg.set(f"Слежу за папкой: новые скрины добавятся сами")

    def _list_imgs(d):
        try:
            return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(IMG_EXT)]
        except OSError:
            return []

    def watch_tick():
        if watch_var.get() and folder["path"]:
            new = []
            for p in _list_imgs(folder["path"]):
                try:
                    if p not in st["watch_seen"] and time.time() - os.path.getmtime(p) > 1.0:   # дописан
                        new.append(p)
                except OSError:
                    pass
            if new:
                st["watch_seen"].update(new)
                add_paths(sorted(new, key=os.path.getmtime))

    # ================================================================ распознавание (фоновый поток)
    def worker():
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        while st["alive"]:
            try:
                s = work.get(timeout=0.5)
            except queue.Empty:
                continue
            if s is None:
                break
            q.put(("busy", f"Распознаю {s['name']}…"))
            try:
                s["res"] = pm_ocr.recognize(s["path"])
            except Exception as ex:
                s["res"] = {"kind": None, "rows": [], "error": f"ошибка: {ex}"}
            q.put(("done", s))
    if ok:
        threading.Thread(target=worker, daemon=True).start()

    # ================================================================ результат
    def window():
        try:
            return max(0, int(win_var.get()))
        except ValueError:
            return 15

    def rebuild():
        results = [s["res"] for s in st["shots"] if s["res"] and s["res"].get("kind")]
        if not results:
            st["snap"] = None
            for w in (et, ct):
                w.delete(*w.get_children())
            diff_txt.delete("1.0", "end")
            f_txt.delete("1.0", "end")
            STATE.update(n=0, online=0, calls=0, ask=0, changes=0)
            return
        snap = PT.merge(results, window())
        PT.apply_roster(snap, st["base"])                              # порядок прошлой проверки, пропавшие - «Вышел»
        for uid, ed in st["edits"].items():                          # ручные правки поверх распознанного
            if uid in snap["employees"]:
                snap["employees"][uid].update(ed)
                snap["employees"][uid]["doubt"] = []
        if snap["employees"]:
            st["roster"] = PT.roster_of(snap)
            try:
                PT.save_roster(st["roster"])
            except OSError as ex:
                msg.set(f"Порядок не сохранился: {ex}")
        apply_marks(snap)
        st["snap"] = snap
        fill(snap)

    def fill(snap):
        sel = et.selection()
        et.delete(*et.get_children())
        for i, (uid, e) in enumerate(PT.sorted_emp(snap)):
            tags = ["odd"] if i % 2 else []
            if e.get("doubt"):
                tags.append("doubt")
            elif e["status"] == "left":
                tags.append("left")
            elif e["status"] == "offline":
                tags.append("off")
            elif e.get("on_calls"):
                tags.append("calls")
            et.insert("", "end", iid=uid, tags=tags, values=(
                i + 1, e["name"] + ("  ⚠" if e.get("doubt") else ""), uid, e.get("rank") or "",
                PT.status_text(e), f"катает ({e['calls']})" if e.get("on_calls") else "",
                ("✓" if e.get("in_group") else "—") if snap.get("has_group") else "",
                {"yes": "✓ есть", "no": "✗ нет"}.get(e.get("brief") or "", "·"), e.get("comment") or ""))
        if sel and et.exists(sel[0]):
            et.selection_set(sel[0])
        ct.delete(*ct.get_children())
        for i, c in enumerate(snap["calls"]):
            tags = ["odd"] if i % 2 else []
            if c["uid"] and c["uid"] in snap["employees"]:
                tags.append("pm")
            ct.insert("", "end", tags=tags, values=(c["employee"] or "-", c["uid"] or "-", c["citizen"] or "",
                                                    c["reason"], c["status"], c["date"]))
        diff_txt.delete("1.0", "end")
        diff_txt.insert("1.0", PT.changes_text(snap, st["old"], st["old_label"]))
        f_txt.delete("1.0", "end")
        f_txt.insert("1.0", PT.f_text(snap))
        emps = snap["employees"].values()
        online = [e for e in emps if e["status"] in pm_ocr.ONLINE]
        STATE.update(n=len(snap["employees"]), online=len(online), calls=sum(1 for e in online if e.get("on_calls")),
                     ask=sum(1 for e in emps if e["status"] in pm_ocr.ASK and not e.get("on_calls")),
                     changes=len(PT.diff(st["old"], snap)) if st["old"] else 0)
        info = [f"Сотрудников: {len(snap['employees'])}", f"в сети: {len(online)}"]
        if snap["has_calls"]:
            info.append(f"вызовов на скринах: {len(snap['calls'])} (самый свежий {snap['calls_newest']})")
        else:
            info.append("скринов «Вызовы» нет")
        if snap.get("has_group"):
            n_in = sum(1 for e in emps if e.get("in_group"))
            info.append(f"в группе: {len(snap['group'])}" + (f" из {snap['group_count']}" if snap.get("group_count") else "")
                        + f", из них в списке PM: {n_in}")
        if snap.get("roster_gone"):
            info.append(f"вышли (нет на скринах): {len(snap['roster_gone'])}")
        if snap.get("roster_new"):
            info.append(f"новых внизу: {len(snap['roster_new'])}")
        if snap["unknown"]:
            info.append(f"строк без статика: {len(snap['unknown'])}")
        if any(e.get("doubt") for e in emps):
            info.append("⚠ жёлтые - проверь (двойной щелчок - исправить)")
        msg.set("  ·  ".join(info))

    # ---- правка вручную
    def edit(e=None):
        uid = (et.selection() or [None])[0]
        if not uid or not st["snap"]:
            return
        emp = st["snap"]["employees"][uid]
        dlg = tk.Toplevel(parent)
        dlg.title("Исправить")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.grab_set()
        v_name, v_rank = tk.StringVar(value=emp["name"]), tk.StringVar(value=str(emp.get("rank") or ""))
        names = [pm_ocr.STATUS_NAME[k] for k in ("active", "away", "arena", "jail", "offline")]
        v_st = tk.StringVar(value=pm_ocr.STATUS_NAME.get(emp["status"], names[0]))
        v_calls = tk.BooleanVar(value=bool(emp.get("on_calls")))
        pad = {"padx": 12, "pady": 5}
        ttk.Label(dlg, text=f"Статик {uid}", style="Big.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(dlg, text="Ник:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(dlg, textvariable=v_name, width=28).grid(row=1, column=1, **pad)
        ttk.Label(dlg, text="Ранг:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(dlg, textvariable=v_rank, width=6).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(dlg, text="Статус:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Combobox(dlg, textvariable=v_st, values=names, state="readonly", width=20).grid(row=3, column=1, sticky="w", **pad)
        ttk.Checkbutton(dlg, text="Катает вызовы", variable=v_calls).grid(row=4, column=1, sticky="w", **pad)

        def ok_():
            code = next(k for k, v in pm_ocr.STATUS_NAME.items() if v == v_st.get())
            ed = {"name": v_name.get().strip(), "status": code, "on_calls": bool(v_calls.get())}
            if v_rank.get().strip().isdigit():
                ed["rank"] = int(v_rank.get())
            st["edits"][uid] = ed
            dlg.destroy()
            rebuild()
        ttk.Button(dlg, text="Сохранить", style="Accent.TButton", command=ok_).grid(row=5, column=0, columnspan=2,
                                                                                   sticky="ew", padx=12, pady=10)
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass
    # ---- брифинг (щелчок по ячейке) и комментарий (двойной щелчок)
    def apply_marks(snap):
        for uid, e in snap["employees"].items():
            m = st["marks"].get(uid) or {}
            e["brief"], e["comment"] = m.get("brief", ""), m.get("comment", "")

    def set_mark(uid, **kw):
        st["marks"].setdefault(uid, {}).update(kw)
        try:
            PT.save_marks(st["marks"])
        except OSError as ex:
            msg.set(f"Не сохранилось: {ex}")
        if st["snap"]:
            apply_marks(st["snap"])
            fill(st["snap"])

    def cell_of(e):
        uid = et.identify_row(e.y)
        col = et.identify_column(e.x)
        if not uid or not col:
            return None, None
        return uid, cols[int(col[1:]) - 1]

    def on_click(e):
        uid, col = cell_of(e)
        if col == "brief" and uid:
            cur = (st["marks"].get(uid) or {}).get("brief", "")
            set_mark(uid, brief={"": "yes", "yes": "no", "no": ""}[cur])
        elif col == "comment" and uid:
            et.after(1, lambda: edit_comment(uid))

    def on_double(e):
        uid, col = cell_of(e)
        if col == "brief":
            on_click(e)                                               # быстрый второй щелчок - тоже переключение
            return "break"
        if col == "comment":
            return "break"
        edit()

    def edit_comment(uid):
        if not et.exists(uid) or not et.bbox(uid, "comment"):
            return
        for old in [w for w in et.winfo_children() if isinstance(w, tk.Entry)]:
            old.destroy()
        x, y, w, h = et.bbox(uid, "comment")
        var = tk.StringVar(value=(st["marks"].get(uid) or {}).get("comment", ""))
        ent = tk.Entry(et, textvariable=var, bg=T.FIELD, fg=T.TEXT, insertbackground=T.TEXT, relief="flat",
                       font=("Segoe UI", 9), highlightthickness=1, highlightbackground=T.ACCENT)
        ent.place(x=x, y=y, width=max(w, 220), height=h)
        ent.focus_set()
        ent.icursor("end")

        def done(save=True):
            if save:
                set_mark(uid, comment=var.get().strip())
            ent.destroy()
        ent.bind("<Return>", lambda e: done())
        ent.bind("<Escape>", lambda e: done(False))
        ent.bind("<FocusOut>", lambda e: done())

    def reset_brief():
        if not any(m.get("brief") for m in st["marks"].values()):
            return
        if messagebox.askyesno("Брифинг", "Сбросить отметки брифинга у всех? Комментарии останутся.", parent=parent):
            for m in st["marks"].values():
                m["brief"] = ""
            set_mark("", brief="")

    et.bind("<Button-1>", on_click, add="+")
    et.bind("<Double-1>", on_double)
    et.bind("<Return>", edit)

    # ---- снимки
    def bases_refresh(select_latest=True):
        lst = PT.snapshots()
        st["bases"] = lst
        base_cb.configure(values=[lab for _, lab in lst] or ["нет снимков"])
        if select_latest:
            if lst:
                base_var.set(lst[0][1])
                st["old"], st["old_label"] = PT.load(lst[0][0]), lst[0][1]
            else:
                base_var.set("нет снимков")
                st["old"], st["old_label"] = None, ""

    def base_pick(e=None):
        for p, lab in st.get("bases", []):
            if lab == base_var.get():
                st["old"], st["old_label"] = PT.load(p), lab
        if st["snap"]:
            fill(st["snap"])
    base_cb.bind("<<ComboboxSelected>>", base_pick)

    def save_snap():
        if not st["snap"]:
            messagebox.showinfo("Снимок", "Сначала добавь скрины", parent=parent)
            return
        PT.save(st["snap"])
        bases_refresh()
        msg.set("Снимок сохранён: следующая проверка сравнится с ним")
        try:
            import stats
            stats.event(f"PM: снимок сохранён ({len(st['snap']['employees'])} сотрудников)", "ok")
        except Exception:
            pass
        fill(st["snap"])

    def copy(text):
        if not text:
            msg.set("Нечего копировать")
            return
        parent.clipboard_clear()
        parent.clipboard_append(text)
        msg.set("Скопировано в буфер обмена ✓")

    win_var.trace_add("write", lambda *a: (cfg.update(window=window()), save_cfg(cfg), rebuild()))

    # ================================================================ обмен с потоком
    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "busy":
                    STATE["busy"] = m[1]
                    msg.set(m[1])
                elif m[0] == "done":
                    STATE["busy"] = ""
                    if m[1] in st["shots"]:
                        new_round(m[1])
                    shots_refresh()
                    rebuild()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if st["alive"]:
            st["tick"] = st.get("tick", 0) + 1
            if st["tick"] % 10 == 0:
                watch_tick()
            parent.after(200, pump)

    def provider():
        s = dict(STATE)
        return {"active": False, "label": (s["busy"] and "Распознаю…" or ("Готов" if s["ok"] else "Нет OCR"),
                                           "ok" if s["ok"] else "warn"),
                "status": s["busy"] or "PM", "pm": s,
                "items": [("Сотрудников", s["n"])]}
    widgets.register("pm", provider)

    def stop():
        st["alive"] = False
        work.put(None)

    bases_refresh()
    if watch_var.get() and folder["path"]:
        watch_toggle()
    parent.after(200, pump)
    return stop
