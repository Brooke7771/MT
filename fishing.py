# -*- coding: utf-8 -*-
"""
Бот рыбалки Majestic RP.

Мини-игра "тяните рыбу в противоположное направление": на воде рядом с брызгами висит
плашка с рыбкой. Если плашка слева от брызг - рыба тянет влево, держим D; справа - держим A.
Дополнительно (по сохранённым образцам): сам забрасывает удочку, подсекает и забирает
или отпускает улов.
"""
import os
import sys
import json
import time
import queue
import threading

import numpy as np
import cv2

import switches

from sewing_bot import key_pressed, vk_name, SKIP_VKS, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}            # True, пока идёт рыбалка (анти-АФК в это время молчит)
CONFIG_PATH = os.path.join(AF.app_dir(), "fishing_config.json")
BASE_W, BASE_H = 1920, 1080
ZONE = (500, 250, 1420, 760)                 # где искать плашку на воде (для 1920x1080)
BAR_BAND = (660, 862, 1210, 890)             # полоска прогресса с рыбкой внизу экрана


def default_config():
    return {"toggle_vk": 0x6F,               # Numpad /
            "invert": False, "hold_after_lost": 1.2, "zone": None,
            "auto_cast": False, "auto_hook": False, "after_catch": "none",   # none | keep | release
            "cast_key": "Space", "hook_key": "Space",
            "tpl": {"cast": None, "hook": None, "keep": None, "release": None},
            "threshold": 0.8, "focus_only": True}


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        tpl = d.pop("tpl", {})
        cfg.update(d)
        cfg["tpl"].update(tpl)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)
    except Exception:
        pass


# ================================================================ зрение

def find_marker(img):
    """Ищет плашку с рыбкой: тёмная вытянутая рыбка на ровном светлом квадрате.
    img - BGR область поиска. -> (cx, cy, size) или None"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    b, g, r = [img[:, :, i].astype(np.int16) for i in range(3)]
    mx = np.maximum(np.maximum(b, g), r)
    mn = np.minimum(np.minimum(b, g), r)
    light = (mx > 115) & ((mx - mn < 50) | ((g - np.maximum(r, b)) > 15))
    dark = (mx < 70).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(dark, connectivity=8)
    H, W = gray.shape
    best = None
    for k in range(1, n):
        x, y, w, h, a = st[k]
        if not (7 <= w <= 45 and 3 <= h <= 28 and 1.3 <= w / h <= 3.8 and 0.3 < a / (w * h) < 0.85):
            continue
        p = max(3, int(w * 0.35))
        X0, Y0, X1, Y1 = x - p, y - p, x + w + p, y + h + p
        if X0 < 0 or Y0 < 0 or X1 > W or Y1 > H:
            continue
        ring = np.ones((Y1 - Y0, X1 - X0), bool)
        ring[p:p + h, p:p + w] = False
        lr = light[Y0:Y1, X0:X1][ring]
        if lr.mean() < 0.85:
            continue
        gr = gray[Y0:Y1, X0:X1][ring]
        if gr.std() > 22:                       # фон плашки ровный, брызги - пёстрые
            continue
        score = lr.mean() - gr.std() / 100
        if best is None or score > best[0]:
            best = (score, x + w / 2, y + h / 2, w * 1.6)
    return None if best is None else best[1:]


def splash_x(img, cx, cy, size):
    """Центр брызг рядом с плашкой. -> x или None"""
    H, W = img.shape[:2]
    half = int(size * 6)
    x0, x1 = max(0, int(cx - half)), min(W, int(cx + half))
    y0, y1 = max(0, int(cy - size * 2.5)), min(H, int(cy + size * 2.5))
    win = img[y0:y1, x0:x1]
    gray = cv2.cvtColor(win, cv2.COLOR_BGR2GRAY).astype(np.int16)
    b, g, r = [win[:, :, i].astype(np.int16) for i in range(3)]
    spread = np.maximum(np.maximum(b, g), r) - np.minimum(np.minimum(b, g), r)
    bright = (gray > 150) & (spread < 40)
    mx0, mx1 = int(cx - size * 0.8) - x0, int(cx + size * 0.8) - x0
    my0, my1 = int(cy - size * 0.8) - y0, int(cy + size * 0.8) - y0
    bright[max(0, my0):max(0, my1), max(0, mx0):max(0, mx1)] = False
    cols = bright.sum(axis=0).astype(float)
    if cols.sum() < size * 3:
        return None
    k = max(3, int(size)) | 1
    sm = np.convolve(cols, np.ones(k) / k, "same")
    return x0 + float(np.argmax(sm))


def scaled(box, W, H):
    sx, sy = W / BASE_W, H / BASE_H
    return int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)


def minigame_on(screen):
    """Полоска прогресса с белой рыбкой внизу экрана видна?"""
    H, W = screen.shape[:2]
    x0, y0, x1, y1 = scaled(BAR_BAND, W, H)
    band = screen[y0:y1, x0:x1]
    return int((band.min(axis=2) > 200).sum()) > 40 * (W / BASE_W) ** 2


def direction(screen, zone):
    """-1: рыба тянет влево, +1: вправо, None: не видно."""
    x0, y0, x1, y1 = zone
    z = screen[y0:y1, x0:x1]
    m = find_marker(z)
    if not m:
        return None
    sx = splash_x(z, *m)
    if sx is None:
        return None
    d = m[0] - sx
    if abs(d) < m[2] * 0.4:
        return None
    return -1 if d < 0 else 1


# ================================================================ окно
def build_fishing_tab(parent):
    import tkinter as tk
    from tkinter import ttk
    import theme as T

    cfg = load_config()
    q = queue.Queue()
    log = lambda m: q.put(m)
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен", "info": ""}
    pad = {"padx": 6, "pady": 3}

    def persist():
        save_config(cfg)

    def toggle():
        if not switches.is_on("fish") and not ctl["active"]:
            log("Рыбалка выключена в боковом меню - сначала включи её там")
            return
        ctl["active"] = not ctl["active"]

    status_var, info_var = tk.StringVar(value="Выключен"), tk.StringVar()
    st_lbl = ttk.Label(parent, textvariable=status_var, style="Status.TLabel")
    st_lbl.grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, textvariable=info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    ttk.Button(parent, text="Старт / стоп", style="Accent.TButton",
               command=lambda: toggle()).grid(row=2, column=0, columnspan=4, sticky="ew", **pad)
    key_var = tk.StringVar(value=vk_name(cfg["toggle_vk"]))
    ttk.Label(parent, text="Горячая клавиша:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Label(parent, textvariable=key_var, style="Key.TLabel", width=16).grid(row=3, column=1, sticky="w", **pad)
    ttk.Button(parent, text="Назначить", command=lambda: bind_key()).grid(row=3, column=2, sticky="w", **pad)

    inv_var = tk.BooleanVar(value=cfg["invert"])
    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    hold_var = tk.StringVar(value=str(cfg["hold_after_lost"]))
    ttk.Checkbutton(parent, text="Поменять A и D местами (если бот тянет не туда)", variable=inv_var,
                    command=lambda: save()).grid(row=4, column=0, columnspan=4, sticky="w", **pad)
    ttk.Checkbutton(parent, text="Только когда активно окно игры", variable=focus_var,
                    command=lambda: save()).grid(row=5, column=0, columnspan=4, sticky="w", **pad)
    hr = ttk.Frame(parent)
    hr.grid(row=6, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(hr, text="Если плашку не видно, держать прежнюю клавишу ещё").pack(side="left")
    ttk.Spinbox(hr, from_=0, to=5, increment=0.1, width=4, textvariable=hold_var, command=lambda: save()).pack(side="left", padx=4)
    ttk.Label(hr, text="сек").pack(side="left")
    zr = ttk.Frame(parent)
    zr.grid(row=7, column=0, columnspan=4, sticky="w", **pad)
    zone_var = tk.StringVar()
    ttk.Button(zr, text="Выбрать зону воды", command=lambda: pick_zone()).pack(side="left")
    ttk.Button(zr, text="Сбросить", command=lambda: (cfg.update(zone=None), save(), show_zone())).pack(side="left", padx=4)
    ttk.Label(zr, textvariable=zone_var, style="Muted.TLabel").pack(side="left", padx=6)

    auto = ttk.LabelFrame(parent, text="Полный цикл (по образцам, по желанию)")
    auto.grid(row=8, column=0, columnspan=4, sticky="ew", **pad)
    cast_var = tk.BooleanVar(value=cfg["auto_cast"])
    hook_var = tk.BooleanVar(value=cfg["auto_hook"])
    after_var = tk.StringVar(value={"none": "Ничего не делать", "keep": "Забрать себе",
                                    "release": "Отпустить"}[cfg["after_catch"]])
    tpl_vars = {}

    def tpl_row(r, key, check_text, var, hint):
        ttk.Checkbutton(auto, text=check_text, variable=var, command=lambda: save()).grid(row=r, column=0, sticky="w", **pad)
        ttk.Button(auto, text="Захватить образец", command=lambda: capture(key, hint)).grid(row=r, column=1, **pad)
        tpl_vars[key] = tk.StringVar()
        ttk.Label(auto, textvariable=tpl_vars[key], style="Muted.TLabel").grid(row=r, column=2, sticky="w", **pad)

    tpl_row(0, "cast", "Сам забрасывать удочку", cast_var, "Обведи подсказку «Забросить удочку»")
    tpl_row(1, "hook", "Сам подсекать", hook_var, "Обведи подсказку «Подсечь рыбу»")
    ttk.Label(auto, text="После улова:").grid(row=2, column=0, sticky="w", **pad)
    acb = ttk.Combobox(auto, textvariable=after_var, state="readonly", width=18,
                       values=["Ничего не делать", "Забрать себе", "Отпустить"])
    acb.grid(row=2, column=1, sticky="w", **pad)
    acb.bind("<<ComboboxSelected>>", lambda e: save())
    for r, key, hint in ((3, "keep", "Обведи кнопку «Забрать себе»"), (4, "release", "Обведи кнопку «Отпустить»")):
        ttk.Button(auto, text=f"Образец «{'Забрать себе' if key == 'keep' else 'Отпустить'}»",
                   command=lambda k=key, h=hint: capture(k, h)).grid(row=r, column=1, **pad)
        tpl_vars[key] = tk.StringVar()
        ttk.Label(auto, textvariable=tpl_vars[key], style="Muted.TLabel").grid(row=r, column=2, sticky="w", **pad)
    ttk.Label(auto, text="Образцы захватываются так же, как значки в анти-АФК: дождись, когда подсказка\n"
                         "или кнопка появится в игре, нажми «Захватить» и обведи её мышкой.",
              style="Muted.TLabel", justify="left").grid(row=5, column=0, columnspan=3, sticky="w", **pad)

    logbox = tk.Text(parent, height=8, width=70, state="disabled", font=("Consolas", 9))
    logbox.grid(row=9, column=0, columnspan=4, sticky="ew", **pad)

    def save():
        try:
            cfg["hold_after_lost"] = max(0.0, float(hold_var.get()))
        except ValueError:
            pass
        cfg.update(invert=inv_var.get(), focus_only=focus_var.get(), auto_cast=cast_var.get(),
                   auto_hook=hook_var.get(),
                   after_catch={"Ничего не делать": "none", "Забрать себе": "keep", "Отпустить": "release"}[after_var.get()])
        persist()

    def show_zone():
        zone_var.set("своя зона" if cfg["zone"] else "по умолчанию: середина экрана")
        for k, v in tpl_vars.items():
            v.set("сохранён" if cfg["tpl"].get(k) else "нет образца")

    def grab_region(hint):
        top = parent.winfo_toplevel()
        was = top.attributes("-topmost")
        top.withdraw()
        time.sleep(0.35)
        try:
            return AF.pick_region(top, hint)
        except Exception as ex:
            log(f"Снимок экрана не получился: {ex}")
        finally:
            top.deiconify()
            top.attributes("-topmost", was)

    def pick_zone():
        r = grab_region("Обведи участок воды, где появляются брызги и плашка с рыбкой")
        if r:
            img, x, y = r
            cfg["zone"] = [x, y, x + img.shape[1], y + img.shape[0]]
            persist()
            show_zone()

    def capture(key, hint):
        r = grab_region(hint)
        if r:
            img, x, y = r
            cfg["tpl"][key] = AF.new_template(img, x, y, f"fish_{key}")
            persist()
            show_zone()

    bind_state = {"armed": False, "t0": 0}

    def bind_key():
        ctl["binding"] = True
        bind_state.update(armed=False, t0=time.time())
        key_var.set("нажми клавишу...")
        parent.after(30, poll_bind)

    def poll_bind():
        down = [vk for vk in range(1, 255) if vk not in SKIP_VKS and key_pressed(vk)]
        if time.time() - bind_state["t0"] > 10:
            key_var.set(vk_name(cfg["toggle_vk"]))
            ctl["binding"] = False
            return
        if not bind_state["armed"]:
            bind_state["armed"] = not down
        elif down:
            if down[0] != 0x1B:
                cfg["toggle_vk"] = down[0]
                persist()
            key_var.set(vk_name(cfg["toggle_vk"]))
            ctl["binding"] = False
            return
        parent.after(30, poll_bind)

    # ---------------------------------------------------------------- поток бота
    def worker():
        if AF.IS_WIN:
            try:
                import ctypes
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        inp = AF.Input2()
        scr = None
        held = None
        last_seen = 0.0
        prev = False
        was = False
        last_try = {}
        catches = 0
        in_game = False

        def hold(key):
            nonlocal held
            if held == key:
                return
            if held:
                inp.key(held, False)
            if key:
                inp.key(key, True)
            held = key

        while not ctl["quit"]:
            if not switches.is_on("fish"):
                ctl["active"] = False
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    ctl["active"] = not ctl["active"]
                prev = k
            if ctl["active"] != was:
                was = ctl["active"]
                hold(None)
                log("Рыбалка включена" if was else "Рыбалка выключена")
            if not ctl["active"] or SEWING_BUSY["on"]:
                hold(None)
                BUSY["on"] = False
                ctl["status"] = ("Отключён в меню" if not switches.is_on("fish") else "Выключен") \
                    if not ctl["active"] else "Пауза: работает швейный бот"
                time.sleep(0.1)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                hold(None)
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            try:
                if scr is None:
                    scr = AF.Screen()
                shot = scr.grab()
                H, W = shot.shape[:2]
                now = time.time()
                if minigame_on(shot):
                    BUSY["on"] = True
                    if not in_game:
                        in_game = True
                        log("Клюнула! Тяну")
                    zone = cfg["zone"] or scaled(ZONE, W, H)
                    d = direction(shot, zone)
                    if d is not None:
                        last_seen = now
                        want = "D" if d < 0 else "A"
                        if cfg["invert"]:
                            want = "A" if want == "D" else "D"
                        if want != held:
                            log(f"Рыба тянет {'влево' if d < 0 else 'вправо'} - держу {want}")
                        hold(want)
                    elif now - last_seen > cfg["hold_after_lost"]:
                        hold(None)
                    ctl["status"] = "Тяну рыбу"
                    ctl["info"] = f"держу: {held or '-'}   ·   поймано за сессию: {catches}"
                    time.sleep(0.03)
                    continue
                hold(None)
                if in_game:
                    in_game = False
                    catches += 1
                    log("Мини-игра закончилась")
                BUSY["on"] = True
                ctl["status"] = "Жду поклёвку"
                # полный цикл по образцам
                thr = cfg["threshold"]
                act = cfg["after_catch"]
                if act != "none" and cfg["tpl"].get(act) and now - last_try.get(act, 0) > 2:
                    s, x, y = AF.find(shot, cfg["tpl"][act], thr)
                    if x is not None:
                        last_try[act] = now
                        time.sleep(0.4)
                        inp.click(x, y)
                        log("Забрал улов" if act == "keep" else "Отпустил рыбу")
                        time.sleep(1.0)
                        continue
                if cfg["auto_hook"] and cfg["tpl"].get("hook") and now - last_try.get("hook", 0) > 1.5:
                    s, x, y = AF.find(shot, cfg["tpl"]["hook"], thr, around=60)
                    if x is not None:
                        last_try["hook"] = now
                        inp.tap(cfg["hook_key"], 80)
                        log("Подсёк")
                        continue
                if cfg["auto_cast"] and cfg["tpl"].get("cast") and now - last_try.get("cast", 0) > 4:
                    s, x, y = AF.find(shot, cfg["tpl"]["cast"], thr, around=60)
                    if x is not None:
                        last_try["cast"] = now
                        time.sleep(0.8)
                        inp.tap(cfg["cast_key"], 80)
                        log("Забросил удочку")
                ctl["info"] = f"поймано за сессию: {catches}"
                time.sleep(0.15)
            except Exception as ex:
                hold(None)
                log(f"Ошибка: {ex}")
                time.sleep(1)
        hold(None)
        BUSY["on"] = False

    def pump():
        try:
            while True:
                m = q.get_nowait()
                logbox.configure(state="normal")
                logbox.insert("end", time.strftime("%H:%M:%S ") + m + "\n")
                logbox.see("end")
                logbox.configure(state="disabled")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            s = ctl["status"]
            status_var.set(s)
            st_lbl.configure(style="StatusOn.TLabel" if s in ("Тяну рыбу", "Жду поклёвку") else "Status.TLabel")
            info_var.set(ctl["info"] if ctl["active"] else f"Включить: кнопка ниже или {vk_name(cfg['toggle_vk'])}")
        except tk.TclError:
            return
        parent.after(150, pump)

    def stop():
        ctl["quit"] = True
        ctl["active"] = False

    show_zone()
    threading.Thread(target=worker, daemon=True).start()
    parent.after(150, pump)
    return stop
