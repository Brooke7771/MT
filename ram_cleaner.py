# -*- coding: utf-8 -*-
"""
Чистильщик оперативки для Majestic RP (GTA5).

Что делает:
  - показывает, сколько памяти занято, и какие процессы игры сколько держат,
    в том числе как быстро растут (видно, кто "течёт");
  - "Очистить": выгружает из оперативки страницы процессов игры, к которым
    давно не обращались (EmptyWorkingSet). Утёкшая память, которую игра
    больше не трогает, уходит в файл подкачки и перестаёт занимать ОЗУ;
    нужная игре память подтянется обратно сама (может быть подлагивание 1-2 с);
  - по желанию чистит ещё и кэш Windows (standby list), нужен запуск от админа;
  - авто-режим: чистит сам, когда занятость ОЗУ выше порога.

Чего он НЕ может: если игра выделила память и держит её ("Выделено" растёт
до предела), это лечится только перезапуском игры. Программа предупредит.

Установка и запуск:
  py -m pip install psutil
  py ram_cleaner.py
Сборка exe (сам попросит права администратора):
  py -m PyInstaller --onefile --noconsole --uac-admin --clean ram_cleaner.py
"""
import os
import sys
import json
import time
import ctypes
import threading
import traceback
from ctypes import wintypes

try:
    import psutil
except Exception as _ex:          # покажем понятную ошибку при запуске
    psutil = None
    PSUTIL_ERROR = _ex

IS_WIN = sys.platform == "win32"

DEFAULT_CFG = {
    "names": "GTA5, majestic, ragemp",   # части имён процессов игры
    "auto": True,
    "threshold": 85,                     # % занятой ОЗУ для авто-очистки
    "min_interval": 5,                   # минут между авто-очистками
    "standby": True,                     # чистить кэш Windows (нужен админ)
}


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "ram_cleaner_config.json")


def load_config():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ================================================================ Windows API
if IS_WIN:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                             wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    ntdll.NtSetSystemInformation.restype = ctypes.c_long

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x0002
SYSTEM_MEMORY_LIST_INFORMATION = 80
MEMORY_PURGE_STANDBY_LIST = 4


def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def memory_status():
    """(занято ОЗУ байт, всего ОЗУ, выделено (commit) байт, предел commit)."""
    if IS_WIN:
        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        k32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return (ms.ullTotalPhys - ms.ullAvailPhys, ms.ullTotalPhys,
                ms.ullTotalPageFile - ms.ullAvailPageFile, ms.ullTotalPageFile)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return vm.total - vm.available, vm.total, vm.used + sw.used, vm.total + sw.total


def enable_privilege(name):
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
        ctypes.set_last_error(0)
        advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        k32.CloseHandle(token)


def purge_standby():
    """Чистит кэш Windows (standby list). Возвращает текст результата."""
    if not IS_WIN:
        return "кэш: только для Windows"
    if not is_admin():
        return "кэш Windows не чищу: нужен запуск от администратора"
    if not enable_privilege("SeProfileSingleProcessPrivilege"):
        return "кэш Windows: не удалось получить право на очистку"
    cmd = ctypes.c_int(MEMORY_PURGE_STANDBY_LIST)
    st = ntdll.NtSetSystemInformation(SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(cmd), ctypes.sizeof(cmd))
    return "кэш Windows очищен" if st == 0 else f"кэш Windows: ошибка 0x{st & 0xFFFFFFFF:08X}"


def trim_process(pid):
    """Выгружает из ОЗУ неиспользуемые страницы процесса. True/False."""
    if not IS_WIN:
        return False
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
    if not h:
        return False
    try:
        return bool(psapi.EmptyWorkingSet(h))
    finally:
        k32.CloseHandle(h)


# ================================================================ процессы
def name_patterns(text):
    return [p.strip().lower() for p in text.split(",") if p.strip()]


def game_processes(patterns):
    """[(pid, имя, в ОЗУ байт, выделено байт)] для процессов игры."""
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info["name"] or "")
            low = name.lower()
            if not any(pat in low for pat in patterns):
                continue
            mi = p.memory_info()
            ws = getattr(mi, "wset", mi.rss)
            private = getattr(mi, "private", getattr(mi, "vms", 0))
            out.append((p.info["pid"], name, ws, private))
        except Exception:             # процесс закрылся, нет доступа и т.п.
            continue
    out.sort(key=lambda x: -x[3])
    return out


def gb(x):
    return f"{x / 1024 ** 3:.1f} ГБ"


def mb(x):
    return f"{x / 1024 ** 2:.0f} МБ"


def clean(patterns, standby, log):
    before = memory_status()[0]
    procs = game_processes(patterns)
    if not procs:
        log("Процессы игры не найдены (проверь список имён)")
    ok = fail = 0
    for pid, name, ws, _ in procs:
        if trim_process(pid):
            ok += 1
        else:
            fail += 1
    if procs:
        msg = f"Выгружено из ОЗУ: {ok} процесс(ов)"
        if fail:
            msg += f", не удалось: {fail} (запусти от администратора)"
        log(msg)
    if standby:
        log(purge_standby())
    time.sleep(1.0)
    after = memory_status()[0]
    log(f"Освободилось: {gb(max(0, before - after))}")


# ================================================================ окно
def build_ram_tab(parent, key_bar=True):
    """Рисует чистильщик внутри parent. Возвращает функцию остановки. key_bar=False - без блока ключа
    (отдельная бесплатная программа PM)."""
    import queue
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    alive_flag = {"on": True}
    q = queue.Queue()
    log = lambda m: q.put(m)
    pad = {"padx": 8, "pady": 3}

    try:
        if key_bar:
            import licenseui
            kb = licenseui.build_key_bar(parent, lambda: None)
            kb.grid(row=99, column=0, columnspan=4, sticky="ew", **pad)
    except Exception:
        pass
    ram_var = tk.StringVar()
    commit_var = tk.StringVar()
    warn_var = tk.StringVar()
    ttk.Label(parent, textvariable=ram_var, style="Big.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=commit_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=warn_var, style="Warn.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", **pad)

    cols = ("name", "ram", "commit", "grow")
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=8)
    for c, t, w in zip(cols, ("Процесс", "В ОЗУ", "Выделено", "Рост за 10 мин"), (170, 80, 95, 125)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w" if c == "name" else "e")
    tree.grid(row=3, column=0, columnspan=4, sticky="ew", **pad)
    tree.tag_configure("odd", background="#19191e")

    ttk.Label(parent, text="Процессы игры:").grid(row=4, column=0, sticky="w", **pad)
    names_var = tk.StringVar(value=cfg["names"])
    ttk.Entry(parent, textvariable=names_var, width=40).grid(row=4, column=1, columnspan=3, sticky="w", **pad)

    auto_var = tk.BooleanVar(value=cfg["auto"])
    thr_var = tk.IntVar(value=cfg["threshold"])
    int_var = tk.IntVar(value=cfg["min_interval"])
    standby_var = tk.BooleanVar(value=cfg["standby"])
    row = ttk.Frame(parent)
    row.grid(row=5, column=0, columnspan=4, sticky="w", **pad)
    ttk.Checkbutton(row, text="Авто: когда ОЗУ занято больше", variable=auto_var).pack(side="left")
    ttk.Spinbox(row, from_=50, to=98, width=4, textvariable=thr_var).pack(side="left", padx=4)
    ttk.Label(row, text="%, не чаще раза в").pack(side="left")
    ttk.Spinbox(row, from_=1, to=60, width=4, textvariable=int_var).pack(side="left", padx=4)
    ttk.Label(row, text="мин").pack(side="left")
    ttk.Checkbutton(parent, text="Чистить ещё и кэш Windows (нужен запуск от администратора)",
                    variable=standby_var).grid(row=6, column=0, columnspan=4, sticky="w", **pad)

    busy = {"on": False, "last": 0.0}
    hist = []                                    # загрузка ОЗУ, % каждые 2 с (для графика на странице)
    cur = {"pct": 0.0, "used": 0, "total": 1, "cpct": 0.0}
    last = {"freed": 0, "t": 0.0}
    try:
        import widgets

        def ram_stats():
            gbf = 2.0 ** 30
            return {"active": False, "label": ("Авто: вкл" if auto_var.get() else "Вручную", "ok"),
                    "status": "Очищаю…" if busy["on"] else ("Автоочистка включена" if auto_var.get()
                                                          else "Только вручную"),
                    "busy": busy["on"], "pct": cur["pct"], "used": cur["used"] / gbf, "total": cur["total"] / gbf,
                    "cpct": cur["cpct"], "hist": list(hist), "last_t": last["t"],
                    "items": [("Освобождено", int(last["freed"] / 2 ** 20))]}
        widgets.register("ram", ram_stats)
    except Exception:
        pass

    def do_clean(reason):
        if busy["on"]:
            return
        busy["on"] = True
        busy["last"] = time.time()
        log(f"Очистка ({reason})...")

        def work():
            try:
                before = memory_status()[0]
                clean(name_patterns(names_var.get()), standby_var.get(), log)
                time.sleep(1.0)
                last["freed"], last["t"] = max(0, before - memory_status()[0]), time.time()
            except Exception as ex:
                log(f"Ошибка: {ex}")
            busy["on"] = False
        threading.Thread(target=work, daemon=True).start()

    ttk.Button(parent, text="Очистить сейчас", style="Accent.TButton", command=lambda: do_clean("вручную")).grid(
        row=7, column=0, columnspan=4, sticky="ew", **pad)

    logbox = tk.Text(parent, height=8, width=70, state="disabled", font=("Consolas", 9))
    logbox.grid(row=8, column=0, columnspan=4, **pad)

    history = {}   # pid -> [(время, выделено)]
    err_state = {"last": None}

    def persist():
        try:
            save_config({"names": names_var.get(), "auto": auto_var.get(),
                         "threshold": int(thr_var.get()), "min_interval": int(int_var.get()),
                         "standby": standby_var.get()})
        except (tk.TclError, ValueError):
            pass

    def refresh():
        try:
            used, total, commit, limit = memory_status()
            load = 100 * used / max(1, total)
            hist.append(load)
            del hist[:-150]
            cur.update(pct=load, used=used, total=total, cpct=100 * commit / max(1, limit))
            ram_var.set(f"ОЗУ: {gb(used)} из {gb(total)} ({load:.0f}%)")
            cload = 100 * commit / max(1, limit)
            commit_var.set(f"Выделено программами: {gb(commit)} из {gb(limit)} ({cload:.0f}%)")
            warn_var.set("Выделено почти до предела: поможет только перезапуск игры"
                         if cload > 90 else "")

            now = time.time()
            procs = game_processes(name_patterns(names_var.get()))
            tree.delete(*tree.get_children())
            alive = set()
            for pid, name, ws, private in procs:
                alive.add(pid)
                h = history.setdefault(pid, [])
                h.append((now, private))
                while h and now - h[0][0] > 600:
                    h.pop(0)
                grow = private - h[0][1] if len(h) > 1 else 0
                grow_txt = ("+" if grow >= 0 else "") + mb(grow) if now - h[0][0] > 30 else "..."
                tree.insert("", "end", values=(f"{name} ({pid})", mb(ws), mb(private), grow_txt),
                            tags=("odd",) if len(tree.get_children()) % 2 else ())
            for pid in list(history):
                if pid not in alive:
                    del history[pid]

            if auto_var.get() and procs and load > int(thr_var.get()) \
                    and now - busy["last"] > 60 * int(int_var.get()):
                do_clean(f"ОЗУ {load:.0f}%")
        except (tk.TclError, ValueError):
            pass
        except Exception as ex:
            msg = f"Ошибка обновления: {ex}"
            if msg != err_state["last"]:
                err_state["last"] = msg
                log(msg)
                write_error_file()
        persist()
        if alive_flag["on"]:
            parent.after(2000, refresh)

    def pump():
        while not q.empty():
            msg = q.get()
            logbox.configure(state="normal")
            logbox.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        if alive_flag["on"]:
            parent.after(100, pump)

    log("Запущено от администратора" if is_admin()
        else "Без прав администратора: кэш Windows чистить не получится")
    def stop():
        alive_flag["on"] = False

    parent.after(0, refresh)
    parent.after(100, pump)
    return stop


def run_gui():
    import tkinter as tk
    root = tk.Tk()
    root.title("Чистильщик ОЗУ для Majestic")
    root.resizable(False, False)
    try:
        import theme
        theme.apply(root)
    except Exception:
        pass
    stop = build_ram_tab(root)
    root.mainloop()
    stop()


ERROR_PATH = os.path.join(app_dir(), "ram_cleaner_error.txt")


def write_error_file():
    try:
        with open(ERROR_PATH, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
    except Exception:
        pass


def fatal(text):
    """Показываем ошибку окном Windows (без tkinter - вдруг дело в нём)."""
    write_error_file()
    msg = f"{text}\n\nПодробности: {ERROR_PATH}"
    if IS_WIN:
        ctypes.windll.user32.MessageBoxW(None, msg, "Чистильщик ОЗУ - ошибка", 0x10)
    else:
        print(msg)


def main():
    if psutil is None:
        fatal(f"Не найдена библиотека psutil ({PSUTIL_ERROR}).\n"
              "Выполни: py -m pip install psutil и собери exe заново.")
        return
    if "--once" in sys.argv:                 # одна очистка из консоли
        c = load_config()
        clean(name_patterns(c["names"]), c["standby"], print)
        return
    try:
        import tkinter  # noqa: F401
    except Exception as ex:
        fatal(f"Не найден tkinter ({ex}).\nПереустанови Python с галочкой "
              "'tcl/tk and IDLE' и собери exe заново.")
        return
    run_gui()


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        fatal(f"Ошибка запуска: {ex}")
