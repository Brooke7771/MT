# -*- coding: utf-8 -*-
"""Гриль: жарит мясо на гриле по кругу. Встань у гриля и включи бота - дальше он сам:
G (меню взаимодействия) -> «Пожарить на гриле» (клавиша 1 или щелчок) -> ждёт, пока мясо прожарится
(уведомление «Вы приготовили Жареное мясо!» внизу) -> снова G.

Что выяснено по записи 20261025_211150 (1920x1080, 4 порции):
  - меню после G появляется через ~0,25-0,3 с по центру экрана: кнопки «Пожарить на гриле» и «Забрать гриль»,
    фон размыт; кнопка под мышью розовая, текст белый в обоих случаях - меню узнаётся по белому тексту
    «Пожарить на гриле» (образец TPL, совпадение 0,86-1,0 при открытом меню, вне меню <= 0,02);
  - клавиша 1 выбирает первую кнопку (меню закрывается за ~0,1 с), щелчок по кнопке - тоже;
  - жарка ~2,9-3,4 с, потом внизу по центру уведомление с зелёной точкой; уведомления копятся стопкой
    (новое снизу, старые уезжают вверх и гаснут) - «готово» = зелёного в колонке точек стало заметно больше
    минимума с начала жарки;
  - игрок жал G через ~0,3-0,5 с после уведомления.
Клавиша 1 без меню выбрала бы слот 1 (оружие/предмет), поэтому её жмём, только когда меню видно."""
import os
import sys
import json
import time
import queue
import zlib
import base64
import random
import threading

import numpy as np
import cv2

import switches
from sewing_bot import key_pressed, vk_name, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}
BASE_W, BASE_H = 1920, 1080
MENU_BOX = (700, 410, 980, 480)        # текст «Пожарить на гриле» (1080p)
FRY_BTN = (843, 445)                   # середина кнопки - для щелчка
DOTS = (750, 900, 820, 1060)           # колонка зелёных точек уведомлений внизу
CHAT = (300, 150, 700, 400)            # фон с чатом: при открытом меню размыт
MENU_ON = 0.6                          # совпадение белого текста с образцом
MENU_WAIT = 1.5                        # ждать меню после G
NOTE_RISE = 35                         # зелёных пикселей больше минимума - новое уведомление
COOK_MAX = 6.0                         # уведомления нет дольше - жарка не удалась
MAX_MISS = 3                           # меню не открылось подряд - стоп
MAX_FAIL = 3                           # жарка без уведомления подряд - стоп (мясо кончилось?)
TPL_B64 = ("eNpjYBgFo2AUDB7AeICgEuYD1DCFQYKwEg4grv+AVwk7YSUg9yYY4FfyAKTkY0vywwpnewOGw/IMf/4YHP/wAV2J4Z8z6cV/wEqsG/78Nzz+cQay"
           "r4HqE4x5cg4Yt7nzAJUwNhw4bHjMmAdZCcgUYzbbxs1s/jz2DId5Gw4c3HzMCFkJQwJYiTz7ZzZvHvuG//ZAJduOmf9BczLIlDZjtmgeA6AlQCV7"
           "jpkxYCjhyTljzFYMUpIAdIvNGWNMJXPOPDPuSeYxbACZ8tzijHEPuhLDn2eSP/9IBrrlMNAt/wxuGP7AGoZwFs7Ip44SxtFcOgpGAW0AAIgRXSs=")
TPL = np.unpackbits(np.frombuffer(zlib.decompress(base64.b64decode(TPL_B64)), np.uint8))[:70 * 280].reshape(70, 280)
METHODS = {"key": "клавишей 1", "click": "щелчком по кнопке"}


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "grill_config.json")


def default_config():
    return {"toggle_vk": 0x63,            # Numpad 3
            "method": "key", "pause": 0.4, "limit": 0, "focus_only": True}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except Exception:
        pass
    if cfg["method"] not in METHODS:
        cfg["method"] = "key"
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ====================================================================== зрение
def norm(frame):
    """Экран другого размера -> 1920x1080 (интерфейс игры масштабируется по высоте и стоит по центру)."""
    H, W = frame.shape[:2]
    if (W, H) == (BASE_W, BASE_H):
        return frame
    k = BASE_H / H
    f = cv2.resize(frame, (int(round(W * k)), BASE_H))
    x0 = (f.shape[1] - BASE_W) // 2
    if x0 >= 0:
        return f[:, x0:x0 + BASE_W]
    return cv2.copyMakeBorder(f, 0, 0, -x0, BASE_W - f.shape[1] + x0, cv2.BORDER_CONSTANT)


def menu_score(f):
    x0, y0, x1, y1 = MENU_BOX
    m = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY) > 170
    t = TPL.astype(bool)
    return float((m & t).sum()) / max(1, int((m | t).sum()))


def menu_on(f):
    return menu_score(f) > MENU_ON


def blurred(f):
    """Открыто какое-то меню взаимодействия (фон размыт): у резкого экрана ~300-1000, у размытого < 30."""
    x0, y0, x1, y1 = CHAT
    return cv2.Laplacian(cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), cv2.CV_32F).var() < 30


def note_px(f):
    """Сколько зелёного в колонке точек уведомлений (одна точка ~150 px)."""
    x0, y0, x1, y1 = DOTS
    hsv = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    return int(((hsv[..., 0] > 45) & (hsv[..., 0] < 85) & (hsv[..., 1] > 120) & (hsv[..., 2] > 120)).sum())


# ====================================================================== вкладка
def build_grill_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import widgets

    cfg = load_config()
    q = queue.Queue()
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}
    st = {"done": 0, "fail": 0, "t_on": None, "sec": 0.0, "last": None}
    pad = {"padx": 6, "pady": 3}

    def log(m):
        q.put(("log", m))

    status_var, info_var = tk.StringVar(value="Выключен"), tk.StringVar()
    ttk.Label(parent, textvariable=status_var, style="Status.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)

    def toggle():
        if not switches.is_on("grill") and not ctl["active"]:
            log("Гриль выключен: включи его переключателем наверху страницы «Тест»")
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

    box = ttk.LabelFrame(parent, text="Настройки")
    box.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    meth_var = tk.StringVar(value=METHODS[cfg["method"]])
    pause_var = tk.StringVar(value=f"{cfg['pause']:g}")
    limit_var = tk.StringVar(value=str(cfg["limit"]))
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    r = ttk.Frame(box)
    r.pack(fill="x", padx=6, pady=4)
    ttk.Label(r, text="«Пожарить на гриле» выбирать:").pack(side="left")
    ttk.Combobox(r, textvariable=meth_var, values=list(METHODS.values()), width=21, state="readonly").pack(side="left", padx=(4, 16))
    ttk.Label(r, text="Пауза после готовности, с:").pack(side="left")
    ttk.Spinbox(r, from_=0.1, to=5, increment=0.1, width=5, textvariable=pause_var).pack(side="left", padx=4)
    r2 = ttk.Frame(box)
    r2.pack(fill="x", padx=6, pady=2)
    ttk.Label(r2, text="Сколько порций пожарить (0 - пока не кончится мясо):").pack(side="left")
    ttk.Spinbox(r2, from_=0, to=9999, width=6, textvariable=limit_var).pack(side="left", padx=4)
    ttk.Checkbutton(box, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).pack(anchor="w", padx=8, pady=(2, 6))
    ttk.Label(parent, text="Встань у гриля (чтобы по G открывалось «Пожарить на гриле») и включи бота. Он сам остановится, "
                           "если меню гриля не открывается или мясо перестало жариться.",
              style="Muted.TLabel", wraplength=640, justify="left").grid(row=5, column=0, columnspan=4, sticky="w", **pad)

    def save(*a):
        cfg["method"] = next((k for k, v in METHODS.items() if v == meth_var.get()), "key")
        try:
            cfg["pause"] = max(0.1, min(5.0, float(pause_var.get().replace(",", "."))))
        except ValueError:
            pass
        try:
            cfg["limit"] = max(0, int(limit_var.get()))
        except ValueError:
            pass
        cfg["focus_only"] = bool(focus_var.get())
        save_config(cfg)
    for v in (meth_var, pause_var, limit_var):
        v.trace_add("write", save)

    logbox = tk.Text(parent, height=8, width=80, state="disabled", font=("Consolas", 9))
    logbox.grid(row=6, column=0, columnspan=4, **pad)

    # ---------------- рабочий поток
    def worker():
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        inp = AF.Input2()
        scr = AF.Screen()

        def grab():
            return norm(scr.grab())

        def sleep(sec):
            """Пауза; False - бота выключили."""
            end = time.time() + sec
            while time.time() < end:
                if ctl["quit"] or not ctl["active"]:
                    return False
                time.sleep(0.02)
            return True

        def wait_for(cond, sec):
            end = time.time() + sec
            while time.time() < end:
                if ctl["quit"] or not ctl["active"]:
                    return None
                f = grab()
                if cond(f):
                    return f
                time.sleep(0.03)
            return False

        def stop(why):
            log(why)
            ctl["active"] = False

        def cycle():
            """Одна порция: меню (открыть G, если закрыто) -> выбрать -> дождаться уведомления."""
            f = grab()
            if not menu_on(f):
                if blurred(f):                                 # открыто чужое меню (Esc не жмём: без меню это пауза игры)
                    stop("Открыто другое меню, не гриль. Закрой его, встань у гриля и включи бота снова")
                    return
                ctl["status"] = "Открываю меню (G)"
                inp.tap("G", 80)
                f = wait_for(menu_on, MENU_WAIT)
                if f is None:
                    return
                if f is False:
                    st["miss"] = st.get("miss", 0) + 1
                    g = grab()
                    if blurred(g) and not menu_on(g):
                        stop("По G открылось другое меню, не гриль. Закрой его, встань у гриля и включи бота снова")
                        return
                    if st["miss"] >= MAX_MISS:
                        stop("Меню гриля не открывается - подойди к грилю и включи бота снова")
                    return
            st["miss"] = 0
            if not sleep(random.uniform(0.12, 0.25)):
                return
            if not menu_on(grab()):                            # меню закрылось само - начинаем заново
                return
            ctl["status"] = "Жарю мясо"
            if cfg["method"] == "click":
                inp.click(*FRY_BTN)
            else:
                inp.tap("1", 70)
            t0 = time.time()
            low = [None]                                       # минимум зелёного с закрытия меню

            def ready(g):
                n = note_px(g)
                low[0] = n if low[0] is None else min(low[0], n)
                return n > low[0] + NOTE_RISE
            if wait_for(lambda g: not menu_on(g), 1.0) is None:
                return
            got = wait_for(ready, COOK_MAX)
            if got is None:
                return
            if got is False:
                st["fail"] += 1
                st["fails"] = st.get("fails", 0) + 1
                log(f"Уведомления «приготовили» нет за {COOK_MAX:g} с")
                if st["fails"] >= MAX_FAIL:
                    stop("Мясо не жарится несколько раз подряд - похоже, кончилось. Бот остановлен")
                return
            st["fails"] = 0
            st["done"] += 1
            st["last"] = time.time() - t0
            log(f"Готово: порция {st['done']} за {st['last']:.1f} с")
            if cfg["limit"] and st["done"] >= cfg["limit"]:
                stop(f"Пожарено {st['done']} - сколько просили. Бот остановлен")
                return
            ctl["status"] = "Готово, дальше"
            sleep(cfg["pause"] * random.uniform(0.85, 1.2))

        prev = was = False
        while not ctl["quit"]:
            if not switches.is_on("grill"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                if was:
                    st.update(done=0, fail=0, miss=0, fails=0, t_on=time.time())
                    log("Гриль включён")
                else:
                    log("Гриль выключен")
                    if st["t_on"]:
                        st["sec"] += time.time() - st["t_on"]
                    st["t_on"] = None
            if not ctl["active"]:
                BUSY["on"] = False
                ctl["status"] = "Выключен" if switches.is_on("grill") else "Выключен (тумблер)"
                time.sleep(0.1)
                continue
            if SEWING_BUSY["on"]:
                ctl["status"] = "Пауза: работает швейный бот"
                time.sleep(0.2)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            BUSY["on"] = True
            try:
                cycle()
            except Exception as ex:
                log(f"Ошибка: {ex}")
                time.sleep(1)
            if len(inp.log) > 2000:
                inp.log.clear()
        BUSY["on"] = False

    threading.Thread(target=worker, daemon=True).start()

    def rate():
        sec = st["sec"] + (time.time() - st["t_on"] if st["t_on"] else 0)
        return int(st["done"] * 3600 / sec) if sec > 60 else 0

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
        info_var.set(f"Пожарено: {st['done']}   ·   в час: {rate()}"
                     + (f"   ·   последняя за {st['last']:.1f} с" if st["last"] else "")
                     + ("" if switches.is_on("grill") else "   ·   включи бота переключателем «Гриль» наверху страницы"))
        if not ctl["quit"]:
            parent.after(200, pump)

    def provider():
        return {"active": ctl["active"], "status": ctl["status"],
                "items": [("Пожарено", st["done"]), ("В час", rate())]}
    widgets.register("grill", provider)
    parent.after(200, pump)

    def stop_all():
        ctl["quit"] = True
    return stop_all
