# -*- coding: utf-8 -*-
"""Окно ввода ключа (в разделе ОЗУ) и админка (по мастер-ключу)."""
import time
import queue
import threading

import license as L


_master = {"token": ""}


def master_token():
    return _master["token"]


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
        row=0, column=0, columnspan=3, sticky="w", padx=6, pady=4)
    ttk.Label(box, text="Код этого компьютера:").grid(row=1, column=0, sticky="w", padx=6)
    hv = tk.StringVar(value=L.hwid_short())
    e = ttk.Entry(box, textvariable=hv, width=24, state="readonly")
    e.grid(row=1, column=1, sticky="w", padx=4, pady=2)
    ttk.Label(box, text="Ключ:").grid(row=2, column=0, sticky="w", padx=6)
    ke = ttk.Entry(box, textvariable=key_var, width=30)
    ke.grid(row=2, column=1, sticky="w", padx=4, pady=2)
    btns = ttk.Frame(box)
    btns.grid(row=2, column=2, sticky="w")

    def apply_key():
        state_var.set("Проверяю ключ...")

        def work():
            L.save_key(key_var.get())
            r = L.check(key_var.get())
            q.put(r)
        threading.Thread(target=work, daemon=True).start()

    def remove_key():
        L.clear_key()
        key_var.set("")
        r = {"ok": False, "reason": "no_key"}
        state_var.set(describe(r))
        on_change()

    ttk.Button(btns, text="Применить", style="Accent.TButton", command=apply_key).pack(side="left", padx=2)
    ttk.Button(btns, text="Выгрузить ключ", command=remove_key).pack(side="left", padx=2)

    def pump():
        try:
            r = q.get_nowait()
            state_var.set(describe(r))
            on_change()
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
    login_status = tk.StringVar(value="")
    ttk.Label(login, textvariable=login_status, style="Muted.TLabel").pack(side="left", padx=6)

    body = ttk.Frame(parent)
    body.grid(row=1, column=0, sticky="nsew", **pad)
    parent.rowconfigure(1, weight=1)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    cols = ("name", "key", "acc", "status", "dev", "exp")
    tv = ttk.Treeview(body, columns=cols, show="headings", height=12, selectmode="browse")
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

    keys_cache = {}

    def token():
        return mv.get().strip()

    def selected():
        s = tv.selection()
        return keys_cache.get(s[0]) if s else None

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
