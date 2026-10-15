# -*- coding: utf-8 -*-
"""Окно ввода ключа (в разделе ОЗУ) и админка (по мастер-ключу)."""
import time
import queue
import threading

import license as L


_master = {"token": ""}


def master_token():
    return _master["token"]


def enable_clipboard(entry):
    """Копировать/вставить в поле на любой раскладке (Ctrl+V по коду клавиши, не по букве)
    и меню по правому клику. Без этого на русской/украинской раскладке Tk не вставляет."""
    import tkinter as tk

    def do(ev_name):
        entry.event_generate(ev_name)
        return "break"

    def on_ctrl(e):
        code = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}.get(e.keycode)
        if code:
            return do(code)
        if e.keycode == 65:
            entry.select_range(0, "end")
            entry.icursor("end")
            return "break"

    entry.bind("<Control-KeyPress>", on_ctrl)
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Вставить", command=lambda: entry.event_generate("<<Paste>>"))
    menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Вырезать", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Выделить всё", command=lambda: (entry.select_range(0, "end"), entry.icursor("end")))
    entry.bind("<Button-3>", lambda e: (entry.focus_set(), menu.tk_popup(e.x_root, e.y_root)))


# ================================================================ панель ввода ключа (вставляется в раздел ОЗУ)
def build_key_bar(parent, on_change):
    import tkinter as tk
    from tkinter import ttk
    import theme as T

    box = ttk.LabelFrame(parent, text="Доступ по ключу")
    q = queue.Queue()
    cur = L.load_key()
    res = L.check() if cur else {"ok": False, "reason": "no_key"}

    state_var = tk.StringVar()
    key_var = tk.StringVar(value=cur)

    def describe(r):
        if r.get("ok"):
            acc = ", ".join(L.ACCESS_NAMES.get(a, a) for a in r.get("accesses", [])) or "нет разделов"
            name = r.get("name") or "ключ"
            exp = r.get("expires")
            extra = ""
            if exp:
                extra = f", до {exp[:10]}"
            if r.get("offline"):
                extra += " (нет связи, доступ по памяти)"
            return f"Ключ «{name}» активен: {acc}{extra}"
        reason = {"no_key": "Ключ не введён. Открыты очистка ОЗУ и калькулятор готовки.",
                  "not_found": "Такого ключа нет.",
                  "revoked": "Ключ отозван.",
                  "expired": "Срок ключа истёк.",
                  "device_limit": "Ключ уже используется на другом компьютере.",
                  "offline": "Нет связи с сервером. Проверь интернет.",
                  }.get(r.get("reason"), "Ключ недействителен.")
        return reason

    state_var.set(describe(res))
    ttk.Label(box, textvariable=state_var, wraplength=520, justify="left").grid(
        row=0, column=0, sticky="w", padx=6, pady=4)
    ttk.Button(box, text="Ввести ключ…", command=lambda: open_dialog()).grid(
        row=1, column=0, sticky="w", padx=6, pady=(0, 6))

    def apply_key():
        key_var.set(key_var.get().strip())
        state_var.set("Проверяю ключ...")

        def work():
            L.save_key(key_var.get())
            r = L.check(key_var.get())
            q.put(r)
        threading.Thread(target=work, daemon=True).start()

    def remove_key():
        from tkinter import messagebox
        if not messagebox.askyesno(
                "Выгрузить ключ",
                "Ключ будет удалён с этого компьютера, закрытые разделы отключатся, а их настройки "
                "и записи будут стёрты.\n\nПродолжить?", parent=box):
            return
        L.clear_key()
        L.request_wipe()
        key_var.set("")
        r = {"ok": False, "reason": "no_key"}
        state_var.set(describe(r))
        on_change()
        L.notify()

    def open_dialog():
        """Ввод ключа спрятан в отдельное окно: в основном разделе виден только статус."""
        dlg = tk.Toplevel(box)
        dlg.title("Ключ")
        dlg.transient(box.winfo_toplevel())
        dlg.resizable(False, False)
        try:
            dlg.configure(bg=T.BG)
        except Exception:
            pass
        f = ttk.Frame(dlg)
        f.pack(padx=14, pady=12)
        ttk.Label(f, textvariable=state_var, wraplength=380, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(f, text="Код этого компьютера:").grid(row=1, column=0, sticky="w")
        hv = tk.StringVar(value=L.hwid_short())
        ttk.Entry(f, textvariable=hv, width=24, state="readonly").grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(f, text="Ключ:").grid(row=2, column=0, sticky="w")
        ke = ttk.Entry(f, textvariable=key_var, width=30)
        ke.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        enable_clipboard(ke)
        ke.focus_set()

        def paste_key():
            try:
                key_var.set(ke.clipboard_get().strip())
            except tk.TclError:
                pass
        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Вставить", command=paste_key).pack(side="left", padx=2)
        ttk.Button(btns, text="Применить", style="Accent.TButton", command=apply_key).pack(side="left", padx=2)
        ttk.Button(btns, text="Выгрузить ключ", command=remove_key).pack(side="left", padx=2)
        ke.bind("<Return>", lambda e: apply_key())
    def pump():
        try:
            r = q.get_nowait()
            state_var.set(describe(r))
            on_change()
            L.notify()
        except queue.Empty:
            pass
        try:
            box.after(300, pump)
        except Exception:
            pass
    box.after(300, pump)
    return box


# ================================================================ админка
def build_admin_tab(parent, refresh_access):
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    import theme as T

    q = queue.Queue()
    pad = {"padx": 6, "pady": 3}
    parent.columnconfigure(0, weight=1)

    # --- вход по мастер-ключу
    login = ttk.Frame(parent)
    login.grid(row=0, column=0, sticky="ew", **pad)
    ttk.Label(login, text="Мастер-ключ:").pack(side="left")
    mv = tk.StringVar(value=_master["token"])
    me = ttk.Entry(login, textvariable=mv, width=40, show="•")
    me.pack(side="left", padx=6)
    enable_clipboard(me)
    login_status = tk.StringVar(value="")
    ttk.Label(login, textvariable=login_status, style="Muted.TLabel").pack(side="left", padx=6)

    body = ttk.Frame(parent)
    body.grid(row=1, column=0, sticky="nsew", **pad)
    parent.rowconfigure(1, weight=1)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    cols = ("name", "key", "acc", "status", "dev", "exp")
    tv = ttk.Treeview(body, columns=cols, show="headings", height=12, selectmode="extended")
    for c, h, w in (("name", "Название", 150), ("key", "Ключ", 170), ("acc", "Доступы", 160),
                    ("status", "Статус", 80), ("dev", "Устройств", 80), ("exp", "До", 100)):
        tv.heading(c, text=h)
        tv.column(c, width=w, anchor="w")
    tv.grid(row=0, column=0, sticky="nsew")
    tv.tag_configure("odd", background=T.FIELD_ALT)
    tv.tag_configure("revoked", foreground=T.MUTED)
    tv.tag_configure("expired", foreground=T.WARN)

    bar = ttk.Frame(parent)
    bar.grid(row=2, column=0, sticky="ew", **pad)
    for text, cmd in (("Создать ключ", lambda: create()), ("Доступы", lambda: edit_access()),
                      ("Продлить", lambda: prolong()), ("Отозвать / вернуть", lambda: toggle_revoke()),
                      ("Отвязать железо", lambda: unbind()), ("Переименовать", lambda: rename()),
                      ("Удалить", lambda: remove()), ("Обновить", lambda: reload())):
        ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
    ttk.Button(bar, text="Массово…", style="Accent.TButton", command=lambda: bulk_dialog()).pack(side="left", padx=(10, 2))
    bar2 = ttk.Frame(parent)
    bar2.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
    ttk.Label(bar2, text="Выбрать:", style="Muted.TLabel").pack(side="left", padx=(0, 4))
    for text, pred in (("все", lambda k: True), ("активные", lambda k: k["status"] == "active"),
                       ("истёкшие", lambda k: k["status"] == "expired"), ("отозванные", lambda k: k["status"] == "revoked"),
                       ("без устройств", lambda k: not k["devices"])):
        ttk.Button(bar2, text=text, command=lambda pr=pred: sel_where(pr)).pack(side="left", padx=2)
    ttk.Button(bar2, text="снять выбор", command=lambda: tv.selection_set(())).pack(side="left", padx=2)
    ttk.Label(bar2, text="  (Ctrl/Shift + клик - несколько ключей)", style="Muted.TLabel").pack(side="left")

    keys_cache = {}
    try:
        import widgets

        def admin_stats():
            ks = list(keys_cache.values())
            by = {"active": 0, "revoked": 0, "expired": 0}
            used = cap = 0
            soon = []
            now = time.time()
            for k in ks:
                by[k.get("status", "active")] = by.get(k.get("status", "active"), 0) + 1
                used += len(k.get("devices", []))
                cap += int(k.get("max_devices", 1) or 1)
                if k.get("status") == "active" and k.get("expires"):
                    try:
                        left = (time.mktime(time.strptime(k["expires"][:10], "%Y-%m-%d")) - now) / 86400.0
                        if left <= 7:
                            soon.append((k.get("name") or k.get("key", "")[:8], max(0, int(left))))
                    except Exception:
                        pass
            soon.sort(key=lambda x: x[1])
            return {"active": False, "label": (f"Ключей: {len(ks)}", "ok"), "status": "Админка",
                    "n_total": len(ks), "by": by, "dev_used": used, "dev_cap": cap, "soon": soon[:3],
                    "items": [("Активных", by["active"])]}
        widgets.register("admin", admin_stats)
    except Exception:
        pass

    def token():
        return mv.get().strip()

    def selected():
        """Один выбранный ключ (для действий над одним ключом). Несколько - нельзя: есть «Массово…»."""
        s = tv.selection()
        if len(s) > 1:
            login_status.set("Выбрано несколько ключей: для них есть кнопка «Массово…»")
            return None
        return keys_cache.get(s[0]) if s else None

    def sel_where(pred):
        ids = [i for i, k in keys_cache.items() if pred(k)]
        tv.selection_set(ids)
        login_status.set(f"Выбрано ключей: {len(ids)}")

    tv.bind("<<TreeviewSelect>>", lambda e: login_status.set(f"Выбрано ключей: {len(tv.selection())}") if len(tv.selection()) > 1 else None)
    tv.bind("<Control-a>", lambda e: (tv.selection_set(tv.get_children()), "break")[1])

    def run(fn, done=None):
        def work():
            try:
                r = fn()
                q.put(("ok", done, r))
            except Exception as ex:
                q.put(("err", None, ex))
        threading.Thread(target=work, daemon=True).start()

    def reload():
        t = token()
        if not L.is_master(t):
            login_status.set("Этот мастер-ключ не прописан в программе")
            return
        _master["token"] = t
        login_status.set("Загружаю...")
        run(lambda: L.admin_get("keys", t), fill)

    def fill(data):
        login_status.set(f"Ключей: {len(data.get('keys', []))}")
        tv.delete(*tv.get_children())
        keys_cache.clear()
        for n, k in enumerate(data.get("keys", [])):
            acc = ", ".join(L.ACCESS_NAMES.get(a, a) for a in k["accesses"]) or "—"
            exp = k["expires"][:10] if k["expires"] else "бессрочно"
            tags = ["odd"] if n % 2 else []
            if k["status"] != "active":
                tags.append(k["status"])
            iid = k["key"]
            keys_cache[iid] = k
            tv.insert("", "end", iid=iid, tags=tags, values=(
                k["name"] or "—", k["key"], acc, {"active": "активен", "revoked": "отозван",
                "expired": "истёк"}[k["status"]], f"{len(k['devices'])}/{k['max_devices']}", exp))
        refresh_access()

    # ------------------------------------------------ окно создания/редактирования доступов
    def access_dialog(title, init_acc, init_name="", init_days="", init_dev="1", with_name=True):
        dlg = tk.Toplevel(parent)
        dlg.title(title)
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.grab_set()
        res = {}
        r = 0
        name_v = tk.StringVar(value=init_name)
        if with_name:
            ttk.Label(dlg, text="Название:").grid(row=r, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(dlg, textvariable=name_v, width=28).grid(row=r, column=1, columnspan=2, sticky="w", padx=10)
            r += 1
        ttk.Label(dlg, text="Доступы:").grid(row=r, column=0, sticky="nw", padx=10, pady=6)
        acc_vars = {}
        fr = ttk.Frame(dlg)
        fr.grid(row=r, column=1, columnspan=2, sticky="w", padx=10)
        for a in L.LOCKED:
            v = tk.BooleanVar(value=a in init_acc)
            acc_vars[a] = v
            ttk.Checkbutton(fr, text=L.ACCESS_NAMES[a], variable=v).pack(anchor="w")
        r += 1
        days_v = tk.StringVar(value=str(init_days))
        dev_v = tk.StringVar(value=str(init_dev))
        if with_name:
            ttk.Label(dlg, text="Срок, дней (пусто = бессрочно):").grid(row=r, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(dlg, textvariable=days_v, width=8).grid(row=r, column=1, sticky="w", padx=10)
            r += 1
            ttk.Label(dlg, text="Компьютеров на ключ:").grid(row=r, column=0, sticky="w", padx=10, pady=6)
            ttk.Spinbox(dlg, from_=1, to=20, width=6, textvariable=dev_v).grid(row=r, column=1, sticky="w", padx=10)
            r += 1

        def ok():
            res["accesses"] = [a for a, v in acc_vars.items() if v.get()]
            res["name"] = name_v.get().strip()
            res["days"] = days_v.get().strip()
            res["max_devices"] = dev_v.get().strip()
            dlg.destroy()
        ttk.Button(dlg, text="Готово", style="Accent.TButton", command=ok).grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=10)
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass
        parent.wait_window(dlg)
        return res or None

    def create():
        if not _master["token"]:
            reload()
            return
        d = access_dialog("Создать ключ", [])
        if not d:
            return
        days = int(d["days"]) if d["days"].isdigit() else None
        payload = {"name": d["name"], "accesses": d["accesses"],
                   "max_devices": int(d["max_devices"] or 1), "days": days}
        run(lambda: L.admin("create", _master["token"], payload), on_created)

    def on_created(r):
        key = r.get("key", "")
        parent.clipboard_clear()
        parent.clipboard_append(key)
        messagebox.showinfo("Ключ создан", f"Ключ:\n\n{key}\n\nСкопирован в буфер обмена. Отдай его человеку.",
                            parent=parent)
        reload()

    def edit_access():
        k = selected()
        if not k:
            return
        d = access_dialog(f"Доступы: {k['name'] or k['key']}", k["accesses"], with_name=False)
        if d is None:
            return
        run(lambda: L.admin("update", _master["token"], {"key": k["key"], "accesses": d["accesses"]}), lambda _: reload())

    def prolong():
        k = selected()
        if not k:
            return
        v = simpledialog.askstring("Продлить", "На сколько дней от сегодня? (0 или пусто = бессрочно)", parent=parent)
        if v is None:
            return
        days = int(v) if v.strip().isdigit() and int(v) > 0 else 0
        run(lambda: L.admin("update", _master["token"], {"key": k["key"], "days": days}), lambda _: reload())

    def toggle_revoke():
        k = selected()
        if not k:
            return
        run(lambda: L.admin("update", _master["token"], {"key": k["key"], "revoked": k["status"] != "revoked"}),
            lambda _: reload())

    def unbind():
        k = selected()
        if not k:
            return
        if messagebox.askyesno("Отвязать", f"Отвязать ключ «{k['name'] or k['key']}» от всех компьютеров?\n"
                               "Человек сможет активировать его на новом ПК.", parent=parent):
            run(lambda: L.admin("unbind", _master["token"], {"key": k["key"]}), lambda _: reload())

    def rename():
        k = selected()
        if not k:
            return
        v = simpledialog.askstring("Переименовать", "Новое название:", initialvalue=k["name"], parent=parent)
        if v is not None:
            run(lambda: L.admin("update", _master["token"], {"key": k["key"], "name": v.strip()}), lambda _: reload())

    def remove():
        k = selected()
        if not k:
            return
        if messagebox.askyesno("Удалить", f"Удалить ключ «{k['name'] or k['key']}» навсегда?", parent=parent):
            run(lambda: L.admin("delete", _master["token"], {"key": k["key"]}), lambda _: reload())

    # ------------------------------------------------ массовые операции
    def run_bulk(tasks, title):
        """tasks: [(подпись, функция)]. Идут по очереди в потоке, прогресс - в строке статуса."""
        n = len(tasks)

        def work():
            ok, errs = 0, []
            for i, (label, fn) in enumerate(tasks, 1):
                try:
                    fn()
                    ok += 1
                except Exception as ex:
                    errs.append(f"{label}: {ex}")
                q.put(("ok", lambda v: login_status.set(f"{title}: {v[0]} из {v[1]}…"), (i, n)))
                time.sleep(0.05)
            q.put(("ok", lambda v: bulk_done(title, v[0], v[1], v[2]), (ok, n, errs)))
        login_status.set(f"{title}: 0 из {n}…")
        threading.Thread(target=work, daemon=True).start()

    def bulk_done(title, ok, n, errs):
        text = f"{title}: готово {ok} из {n}."
        if errs:
            text += "\n\nНе удалось:\n" + "\n".join(errs[:10]) + (f"\n… и ещё {len(errs) - 10}" if len(errs) > 10 else "")
        messagebox.showinfo("Массовое изменение", text, parent=parent)
        reload()

    def bulk_dialog():
        if not _master["token"]:
            reload()
            return
        sel = [keys_cache[i] for i in tv.selection() if i in keys_cache]
        if not sel:
            messagebox.showinfo("Массово", "Сначала выбери ключи в таблице (Ctrl/Shift + клик) или кнопками «Выбрать».",
                                parent=parent)
            return
        dlg = tk.Toplevel(parent)
        dlg.title("Массовое изменение")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.grab_set()
        ttk.Label(dlg, text=f"Выбрано ключей: {len(sel)}", font=T.FONT_TITLE).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 2))
        ttk.Label(dlg, text="Выбери, что сделать со всеми ними:", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=14)
        act = tk.StringVar(value="add")
        ACTS = [("add", "Выдать доступы (добавить к текущим)"), ("remove", "Забрать доступы"),
                ("set", "Заменить доступы на отмеченные"), ("days", "Изменить срок (дней от сегодня)"),
                ("revoke", "Отозвать ключи"), ("restore", "Вернуть отозванные"),
                ("unbind", "Отвязать от компьютеров"), ("delete", "Удалить навсегда")]
        left = ttk.Frame(dlg)
        left.grid(row=2, column=0, sticky="nw", padx=14, pady=8)
        for k_, t_ in ACTS:
            ttk.Radiobutton(left, text=t_, value=k_, variable=act).pack(anchor="w", pady=1)
        right = ttk.LabelFrame(dlg, text="Параметры")
        right.grid(row=2, column=1, sticky="nw", padx=(0, 14), pady=8)
        acc_vars = {}
        for a in L.LOCKED:
            v = tk.BooleanVar(value=False)
            acc_vars[a] = v
            ttk.Checkbutton(right, text=L.ACCESS_NAMES.get(a, a), variable=v).pack(anchor="w", padx=8, pady=1)
        days_v = tk.StringVar(value="30")
        row = ttk.Frame(right)
        row.pack(fill="x", padx=8, pady=(8, 6))
        ttk.Label(row, text="Дней (0 = бессрочно):").pack(side="left")
        ttk.Entry(row, textvariable=days_v, width=7).pack(side="left", padx=4)

        def apply():
            a = act.get()
            chosen = [x for x, v in acc_vars.items() if v.get()]
            tok = _master["token"]
            tasks = []
            if a in ("add", "remove", "set"):
                if not chosen and a != "set":
                    messagebox.showinfo("Массово", "Отметь хотя бы один доступ справа.", parent=dlg)
                    return
                for k in sel:
                    cur = set(k["accesses"])
                    new = (cur | set(chosen)) if a == "add" else (cur - set(chosen)) if a == "remove" else set(chosen)
                    if new != cur:
                        tasks.append((k["name"] or k["key"], lambda k=k, new=new: L.admin(
                            "update", tok, {"key": k["key"], "accesses": sorted(new)})))
                title = {"add": "Выдача доступов", "remove": "Снятие доступов", "set": "Замена доступов"}[a]
            elif a == "days":
                s = days_v.get().strip()
                if not s.isdigit():
                    messagebox.showinfo("Массово", "Введи число дней (0 = бессрочно).", parent=dlg)
                    return
                d = int(s)
                tasks = [(k["name"] or k["key"], lambda k=k: L.admin("update", tok, {"key": k["key"], "days": d})) for k in sel]
                title = "Изменение срока"
            elif a in ("revoke", "restore"):
                want = a == "revoke"
                tasks = [(k["name"] or k["key"], lambda k=k: L.admin("update", tok, {"key": k["key"], "revoked": want}))
                         for k in sel if (k["status"] == "revoked") != want]
                title = "Отзыв ключей" if want else "Возврат ключей"
            elif a == "unbind":
                tasks = [(k["name"] or k["key"], lambda k=k: L.admin("unbind", tok, {"key": k["key"]})) for k in sel]
                title = "Отвязка"
            else:
                tasks = [(k["name"] or k["key"], lambda k=k: L.admin("delete", tok, {"key": k["key"]})) for k in sel]
                title = "Удаление"
            if not tasks:
                messagebox.showinfo("Массово", "Для выбранных ключей менять нечего.", parent=dlg)
                return
            if a in ("revoke", "unbind", "delete"):
                if not messagebox.askyesno("Подтверди", f"{title}: {len(tasks)} ключей. Продолжить?", parent=dlg):
                    return
            dlg.destroy()
            run_bulk(tasks, title)
        ttk.Button(dlg, text="Применить ко всем выбранным", style="Accent.TButton", command=apply).grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 12))
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    def pump():
        try:
            while True:
                kind, done, val = q.get_nowait()
                if kind == "err":
                    login_status.set(f"Ошибка: {val}")
                elif done:
                    done(val)
        except queue.Empty:
            pass
        try:
            parent.after(150, pump)
        except Exception:
            pass

    me.bind("<Return>", lambda e: reload())
    ttk.Button(login, text="Войти", style="Accent.TButton", command=reload).pack(side="left", padx=4)
    parent.after(150, pump)
    if _master["token"]:
        reload()
    return lambda: None
