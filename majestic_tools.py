# -*- coding: utf-8 -*-
"""Majestic Tools: швейный бот + чистильщик ОЗУ в одном окне, с обновлением через GitHub."""
import os
import sys
import json
import queue
import ctypes
import threading
import traceback

from version import __version__

IS_WIN = sys.platform == "win32"


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
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
    ("market", "⚖", "Рынок", "Цены Маркетплейса по серверам и где выгодно купить и продать (официальное API Majestic)"),
    ("rec", "⏺", "Запись", "Запись экрана и нажатий для разбора: по ней можно восстановить логику нового бота"),
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
            ui.put(("rebuild", None, None))        # в окно - только через очередь (из потока tk трогать нельзя)
        return res

    updater.cleanup_old()
    root = tk.Tk()
    root.title("Очистка оперативки Majestic")
    root.minsize(1000, 640)
    T.apply(root)
    root.attributes("-topmost", True)

    # настройки окна: размер и положение запоминаются между запусками
    import re
    ui_path = os.path.join(app_dir(), "ui_config.json")
    try:
        with open(ui_path, encoding="utf-8") as f:
            ui_cfg = json.load(f)
    except Exception:
        ui_cfg = {}
    scr_w, scr_h = root.winfo_screenwidth(), root.winfo_screenheight()
    gw, gh = min(1260, scr_w - 80), min(820, scr_h - 100)
    gx, gy = (scr_w - gw) // 2, (scr_h - gh) // 2
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(ui_cfg.get("geometry", "")))
    if m:
        w_, h_, x_, y_ = map(int, m.groups())
        if w_ >= 1000 and h_ >= 640:
            gw, gh = min(w_, scr_w), min(h_, scr_h)
            gx, gy = max(0, min(x_, scr_w - 200)), max(0, min(y_, scr_h - 120))
    root.geometry(f"{gw}x{gh}+{gx}+{gy}")

    import widgets
    import appui

    # ---------------- каркас: меню слева, страница справа
    side = tk.Frame(root, bg=T.SIDEBAR, width=appui.SIDE_W)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    tk.Frame(root, bg=T.BORDER, width=1).pack(side="left", fill="y")
    main_area = ttk.Frame(root)
    main_area.pack(side="left", fill="both", expand=True)
    foot = tk.Frame(side, bg=T.SIDEBAR)          # низ меню (заполняется ниже), пакуем до самого меню
    foot.pack(side="bottom", fill="x", padx=14, pady=(0, 12))

    header = tk.Frame(main_area, bg=T.BG)
    header.pack(fill="x", padx=22, pady=(18, 0))
    title_lbl = tk.Label(header, text="", bg=T.BG, fg=T.TEXT, font=T.FONT_TITLE, anchor="w")
    title_lbl.pack(anchor="w")
    sub_lbl = tk.Label(header, text="", bg=T.BG, fg=T.MUTED, font=T.FONT, anchor="w")
    sub_lbl.pack(anchor="w")
    line_box = tk.Frame(main_area, bg=T.BORDER, height=2)
    line_box.pack(fill="x", padx=22, pady=(10, 10))
    line_acc = tk.Frame(line_box, bg=T.ACCENT)
    line_acc.place(relx=0, rely=0, relwidth=1, relheight=1)

    holder = ttk.Frame(main_area)
    holder.pack(fill="both", expand=True, padx=16, pady=(0, 10))
    pages = {}
    HOME = ("home", "⌂", "Главная", "Обзор: состояние ключа, память и быстрые переходы")
    ADMIN = ("admin", "🔑", "Админка", "Создание и управление ключами")
    PAGES_ALL = [HOME] + PAGES + [ADMIN]
    for key, *_ in PAGES_ALL:
        f = ttk.Frame(holder)
        f.place(x=0, y=0, relwidth=1, relheight=1)      # place - чтобы страница могла «въезжать»
        pages[key] = f
    dash = widgets.Dashboard(root, main_area, before=holder)
    import stats
    toasts = appui.Toasts(root)

    def toast(text, kind="info", ttl=4.5):
        """Всплывающее уведомление и запись в ленту событий на «Главной»."""
        stats.event(text, kind)
        toasts.push(text, kind, ttl)
    names = {p[0]: p[2] for p in PAGES}
    dash.on_toggle = lambda k, a: toast(f"{names.get(k, k)}: {'запущен' if a else 'остановлен'}",
                                        "ok" if a else "info", 3.0)
    refresh_access(initial=True)

    state = {"page": None, "busy": False, "had_key": None}

    def select(key):
        if key not in pages:
            return
        for k, icon, name, sub in PAGES_ALL:
            if k == key:
                title_lbl.configure(text=name)
                sub_lbl.configure(text=sub)
        sidebar.select(key)
        changed = state["page"] != key
        # всё тяжёлое (раскладка страницы, блок бота над ней, первая окраска) - ДО анимации:
        # иначе первый кадр анимации «залипает» на десятки миллисекунд
        pages[key].tkraise()
        state["page"] = key
        dash.show(key)
        home.active = key == "home"
        if key == "home":
            home.replay()
        if changed:
            root.update()
            # страницу целиком не двигаем (тяжёлые перерисовывались по 60-110 мс на кадр):
            # анимируются только лёгкие элементы - линия, заголовок и «въезд» блоков на Canvas
            widgets.tween(root, "line", lambda t: line_acc.place(relx=0, rely=0, relwidth=t, relheight=1), 300)
            widgets.tween(root, "title", lambda t: (title_lbl.configure(fg=widgets.mix(T.BG, T.TEXT, t)),
                                                    sub_lbl.configure(fg=widgets.mix(T.BG, T.MUTED, t))), 220)

    sidebar = appui.Sidebar(side, on_select=select,
                            on_toggle=lambda k: switches.set_on(k, not switches.is_on(k)),
                            get_toggle=switches.is_on, switchable=list(switches.NAMES))
    sidebar.pack(fill="both", expand=True)
    home = appui.HomePage(pages["home"], on_open=select,
                          on_locked=lambda k: (toast("Для этого раздела нужен ключ: введи его в «Чистильщик ОЗУ»", "warn", 5.0),
                                               select("ram")))
    home.pack(fill="both", expand=True)

    def visible_pages():
        acc = access["set"]
        out = [HOME] + [p for p in PAGES if p[0] in acc and p[0] != "rec"]
        if L.has_master():                         # инструменты владельца: запись и админка
            out.append(next(p for p in PAGES if p[0] == "rec"))
            out.append(ADMIN)
        return out

    def rebuild_sidebar():
        sync_tabs()
        res = access["res"]
        has_key = bool(res and res.get("ok"))
        root.title(f"Majestic Tools {__version__}" if has_key else "Очистка оперативки Majestic")
        vp = visible_pages()
        sidebar.set_items([(k, i, n) for k, i, n, _ in vp])
        if has_key:
            exp = res.get("expires")
            sidebar.set_status(f"Ключ «{res.get('name') or 'ключ'}»"[:24],
                               f"до {exp[:10]}" if exp else "доступ активен", True)
        else:
            sidebar.set_status("Нет ключа", "ОЗУ и готовка бесплатны", False)
        home.set_state(access["set"], res, __version__)
        if state["had_key"] is not None and has_key != state["had_key"]:
            if has_key:
                toast("Ключ активен: закрытые разделы открыты", "ok")
            else:
                toast("Доступ закрыт: закрытые разделы отключены", "warn", 5.0)
        state["had_key"] = has_key
        cur = state.get("page")
        select(cur if cur in [k for k, *_ in vp] else "home")

    stops = []

    def close():
        try:
            save_ui()
            dash.flush()
        except Exception:
            pass
        try:
            hud.destroy()
        except Exception:
            pass
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

    built = {}                    # закрытые разделы, которые сейчас работают -> их функция остановки
    NAMES = {"bot": "Бот", "afk": "Анти-АФК", "fish": "Рыбалка"}

    def make_tab(key):
        if key == "bot":
            import sewing_bot
            return sewing_bot.build_bot_tab(pages["bot"], request_close=close)
        if key == "afk":
            import antiafk
            return antiafk.build_antiafk_tab(pages["afk"])
        import fishing
        return fishing.build_fishing_tab(pages["fish"])

    def teardown(key):
        """Ключ пропал/отозван: останавливаем бота, убираем его интерфейс и статистику."""
        st = built.pop(key, None)
        if st is not None:
            try:
                st()
            except Exception:
                pass
            if st in stops:
                stops.remove(st)
        widgets.PROVIDERS.pop(key, None)
        for w in list(pages[key].winfo_children()):
            w.destroy()
        dash.views.pop(key, None)

    def sync_tabs():
        """Приводит разделы в соответствие с ключом: открыл ключ - собрал, нет ключа - снёс."""
        for key in ("bot", "afk", "fish"):
            if key in access["set"] and key not in built:
                for w in list(pages[key].winfo_children()):
                    w.destroy()
                try:
                    st = make_tab(key)
                    built[key] = st
                    stops.append(st)
                except Exception as ex:
                    failed(pages[key], NAMES[key], ex)
            elif key not in access["set"] and key in built:
                teardown(key)
        if L.wipe_pending() and not built:
            L.wipe_bot_files()
        if state.get("page") in ("bot", "afk", "fish") and state["page"] not in access["set"]:
            select("home")

    sync_tabs()
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
    try:
        import market
        stops.append(market.build_market_tab(pages["market"]))
    except Exception as ex:
        failed(pages["market"], "Рынок", ex)
    try:
        import recorder
        stops.append(recorder.build_rec_tab(pages["rec"]))
    except Exception as ex:
        failed(pages["rec"], "Запись", ex)
    try:
        licenseui.build_admin_tab(pages["admin"], refresh_access)
    except Exception as ex:
        failed(pages["admin"], "Админка", ex)

    # ---------------- низ меню: поверх окон, версия, обновления
    top_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(foot, text="Поверх окон", variable=top_var, style="Side.TCheckbutton",
                    command=lambda: root.attributes("-topmost", top_var.get())).pack(anchor="w", pady=1)
    import hud as _hud
    hud = _hud.Hud(root, dash)
    hud.set_side(ui_cfg.get("hud_side", "right"))
    hud.set_enabled(bool(ui_cfg.get("hud", False)))
    hud_var = tk.BooleanVar(value=hud.enabled)

    def save_ui():
        ui_cfg.update(hud=hud_var.get(), hud_side=hud.side, geometry=root.geometry())
        try:
            with open(ui_path, "w", encoding="utf-8") as f:
                json.dump(ui_cfg, f)
        except Exception:
            pass

    ttk.Checkbutton(foot, text="HUD поверх игры", variable=hud_var, style="Side.TCheckbutton",
                    command=lambda: (hud.set_enabled(hud_var.get()), save_ui())).pack(anchor="w", pady=1)
    side_var = tk.StringVar()

    def paint_side():
        side_var.set("      положение HUD: " + ("справа" if hud.side == "right" else "слева"))
    paint_side()
    side_lbl = tk.Label(foot, textvariable=side_var, bg=T.SIDEBAR, fg=T.MUTED, font=("Segoe UI", 8),
                        cursor="hand2")
    side_lbl.pack(anchor="w")
    side_lbl.bind("<Button-1>", lambda e: (hud.set_side("left" if hud.side == "right" else "right"),
                                           paint_side(), save_ui()))
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
            if state.get("manual"):
                toast("У тебя последняя версия", "ok")
            return
        toast(f"Доступна версия {info['version']}", "info", 6.0)
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
                if kind == "rebuild":
                    rebuild_sidebar()
                elif kind == "progress":
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
    L.on_change(lambda: threading.Thread(target=lambda: refresh_access(), daemon=True).start())
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
