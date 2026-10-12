# -*- coding: utf-8 -*-
"""Majestic Tools: швейный бот + чистильщик ОЗУ в одном окне, с обновлением через GitHub."""
import os
import sys
import queue
import ctypes
import threading
import traceback

from version import __version__

IS_WIN = sys.platform == "win32"


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ERROR_PATH = os.path.join(app_dir(), "majestic_tools_error.txt")


def fatal(text):
    try:
        with open(ERROR_PATH, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
    except Exception:
        pass
    msg = f"{text}\n\nПодробности: {ERROR_PATH}"
    if IS_WIN:
        ctypes.windll.user32.MessageBoxW(None, msg, "Majestic Tools - ошибка", 0x10)
    else:
        print(msg)


PAGES = [  # (ключ, значок, название, подзаголовок)
    ("bot", "✂", "Швейный бот", "Автопрохождение мини-игры в ателье"),
    ("ram", "⚡", "Чистильщик ОЗУ", "Освобождает память, которую держит игра"),
    ("cook", "♨", "Готовка", "Калькулятор блюд, таймеры и уведомления в Telegram"),
    ("afk", "♻", "Анти-АФК", "Случайные нажатия по пресету, еда и вода по значкам на экране"),
    ("fish", "⚓", "Рыбалка", "Полный цикл: E, заброс в зелёную зону, подсечка, вываживание A/D, улов"),
]


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox
    import updater
    import theme as T
    import switches
    import license as L
    import licenseui

    access = {"set": set(L.FREE), "res": None}

    def refresh_access(initial=False):
        res = L.check()
        access["res"] = res
        access["set"] = L.granted(res)
        if not initial:
            root.after(0, rebuild_sidebar)
        return res

    updater.cleanup_old()
    root = tk.Tk()
    root.title("Очистка оперативки Majestic")
    root.minsize(900, 560)
    T.apply(root)
    root.attributes("-topmost", True)

    # ---------------- каркас: меню слева, страница справа
    side = tk.Frame(root, bg=T.SIDEBAR, width=232)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    tk.Frame(root, bg=T.BORDER, width=1).pack(side="left", fill="y")
    main_area = ttk.Frame(root)
    main_area.pack(side="left", fill="both", expand=True)

    brand = tk.Frame(side, bg=T.SIDEBAR)
    brand.pack(fill="x", padx=18, pady=(20, 22))
    tk.Label(brand, text="MAJESTIC", bg=T.SIDEBAR, fg=T.ACCENT,
             font=("Segoe UI", 17, "bold")).pack(anchor="w")
    tk.Label(brand, text="TOOLS", bg=T.SIDEBAR, fg=T.TEXT,
             font=("Segoe UI", 11, "bold")).pack(anchor="w")

    header = ttk.Frame(main_area)
    header.pack(fill="x", padx=22, pady=(16, 0))
    title_var, sub_var = tk.StringVar(), tk.StringVar()
    ttk.Label(header, textvariable=title_var, font=T.FONT_TITLE).pack(anchor="w")
    ttk.Label(header, textvariable=sub_var, style="Muted.TLabel").pack(anchor="w")
    tk.Frame(main_area, bg=T.ACCENT, height=2).pack(fill="x", padx=22, pady=(10, 6))

    holder = ttk.Frame(main_area)
    holder.pack(fill="both", expand=True, padx=16, pady=(0, 10))
    holder.rowconfigure(0, weight=1)
    holder.columnconfigure(0, weight=1)
    pages, nav = {}, {}
    PAGES_ALL = PAGES + [("admin", "🔑", "Админка", "Создание и управление ключами")]
    for key, *_ in PAGES_ALL:
        f = ttk.Frame(holder)
        f.grid(row=0, column=0, sticky="nsew")
        pages[key] = f
    refresh_access(initial=True)

    def select(key):
        for k, (bar, lbl, row) in nav.items():
            on = k == key
            bg = "#1a0c10" if on else T.SIDEBAR
            for w in (row, lbl):
                w.configure(bg=bg)
            bar.configure(bg=T.ACCENT if on else T.SIDEBAR)
            lbl.configure(fg="#ffffff" if on else T.MUTED)
        for k, icon, name, sub in PAGES_ALL:
            if k == key:
                title_var.set(name if access["res"] and access["res"].get("ok") or k in ("ram", "cook", "admin") else name)
                sub_var.set(sub)
        pages[key].tkraise()
        state["page"] = key

    state = {"page": None, "busy": False}

    nav_rows = {}

    def visible_pages():
        acc = access["set"]
        out = [p for p in PAGES if p[0] in acc]
        if L.has_master():
            out.append(("admin", "🔑", "Админка", "Создание и управление ключами"))
        return out

    def rebuild_sidebar():
        has_key = bool(access["res"] and access["res"].get("ok"))
        root.title(f"Majestic Tools {__version__}" if has_key else "Очистка оперативки Majestic")
        for w in list(navbox.winfo_children()):
            w.destroy()
        nav.clear(); pills.clear(); nav_rows.clear()
        for key, icon, name, _ in visible_pages():
            build_nav_row(key, icon, name)
        cur = state.get("page")
        select(cur if cur in nav else (visible_pages()[0][0] if visible_pages() else "ram"))

    pills = {}

    def paint_pill(key):
        on = switches.is_on(key)
        pills[key].configure(text="ВКЛ" if on else "ВЫКЛ", fg="#3ecf6e" if on else T.MUTED,
                             bg="#12291a" if on else "#1d1d23")

    switches.listen(lambda k, v: root.after(0, lambda: k in pills and paint_pill(k)))

    navbox = tk.Frame(side, bg=T.SIDEBAR)
    navbox.pack(fill="x")

    def build_nav_row(key, icon, name):
        row = tk.Frame(navbox, bg=T.SIDEBAR, cursor="hand2")
        row.pack(fill="x", pady=1)
        bar = tk.Frame(row, bg=T.SIDEBAR, width=4)
        bar.pack(side="left", fill="y")
        lbl = tk.Label(row, text=f"  {icon}   {name}", bg=T.SIDEBAR, fg=T.MUTED, anchor="w",
                       font=("Segoe UI", 11), padx=10, pady=10, cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True)
        if key in switches.NAMES:
            pill = tk.Label(row, font=("Segoe UI", 8, "bold"), padx=7, pady=2, cursor="hand2")
            pill.pack(side="right", padx=(0, 10))
            pills[key] = pill
            pill.bind("<Button-1>", lambda e, k=key: switches.set_on(k, not switches.is_on(k)))
            paint_pill(key)
        nav[key] = (bar, lbl, row)
        nav_rows[key] = row
        for w in (row, lbl):
            w.bind("<Button-1>", lambda e, k=key: select(k))
            w.bind("<Enter>", lambda e, k=key: state["page"] != k and nav[k][1].configure(fg=T.TEXT))
            w.bind("<Leave>", lambda e, k=key: state["page"] != k and nav[k][1].configure(fg=T.MUTED))

    stops = []

    def close():
        for st in stops:
            try:
                st()
            except Exception:
                pass
        root.after(200, root.destroy)

    def failed(page, what, ex):
        traceback.print_exc()
        ttk.Label(page, text=f"{what} не запустился:\n{ex}", style="Warn.TLabel",
                  justify="left").pack(padx=10, pady=10, anchor="w")

    if "bot" in access["set"]:
        try:
            import sewing_bot
            stops.append(sewing_bot.build_bot_tab(pages["bot"], request_close=close))
        except Exception as ex:
            failed(pages["bot"], "Бот", ex)
    try:
        import ram_cleaner
        if ram_cleaner.psutil is None:
            raise RuntimeError("не найдена библиотека psutil")
        stops.append(ram_cleaner.build_ram_tab(pages["ram"]))
    except Exception as ex:
        failed(pages["ram"], "Чистильщик", ex)
    try:
        import cooking
        stops.append(cooking.build_cooking_tab(pages["cook"]))
    except Exception as ex:
        failed(pages["cook"], "Раздел готовки", ex)
    if "afk" in access["set"]:
        try:
            import antiafk
            stops.append(antiafk.build_antiafk_tab(pages["afk"]))
        except Exception as ex:
            failed(pages["afk"], "Анти-АФК", ex)
    if "fish" in access["set"]:
        try:
            import fishing
            stops.append(fishing.build_fishing_tab(pages["fish"]))
        except Exception as ex:
            failed(pages["fish"], "Рыбалка", ex)
    try:
        licenseui.build_admin_tab(pages["admin"], refresh_access)
    except Exception as ex:
        failed(pages["admin"], "Админка", ex)

    # ---------------- низ меню: поверх окон, версия, обновления
    foot = tk.Frame(side, bg=T.SIDEBAR)
    foot.pack(side="bottom", fill="x", padx=14, pady=14)
    top_var = tk.BooleanVar(value=True)
    tk.Checkbutton(foot, text="Поверх окон", variable=top_var, bg=T.SIDEBAR, fg=T.MUTED,
                   activebackground=T.SIDEBAR, activeforeground=T.TEXT, selectcolor=T.FIELD,
                   bd=0, highlightthickness=0, font=("Segoe UI", 9),
                   command=lambda: root.attributes("-topmost", top_var.get())).pack(anchor="w")
    upd_var = tk.StringVar(value=f"Версия {__version__}")
    tk.Label(foot, textvariable=upd_var, bg=T.SIDEBAR, fg=T.MUTED, font=("Segoe UI", 9),
             wraplength=200, justify="left").pack(anchor="w", pady=(8, 6))
    btn = ttk.Button(foot, text="Проверить обновления")
    btn.pack(fill="x")

    ui = queue.Queue()            # результаты из фоновых потоков -> в окно

    def bg(fn, *args):
        def run():
            try:
                ui.put(("ok", fn, fn(*args)))
            except Exception as ex:
                ui.put(("err", fn, ex))
        threading.Thread(target=run, daemon=True).start()

    def start_check(manual):
        if state["busy"]:
            return
        if not updater.is_frozen():
            if manual:
                messagebox.showinfo("Обновления", "Обновление работает только в собранном exe.")
            return
        state["busy"] = True
        state["manual"] = manual
        upd_var.set("Проверяю обновления...")
        bg(updater.check)

    def on_checked(info):
        if info is None:
            upd_var.set(f"Версия {__version__} — последняя")
            state["busy"] = False
            return
        notes = info["notes"][:500]
        text = f"Доступна версия {info['version']} (у тебя {__version__})."
        if notes:
            text += f"\n\nЧто нового:\n{notes}"
        text += "\n\nОбновить сейчас? Программа перезапустится."
        if not messagebox.askyesno("Обновление", text, parent=root):
            upd_var.set(f"Версия {__version__} (есть {info['version']})")
            state["busy"] = False
            return
        upd_var.set("Скачиваю 0%")
        bg(lambda: updater.download(info, lambda p: ui.put(("progress", None, p))))

    def on_downloaded(path):
        upd_var.set("Устанавливаю...")
        try:
            updater.apply_and_restart(path)
        except Exception as ex:
            messagebox.showerror("Обновление", f"Не удалось установить: {ex}", parent=root)
            upd_var.set(f"Версия {__version__}")
            state["busy"] = False
            return
        close()

    def pump():
        try:
            while True:
                kind, fn, val = ui.get_nowait()
                if kind == "progress":
                    upd_var.set(f"Скачиваю {val * 100:.0f}%")
                elif kind == "err":
                    state["busy"] = False
                    upd_var.set(f"Версия {__version__} (обновления не проверить)")
                    if state.get("manual") or fn is not updater.check:
                        messagebox.showerror("Обновление", f"Ошибка: {val}", parent=root)
                elif fn is updater.check:
                    on_checked(val)
                else:
                    on_downloaded(val)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        root.after(100, pump)

    btn.configure(command=lambda: start_check(True))
    root.protocol("WM_DELETE_WINDOW", close)
    rebuild_sidebar()

    def recheck_loop():
        import threading as _t
        def work():
            try:
                refresh_access()
            except Exception:
                pass
        _t.Thread(target=work, daemon=True).start()
        root.after(300000, recheck_loop)      # перепроверка ключа раз в 5 минут
    root.after(300000, recheck_loop)
    T.dark_titlebar(root)
    root.after(100, pump)
    root.after(1500, lambda: start_check(False))      # тихая проверка при запуске
    root.mainloop()
    for st in stops:
        st()


if __name__ == "__main__":
    try:
        if "--debug" in sys.argv:
            import sewing_bot
            sewing_bot.Bot().debug_shot()
        elif "--video" in sys.argv:
            import sewing_bot
            sewing_bot.test_video(sys.argv[sys.argv.index("--video") + 1], gui="--nogui" not in sys.argv)
        elif "--console" in sys.argv:
            import sewing_bot
            sewing_bot.run_console()
        else:
            main()
    except Exception as ex:
        fatal(f"Ошибка запуска: {ex}")
