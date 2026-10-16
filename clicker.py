# -*- coding: utf-8 -*-
"""Кликер: быстро нажимает выбранную клавишу (по умолчанию пробел) или кнопку мыши.
Скорость настраивается (нажатий в секунду). Два режима: вкл/выкл горячей клавишей или «пока держишь
горячую клавишу». Нажатия идут через SendInput со скан-кодами (как у остальных ботов), паузы точные:
на время работы включается таймер Windows с шагом 1 мс."""
import os
import sys
import json
import time
import queue
import threading

import switches
from sewing_bot import key_pressed, vk_name, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}
CHOICES = ["Space", "E", "F", "Q", "R", "Enter", "ЛКМ", "ПКМ"]
MAX_CPS = 200


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "clicker_config.json")


def default_config():
    return {"toggle_vk": 0x73,            # F4
            "key": "Space", "cps": 30, "hold_ms": 8, "mode": "toggle", "focus_only": True}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except Exception:
        pass
    cfg["cps"] = max(1, min(MAX_CPS, int(cfg["cps"])))
    if cfg["key"] not in CHOICES:
        cfg["key"] = "Space"
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def press(inp, key, hold):
    """Одно нажатие: вниз, пауза hold (с), вверх."""
    if key in ("ЛКМ", "ПКМ"):
        down, up = (AF.MOUSEEVENTF_LEFTDOWN, AF.MOUSEEVENTF_LEFTUP) if key == "ЛКМ" else \
                   (AF.MOUSEEVENTF_RIGHTDOWN, AF.MOUSEEVENTF_RIGHTUP)
        inp.mouse(down)
        wait_until(time.perf_counter() + hold)
        inp.mouse(up)
    else:
        inp.key(key, True)
        wait_until(time.perf_counter() + hold)
        inp.key(key, False)


def wait_until(t_end):
    """Точное ожидание: основную часть спим, последние ~1.5 мс отдаём процессор."""
    while True:
        r = t_end - time.perf_counter()
        if r <= 0:
            return
        time.sleep(r - 0.0015 if r > 0.003 else 0)


def build_clicker_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    st = {"presses": 0, "cps": 0.0}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    status_var, info_var = tk.StringVar(value="Выключен"), tk.StringVar()
    ttk.Label(parent, textvariable=status_var, style="Status.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)

    def toggle():
        if not switches.is_on("click") and not ctl["active"]:
            log("Кликер выключен: включи его переключателем наверху страницы «Тест»")
            return
        ctl["active"] = not ctl["active"]
    ttk.Button(parent, text="Старт / стоп", style="Accent.TButton", command=toggle).grid(
        row=2, column=0, columnspan=4, sticky="ew", **pad)

    key_var = tk.StringVar(value=vk_name(cfg["toggle_vk"]))
    ttk.Label(parent, text="Горячая клавиша:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Label(parent, textvariable=key_var, style="Key.TLabel", width=16).grid(row=3, column=1, sticky="w", **pad)

    def bind_key():
        ctl["binding"] = True
        key_var.set("нажми клавишу…")

        def wait():
            end = time.time() + 6
            while time.time() < end and not ctl["quit"]:
                for vk in range(8, 255):
                    if vk in (0x01, 0x02, 0x04):
                        continue
                    if key_pressed(vk):
                        q.put(("bound", vk))
                        return
                time.sleep(0.02)
            q.put(("bound", None))
        threading.Thread(target=wait, daemon=True).start()
    ttk.Button(parent, text="Назначить", command=bind_key).grid(row=3, column=2, sticky="w", **pad)

    box = ttk.LabelFrame(parent, text="Что и как быстро нажимать")
    box.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    key_choice = tk.StringVar(value=cfg["key"])
    cps_var = tk.IntVar(value=cfg["cps"])
    hold_var = tk.IntVar(value=cfg["hold_ms"])
    mode_var = tk.StringVar(value=cfg["mode"])
    focus_var = tk.BooleanVar(value=cfg["focus_only"])

    r = ttk.Frame(box)
    r.pack(fill="x", padx=6, pady=4)
    ttk.Label(r, text="Клавиша:").pack(side="left")
    ttk.Combobox(r, textvariable=key_choice, values=CHOICES, width=8, state="readonly").pack(side="left", padx=(4, 16))
    ttk.Label(r, text="Нажатий в секунду:").pack(side="left")
    spin = ttk.Spinbox(r, from_=1, to=MAX_CPS, width=5, textvariable=cps_var)
    spin.pack(side="left", padx=4)
    guard = {"on": False}
    scale = ttk.Scale(r, from_=1, to=MAX_CPS, orient="horizontal", length=260)
    scale.pack(side="left", padx=8)
    scale.set(cfg["cps"])

    def on_scale(v):
        if guard["on"]:
            return
        guard["on"] = True
        cps_var.set(int(round(float(v))))
        guard["on"] = False
        save()
    scale.configure(command=on_scale)

    def on_spin(*a):
        if guard["on"]:
            return
        try:
            v = max(1, min(MAX_CPS, int(cps_var.get())))
        except (tk.TclError, ValueError):
            return
        guard["on"] = True
        scale.set(v)
        guard["on"] = False
        save()
    cps_var.trace_add("write", on_spin)

    r2 = ttk.Frame(box)
    r2.pack(fill="x", padx=6, pady=2)
    ttk.Label(r2, text="Режим:").pack(side="left")
    ttk.Radiobutton(r2, text="включил и жмёт, выключил - стоп", value="toggle", variable=mode_var,
                    command=lambda: save()).pack(side="left", padx=8)
    ttk.Radiobutton(r2, text="жмёт, пока держишь горячую клавишу", value="hold", variable=mode_var,
                    command=lambda: save()).pack(side="left", padx=8)
    r3 = ttk.Frame(box)
    r3.pack(fill="x", padx=6, pady=2)
    ttk.Label(r3, text="Удержание одного нажатия, мс:").pack(side="left")
    ttk.Spinbox(r3, from_=1, to=100, width=5, textvariable=hold_var, command=lambda: save()).pack(side="left", padx=4)
    ttk.Label(r3, text="(при высокой скорости само сокращается, чтобы нажатия не слипались)",
              style="Muted.TLabel").pack(side="left", padx=6)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).pack(anchor="w", padx=8, pady=(2, 6))
    key_choice.trace_add("write", lambda *a: save())

    def save():
        try:
            cfg["cps"] = max(1, min(MAX_CPS, int(cps_var.get())))
            cfg["hold_ms"] = max(1, min(100, int(hold_var.get())))
        except (tk.TclError, ValueError):
            pass
        cfg["key"] = key_choice.get() if key_choice.get() in CHOICES else "Space"
        cfg["mode"] = mode_var.get()
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)

    logbox = tk.Text(parent, height=8, width=80, state="disabled", font=("Consolas", 9))
    logbox.grid(row=5, column=0, columnspan=4, **pad)

    # ---------------- рабочий поток
    def worker():
        import ctypes
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        inp = AF.Input2()
        prev = True
        was = False
        next_t = time.perf_counter()
        win_t, win_n = time.time(), 0
        try:
            while not ctl["quit"]:
                if not switches.is_on("click"):
                    ctl["active"] = False
                    prev = True
                elif not ctl["binding"]:
                    k = key_pressed(cfg["toggle_vk"])
                    if cfg["mode"] == "hold":
                        ctl["active"] = k
                    elif k and not prev:
                        ctl["active"] = not ctl["active"]
                    prev = k
                if ctl["active"] != was:
                    was = ctl["active"]
                    next_t = time.perf_counter()
                    if was:
                        log(f"Кликер включён: {cfg['key']}, {cfg['cps']} нажатий/с")
                    else:
                        log("Кликер выключен")
                if not ctl["active"]:
                    BUSY["on"] = False
                    st["cps"] = 0.0
                    ctl["status"] = "Выключен" if switches.is_on("click") else "Выключен (тумблер)"
                    time.sleep(0.02)
                    continue
                if SEWING_BUSY["on"]:
                    ctl["status"] = "Пауза: работает швейный бот"
                    time.sleep(0.1)
                    continue
                if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                                 for t in ("gta", "rage", "majestic", "grand theft")):
                    ctl["status"] = "Пауза: окно игры не активно"
                    time.sleep(0.1)
                    continue
                BUSY["on"] = True
                ctl["status"] = f"Жму {cfg['key']}: {cfg['cps']}/с"
                period = 1.0 / cfg["cps"]
                hold = min(cfg["hold_ms"] / 1000.0, period * 0.6)
                press(inp, cfg["key"], hold)
                st["presses"] += 1
                win_n += 1
                if len(inp.log) > 2000:
                    inp.log.clear()
                now = time.time()
                if now - win_t >= 1.0:
                    st["cps"] = win_n / (now - win_t)
                    win_t, win_n = now, 0
                next_t += period
                if time.perf_counter() - next_t > 0.1:       # сильно отстали (пауза) - не пытаемся наверстать
                    next_t = time.perf_counter()
                wait_until(next_t)
        finally:
            BUSY["on"] = False
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()

    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", time.strftime("%H:%M:%S ") + m[1] + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                elif m[0] == "bound":
                    ctl["binding"] = False
                    if m[1]:
                        cfg["toggle_vk"] = m[1]
                        save_config(cfg)
                    key_var.set(vk_name(cfg["toggle_vk"]))
        except queue.Empty:
            pass
        except tk.TclError:
            return
        status_var.set(ctl["status"])
        info_var.set(f"Нажатий всего: {st['presses']}   ·   сейчас: {st['cps']:.0f} в секунду"
                     + ("" if switches.is_on("click") else "   ·   включи бота переключателем «Кликер» наверху страницы"))
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"],
                "items": [("Нажатий", st["presses"]), ("В секунду", int(st["cps"]))]}
    widgets.register("click", provider)
    parent.after(200, pump)

    def stop():
        ctl["quit"] = True
    return stop
