# -*- coding: utf-8 -*-
"""
Анти-АФК для Majestic RP.

Что умеет:
  - раз в N..M секунд нажимает случайную клавишу из пресета (у каждой своя частота
    и время удержания), умеет поворачивать камеру мышью;
  - правило "парных клавиш" как в gta.ahk: вторую клавишу пары (Down) нельзя нажать,
    пока снова не нажата первая (Up);
  - раз в N минут: выровнять пары до чётного числа нажатий и нажать клавишу (например 1);
  - еда и вода: по сохранённым картинкам находит на экране значок голода / жажды и
    либо жмёт клавишу быстрого слота, либо открывает инвентарь, ищет нужный предмет
    (бургер, воду, ИРП...) и использует его;
  - молчит, пока окно игры не активно и пока работает швейный бот.
"""
import os
import sys
import json
import time
import queue
import random
import ctypes
import threading

import numpy as np
import cv2

import switches

from sewing_bot import (Input, InputI, KeyBdInput, MouseInput, key_pressed, vk_name,
                        SKIP_VKS, BUSY as SEWING_BUSY)


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "antiafk_config.json")
TEMPL_DIR = os.path.join(app_dir(), "antiafk_templates")
IS_WIN = sys.platform == "win32"

# ================================================================ клавиши
# имя -> (скан-код, расширенная клавиша)
KEYS = {}
for ch, sc in zip("QWERTYUIOP", range(0x10, 0x1A)):
    KEYS[ch] = (sc, False)
for ch, sc in zip("ASDFGHJKL", range(0x1E, 0x27)):
    KEYS[ch] = (sc, False)
for ch, sc in zip("ZXCVBNM", range(0x2C, 0x33)):
    KEYS[ch] = (sc, False)
for n, sc in zip("1234567890", range(0x02, 0x0C)):
    KEYS[n] = (sc, False)
for n in range(1, 11):
    KEYS[f"F{n}"] = (0x3A + n, False)
KEYS.update({"F11": (0x57, False), "F12": (0x58, False), "Space": (0x39, False),
             "Enter": (0x1C, False), "Esc": (0x01, False), "Tab": (0x0F, False),
             "Backspace": (0x0E, False), "Shift": (0x2A, False), "Ctrl": (0x1D, False),
             "Alt": (0x38, False), "CapsLock": (0x3A, False),
             "Up": (0x48, True), "Down": (0x50, True), "Left": (0x4B, True), "Right": (0x4D, True),
             "Insert": (0x52, True), "Delete": (0x53, True), "Home": (0x47, True), "End": (0x4F, True),
             "PageUp": (0x49, True), "PageDown": (0x51, True)})
for n, sc in zip("0123456789", (0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49)):
    KEYS[f"Num{n}"] = (sc, False)
KEYS.update({"Num+": (0x4E, False), "Num-": (0x4A, False), "Num*": (0x37, False)})
MOUSE_ACTIONS = ["Мышь: камера влево-вправо", "Мышь: камера вверх-вниз", "ЛКМ", "ПКМ"]
ALL_ACTIONS = list(KEYS) + MOUSE_ACTIONS

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010


class Input2:
    """Нажатия клавиш и мыши через SendInput (dry=True - только для проверки логики)."""

    def __init__(self, dry=False):
        self.live = IS_WIN and not dry
        self.log = []

    def _send(self, inp):
        if self.live:
            ctypes.windll.user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))

    def key(self, name, down):
        self.log.append((name, down))
        sc, ext = KEYS[name]
        extra = ctypes.c_ulong(0)
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0) | (0 if down else KEYEVENTF_KEYUP)
        ii = InputI()
        ii.ki = KeyBdInput(0, sc, flags, 0, ctypes.pointer(extra))
        self._send(Input(ctypes.c_ulong(1), ii))

    def tap(self, name, hold_ms=80):
        self.key(name, True)
        time.sleep(hold_ms / 1000)
        self.key(name, False)

    def mouse(self, flags, dx=0, dy=0):
        self.log.append(("mouse", flags, dx, dy))
        extra = ctypes.c_ulong(0)
        ii = InputI()
        ii.mi = MouseInput(dx, dy, 0, flags, 0, ctypes.pointer(extra))
        self._send(Input(ctypes.c_ulong(0), ii))

    def move_to(self, x, y):
        self.log.append(("move_to", x, y))
        if self.live:
            ctypes.windll.user32.SetCursorPos(int(x), int(y))

    def click(self, x, y, right=False, double=False):
        self.move_to(x, y)
        time.sleep(0.08)
        down, up = (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if right else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
        for n in range(2 if double else 1):
            self.mouse(down)
            time.sleep(0.05)
            self.mouse(up)
            if double and n == 0:
                time.sleep(0.09)

    def action(self, name, hold_ms):
        """Одно действие из пресета."""
        if name in KEYS:
            self.tap(name, hold_ms)
        elif name == "ЛКМ":
            self.mouse(MOUSEEVENTF_LEFTDOWN)
            time.sleep(hold_ms / 1000)
            self.mouse(MOUSEEVENTF_LEFTUP)
        elif name == "ПКМ":
            self.mouse(MOUSEEVENTF_RIGHTDOWN)
            time.sleep(hold_ms / 1000)
            self.mouse(MOUSEEVENTF_RIGHTUP)
        else:
            dist = random.randint(60, 220) * random.choice((-1, 1))
            steps = 12
            vertical = "вверх" in name
            for back in (False, True):
                for _ in range(steps):
                    d = int(dist / steps) * (-1 if back else 1)
                    self.mouse(MOUSEEVENTF_MOVE, 0 if vertical else d, d if vertical else 0)
                    time.sleep(0.012)
                time.sleep(hold_ms / 1000 / 2)


# ================================================================ пресеты и настройки
def A(key, weight=1, hold=(60, 140)):
    return {"key": key, "weight": weight, "hold": list(hold)}


PRESETS_BUILTIN = {
    "Как в gta.ahk": {
        "interval": [30, 60],
        "actions": [A("Up"), A("Down"), A("W"), A("A"), A("S"), A("D")],
        "pairs": [["Up", "Down"]], "no_repeat": False,
        "periodic": {"enabled": True, "minutes": 60, "key": "1", "even_pairs": True}},
    "Шаги WASD": {
        "interval": [40, 90],
        "actions": [A("W", 2, (150, 400)), A("S", 2, (150, 400)), A("A", 1, (100, 300)), A("D", 1, (100, 300))],
        "pairs": [], "no_repeat": True,
        "periodic": {"enabled": False, "minutes": 60, "key": "1", "even_pairs": False}},
    "Камера + шаги": {
        "interval": [45, 120],
        "actions": [A("Мышь: камера влево-вправо", 3, (200, 500)), A("W", 1, (120, 300)),
                    A("S", 1, (120, 300)), A("Space", 1, (60, 100))],
        "pairs": [], "no_repeat": True,
        "periodic": {"enabled": False, "minutes": 60, "key": "1", "even_pairs": False}},
}

NEEDS = {"hunger": "Голод", "thirst": "Жажда"}


def default_config():
    return {
        "preset_name": "Как в gta.ahk",
        "preset": json.loads(json.dumps(PRESETS_BUILTIN["Как в gta.ahk"])),
        "user_presets": {},
        "toggle_vk": 0x6A,            # Numpad *
        "focus_only": True,
        "window_titles": "GTA5, Grand Theft Auto, RAGE, Majestic",
        "overlay": False,
        "telegram": False,
        "food": {
            "enabled": False, "check_sec": 10, "cooldown_sec": 300, "threshold": 0.80,
            "inv_key": "I", "close_key": "I", "open_delay": 1.0,
            "gesture": "double",       # double | right_use
            "use_template": None,
            "needs": {"hunger": {"icon": None, "mode": "key", "key": "3", "items": []},
                      "thirst": {"icon": None, "mode": "key", "key": "4", "items": []}},
        },
    }


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        food = data.pop("food", {})
        cfg.update(data)
        needs = food.pop("needs", {})
        cfg["food"].update(food)
        for k in NEEDS:
            cfg["food"]["needs"][k].update(needs.get(k, {}))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# ================================================================ картинки-образцы
def imread(path):
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def imwrite(path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(path)
    return ok


def new_template(img, x, y, prefix):
    """Сохраняет образец. Возвращает {'file', 'x', 'y', 'w', 'h'} (координаты на экране)."""
    os.makedirs(TEMPL_DIR, exist_ok=True)
    name = f"{prefix}_{int(time.time() * 1000) % 10 ** 10}.png"
    imwrite(os.path.join(TEMPL_DIR, name), img)
    return {"file": name, "x": int(x), "y": int(y), "w": int(img.shape[1]), "h": int(img.shape[0])}


_tcache = {}


def template_img(t):
    if not t:
        return None
    p = os.path.join(TEMPL_DIR, t["file"])
    if p not in _tcache:
        _tcache[p] = imread(p)
    return _tcache[p]


def find(screen, t, threshold, around=None):
    """Ищет образец на снимке экрана. around=пиксели вокруг места, где его сохраняли
    (None = по всему экрану). -> (score, cx, cy) или (score, None, None)."""
    tpl = template_img(t)
    if tpl is None or screen is None:
        return 0.0, None, None
    H, W = screen.shape[:2]
    x0 = y0 = 0
    area = screen
    if around is not None:
        x0 = max(0, t["x"] - around)
        y0 = max(0, t["y"] - around)
        x1 = min(W, t["x"] + t["w"] + around)
        y1 = min(H, t["y"] + t["h"] + around)
        area = screen[y0:y1, x0:x1]
    if area.shape[0] < tpl.shape[0] or area.shape[1] < tpl.shape[1]:
        return 0.0, None, None
    res = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < threshold:
        return float(mx), None, None
    return float(mx), x0 + loc[0] + tpl.shape[1] // 2, y0 + loc[1] + tpl.shape[0] // 2


class Screen:
    """Снимки основного монитора (mss создаётся в том потоке, где используется)."""

    def __init__(self):
        import mss
        self.sct = mss.mss()
        self.mon = self.sct.monitors[1]

    def grab(self):
        return np.ascontiguousarray(np.asarray(self.sct.grab(self.mon))[:, :, :3])


def foreground_title():
    if not IS_WIN:
        return ""
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value
    except Exception:
        return ""


# ================================================================ движок
class Engine:
    """Логика анти-АФК без окна (так её можно проверить отдельно)."""

    def __init__(self, cfg, inp, log, clock=time.time):
        self.cfg, self.inp, self.log, self.clock = cfg, inp, log, clock
        self.reset()

    def reset(self):
        now = self.clock()
        self.counts = {}
        self.allowed = {}              # вторая клавиша пары доступна?
        self.last = None
        self.recent = []               # последние нажатые клавиши (для интерфейса)
        self.next_at = now + self._interval()
        self.cycle_at = now
        self.paused_since = None
        self.next_check = now
        self.cooldown = {k: 0 for k in NEEDS}
        self.done = 0

    def _interval(self):
        lo, hi = self.cfg["preset"]["interval"]
        lo, hi = max(1, float(lo)), max(1, float(hi))
        self.span = random.uniform(min(lo, hi), max(lo, hi))     # для кольца обратного отсчёта
        return self.span

    def _pair_second(self):
        return {p[1]: p[0] for p in self.cfg["preset"].get("pairs", []) if len(p) == 2}

    def _pair_first(self):
        return {p[0]: p[1] for p in self.cfg["preset"].get("pairs", []) if len(p) == 2}

    def choose(self):
        acts = [a for a in self.cfg["preset"]["actions"] if a.get("weight", 1) > 0]
        second = self._pair_second()
        ok = []
        for a in acts:
            k = a["key"]
            if k in second and not self.allowed.get(k, True):
                continue
            if self.cfg["preset"].get("no_repeat") and k == self.last and len(acts) > 1:
                continue
            ok.append(a)
        if not ok:
            return None
        return random.choices(ok, weights=[a.get("weight", 1) for a in ok])[0]

    def press(self, a):
        k = a["key"]
        lo, hi = a.get("hold", [60, 140])
        self.inp.action(k, random.uniform(min(lo, hi), max(lo, hi)))
        self.counts[k] = self.counts.get(k, 0) + 1
        self.last = k
        self.recent = (self.recent + [k])[-7:]
        first, second = self._pair_first(), self._pair_second()
        if k in first:
            self.allowed[first[k]] = True
        if k in second:
            self.allowed[k] = False
        self.done += 1

    def periodic(self):
        p = self.cfg["preset"]["periodic"]
        if p.get("even_pairs"):
            for a, b in [x for x in self.cfg["preset"].get("pairs", []) if len(x) == 2]:
                for k in (a, b):
                    if self.counts.get(k, 0) % 2:
                        self.press({"key": k, "hold": [60, 120]})
                        time.sleep(0.5)
        if p.get("key") in KEYS or p.get("key") in MOUSE_ACTIONS:
            time.sleep(0.5)
            self.inp.action(p["key"], 90)
        self.log(f"Цикл {p['minutes']} мин: нажал {p.get('key')}"
                 + (" (стрелки выровнены)" if p.get("even_pairs") else ""))
        self.counts.clear()
        self.allowed.clear()
        self.cycle_at = self.clock()

    def pause(self, now):
        if self.paused_since is None:
            self.paused_since = now

    def resume(self, now):
        if self.paused_since is not None:
            gap = now - self.paused_since
            self.next_at += gap            # на паузе таймеры стоят
            self.cycle_at += gap
            self.paused_since = None

    def step(self, screen=None):
        """Один шаг движка. screen - функция, возвращающая снимок экрана (для еды)."""
        now = self.clock()
        food = self.cfg["food"]
        if food["enabled"] and screen is not None and now >= self.next_check:
            self.next_check = now + max(2, food["check_sec"])
            self.check_food(screen)
            now = self.clock()
        p = self.cfg["preset"]["periodic"]
        if p.get("enabled") and now - self.cycle_at >= max(1, p["minutes"]) * 60:
            self.periodic()
            self.next_at = self.clock() + self._interval()
            return
        if now >= self.next_at:
            a = self.choose()
            if a:
                self.press(a)
            self.next_at = self.clock() + self._interval()

    # ---------------------------------------------------------------- еда и вода
    def check_food(self, screen):
        food = self.cfg["food"]
        img = None
        for need, title in NEEDS.items():
            n = food["needs"][need]
            if not n.get("icon") or self.clock() < self.cooldown[need]:
                continue
            if img is None:
                img = screen()
            score, x, y = find(img, n["icon"], food["threshold"], around=120)
            if x is None:
                continue
            self.log(f"{title}: вижу значок (совпадение {score:.2f})")
            ok = self.feed(need, screen)
            self.cooldown[need] = self.clock() + (food["cooldown_sec"] if ok else 60)
            img = None

    def feed(self, need, screen):
        food = self.cfg["food"]
        n = food["needs"][need]
        title = NEEDS[need]
        if n["mode"] == "key":
            if n["key"] not in KEYS:
                self.log(f"{title}: не задана клавиша")
                return False
            self.inp.tap(n["key"], 90)
            self.log(f"{title}: нажал {n['key']}")
            return True
        if not n["items"]:
            self.log(f"{title}: не добавлено ни одного предмета")
            return False
        self.inp.tap(food["inv_key"], 90)
        time.sleep(max(0.3, food["open_delay"]))
        img = screen()
        used = None
        for item in n["items"]:
            score, x, y = find(img, item, food["threshold"])
            if x is None:
                continue
            if food["gesture"] == "right_use":
                self.inp.click(x, y, right=True)
                time.sleep(0.5)
                s2, bx, by = find(screen(), food.get("use_template"), food["threshold"])
                if bx is None:
                    self.log(f"{title}: не нашёл кнопку «Использовать»")
                    break
                self.inp.click(bx, by)
            else:
                self.inp.click(x, y, double=True)
            used = item.get("name", "предмет")
            break
        time.sleep(0.6)
        if food["close_key"] in KEYS:
            self.inp.tap(food["close_key"], 90)
        if used:
            self.log(f"{title}: использовал «{used}»")
            return True
        self.log(f"{title}: в инвентаре не нашёл ни одного предмета из списка")
        return False


# ================================================================ выбор области на экране
def pick_region(root, hint):
    """Полноэкранный снимок, на котором мышкой выделяешь область.
    -> (картинка BGR, x, y) в пикселях экрана или None."""
    import tkinter as tk
    from PIL import Image, ImageTk, ImageEnhance
    scr = Screen()
    shot = scr.grab()
    H, W = shot.shape[:2]
    top = tk.Toplevel(root)
    top.attributes("-topmost", True)
    top.overrideredirect(True)
    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
    top.geometry(f"{sw}x{sh}+0+0")
    kx, ky = W / sw, H / sh
    pil = Image.fromarray(cv2.cvtColor(shot, cv2.COLOR_BGR2RGB)).resize((sw, sh))
    dim = ImageTk.PhotoImage(ImageEnhance.Brightness(pil).enhance(0.55))
    cv = tk.Canvas(top, width=sw, height=sh, highlightthickness=0, cursor="crosshair")
    cv.pack()
    cv.create_image(0, 0, image=dim, anchor="nw")
    cv.create_rectangle(20, 20, 20 + 560, 70, fill="#111115", outline="#e3263d")
    cv.create_text(36, 45, anchor="w", fill="#ffffff", font=("Segoe UI", 11, "bold"),
                   text=hint + "   (Esc - отмена)")
    st = {"x": 0, "y": 0, "r": None, "res": None}

    def down(e):
        st["x"], st["y"] = e.x, e.y
        st["r"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3d55", width=2)

    def move(e):
        if st["r"]:
            cv.coords(st["r"], st["x"], st["y"], e.x, e.y)

    def up(e):
        x0, x1 = sorted((st["x"], e.x))
        y0, y1 = sorted((st["y"], e.y))
        if x1 - x0 > 4 and y1 - y0 > 4:
            X0, Y0, X1, Y1 = int(x0 * kx), int(y0 * ky), int(x1 * kx), int(y1 * ky)
            st["res"] = (shot[Y0:Y1, X0:X1].copy(), X0, Y0)
        top.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    top.bind("<Escape>", lambda e: top.destroy())
    top.focus_force()
    top.grab_set()
    root.wait_window(top)
    return st["res"]


# ================================================================ окно
def build_antiafk_tab(parent):
    import tkinter as tk
    from tkinter import ttk, simpledialog, messagebox
    import theme as T

    cfg = load_config()
    q = queue.Queue()
    log = lambda m: q.put(("log", m))
    ctl = {"active": False, "quit": False, "binding": False, "status": "Выключен"}

    pad = {"padx": 6, "pady": 3}
    parent.columnconfigure(0, weight=1)
    parent.columnconfigure(1, weight=1)

    def persist():
        save_config(cfg)

    def ival(v, d):
        try:
            return float(v.get())
        except Exception:
            return d

    # ================================================================ левая колонка: нажатия
    L = ttk.Frame(parent)
    L.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    status_var = tk.StringVar(value="Выключен")
    status_lbl = ttk.Label(L, textvariable=status_var, style="Status.TLabel")
    status_lbl.grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    info_var = tk.StringVar(value="")
    ttk.Label(L, textvariable=info_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    ttk.Button(L, text="Старт / стоп", style="Accent.TButton", command=lambda: toggle()).grid(
        row=2, column=0, columnspan=4, sticky="ew", **pad)

    key_var = tk.StringVar(value=vk_name(cfg["toggle_vk"]))
    ttk.Label(L, text="Горячая клавиша:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Label(L, textvariable=key_var, style="Key.TLabel", width=16).grid(row=3, column=1, sticky="w", **pad)
    ttk.Button(L, text="Назначить", command=lambda: bind_key()).grid(row=3, column=2, sticky="w", **pad)

    focus_var = tk.BooleanVar(value=cfg["focus_only"])
    titles_var = tk.StringVar(value=cfg["window_titles"])
    ttk.Checkbutton(L, text="Только когда активно окно игры:", variable=focus_var,
                    command=lambda: save_misc()).grid(row=4, column=0, columnspan=2, sticky="w", **pad)
    e = ttk.Entry(L, textvariable=titles_var, width=30)
    e.grid(row=4, column=2, columnspan=2, sticky="ew", **pad)
    e.bind("<FocusOut>", lambda ev: save_misc())
    overlay_var = tk.BooleanVar(value=cfg["overlay"])
    tg_var = tk.BooleanVar(value=cfg["telegram"])
    ttk.Checkbutton(L, text="Показывать статус поверх игры", variable=overlay_var,
                    command=lambda: (save_misc(), overlay_update(force=True))).grid(
        row=5, column=0, columnspan=2, sticky="w", **pad)
    ttk.Checkbutton(L, text="Сообщать в Telegram о еде/воде", variable=tg_var,
                    command=lambda: save_misc()).grid(row=5, column=2, columnspan=2, sticky="w", **pad)

    pf = ttk.LabelFrame(L, text="Пресет нажатий")
    pf.grid(row=6, column=0, columnspan=4, sticky="nsew", **pad)
    preset_var = tk.StringVar(value=cfg["preset_name"])
    pcb = ttk.Combobox(pf, textvariable=preset_var, state="readonly", width=24)
    pcb.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
    ttk.Button(pf, text="Сохранить как...", command=lambda: save_preset()).grid(row=0, column=2, **pad)
    ttk.Button(pf, text="Удалить", command=lambda: delete_preset()).grid(row=0, column=3, **pad)

    lo_var, hi_var = tk.StringVar(), tk.StringVar()
    ir = ttk.Frame(pf)
    ir.grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(ir, text="Нажатие каждые").pack(side="left")
    ttk.Spinbox(ir, from_=1, to=3600, width=5, textvariable=lo_var, command=lambda: save_preset_fields()).pack(side="left", padx=4)
    ttk.Label(ir, text="–").pack(side="left")
    ttk.Spinbox(ir, from_=1, to=3600, width=5, textvariable=hi_var, command=lambda: save_preset_fields()).pack(side="left", padx=4)
    ttk.Label(ir, text="сек (случайно в этих пределах)").pack(side="left")

    tv = ttk.Treeview(pf, columns=("k", "w", "h"), show="headings", height=7, selectmode="browse")
    for c, h_, w in (("k", "Клавиша / действие", 210), ("w", "Частота", 70), ("h", "Удержание, мс", 110)):
        tv.heading(c, text=h_)
        tv.column(c, width=w, anchor="w" if c == "k" else "center")
    tv.grid(row=2, column=0, columnspan=4, sticky="ew", **pad)
    tv.tag_configure("odd", background=T.FIELD_ALT)
    br = ttk.Frame(pf)
    br.grid(row=3, column=0, columnspan=4, sticky="w", **pad)
    ttk.Button(br, text="+ Добавить", command=lambda: edit_action(None)).pack(side="left")
    ttk.Button(br, text="Изменить", command=lambda: edit_action(selected())).pack(side="left", padx=4)
    ttk.Button(br, text="Удалить", command=lambda: delete_action()).pack(side="left")
    tv.bind("<Double-1>", lambda e: edit_action(selected()))

    pairs_var = tk.StringVar()
    norep_var = tk.BooleanVar()
    ttk.Label(pf, text="Парные клавиши:").grid(row=4, column=0, sticky="w", **pad)
    pe = ttk.Entry(pf, textvariable=pairs_var, width=20)
    pe.grid(row=4, column=1, sticky="w", **pad)
    pe.bind("<FocusOut>", lambda ev: save_preset_fields())
    ttk.Label(pf, text="например Up-Down: Down только после Up", style="Muted.TLabel").grid(
        row=4, column=2, columnspan=2, sticky="w", **pad)
    ttk.Checkbutton(pf, text="Не повторять одну клавишу два раза подряд", variable=norep_var,
                    command=lambda: save_preset_fields()).grid(row=5, column=0, columnspan=4, sticky="w", **pad)

    per_en, per_min, per_key, per_even = tk.BooleanVar(), tk.StringVar(), tk.StringVar(), tk.BooleanVar()
    prow = ttk.Frame(pf)
    prow.grid(row=6, column=0, columnspan=4, sticky="w", **pad)
    ttk.Checkbutton(prow, text="Каждые", variable=per_en, command=lambda: save_preset_fields()).pack(side="left")
    ttk.Spinbox(prow, from_=1, to=600, width=4, textvariable=per_min, command=lambda: save_preset_fields()).pack(side="left", padx=4)
    ttk.Label(prow, text="мин нажимать").pack(side="left")
    kcb = ttk.Combobox(prow, textvariable=per_key, values=ALL_ACTIONS, width=8, state="readonly")
    kcb.pack(side="left", padx=4)
    kcb.bind("<<ComboboxSelected>>", lambda e: save_preset_fields())
    ttk.Checkbutton(pf, text="Перед этим выровнять парные клавиши до чётного числа нажатий",
                    variable=per_even, command=lambda: save_preset_fields()).grid(
        row=7, column=0, columnspan=4, sticky="w", **pad)

    logbox = tk.Text(L, height=7, width=60, state="disabled", font=("Consolas", 9))
    logbox.grid(row=7, column=0, columnspan=4, sticky="ew", **pad)

    # ================================================================ правая колонка: еда и вода
    R = ttk.LabelFrame(parent, text="Еда и вода")
    R.grid(row=0, column=1, sticky="nsew")
    food = cfg["food"]
    fen = tk.BooleanVar(value=food["enabled"])
    ttk.Checkbutton(R, text="Следить за голодом и жаждой", variable=fen,
                    command=lambda: save_food()).grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    fr = ttk.Frame(R)
    fr.grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    chk_var = tk.StringVar(value=str(food["check_sec"]))
    cd_var = tk.StringVar(value=str(food["cooldown_sec"]))
    thr_var = tk.StringVar(value=str(int(food["threshold"] * 100)))
    ttk.Label(fr, text="Проверять каждые").pack(side="left")
    ttk.Spinbox(fr, from_=2, to=600, width=4, textvariable=chk_var, command=lambda: save_food()).pack(side="left", padx=4)
    ttk.Label(fr, text="сек, после еды ждать").pack(side="left")
    ttk.Spinbox(fr, from_=10, to=3600, increment=10, width=5, textvariable=cd_var, command=lambda: save_food()).pack(side="left", padx=4)
    ttk.Label(fr, text="сек").pack(side="left")
    fr2 = ttk.Frame(R)
    fr2.grid(row=2, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(fr2, text="Точность распознавания, %:").pack(side="left")
    ttk.Spinbox(fr2, from_=50, to=99, width=4, textvariable=thr_var, command=lambda: save_food()).pack(side="left", padx=4)
    ttk.Label(fr2, text="(меньше - находит легче, но может ошибаться)", style="Muted.TLabel").pack(side="left")

    need_ui = {}
    row = 3
    for need, title in NEEDS.items():
        n = food["needs"][need]
        box = ttk.LabelFrame(R, text=title)
        box.grid(row=row, column=0, columnspan=4, sticky="ew", **pad)
        row += 1
        ivar = tk.StringVar()
        ttk.Button(box, text="Захватить значок", command=lambda k=need: capture_icon(k)).grid(row=0, column=0, **pad)
        ttk.Button(box, text="Проверить", command=lambda k=need: test_icon(k)).grid(row=0, column=1, **pad)
        ttk.Label(box, textvariable=ivar, style="Muted.TLabel").grid(row=0, column=2, columnspan=2, sticky="w", **pad)
        mode = tk.StringVar(value="Клавиша быстрого слота" if n["mode"] == "key" else "Через инвентарь")
        mcb = ttk.Combobox(box, textvariable=mode, state="readonly", width=24,
                           values=["Клавиша быстрого слота", "Через инвентарь"])
        mcb.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        kv = tk.StringVar(value=n["key"])
        kcb2 = ttk.Combobox(box, textvariable=kv, values=list(KEYS), width=6, state="readonly")
        kcb2.grid(row=1, column=2, sticky="w", **pad)
        mcb.bind("<<ComboboxSelected>>", lambda e: save_food())
        kcb2.bind("<<ComboboxSelected>>", lambda e: save_food())
        items = tk.Listbox(box, height=3, bg=T.FIELD, fg=T.TEXT, selectbackground=T.ACCENT_DIM,
                           highlightthickness=1, highlightbackground=T.BORDER, relief="flat",
                           font=("Segoe UI", 9), activestyle="none")
        items.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        ib = ttk.Frame(box)
        ib.grid(row=2, column=2, columnspan=2, sticky="nw", **pad)
        ttk.Button(ib, text="+ Предмет", command=lambda k=need: add_item(k)).pack(fill="x")
        ttk.Button(ib, text="Убрать", command=lambda k=need: del_item(k)).pack(fill="x", pady=2)
        ttk.Button(ib, text="Выше", command=lambda k=need: move_item(k)).pack(fill="x")
        need_ui[need] = {"icon": ivar, "mode": mode, "key": kv, "items": items}

    inv = ttk.LabelFrame(R, text="Инвентарь")
    inv.grid(row=row, column=0, columnspan=4, sticky="ew", **pad)
    inv_key = tk.StringVar(value=food["inv_key"])
    close_key = tk.StringVar(value=food["close_key"])
    delay_var = tk.StringVar(value=str(food["open_delay"]))
    gest = tk.StringVar(value="Двойной клик по предмету" if food["gesture"] == "double"
                        else "ПКМ по предмету + «Использовать»")
    ttk.Label(inv, text="Открыть:").grid(row=0, column=0, sticky="w", **pad)
    c1 = ttk.Combobox(inv, textvariable=inv_key, values=list(KEYS), width=6, state="readonly")
    c1.grid(row=0, column=1, sticky="w", **pad)
    ttk.Label(inv, text="Закрыть:").grid(row=0, column=2, sticky="w", **pad)
    c2 = ttk.Combobox(inv, textvariable=close_key, values=list(KEYS), width=6, state="readonly")
    c2.grid(row=0, column=3, sticky="w", **pad)
    ttk.Label(inv, text="Ждать открытия, сек:").grid(row=1, column=0, columnspan=2, sticky="w", **pad)
    ttk.Spinbox(inv, from_=0.3, to=5, increment=0.1, width=5, textvariable=delay_var,
                command=lambda: save_food()).grid(row=1, column=2, sticky="w", **pad)
    gcb = ttk.Combobox(inv, textvariable=gest, state="readonly", width=32,
                       values=["Двойной клик по предмету", "ПКМ по предмету + «Использовать»"])
    gcb.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
    use_var = tk.StringVar()
    ttk.Button(inv, text="Захватить «Использовать»", command=lambda: capture_use()).grid(row=3, column=0, columnspan=2, sticky="w", **pad)
    ttk.Label(inv, textvariable=use_var, style="Muted.TLabel").grid(row=3, column=2, columnspan=2, sticky="w", **pad)
    for c in (c1, c2, gcb):
        c.bind("<<ComboboxSelected>>", lambda e: save_food())
    ttk.Label(R, text="Как настроить: дождись, когда в игре появится значок голода или жажды, нажми\n"
                      "«Захватить значок» и обведи его мышкой. Для инвентаря открой его и обведи\n"
                      "мышкой каждый предмет, который можно использовать: бургер, воду, ИРП.",
              style="Muted.TLabel", justify="left").grid(row=row + 1, column=0, columnspan=4, sticky="w", **pad)

    # ================================================================ логика окна
    def all_presets():
        d = dict(PRESETS_BUILTIN)
        d.update(cfg["user_presets"])
        return d

    def load_preset_fields():
        p = cfg["preset"]
        lo_var.set(str(p["interval"][0]))
        hi_var.set(str(p["interval"][1]))
        pairs_var.set(", ".join("-".join(x) for x in p.get("pairs", [])))
        norep_var.set(bool(p.get("no_repeat")))
        per = p["periodic"]
        per_en.set(bool(per.get("enabled")))
        per_min.set(str(per.get("minutes", 60)))
        per_key.set(per.get("key", "1"))
        per_even.set(bool(per.get("even_pairs")))
        tv.delete(*tv.get_children())
        for n_, a in enumerate(p["actions"]):
            tv.insert("", "end", iid=str(n_), values=(a["key"], a.get("weight", 1), f"{a['hold'][0]}–{a['hold'][1]}"),
                      tags=("odd",) if n_ % 2 else ())
        pcb.configure(values=list(all_presets()))

    def save_preset_fields():
        p = cfg["preset"]
        p["interval"] = [max(1, ival(lo_var, 30)), max(1, ival(hi_var, 60))]
        pairs = []
        for part in pairs_var.get().split(","):
            ab = [x.strip() for x in part.split("-")]
            if len(ab) == 2:
                ks = [next((k for k in KEYS if k.lower() == x.lower()), None) for x in ab]
                if all(ks):
                    pairs.append(ks)
        p["pairs"] = pairs
        p["no_repeat"] = norep_var.get()
        p["periodic"] = {"enabled": per_en.get(), "minutes": max(1, ival(per_min, 60)),
                         "key": per_key.get() or "1", "even_pairs": per_even.get()}
        persist()

    def on_preset(_=None):
        name = preset_var.get()
        p = all_presets().get(name)
        if p:
            cfg["preset_name"] = name
            cfg["preset"] = json.loads(json.dumps(p))
            persist()
            load_preset_fields()
            engine_reset()
            log(f"Пресет: {name}")
    pcb.bind("<<ComboboxSelected>>", on_preset)

    def save_preset():
        save_preset_fields()
        name = simpledialog.askstring("Сохранить пресет", "Название пресета:",
                                      initialvalue=cfg["preset_name"] if cfg["preset_name"] not in PRESETS_BUILTIN
                                      else "Мой пресет", parent=parent)
        if not name:
            return
        if name in PRESETS_BUILTIN:
            messagebox.showinfo("Пресет", "Встроенные пресеты не перезаписываются, выбери другое имя.", parent=parent)
            return
        cfg["user_presets"][name] = json.loads(json.dumps(cfg["preset"]))
        cfg["preset_name"] = name
        preset_var.set(name)
        persist()
        load_preset_fields()

    def delete_preset():
        name = preset_var.get()
        if name in cfg["user_presets"]:
            del cfg["user_presets"][name]
            preset_var.set("Как в gta.ahk")
            on_preset()

    def selected():
        s = tv.selection()
        return int(s[0]) if s else None

    def edit_action(idx):
        acts = cfg["preset"]["actions"]
        a = acts[idx] if idx is not None else A("W")
        dlg = tk.Toplevel(parent)
        dlg.title("Действие")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        kvar = tk.StringVar(value=a["key"])
        wvar = tk.StringVar(value=str(a.get("weight", 1)))
        h1, h2 = tk.StringVar(value=str(a["hold"][0])), tk.StringVar(value=str(a["hold"][1]))
        ttk.Label(dlg, text="Клавиша или действие:").grid(row=0, column=0, sticky="w", padx=10, pady=6)
        ttk.Combobox(dlg, textvariable=kvar, values=ALL_ACTIONS, state="readonly", width=26).grid(
            row=0, column=1, columnspan=3, sticky="w", padx=10)
        ttk.Label(dlg, text="Частота (вес):").grid(row=1, column=0, sticky="w", padx=10, pady=6)
        ttk.Spinbox(dlg, from_=0, to=100, width=5, textvariable=wvar).grid(row=1, column=1, sticky="w", padx=10)
        ttk.Label(dlg, text="Удержание, мс:").grid(row=2, column=0, sticky="w", padx=10, pady=6)
        ttk.Spinbox(dlg, from_=10, to=10000, increment=10, width=6, textvariable=h1).grid(row=2, column=1, sticky="w", padx=10)
        ttk.Label(dlg, text="–").grid(row=2, column=2)
        ttk.Spinbox(dlg, from_=10, to=10000, increment=10, width=6, textvariable=h2).grid(row=2, column=3, sticky="w", padx=10)
        ttk.Label(dlg, text="Вес 2 - нажимается вдвое чаще, чем с весом 1. Вес 0 - выключено.",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", padx=10)

        def ok():
            new = {"key": kvar.get(), "weight": max(0, int(ival(wvar, 1))),
                   "hold": sorted([max(10, int(ival(h1, 80))), max(10, int(ival(h2, 140)))])}
            if idx is None:
                acts.append(new)
            else:
                acts[idx] = new
            persist()
            load_preset_fields()
            dlg.destroy()
        ttk.Button(dlg, text="Сохранить", style="Accent.TButton", command=ok).grid(
            row=4, column=0, columnspan=4, sticky="ew", padx=10, pady=10)
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    def delete_action():
        i = selected()
        if i is not None:
            del cfg["preset"]["actions"][i]
            persist()
            load_preset_fields()

    def save_misc():
        cfg.update(focus_only=focus_var.get(), window_titles=titles_var.get(),
                   overlay=overlay_var.get(), telegram=tg_var.get())
        persist()

    # ---------------------------------------------------------------- еда: настройки
    def refresh_food():
        for need, ui_ in need_ui.items():
            n = food["needs"][need]
            ui_["icon"].set("значок сохранён" if n.get("icon") else "значок не задан")
            ui_["items"].delete(0, "end")
            for it in n["items"]:
                ui_["items"].insert("end", it.get("name", "предмет"))
        use_var.set("кнопка сохранена" if food.get("use_template") else "не задана")

    def save_food():
        food["enabled"] = fen.get()
        food["check_sec"] = max(2, ival(chk_var, 10))
        food["cooldown_sec"] = max(10, ival(cd_var, 300))
        food["threshold"] = min(0.99, max(0.5, ival(thr_var, 80) / 100))
        food["inv_key"] = inv_key.get()
        food["close_key"] = close_key.get()
        food["open_delay"] = max(0.3, ival(delay_var, 1.0))
        food["gesture"] = "double" if gest.get().startswith("Двойной") else "right_use"
        for need, ui_ in need_ui.items():
            food["needs"][need]["mode"] = "key" if ui_["mode"].get().startswith("Клавиша") else "inventory"
            food["needs"][need]["key"] = ui_["key"].get()
        persist()

    def grab_region(hint):
        top = parent.winfo_toplevel()
        was_top = top.attributes("-topmost")
        top.withdraw()
        time.sleep(0.35)
        try:
            return pick_region(top, hint)
        except Exception as ex:
            log(f"Не получилось сделать снимок экрана: {ex}")
            return None
        finally:
            top.deiconify()
            top.attributes("-topmost", was_top)

    def capture_icon(need):
        res = grab_region(f"Обведи значок «{NEEDS[need]}»")
        if res:
            img, x, y = res
            food["needs"][need]["icon"] = new_template(img, x, y, need)
            persist()
            refresh_food()
            log(f"{NEEDS[need]}: значок сохранён ({img.shape[1]}x{img.shape[0]})")

    def capture_use():
        res = grab_region("Обведи кнопку «Использовать» в меню предмета")
        if res:
            img, x, y = res
            food["use_template"] = new_template(img, x, y, "use")
            persist()
            refresh_food()

    def add_item(need):
        res = grab_region(f"Открой инвентарь и обведи предмет от «{NEEDS[need]}»")
        if not res:
            return
        name = simpledialog.askstring("Предмет", "Как назвать предмет? (например Бургер, Вода, ИРП)",
                                      parent=parent) or "предмет"
        img, x, y = res
        t = new_template(img, x, y, f"{need}_item")
        t["name"] = name
        food["needs"][need]["items"].append(t)
        persist()
        refresh_food()

    def del_item(need):
        s = need_ui[need]["items"].curselection()
        if s:
            del food["needs"][need]["items"][s[0]]
            persist()
            refresh_food()

    def move_item(need):
        s = need_ui[need]["items"].curselection()
        lst = food["needs"][need]["items"]
        if s and s[0] > 0:
            i = s[0]
            lst[i - 1], lst[i] = lst[i], lst[i - 1]
            persist()
            refresh_food()
            need_ui[need]["items"].selection_set(i - 1)

    def test_icon(need):
        t = food["needs"][need].get("icon")
        if not t:
            log(f"{NEEDS[need]}: сначала захвати значок")
            return

        def work():
            try:
                score, x, y = find(Screen().grab(), t, food["threshold"], around=120)
                log(f"{NEEDS[need]}: " + (f"значок на экране есть (совпадение {score:.2f})" if x is not None
                                         else f"значка сейчас нет (лучшее совпадение {score:.2f})"))
            except Exception as ex:
                log(f"Ошибка проверки: {ex}")
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- горячая клавиша
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

    # ---------------------------------------------------------------- оверлей
    ov = {"win": None, "var": tk.StringVar()}

    def overlay_update(force=False):
        want = cfg["overlay"] and ctl["active"]
        if want and ov["win"] is None:
            w = tk.Toplevel(parent)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            try:
                w.attributes("-alpha", 0.88)
            except Exception:
                pass
            w.geometry("+12+12")
            tk.Label(w, textvariable=ov["var"], bg="#0a0a0d", fg="#ffffff", font=("Segoe UI", 9, "bold"),
                     justify="left", padx=10, pady=6, highlightthickness=1,
                     highlightbackground=T.ACCENT).pack()
            ov["win"] = w
        elif not want and ov["win"] is not None:
            ov["win"].destroy()
            ov["win"] = None

    # ---------------------------------------------------------------- движок в потоке
    eng = {"e": None}
    import widgets

    def afk_stats():
        e = eng["e"]
        left, span, paused = 0.0, 1.0, False
        if e is not None:
            paused = e.paused_since is not None
            left = max(0.0, e.next_at - (e.paused_since if paused else time.time()))
            span = max(1.0, getattr(e, "span", 1.0))
        return {"active": ctl["active"], "status": ctl["status"], "left": left, "span": span,
                "paused": paused, "recent": list(e.recent) if e is not None else [],
                "items": [("Нажатий", e.done if e is not None else 0)]}
    widgets.register("afk", afk_stats)

    def engine_reset():
        if eng["e"]:
            eng["e"].reset()

    def toggle():
        if not switches.is_on("afk") and not ctl["active"]:
            log("Анти-АФК выключен в боковом меню - сначала включи его там")
            return
        ctl["active"] = not ctl["active"]
        q.put(("toggled", ctl["active"]))

    def game_focused():
        if not cfg["focus_only"]:
            return True
        title = foreground_title().lower()
        return any(t.strip().lower() in title for t in cfg["window_titles"].split(",") if t.strip())

    def tg_notify(text):
        if not cfg["telegram"]:
            return
        try:
            import cooking
            c = cooking.load_json(cooking.CONFIG_PATH, {})
            if c.get("token") and c.get("chat_id"):
                cooking.tg_send(c["token"], c["chat_id"], text)
        except Exception:
            pass

    def worker():
        if IS_WIN:
            try:   # координаты экрана и мыши - в настоящих пикселях
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        scr = None

        def screen():
            nonlocal scr
            if scr is None:
                scr = Screen()
            return scr.grab()

        def wlog(m):
            log(m)
            if any(w in m for w in ("использовал", "нажал", "не нашёл")) and ":" in m and \
                    m.split(":")[0] in NEEDS.values():
                threading.Thread(target=tg_notify, args=(f"🍔 Анти-АФК: {m}",), daemon=True).start()

        e = Engine(cfg, Input2(), wlog)
        eng["e"] = e
        prev = False
        was_active = False
        while not ctl["quit"]:
            if not switches.is_on("afk"):
                if ctl["active"]:
                    ctl["active"] = False
                    q.put(("toggled", False))
                prev = True
            elif not ctl["binding"]:
                k = key_pressed(cfg["toggle_vk"])
                if k and not prev:
                    toggle()
                prev = k
            if ctl["active"] != was_active:
                was_active = ctl["active"]
                e.reset()
                log("Анти-АФК включён" if was_active else "Анти-АФК выключен")
            now = time.time()
            if not ctl["active"]:
                ctl["status"] = "Выключен" if switches.is_on("afk") else "Отключён в меню"
                time.sleep(0.1)
                continue
            fishing_busy = False
            try:
                import fishing
                fishing_busy = fishing.BUSY["on"]
            except Exception:
                pass
            try:
                import sanding
                fishing_busy = fishing_busy or sanding.BUSY["on"]
                import clicker
                fishing_busy = fishing_busy or clicker.BUSY["on"]
            except Exception:
                pass
            if SEWING_BUSY["on"] or fishing_busy:
                e.pause(now)
                ctl["status"] = "Пауза: работает " + ("швейный бот" if SEWING_BUSY["on"] else "рыбалка")
            elif not game_focused():
                e.pause(now)
                ctl["status"] = "Пауза: окно игры не активно"
            else:
                e.resume(now)
                try:
                    e.step(screen if cfg["food"]["enabled"] else None)
                except Exception as ex:
                    log(f"Ошибка: {ex}")
                    time.sleep(2)
                ctl["status"] = "Работает"
            left = max(0, e.next_at - time.time()) if e.paused_since is None else max(0, e.next_at - e.paused_since)
            per = cfg["preset"]["periodic"]
            cyc = ""
            if per.get("enabled"):
                ref = e.paused_since or time.time()
                cyc = f"   ·   до «{per.get('key')}»: {fmt(per['minutes'] * 60 - (ref - e.cycle_at))}"
            ctl["info"] = f"Следующее нажатие через {fmt(left)}{cyc}   ·   нажатий: {e.done}"
            time.sleep(0.1)

    def fmt(sec):
        sec = max(0, int(sec))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def pump():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", time.strftime("%H:%M:%S ") + str(val) + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                elif kind == "toggled":
                    overlay_update()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            s = ctl["status"]
            status_var.set(s)
            status_lbl.configure(style="StatusOn.TLabel" if s == "Работает" else "Status.TLabel")
            info_var.set(ctl.get("info", "") if ctl["active"] else
                         f"Включить: кнопка выше или {vk_name(cfg['toggle_vk'])}")
            if ov["win"] is not None:
                ov["var"].set(f"АНТИ-АФК: {s}\n{ctl.get('info', '')}")
        except tk.TclError:
            return
        parent.after(200, pump)

    def stop():
        ctl["quit"] = True
        ctl["active"] = False
        if ov["win"] is not None:
            try:
                ov["win"].destroy()
            except Exception:
                pass

    load_preset_fields()
    refresh_food()
    threading.Thread(target=worker, daemon=True).start()
    parent.after(200, pump)
    return stop


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    root.title("Анти-АФК")
    try:
        import theme
        theme.apply(root)
    except Exception:
        pass
    stop = build_antiafk_tab(root)
    root.mainloop()
    stop()
