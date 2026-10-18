# -*- coding: utf-8 -*-
"""Majestic PM: отдельная бесплатная программа - только вкладка «PM» и чистильщик ОЗУ, без ключей.
Свои данные (%LOCALAPPDATA%\\MajesticPM) и свой канал обновлений: релизы с тегом pm-vX.Y.Z, exe MajesticPM.exe."""
import os
import sys
import json
import queue
import ctypes
import threading
import traceback

import paths
paths.APP = "MajesticPM"                 # до импорта модулей, которые считают пути при загрузке
import updater
updater.TAG_PREFIX = "pm-v"
updater.ASSET_NAME = "MajesticPM.exe"

from version import __version__

ERROR_PATH = os.path.join(paths.data_dir(), "majestic_pm_error.txt")

PAGES = [  # (ключ, значок, название, подзаголовок)
    ("pm", "☰", "PM", "Таблица сотрудников по скринам планшета: кто в сети, кто катает вызовы, группа, тексты /f"),
    ("ram", "⚡", "Чистильщик ОЗУ", "Освобождает память, которую держит игра"),
]


def fatal(text):
    try:
        with open(ERROR_PATH, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
    except Exception:
        pass
    ctypes.windll.user32.MessageBoxW(None, f"{text}\n\nПодробности: {ERROR_PATH}", "Majestic PM - ошибка", 0x10)


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox
    import theme as T

    updater.cleanup_old()
    root = tk.Tk()
    root.title(f"Majestic PM {__version__}")
    root.minsize(1000, 640)
    T.apply(root)

    ui_path = os.path.join(paths.data_dir(), "ui_config.json")
    try:
        with open(ui_path, encoding="utf-8") as f:
            ui_cfg = json.load(f)
    except Exception:
        ui_cfg = {}
    scr_w, scr_h = root.winfo_screenwidth(), root.winfo_screenheight()
    geo = ui_cfg.get("geometry")
    if not geo:
        gw, gh = min(1260, scr_w - 80), min(800, scr_h - 100)
        geo = f"{gw}x{gh}+{(scr_w - gw) // 2}+{(scr_h - gh) // 2}"
    root.geometry(geo)
    top_var = tk.BooleanVar(value=bool(ui_cfg.get("topmost", False)))
    root.attributes("-topmost", top_var.get())

    def save_ui():
        ui_cfg.update(geometry=root.geometry(), topmost=top_var.get(), page=state["page"])
        try:
            with open(ui_path, "w", encoding="utf-8") as f:
                json.dump(ui_cfg, f)
        except Exception:
            pass

    # ---------------- каркас: меню слева, страница справа
    side = tk.Frame(root, bg=T.SIDEBAR, width=220)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    tk.Frame(root, bg=T.BORDER, width=1).pack(side="left", fill="y")
    main_area = ttk.Frame(root)
    main_area.pack(side="left", fill="both", expand=True)

    tk.Label(side, text="Majestic PM", bg=T.SIDEBAR, fg=T.TEXT, font=("Segoe UI", 15, "bold"),
             anchor="w").pack(fill="x", padx=16, pady=(18, 0))
    tk.Label(side, text="бесплатно, без ключа", bg=T.SIDEBAR, fg=T.MUTED, font=("Segoe UI", 9),
             anchor="w").pack(fill="x", padx=16, pady=(0, 14))
    foot = tk.Frame(side, bg=T.SIDEBAR)
    foot.pack(side="bottom", fill="x", padx=14, pady=(0, 12))

    header = tk.Frame(main_area, bg=T.BG)
    header.pack(fill="x", padx=22, pady=(18, 0))
    title_lbl = tk.Label(header, text="", bg=T.BG, fg=T.TEXT, font=T.FONT_TITLE, anchor="w")
    title_lbl.pack(anchor="w")
    sub_lbl = tk.Label(header, text="", bg=T.BG, fg=T.MUTED, font=T.FONT, anchor="w")
    sub_lbl.pack(anchor="w")
    tk.Frame(main_area, bg=T.ACCENT, height=2).pack(fill="x", padx=22, pady=(10, 10))
    holder = ttk.Frame(main_area)
    holder.pack(fill="both", expand=True, padx=16, pady=(0, 10))

    pages, items, stops = {}, {}, []
    state = {"page": None, "busy": False, "manual": False}

    def paint_items():
        for k, (row, ico, lab) in items.items():
            on = k == state["page"]
            bg = T.ACCENT_DIM if on else T.SIDEBAR
            for w in (row, ico, lab):
                w.configure(bg=bg)
            ico.configure(fg=T.ACCENT if on else T.MUTED)
            lab.configure(fg=T.TEXT if on else T.MUTED)

    def select(key):
        state["page"] = key
        pages[key].tkraise()
        _, _, name, sub = next(p for p in PAGES if p[0] == key)
        title_lbl.configure(text=name)
        sub_lbl.configure(text=sub)
        paint_items()

    for key, icon, name, _ in PAGES:
        f = ttk.Frame(holder)
        f.place(x=0, y=0, relwidth=1, relheight=1)
        pages[key] = f
        row = tk.Frame(side, bg=T.SIDEBAR, cursor="hand2")
        row.pack(fill="x", padx=8, pady=1)
        ico = tk.Label(row, text=icon, bg=T.SIDEBAR, fg=T.MUTED, font=("Segoe UI Symbol", 13), width=2)
        ico.pack(side="left", padx=(8, 4), pady=8)
        lab = tk.Label(row, text=name, bg=T.SIDEBAR, fg=T.MUTED, font=T.FONT_BOLD, anchor="w")
        lab.pack(side="left", fill="x", expand=True)
        for w in (row, ico, lab):
            w.bind("<Button-1>", lambda e, k=key: select(k))
        items[key] = (row, ico, lab)

    def failed(page, what, ex):
        ttk.Label(page, text=f"{what}: не удалось открыть ({ex})", style="Warn.TLabel").pack(anchor="w", padx=10, pady=10)

    try:
        import pm
        stops.append(pm.build_pm_tab(pages["pm"]))
    except Exception as ex:
        failed(pages["pm"], "PM", ex)
    try:
        import ram_cleaner
        if ram_cleaner.psutil is None:
            raise RuntimeError("не найдена библиотека psutil")
        stops.append(ram_cleaner.build_ram_tab(pages["ram"], key_bar=False))
    except Exception as ex:
        failed(pages["ram"], "Чистильщик", ex)

    # ---------------- низ меню: поверх окон, версия, обновления
    ttk.Checkbutton(foot, text="Поверх окон", variable=top_var, style="Side.TCheckbutton",
                    command=lambda: (root.attributes("-topmost", top_var.get()), save_ui())).pack(anchor="w", pady=1)
    upd_var = tk.StringVar(value=f"Версия {__version__}")
    tk.Label(foot, textvariable=upd_var, bg=T.SIDEBAR, fg=T.MUTED, font=("Segoe UI", 9),
             wraplength=190, justify="left").pack(anchor="w", pady=(8, 6))
    btn = ttk.Button(foot, text="Проверить обновления", command=lambda: start_check(True))
    btn.pack(fill="x")

    ui = queue.Queue()

    def bg(fn):
        def run():
            try:
                ui.put(("ok", fn, fn()))
            except Exception as ex:
                ui.put(("err", fn, ex))
        threading.Thread(target=run, daemon=True).start()

    def start_check(manual):
        if state["busy"]:
            return
        if not updater.is_frozen():
            if manual:
                messagebox.showinfo("Обновления", "Обновление работает только в собранном exe.", parent=root)
            return
        state.update(busy=True, manual=manual)
        upd_var.set("Проверяю обновления...")
        bg(updater.check)

    def on_checked(info):
        if info is None:
            upd_var.set(f"Версия {__version__} — последняя")
            state["busy"] = False
            if state["manual"]:
                messagebox.showinfo("Обновления", "У тебя последняя версия", parent=root)
            return
        text = f"Доступна версия {info['version']} (у тебя {__version__})."
        if info["notes"]:
            text += f"\n\nЧто нового:\n{info['notes'][:500]}"
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
                    if state["manual"] or fn is not updater.check:
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

    def close():
        save_ui()
        for st in stops:
            try:
                st()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    select(ui_cfg.get("page") if ui_cfg.get("page") in pages else "pm")
    try:
        T.dark_titlebar(root)
    except Exception:
        pass
    root.after(100, pump)
    root.after(1500, lambda: start_check(False))      # тихая проверка при запуске
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        fatal(f"Ошибка запуска: {ex}")
