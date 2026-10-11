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


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox
    import updater

    updater.cleanup_old()
    root = tk.Tk()
    root.title(f"Majestic Tools {__version__}")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=4, pady=4)
    tab_bot = ttk.Frame(nb)
    tab_ram = ttk.Frame(nb)
    tab_cook = ttk.Frame(nb)
    nb.add(tab_bot, text="Швейный бот")
    nb.add(tab_ram, text="Чистильщик ОЗУ")
    nb.add(tab_cook, text="Готовка")

    stops = []

    def close():
        for st in stops:
            try:
                st()
            except Exception:
                pass
        root.after(200, root.destroy)

    try:
        import sewing_bot
        stops.append(sewing_bot.build_bot_tab(tab_bot, request_close=close))
    except Exception as ex:
        traceback.print_exc()
        tk.Label(tab_bot, text=f"Бот не запустился:\n{ex}", fg="#c0392b",
                 justify="left").pack(padx=10, pady=10)
    try:
        import ram_cleaner
        if ram_cleaner.psutil is None:
            raise RuntimeError("не найдена библиотека psutil")
        stops.append(ram_cleaner.build_ram_tab(tab_ram))
    except Exception as ex:
        traceback.print_exc()
        tk.Label(tab_ram, text=f"Чистильщик не запустился:\n{ex}", fg="#c0392b",
                 justify="left").pack(padx=10, pady=10)

    try:
        import cooking
        stops.append(cooking.build_cooking_tab(tab_cook))
    except Exception as ex:
        traceback.print_exc()
        tk.Label(tab_cook, text=f"Готовка не запустилась:\n{ex}", fg="#c0392b",
                 justify="left").pack(padx=10, pady=10)

    # ---------------- нижняя панель: поверх окон + обновления
    bar = ttk.Frame(root)
    bar.pack(fill="x", padx=6, pady=(0, 6))
    top_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(bar, text="Поверх окон", variable=top_var,
                    command=lambda: root.attributes("-topmost", top_var.get())).pack(side="left")
    upd_var = tk.StringVar(value=f"Версия {__version__}")
    ttk.Label(bar, textvariable=upd_var).pack(side="left", padx=10)
    btn = ttk.Button(bar, text="Проверить обновления")
    btn.pack(side="right")

    ui = queue.Queue()            # результаты из фоновых потоков -> в окно
    state = {"busy": False}

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
            upd_var.set(f"Версия {__version__} - последняя")
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
