# -*- coding: utf-8 -*-
"""
Бот рыбалки Majestic RP - полный цикл, как в игре:
  1) E - начать рыбалку (если включено);
  2) заброс: по полоске бегает белая метка, Space нужно нажать, когда она в зелёной зоне -
     бот предугадывает движение метки с поправкой на задержку;
  3) ожидание: в окошке с поплавком справа от хотбара при поклёвке появляются брызги -
     бот подсекает (Space);
  4) вываживание: плашка с рыбкой слева от брызг - держит D, справа - держит A;
  5) окно "Вы поймали рыбу": забрать всё или отпустить всё (ради опыта) - и снова заброс.
"""
import os
import sys
import json
import time
import queue
import random
import threading

import numpy as np
import cv2

import switches
from sewing_bot import key_pressed, vk_name, SKIP_VKS, BUSY as SEWING_BUSY
import antiafk as AF

BUSY = {"on": False}            # True, пока идёт рыбалка (анти-АФК в это время молчит)
CONFIG_PATH = os.path.join(AF.app_dir(), "fishing_config.json")
ZONE = (500, 150, 1420, 760)                 # где искать плашку с рыбкой на воде (для 1920x1080)


def default_config():
    return {"toggle_vk": 0x6F,               # Numpad /
            "invert": False, "hold_after_lost": 1.2, "zone": None, "focus_only": True,
            "auto_start": True, "start_key": "E", "auto_cast": True, "cast_key": "Space",
            "latency_ms": 45, "auto_hook": True, "hook_key": "Space",
            "after_catch": "keep",           # keep | release | filter | none
            "fish_release": [],              # filter: какие рыбы отпускать (остальные - забирать)
            "fish_unknown": "keep",          # filter: название не прочиталось
            "keep_record": False,            # filter: «Рекордный улов» забирать всегда
            "collect": False}                # сохранять снимок окна улова каждой новой рыбы (для настройки)


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        cfg.update({k: v for k, v in d.items() if k in cfg})
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


# ================================================================ состояния игры (координаты для 1920x1080)
BASE_W, BASE_H = 1920, 1080
CAST_BAND = (600, 878, 1320, 907)
FLOAT_BOX = (1350, 845, 1520, 1005)
HINTS_BOX = (1450, 1020, 1920, 1075)
PROG_LEFT = (677, 891, 702, 899)
BAR_BAND = (660, 862, 1210, 890)
PANEL = (760, 300, 1170, 770)
KEEP_BOX = (778, 706, 955, 748)
REL_BOX = (966, 706, 1143, 748)

def sc(box, W, H):
    kx, ky = W / BASE_W, H / BASE_H
    return int(box[0] * kx), int(box[1] * ky), int(box[2] * kx), int(box[3] * ky)

def crop(img, box):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = sc(box, W, H)
    return img[y0:y1, x0:x1], x0, y0

def hints_on(img):
    a, _, _ = crop(img, HINTS_BOX)
    return (a.min(axis=2) > 200).mean() > 0.04

def bite(img):
    a, _, _ = crop(img, FLOAT_BOX)
    return (a.min(axis=2) > 150).mean() > 0.012

def bar_fish_on(img):
    """Белая рыбка на полоске прогресса вываживания."""
    a, _, _ = crop(img, BAR_BAND)
    return (a.min(axis=2) > 200).sum() > 40 * (img.shape[1] / BASE_W) ** 2


def minigame_on(img):
    """Полоска прогресса с белой рыбкой (подсказок Esc/Space в это время нет)."""
    return bar_fish_on(img) and not hints_on(img)

PROG_FILL = (688, 894, 1226, 902)   # заливка полоски прогресса (жёлтая -> зелёная)


def progress_px(img):
    """Сколько пикселей (в масштабе 1080p) занимает заливка полоски вываживания слева.
    Это сама игра говорит, растёт прогресс или падает. None - заливки нет (пустая полоска)."""
    H, W = img.shape[:2]
    x0, y0, x1, y1 = sc(PROG_FILL, W, H)
    a = img[y0:y1, x0:x1].astype(np.int16)
    fill = (a[:, :, 1] > 150) & (a[:, :, 0] < 110)           # BGR: зелёного много, синего нет
    cols = fill.sum(axis=0) >= max(2, int(fill.shape[0] * 0.6))
    if cols.size == 0 or not cols[0]:
        return None
    bad = np.nonzero(~cols)[0]
    n = int(bad[0]) if len(bad) else cols.size
    return n * BASE_W / W


def cast_bar(img):
    """-> (зона_слева, зона_справа, x_метки) в пикселях экрана или None"""
    a, x0, _ = crop(img, CAST_BAND)
    a = a.astype(np.int16)
    green = ((a[:, :, 1] - np.maximum(a[:, :, 0], a[:, :, 2])) > 60).sum(axis=0) >= 3
    white = (a.min(axis=2) > 215).sum(axis=0) >= max(6, a.shape[0] // 4)
    gx = np.nonzero(green)[0]
    if len(gx) < 8:
        return None
    # самый длинный непрерывный кусок зелёного
    runs, s = [], gx[0]
    for p, q in zip(gx, gx[1:]):
        if q - p > 3:
            runs.append((s, p)); s = q
    runs.append((s, gx[-1]))
    g0, g1 = max(runs, key=lambda r: r[1] - r[0])
    k = img.shape[1] / BASE_W
    if not (25 * k <= g1 - g0 <= 130 * k):
        return None
    wx = np.nonzero(white)[0]
    if len(wx) == 0:
        return (x0 + g0, x0 + g1, None)
    return (x0 + g0, x0 + g1, x0 + float(wx.mean()))

TITLE = (772, 322, 1000, 346)
TITLE_BG = (772, 300, 1000, 318)


def dialog_on(img):
    """Окно "Вы поймали рыбу": белый заголовок на тёмной панели и две кнопки."""
    t, _, _ = crop(img, TITLE)
    bg, _, _ = crop(img, TITLE_BG)
    k = img.shape[1] / BASE_W
    if (t.min(axis=2) > 200).sum() < 500 * k * k or bg.max() > 70:
        return False
    kb, _, _ = crop(img, KEEP_BOX)
    rb, _, _ = crop(img, REL_BOX)
    return (kb.max(axis=2) > 95).sum() > 150 * k * k and (rb.max(axis=2) > 95).sum() > 150 * k * k

# ================================================================ какая рыба в окне улова
# Все рыбы игры (таблица Tweet «Гайд таблица по рыбалке»): название, уровень рыбалки
# (с какого ловится, 10 = секретная зона), цена мин-макс без VIP.
FISH = [
    ("Вобла", 1, 0.65, 0.70), ("Краснопёрка", 1, 1.01, 1.08), ("Лещ", 1, 0.61, 0.67), ("Плотва", 1, 0.93, 1.00),
    ("Коричневый сом", 2, 0.77, 0.82), ("Серебряный карась", 2, 0.81, 0.87),
    ("Речной окунь", 3, 0.51, 0.56), ("Обыкновенная щука", 3, 0.46, 0.50), ("Радужная форель", 3, 0.47, 0.51),
    ("Зеркальный карп", 3, 0.40, 0.44), ("Сом обыкновенный", 3, 0.41, 0.45),
    ("Судак обыкновенный", 4, 0.54, 0.58), ("Голавль", 4, 0.52, 0.58), ("Сазан", 4, 0.46, 0.50),
    ("Жерех", 5, 0.55, 0.59), ("Стерлядь", 5, 0.67, 0.73),
    ("Полосатый лаврак", 6, 0.54, 0.59), ("Снук обыкновенный", 6, 0.49, 0.51), ("Альбула", 6, 0.52, 0.58),
    ("Прибрежный басс", 6, 0.50, 0.54), ("Стальноголовый лосось", 6, 0.51, 0.54),
    ("Круглый трахинот", 7, 0.46, 0.50), ("Тёмный горбыль", 7, 0.45, 0.49), ("Барракуда", 7, 0.51, 0.55),
    ("Красный горбыль", 8, 0.49, 0.51), ("Тарпон", 8, 0.60, 0.63), ("Марлин", 8, 0.54, 0.58),
    ("Рустер", 9, 0.84, 0.90), ("Сериола", 9, 0.86, 0.88),
    ("Токсичный окунь", 10, 1.53, 1.60), ("Древняя гинерия", 10, 2.11, 2.15),
]
FISH_NAME = (788, 566, 1062, 604)    # название рыбы под картинкой
FISH_KG = (1060, 566, 1112, 604)     # вес «0.75 KG»
FISH_TAG = (790, 388, 1000, 418)     # плашка над картинкой («Рекордный улов»)


def _letters(s):
    return "".join(c for c in s.lower().replace("ё", "е") if c.isalpha())


def match_fish(text):
    """Прочитанный текст -> название из FISH (OCR может ошибиться в букве-двух) или None."""
    import difflib
    t = _letters(text)
    if len(t) < 3:
        return None
    best, score = None, 0.0
    for name, *_ in FISH:
        r = difflib.SequenceMatcher(None, t, _letters(name)).ratio()
        if r > score:
            best, score = name, r
    return best if score >= 0.75 else None


def _ocr_box(img, box, lang, k=3.0):
    """OCR кусочка экрана. Вокруг текста - поля цветом фона: короткое слово без полей
    OCR Windows часто не видит вовсе (как ранги из одной цифры во вкладке PM)."""
    import pm_ocr
    from PIL import Image
    a, _, _ = crop(img, box)
    f = k * BASE_H / img.shape[0]
    a = cv2.resize(a, (max(1, int(a.shape[1] * f)), max(1, int(a.shape[0] * f))), interpolation=cv2.INTER_CUBIC)
    m = int(12 * k)
    bg = [int(c) for c in np.median(a.reshape(-1, 3), axis=0)]
    a = cv2.copyMakeBorder(a, m, m, m, m, cv2.BORDER_CONSTANT, value=bg)
    return " ".join(w[0] for w in pm_ocr.ocr(Image.fromarray(cv2.cvtColor(a, cv2.COLOR_BGR2RGB)), lang))


OCR_SCALES = (3.0, 2.0, 4.0, 2.5, 1.5)


def read_catch(img):
    """Окно улова -> {"name": название или None, "text": что прочитано, "kg": вес или None, "record": bool}.
    Читает встроенный OCR Windows (как во вкладке PM); не узнал название - пробует другие масштабы."""
    import re
    name, text = None, ""
    for k in OCR_SCALES:
        t = _ocr_box(img, FISH_NAME, "ru", k)
        text = text or t
        name = match_fish(t)
        if name:
            text = t
            break
    kg = None
    for k in (3.0, 2.0):
        m = re.search(r"\d+[.,]\d+", _ocr_box(img, FISH_KG, "en", k))
        if m:
            kg = float(m.group(0).replace(",", "."))
            break
    tag = _letters(_ocr_box(img, FISH_TAG, "ru"))
    return {"name": name, "text": text, "kg": kg, "record": "рекорд" in tag}


def catch_action(cfg, info):
    """Что делать с этой рыбой: "keep" / "release" и причина для журнала."""
    act = cfg["after_catch"]
    if act != "filter":
        return act, ""
    if info.get("record") and cfg.get("keep_record", True):
        return "keep", "рекордный улов"
    name = info.get("name")
    if name is None:
        return cfg.get("fish_unknown", "keep"), "название не прочитал"
    return ("release" if name in cfg.get("fish_release", []) else "keep"), "по списку"


SAMPLES_DIR = os.path.join(AF.app_dir(), "fish_samples")
UNKNOWN_MAX = 40                     # сколько снимков нераспознанных рыб хранить


def save_sample(img, info):
    """Сбор образцов: снимок экрана с окном улова - по одному на каждую рыбу, которой ещё нет
    в папке; нераспознанные - каждый раз (до UNKNOWN_MAX). Плюс строка в samples.csv.
    -> имя файла или None (такая рыба уже есть)."""
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    if info.get("name"):
        fn = info["name"].replace(" ", "_") + ".png"
        if os.path.exists(os.path.join(SAMPLES_DIR, fn)):
            return None
    else:
        if len([f for f in os.listdir(SAMPLES_DIR) if f.startswith("нераспознано_")]) >= UNKNOWN_MAX:
            return None
        fn = time.strftime("нераспознано_%Y%m%d_%H%M%S.png")
    ok, buf = cv2.imencode(".png", img)          # cv2.imwrite не умеет русские пути
    if not ok:
        return None
    buf.tofile(os.path.join(SAMPLES_DIR, fn))
    new = not os.path.exists(os.path.join(SAMPLES_DIR, "samples.csv"))
    with open(os.path.join(SAMPLES_DIR, "samples.csv"), "a", encoding="utf-8-sig") as f:
        if new:
            f.write("время;файл;рыба;прочитано;кг;рекорд;экран\n")
        f.write(";".join([time.strftime("%Y-%m-%d %H:%M:%S"), fn, info.get("name") or "",
                          (info.get("text") or "").replace(";", ","), str(info.get("kg") or ""),
                          "да" if info.get("record") else "", f"{img.shape[1]}x{img.shape[0]}"]) + "\n")
    return fn


# ================================================================ журнал улова (какая рыба, сколько кг)
CATCH_PATH = os.path.join(AF.app_dir(), "fish_catch.json")
CATCH = {"rows": None, "ver": 0}        # rows: [[время, название или "", кг или None, "keep"/"release"]]
_catch_lock = threading.Lock()
UNKNOWN_FISH = "Не прочитано"


def catch_rows():
    with _catch_lock:
        if CATCH["rows"] is None:
            try:
                with open(CATCH_PATH, encoding="utf-8") as f:
                    CATCH["rows"] = [r for r in json.load(f) if isinstance(r, list) and len(r) == 4]
            except Exception:
                CATCH["rows"] = []
        return list(CATCH["rows"])


def catch_add(name, kg, act, t=None):
    catch_rows()
    with _catch_lock:
        CATCH["rows"].append([t or time.time(), name or "", kg, act])
        CATCH["ver"] += 1
        rows = list(CATCH["rows"])
    try:
        with open(CATCH_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(CATCH_PATH + ".tmp", CATCH_PATH)
    except Exception:
        pass


def catch_clear():
    with _catch_lock:
        CATCH["rows"] = []
        CATCH["ver"] += 1
    try:
        os.remove(CATCH_PATH)
    except OSError:
        pass


def catch_summary(rows, since=0.0, act="keep"):
    """-> (по рыбам [(название, штук, кг всего, самая крупная, штук без веса)] по убыванию кг, итог)."""
    by = {}
    for t, name, kg, a in rows:
        if t < since or (act != "all" and a != act):
            continue
        r = by.setdefault(name or UNKNOWN_FISH, [0, 0.0, 0.0, 0])
        r[0] += 1
        if kg:
            r[1] += kg
            r[2] = max(r[2], kg)
        else:
            r[3] += 1
    out = sorted(((n, *v) for n, v in by.items()), key=lambda r: (-r[2], -r[1], r[0]))
    tot = (sum(r[1] for r in out), sum(r[2] for r in out), max([r[3] for r in out] or [0.0]), sum(r[4] for r in out))
    return out, tot


def fmt_kg(kg):
    return f"{kg:.2f}".replace(".", ",") + " кг"


def button_center(img, which):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = sc(KEEP_BOX if which == "keep" else REL_BOX, W, H)
    return (x0 + x1) // 2, (y0 + y1) // 2


# ================================================================ зрение

def _glyph_all(img, min_w=4):
    """Кандидаты плашки по тёмному силуэту рыбки на светлом квадрате (включая крошечные дальние)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mx = img.max(axis=2)
    H, W = gray.shape
    out = []
    for thr in (70, 95):
        dark = (mx < thr).astype(np.uint8)
        n, lab, st, cen = cv2.connectedComponentsWithStats(dark, connectivity=8)
        for k in range(1, n):
            x, y, w, h, a = st[k]
            if not (min_w <= w <= 48 and 2 <= h <= 40 and 1.2 <= w / max(1, h) <= 4.8
                    and 0.25 < a / (w * h) < 0.92):
                continue
            p = max(3, int(w * 0.45))
            X0, Y0, X1, Y1 = max(0, x - p), max(0, y - p), min(W, x + w + p), min(H, y + h + p)
            ring = gray[Y0:Y1, X0:X1]
            if ring.size == 0:
                continue
            light = float((ring > 110).mean())
            if light < 0.33:
                continue
            # вокруг плашки - тёмная вода (не камни/берег): проверяем кольцо побольше
            q = int(w * 1.6)
            R0, R1 = max(0, y - q), min(H, y + h + q)
            C0, C1 = max(0, x - q), min(W, x + w + q)
            outer = gray[R0:R1, C0:C1]
            water = float((outer < 70).mean()) if outer.size else 0.0
            out.append({"x": x + w / 2, "y": y + h / 2, "size": w * 1.7,
                        "score": a * light * (0.3 + water)})
        if out:
            break
    return out


def _green_all(img):
    b, g, r = [img[:, :, i].astype(np.int16) for i in range(3)]
    green = ((g - np.maximum(r, b) > 22) & (g > 70)).astype(np.uint8)
    green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(green, connectivity=8)
    out = []
    for k in range(1, n):
        x, y, w, h, a = st[k]
        if 16 <= w <= 90 and 16 <= h <= 90 and 0.7 < w / h < 1.4 and a > 0.7 * w * h:
            out.append({"x": float(cen[k][0]), "y": float(cen[k][1]), "size": float(w), "score": float(a) * 1.5})
    return out


def speck_cloud(img):
    """Где на тёмной воде самое густое облако мелких брызг. -> (x, y, плотность) или None"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    spread = img.max(axis=2).astype(np.int16) - img.min(axis=2).astype(np.int16)
    bright = ((gray > 140) & (spread < 45)).astype(np.uint8)
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    solid = cv2.dilate(cv2.morphologyEx(bright, cv2.MORPH_OPEN, kern), np.ones((5, 5), np.uint8))
    specks = (bright & (1 - solid)).astype(np.float32)
    dark = (gray < 60).astype(np.float32)
    small = cv2.resize(specks, (img.shape[1] // 4, img.shape[0] // 4), interpolation=cv2.INTER_AREA)
    dsm = cv2.resize(dark, (img.shape[1] // 4, img.shape[0] // 4), interpolation=cv2.INTER_AREA)
    dens = cv2.GaussianBlur(small, (0, 0), 6) * (0.2 + cv2.GaussianBlur(dsm, (0, 0), 10))
    _, mxv, _, loc = cv2.minMaxLoc(dens)
    if mxv < 0.02:
        return None
    return loc[0] * 4.0, loc[1] * 4.0, float(mxv)


def find_marker(img, near=None, cloud=None, cx=None):
    """Плашка с рыбкой. near=(x, y, r) - искать рядом с прошлым местом;
    cloud=(x, y) - облако брызг (плашка всегда рядом с ним);
    cx=(x, сигма) - камера обычно держит рыбу по центру экрана: мягкий бонус центру.
    -> (cx, cy, size) или None"""
    cands = _glyph_all(img) + _green_all(img)
    if not cands:
        return None
    best, bscore = None, -1e9
    for c in cands:
        sc_ = c["score"]
        if cx is not None:
            sc_ *= 0.4 + 0.6 * float(np.exp(-((c["x"] - cx[0]) / cx[1]) ** 2))
        if near is not None:
            # рядом с прошлым местом - бонус, но не запрет: сильная плашка в стороне всё равно победит
            d = np.hypot(c["x"] - near[0], c["y"] - near[1])
            if d < near[2]:
                sc_ *= 1.0 + 1.5 * (1 - d / near[2])
        if cloud is not None:
            d = np.hypot(c["x"] - cloud[0], c["y"] - cloud[1])
            lim = max(60.0, c["size"] * 7)
            if d > lim * 2.5:
                sc_ *= 0.15                      # далеко от брызг - почти наверняка камень
            else:
                sc_ *= 1.0 + 2.0 * (1 - min(1.0, d / (lim * 2.5)))
        if sc_ > bscore:
            best, bscore = c, sc_
    if best is None:
        return None
    return best["x"], best["y"], best["size"]


def fish_heading(sub):
    """Куда смотрит рыбка внутри плашки: +1 голова справа, -1 слева, 0 не ясно.
    Голова - толстый конец (силуэт выше), хвост - тонкий."""
    if sub.size == 0:
        return 0
    gi = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY).astype(np.int16)
    dark = gi < gi.mean() - 16
    xs = np.nonzero(dark.any(axis=0))[0]
    if len(xs) < 6:
        return 0
    x0, x1 = xs.min(), xs.max()
    heights = np.array([np.ptp(np.nonzero(dark[:, c])[0]) if dark[:, c].any() else 0
                        for c in range(x0, x1 + 1)], dtype=float)
    n = len(heights)
    if n < 6:
        return 0
    lh = heights[:n // 2].mean()
    rh = heights[n // 2:].mean()
    if abs(rh - lh) < 1.0:
        return 0
    return 1 if rh > lh else -1


def splash_x(img, cx, cy, size):
    """Центр облака брызг рядом с плашкой. -> x или None.
    Брызги - это много мелких отдельных капель. Сплошные светлые пятна (белая одежда
    персонажа, берег, фонари) отбрасываем: оставляем только мелкие яркие точки."""
    H, W = img.shape[:2]
    half = int(size * 6)
    x0, x1 = max(0, int(cx - half)), min(W, int(cx + half))
    y0, y1 = max(0, int(cy - size * 1.6)), min(H, int(cy + size * 1.6))   # только на уровне плашки
    win = img[y0:y1, x0:x1]
    if win.size == 0:
        return None
    gray = cv2.cvtColor(win, cv2.COLOR_BGR2GRAY)
    spread = win.max(axis=2).astype(np.int16) - win.min(axis=2).astype(np.int16)
    bright = ((gray > 140) & (spread < 45)).astype(np.uint8)
    # сплошное (одежда, бетон) переживает "открытие" крупным ядром, мелкие капли - нет
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    solid = cv2.morphologyEx(bright, cv2.MORPH_OPEN, kern)
    solid = cv2.dilate(solid, np.ones((5, 5), np.uint8))
    specks = bright & (1 - solid)
    # саму плашку убираем
    mx0, mx1 = int(cx - size * 0.8) - x0, int(cx + size * 0.8) - x0
    my0, my1 = int(cy - size * 0.8) - y0, int(cy + size * 0.8) - y0
    specks[max(0, my0):max(0, my1), max(0, mx0):max(0, mx1)] = 0
    cols = specks.sum(axis=0).astype(float)
    if cols.sum() < size * 2:
        return None
    k = max(5, int(size * 1.5)) | 1
    sm = np.convolve(cols, np.ones(k) / k, "same")
    return x0 + float(np.argmax(sm))


# область фона (для 1920x1080): БЕЗ чата слева (x<520), панелей справа (x>1590) - они неподвижны
# на экране и глушили сдвиг фазовой корреляции (на записи 143614: 2,5 против 49 px/с)
BG_AREA = (520, 60, 1590, 560)
BG_CUT = 300                       # вырезаем центр +-300 px: там плашка, брызги и персонаж


class PlateTracker:
    """Ведёт СВОЮ иконку от кадра к кадру: запоминает, как она выглядит, и ищет точно
    такую же рядом (с точностью до долей пикселя). Чужие плашки других игроков не цепляет.
    Если иконку на миг закрыло удочкой/игроком - коротко "доводит" её по прежней скорости."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.tpl = None            # картинка иконки (серая)
        self.pos = None            # (x, y, size)
        self.t = None
        self.vx = 0.0
        self.lost_since = None

    def _grab_tpl(self, gray, x, y, size):
        h = max(6, int(size * 0.45))
        x0, y0 = int(round(x)) - h, int(round(y)) - h
        if x0 < 0 or y0 < 0 or x0 + 2 * h >= gray.shape[1] or y0 + 2 * h >= gray.shape[0]:
            return None
        return gray[y0:y0 + 2 * h, x0:x0 + 2 * h].copy()

    def update(self, screen, gray, zone, now, k):
        """-> (x, y, size, точное: bool) или None. x/y - экранные координаты."""
        if self.tpl is not None and self.pos is not None:
            x, y, size = self.pos
            th, tw = self.tpl.shape
            r = int(max(40 * k, size * 2.5))
            X0, Y0 = int(max(0, x - tw / 2 - r)), int(max(0, y - th / 2 - r))
            X1, Y1 = int(min(gray.shape[1], x + tw / 2 + r)), int(min(gray.shape[0], y + th / 2 + r))
            win = gray[Y0:Y1, X0:X1]
            if win.shape[0] > th + 2 and win.shape[1] > tw + 2:
                res = cv2.matchTemplate(win, self.tpl, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx > 0.6:
                    cx, cy = loc
                    # уточнение до долей пикселя по параболе
                    dx = 0.0
                    if 0 < cx < res.shape[1] - 1:
                        l, c, rr = res[cy, cx - 1], res[cy, cx], res[cy, cx + 1]
                        den = (l - 2 * c + rr)
                        if abs(den) > 1e-6:
                            dx = float(np.clip(0.5 * (l - rr) / den, -0.5, 0.5))
                    nx = X0 + cx + dx + tw / 2
                    ny = Y0 + cy + th / 2
                    if self.t is not None and now > self.t:
                        self.vx = 0.7 * self.vx + 0.3 * (nx - x) / (now - self.t)
                    self.pos = (nx, ny, size)
                    self.t = now
                    self.lost_since = None
                    if mx > 0.85:              # медленно обновляем образец (иконка растёт с приближением)
                        nt = self._grab_tpl(gray, nx, ny, size)
                        if nt is not None and nt.shape == self.tpl.shape:
                            self.tpl = cv2.addWeighted(self.tpl, 0.8, nt, 0.2, 0)
                    return (nx, ny, size, True)
        # не нашли по образцу - ищем заново (рядом с прошлым местом предпочтительно)
        x0, y0, x1, y1 = zone
        near = None
        if self.pos is not None:
            near = (self.pos[0] - x0, self.pos[1] - y0, max(80 * k, self.pos[2] * 4))
        m = find_marker(screen[y0:y1, x0:x1], near=near, cx=(screen.shape[1] / 2 - x0, 300 * k))
        if m is not None:
            nx, ny, size = x0 + m[0], y0 + m[1], m[2]
            jump = self.pos is not None and np.hypot(nx - self.pos[0], ny - self.pos[1]) > max(80 * k, self.pos[2] * 4)
            recently = self.lost_since is None or now - self.lost_since < 1.0
            if jump and recently and self.tpl is not None:
                m = None                        # скорее всего чужая плашка - подождём свою
            else:
                self.tpl = self._grab_tpl(gray, nx, ny, size)
                self.pos = (nx, ny, size)
                self.t = now
                self.vx = 0.0
                self.lost_since = None
                return (nx, ny, size, False)
        if self.lost_since is None:
            self.lost_since = now
        if self.pos is not None and now - self.lost_since < 0.5 and self.t is not None:
            # коротко ведём по прежней скорости (иконку закрыли удочкой)
            x, y, size = self.pos
            return (x + self.vx * (now - self.t), y, size, False)
        return None


class Tracker:
    """Куда плывёт рыба: +1 вправо, -1 влево. Только иконка и фон.

    Скорость рыбы = скорость иконки на экране МИНУС скорость фона (в пикселях в секунду,
    с учётом реального времени между кадрами). Иконку ведём по образцу (точно до долей
    пикселя), фон сравниваем между соседними кадрами. Скорость сглаживается (~0.3 с),
    сторона меняется, только если новая держится 0.2 с. Во время вываживания направление
    не сбрасывается: если данных нет - держим последнее (отпущенная клавиша = откат)."""

    USE_PLATE = False      # True - добавлять скорость иконки (старый режим "иконка - фон")
    # запись 20261029_213131 (повтор трекера по сдвигам фона): 0.3/0.2/3 - разворот рыбы замечали
    # через 0,37 с, 0.15/0.1/5 - через 0,15 с, лишних смен нет (и на 20261020_231750 тоже)
    TAU = 0.15             # с: сглаживание скорости
    VTH = 5.0              # px/с на 1080p: порог уверенной скорости
    CONFIRM = 0.1          # с: сколько новая сторона должна держаться для смены

    def __init__(self):
        self.plate = PlateTracker()
        self.reset()

    def reset(self):
        self.plate.reset()
        self.prev = None
        self.prev_t = None
        self.prev_plate = None
        self.v = 0.0
        self.last_dir = 0
        self.pending = 0
        self.pending_since = None
        self.last_seen = -1e9
        self.source = "-"
        self.dbg = {}

    @staticmethod
    def _prep(gray, plate, W):
        H = gray.shape[0]
        x0, y0, x1, y1 = sc(BG_AREA, W, H)
        g = gray[y0:y1, x0:x1].astype(np.float32)
        mean = float(g.mean())
        k = W / BASE_W
        cx = W // 2 - x0
        g[:, max(0, int(cx - 150 * k)):int(cx + 150 * k)] = mean
        if plate is not None:
            px, py, size = plate[:3]
            r = max(60 * k, size * 4)
            gx0, gx1 = int(max(0, px - r - x0)), int(min(x1 - x0, px + r - x0))
            gy0, gy1 = int(max(0, py - r - y0)), int(min(y1 - y0, py + r - y0))
            if gx1 > gx0 and gy1 > gy0:
                g[gy0:gy1, gx0:gx1] = mean
        small = cv2.resize(g, (700, max(60, int(g.shape[0] * 700 / g.shape[1]))))
        return small, g.shape[1] / 700.0

    def update(self, screen, zone, now):
        H, W = screen.shape[:2]
        k = W / BASE_W
        if zone is None:
            zone = sc(ZONE, W, H)
        gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        # иконку не ведём: решение только по движению фона (на записи с эталоном по прогрессу
        # 95% окон совпало, +8,6 px/с против -10 px/с); ведение иконки цеплялось за берег/деревья
        plate = self.plate.update(screen, gray, zone, now, k) if self.USE_PLATE else None
        small, scale = self._prep(gray, plate, W)
        dt = None if self.prev_t is None else now - self.prev_t
        step_v = None
        if dt and 0 < dt < 0.5 and self.prev is not None and self.prev.shape == small.shape:
            (dx, dy), resp = cv2.phaseCorrelate(self.prev, small)
            bg = dx * scale if resp > 0.03 and abs(dx) < 40 else None
            pl = None
            if plate is not None and plate[3] and self.prev_plate is not None and self.prev_plate[3]:
                pl = plate[0] - self.prev_plate[0]
            if bg is not None or pl is not None:
                step_v = ((pl or 0.0) - (bg or 0.0)) / dt
        self.prev, self.prev_t, self.prev_plate = small, now, plate
        if step_v is not None:
            a = 1.0 - np.exp(-dt / self.TAU)
            self.v = (1 - a) * self.v + a * step_v
            self.last_seen = now
        vth = self.VTH * k
        if abs(self.v) > vth:
            d = 1 if self.v > 0 else -1
            if not self.last_dir:
                self.last_dir = d
            elif d != self.last_dir:
                if self.pending != d:
                    self.pending, self.pending_since = d, now
                elif now - self.pending_since >= self.CONFIRM:
                    self.last_dir = d
                    self.pending = 0
            else:
                self.pending = 0
        self.source = "иконка+фон" if plate is not None and plate[3] else "фон"
        self.dbg = {"marker": None if plate is None else plate[:3], "splash": None, "cloud": None,
                    "bg": self.v, "bg_conf": abs(self.v) / vth, "sp": 0.0, "sp_n": 0}
        return self.last_dir or None


# ================================================================ движок
class Fisher:
    """Полный цикл рыбалки. grab() - снимок экрана, grab_box(box) - снимок кусочка
    (для быстрого слежения за меткой заброса), clock() - время."""

    def __init__(self, cfg, inp, log, grab, grab_box=None, clock=time.time, sleep=time.sleep):
        self.cfg, self.inp, self.log = cfg, inp, log
        self.grab, self.grab_box = grab, grab_box
        self.clock, self.sleep = clock, sleep
        self.held = None
        self.last_seen = 0.0
        self.bite_frames = 0
        self.last = {}
        self.e_tries = 0
        self.ui_seen = 0.0
        self.in_fight = False
        self.prog = []                 # (время, прогресс px) последних ~6 с вываживания
        self.key_t = 0.0               # когда последний раз сменили клавишу
        self.fb_until = 0.0            # до какого момента клавиша навязана обратной связью
        self.fb_key = None
        self.fb_rate = 0.0             # скорость прогресса до проверки другой клавиши
        self.fb_off = None             # проверка не помогла: не спорим с трекером, пока он держит эту сторону
        self.vh = []                   # (время, |скорость фона|) - уверенность трекера
        self.tracker = Tracker()
        self.stats = {"casts": 0, "bites": 0, "kept": 0, "released": 0, "fish": {}, "kg": 0.0}
        self.state = "Жду"
        self.dbg = {}
        self.recorder = None

    def hold(self, key):
        if self.held == key:
            return
        self.key_t = self.clock()
        if self.held:
            self.inp.key(self.held, False)
        if key:
            self.inp.key(key, True)
        self.held = key

    def ago(self, what):
        return self.clock() - self.last.get(what, -1e9)

    def mark(self, what):
        self.last[what] = self.clock()

    # ---------------------------------------------------------------- заброс
    def cast(self, first):
        """Следит за меткой и жмёт Space, когда она будет в зелёной зоне."""
        cfg = self.cfg
        lat = cfg["latency_ms"] / 1000.0
        prev = None
        t_end = self.clock() + 8
        t_check = self.clock()
        cb = first
        while self.clock() < t_end:
            if self.clock() - t_check > 0.3:          # не открылось ли окно улова
                t_check = self.clock()
                if dialog_on(self.grab()):
                    return False
            if cb is None:
                return False
            g0, g1, x = cb
            now = self.clock()
            if x is not None:
                if prev is not None and now > prev[0]:
                    v = (x - prev[1]) / (now - prev[0])
                    if abs(v) > 50:                  # метка движется
                        xp = x + v * lat
                        m = (g1 - g0) * 0.2
                        if g0 + m <= xp <= g1 - m:
                            self.inp.tap(cfg["cast_key"], 60)
                            self.stats["casts"] += 1
                            self.log(f"Заброс: метка в зелёной зоне ({int(xp)} в {int(g0)}-{int(g1)})")
                            self.mark("cast")
                            return True
                prev = (now, x)
            if self.grab_box:
                img, bx0 = self.grab_box(CAST_BAND)
                cb = cast_bar_band(img, bx0, g0, g1)
            else:
                cb = cast_bar(self.grab())
        return False

    # ---------------------------------------------------------------- один шаг
    def step(self):
        cfg = self.cfg
        shot = self.grab()
        H, W = shot.shape[:2]
        now = self.clock()
        self._last_shot = shot
        try:
            self._step(shot, H, W, now)
        finally:
            dbg = dict(self.tracker.dbg) if self.state == "Тяну рыбу" else {}
            dbg.update(state=self.state, held=self.held, dir=self.tracker.last_dir,
                       source=self.tracker.source, W=W, H=H, t=now)
            if self.state == "Заброс":
                cb = cast_bar(shot)
                if cb:
                    dbg["cast"] = cb
            self.dbg = dbg
            if self.recorder is not None:
                try:
                    self.recorder.add(shot, dbg)
                except Exception:
                    pass

    def identify(self):
        """Читает название и вес рыбы в окне улова (до 3 попыток, пока окно «проявляется»).
        -> (info, снимок с окном улова или None)."""
        info = {"name": None, "text": "", "kg": None, "record": False}
        last = None
        for i in range(3):
            if i:
                self.sleep(0.2)
            shot = self.grab()
            if not dialog_on(shot):
                break
            last = shot
            try:
                info = read_catch(shot)
            except Exception as ex:
                if not getattr(self, "_ocr_warned", False):
                    self._ocr_warned = True
                    self.log(f"Не могу прочитать название рыбы (OCR Windows): {ex}")
                break
            if info["name"]:
                break
        return info, last

    # Запись 20261029_213131 (тяжёлый крючок, бои по 80-190 с вместо ~40): игра отвечает на клавишу
    # МЕДЛЕННО - верную клавишу надо держать 2-5 с, пока прогресс не разгонится (0 -> +16 px/с),
    # а сразу после разворота рыбы он ещё падает и при верной. Старая обратная связь (падение 5 px
    # за 0,7 с -> сменить на 1,5 с) на тяжёлой рыбе дёргала A/D каждые ~1,5 с: в таких фазах
    # прогресс падал -9 px/с при ЛЮБОЙ клавише, а где бот спокойно держал D - около 0..+5.
    FALL_PX = 15.0         # px (1080p): падение прогресса за окно, после которого клавиша под подозрением
    FALL_WIN = 2.5         # с: окно оценки (и минимум с момента смены клавиши)
    FB_LOCK = 3.0          # с: сколько держим навязанную клавишу, не слушая трекер
    FB_SURE = 6.0          # трекер уверен (|скорость фона| > FB_SURE x порог 3/4 окна) - не спорим с ним

    def _feedback(self, want, now, k):
        """Полоска прогресса - запасной судья, когда трекер сам не уверен (у берега фон шумит):
        если при текущей клавише прогресс долго падает, пробуем другую на FB_LOCK. Не помогло
        (падало так же) - возвращаем клавишу трекера и до следующего разворота рыбы не спорим."""
        tr = self.tracker
        if self.fb_off is not None and tr.last_dir != self.fb_off:
            self.fb_off = None                        # рыба развернулась - снова можно проверять
        if self.fb_key:
            if now < self.fb_until:
                return self.fb_key
            # проверка закончилась: помогла ли другая клавиша
            rate = self._prog_rate(self.fb_until - self.FB_LOCK + 1.0, now)
            if rate is not None and rate <= self.fb_rate + 2.0:
                tr.last_dir = -tr.last_dir if tr.last_dir else 0
                tr.pending = 0
                want = "D" if self.fb_key == "A" else "A"
                self.fb_off = tr.last_dir
                self.log(f"Другая клавиша не помогла ({rate:+.0f} px/с) - вернул {want}")
            self.fb_key = None
            return want
        held = self.held
        if not held or self.fb_off is not None or now - self.key_t < self.FALL_WIN or not self.prog:
            return want
        vth = tr.VTH * k
        if self.vh and np.percentile([v for t, v in self.vh if t >= now - self.FALL_WIN], 25) > self.FB_SURE * vth:
            return want
        old = [v for t, v in self.prog if now - self.FALL_WIN - 0.3 <= t <= now - self.FALL_WIN]
        cur = self.prog[-1][1]
        if old and cur > 12 and old[-1] - cur >= self.FALL_PX:
            new = "D" if held == "A" else "A"
            self.fb_rate = (cur - old[-1]) / self.FALL_WIN
            self.fb_key, self.fb_until = new, now + self.FB_LOCK
            tr.last_dir = -tr.last_dir if tr.last_dir else 0
            tr.pending = 0
            self.log(f"Прогресс падает ({old[-1]:.0f} -> {cur:.0f}) - пробую {new}")
            return new
        return want

    def _prog_rate(self, t0, t1):
        """px/с прогресса между t0 и t1 (по точкам self.prog) или None."""
        pts = [(t, v) for t, v in self.prog if t0 <= t <= t1]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.5:
            return None
        return (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0])

    def _step(self, shot, H, W, now):
        cfg = self.cfg

        if dialog_on(shot):
            self.hold(None)
            self.ui_seen = now
            self.mark("dialog_seen")
            act = cfg["after_catch"]
            self.state = "Улов"
            if act != "none" and self.ago("dialog") > 2.5:
                self.mark("dialog")
                self.sleep(random.uniform(0.4, 0.8))
                info, dshot = self.identify()
                if cfg.get("collect") and dshot is not None:
                    try:
                        fn = save_sample(dshot, info)
                        if fn:
                            self.log(f"Сбор образцов: сохранил {fn}")
                    except Exception as ex:
                        self.log(f"Сбор образцов: не сохранил ({ex})")
                act, why = catch_action(cfg, info)
                x, y = button_center(shot, act)
                self.inp.click(x, y)
                self.stats["kept" if act == "keep" else "released"] += 1
                if act == "keep" and info["kg"]:
                    self.stats["kg"] += info["kg"]
                catch_add(info["name"], info["kg"], act)
                what = "рыба" if info["name"] is None else info["name"]
                if info["name"]:
                    self.stats["fish"][what] = self.stats["fish"].get(what, 0) + 1
                kg = f" {info['kg']:.2f} кг" if info["kg"] else ""
                self.log(f"Улов: {what}{kg} - " + ("забрал себе" if act == "keep" else "отпустил (+опыт)") +
                         (f" ({why})" if why else ""))
                self.sleep(0.4)
            return

        mg = minigame_on(shot)
        if not mg and self.in_fight and bar_fish_on(shot) and progress_px(shot) is not None:
            # бой идёт, а «подсказки» справа внизу - чужое окошко (уведомление Discord/Steam на записи
            # 20261029_213131 закрыло их на 5 с, бот отпустил клавиши посреди боя)
            mg = True
        if mg and not self.in_fight and progress_px(shot) is None:
            # без заливки прогресса - не бой: после заброса белые уведомления внизу попадают
            # на полоску, и бот 2-3 с жал A/D впустую (запись 20261020_231750). Рыбалка при этом идёт.
            self.hold(None)
            self.ui_seen = now
            self.state = "Жду поклёвку"
            return
        if mg:
            self.ui_seen = now
            self.state = "Тяну рыбу"
            if not self.in_fight:
                self.in_fight = True
                self.tracker.reset()
                self.prog = []
                self.vh = []
                self.fb_until = 0.0
                self.fb_key = self.fb_off = None
                self.log("Рыба на крючке, тяну")
            d = self.tracker.update(shot, cfg["zone"] or sc(ZONE, W, H), now)
            p = progress_px(shot)
            if p is not None:
                self.prog = [(t, v) for t, v in self.prog if now - t < 6.0] + [(now, p)]
            self.vh = [(t, v) for t, v in self.vh if now - t < 3.0] + [(now, abs(self.tracker.v))]
            if d is not None:
                self.last_seen = now
                want = "D" if d < 0 else "A"
                if cfg["invert"]:
                    want = "A" if want == "D" else "D"
                want = self._feedback(want, now, W / BASE_W)
                self.hold(want)
            elif now - self.last_seen > cfg["hold_after_lost"]:
                self.hold(None)
            return
        self.hold(None)
        if self.in_fight:
            self.in_fight = False
            self.log("Вываживание закончилось")

        cb = cast_bar(shot)
        if cb is not None and hints_on(shot):
            self.ui_seen = now
            self.e_tries = 0
            self.state = "Заброс"
            if cfg["auto_cast"] and self.ago("cast") > 2 and self.ago("dialog_seen") > 1.0:
                self.sleep(0.25)                       # окно улова появляется чуть позже полоски
                if not dialog_on(self.grab()):
                    self.cast(cb)
            return

        if hints_on(shot):
            self.ui_seen = now
            self.e_tries = 0
            self.state = "Жду поклёвку"
            if bite(shot):
                self.bite_frames += 1
            else:
                self.bite_frames = 0
            if cfg["auto_hook"] and self.bite_frames >= 2 and self.ago("hook") > 2:
                self.sleep(random.uniform(0.08, 0.2))
                self.inp.tap(cfg["hook_key"], 60)
                self.mark("hook")
                self.stats["bites"] += 1
                self.log("Поклёвка! Подсёк")
            return

        self.state = "Не рыбачу"
        if cfg["auto_start"] and now - self.ui_seen > 3 and self.ago("start") > 6:
            if self.e_tries < 3:
                self.inp.tap(cfg["start_key"], 60)
                self.mark("start")
                self.e_tries += 1
                self.log(f"Нажал {cfg['start_key']} - начать рыбалку")
            elif self.e_tries == 3:
                self.e_tries += 1
                self.log("Рыбалка не начинается по E - подойди к воде с удочкой")


def cast_bar_band(band, x0, g0_hint, g1_hint):
    """Разбор быстрого снимка только полоски заброса (координаты - экранные)."""
    a = band.astype(np.int16)
    green = ((a[:, :, 1] - np.maximum(a[:, :, 0], a[:, :, 2])) > 60).sum(axis=0) >= 3
    white = (a.min(axis=2) > 215).sum(axis=0) >= max(6, a.shape[0] // 4)
    if green.sum() < 8:
        return None
    wx = np.nonzero(white)[0]
    return (g0_hint, g1_hint, None if len(wx) == 0 else x0 + float(wx.mean()))



# ================================================================ окно
AFTER = {"keep": "Забирать всё", "release": "Отпускать всё (ради опыта)", "filter": "По списку рыб",
         "none": "Ничего не делать"}
LEVEL_NAME = {10: "Секретная зона"}


def build_fishing_tab(parent):
    import tkinter as tk
    from tkinter import ttk

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
    ttk.Button(parent, text="Старт / стоп", style="Accent.TButton", command=toggle).grid(
        row=2, column=0, columnspan=4, sticky="ew", **pad)
    key_var = tk.StringVar(value=vk_name(cfg["toggle_vk"]))
    ttk.Label(parent, text="Горячая клавиша:").grid(row=3, column=0, sticky="w", **pad)
    ttk.Label(parent, textvariable=key_var, style="Key.TLabel", width=16).grid(row=3, column=1, sticky="w", **pad)
    ttk.Button(parent, text="Назначить", command=lambda: bind_key()).grid(row=3, column=2, sticky="w", **pad)

    # ниже - вкладки: всё на одной странице не влезало в окно (с таблицей улова - тем более)
    nb = ttk.Notebook(parent)
    nb.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
    parent.columnconfigure(3, weight=1)
    parent.rowconfigure(4, weight=1)
    tab_cyc, tab_cat, tab_set, tab_log = (ttk.Frame(nb, padding=(8, 4)) for _ in range(4))
    for fr_, name in ((tab_cyc, "Цикл рыбалки"), (tab_cat, "Улов"), (tab_set, "Настройка и отладка"), (tab_log, "Журнал")):
        nb.add(fr_, text=name)
        fr_.columnconfigure(3, weight=1)
    cyc = ttk.LabelFrame(tab_cyc, text="Цикл рыбалки")
    cyc.grid(row=0, column=0, columnspan=4, sticky="ew", **pad)
    v = {k: tk.BooleanVar(value=cfg[k]) for k in ("auto_start", "auto_cast", "auto_hook", "invert", "focus_only")}
    ttk.Checkbutton(cyc, text="1. Сам начинать рыбалку клавишей E", variable=v["auto_start"],
                    command=lambda: save()).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(cyc, text="2. Сам забрасывать точно в зелёную зону (Space)", variable=v["auto_cast"],
                    command=lambda: save()).grid(row=1, column=0, columnspan=3, sticky="w", **pad)
    lr = ttk.Frame(cyc)
    lr.grid(row=2, column=0, columnspan=3, sticky="w", padx=28)
    lat_var = tk.StringVar(value=str(cfg["latency_ms"]))
    ttk.Label(lr, text="Поправка на задержку:").pack(side="left")
    ttk.Spinbox(lr, from_=0, to=200, increment=5, width=5, textvariable=lat_var, command=lambda: save()).pack(side="left", padx=4)
    ttk.Label(lr, text="мс  (если бот жмёт поздно - увеличь, если рано - уменьши)", style="Muted.TLabel").pack(side="left")
    ttk.Checkbutton(cyc, text="3. Сам подсекать при поклёвке (Space)", variable=v["auto_hook"],
                    command=lambda: save()).grid(row=3, column=0, columnspan=3, sticky="w", **pad)
    ttk.Label(cyc, text="4. Вываживание A / D - всегда, пока бот включён", style="Muted.TLabel").grid(
        row=4, column=0, columnspan=3, sticky="w", **pad)
    ar = ttk.Frame(cyc)
    ar.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
    ttk.Label(ar, text="5. После улова:").pack(side="left")
    after_var = tk.StringVar(value=AFTER[cfg["after_catch"]])
    acb = ttk.Combobox(ar, textvariable=after_var, values=list(AFTER.values()), state="readonly", width=28)
    acb.pack(side="left", padx=6)
    acb.bind("<<ComboboxSelected>>", lambda e: (save(), show_filter()))
    ttk.Button(ar, text="Список рыб...", command=lambda: fish_window()).pack(side="left", padx=4)
    filter_var = tk.StringVar()
    ttk.Label(cyc, textvariable=filter_var, style="Muted.TLabel", wraplength=640, justify="left").grid(
        row=6, column=0, columnspan=3, sticky="w", **pad)

    def show_filter():
        if cfg["after_catch"] != "filter":
            filter_var.set("Название и вес каждой рыбы - во вкладке «Улов» и в журнале. Свой выбор рыб - кнопка «Список рыб...».")
            return
        rel = [n for n, *_ in FISH if n in cfg["fish_release"]]
        filter_var.set(f"По списку: забираю {len(FISH) - len(rel)} из {len(FISH)} рыб" +
                       (f", отпускаю: {', '.join(rel[:4])}" + (f" и ещё {len(rel) - 4}" if len(rel) > 4 else "")
                        if rel else "") + ".")

    fish_win = {"w": None}

    def fish_window():
        """Окно со всеми рыбами: включено - забирать, выключено - отпускать (+опыт)."""
        import theme as T
        if fish_win["w"] is not None and fish_win["w"].winfo_exists():
            fish_win["w"].lift()
            return
        if cfg["after_catch"] != "filter":
            after_var.set(AFTER["filter"])
            save()
            log("После улова: «По списку рыб»")
        dlg = tk.Toplevel(parent)
        fish_win["w"] = dlg
        dlg.title("Какую рыбу забирать")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        ttk.Label(dlg, text="Включено - забрать себе, выключено - отпустить (+1 опыта). Цены - из таблицы Tweet "
                            "(без VIP).", style="Muted.TLabel").grid(row=0, column=0, columnspan=3, sticky="w",
                                                                     padx=12, pady=(10, 4))
        fv = {}

        def changed():
            cfg["fish_release"] = [n for n, var in fv.items() if not var.get()]
            persist()
            show_filter()
        cols = [ttk.Frame(dlg) for _ in range(3)]
        for i, c in enumerate(cols):
            c.grid(row=1, column=i, sticky="nw", padx=6)
        where = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 2}
        for lvl in sorted({f[1] for f in FISH}):
            box = ttk.LabelFrame(cols[where[lvl]], text=LEVEL_NAME.get(lvl, f"{lvl} уровень"))
            box.pack(fill="x", padx=4, pady=3)
            for name, l_, lo, hi in FISH:
                if l_ != lvl:
                    continue
                fv[name] = tk.BooleanVar(value=name not in cfg["fish_release"])
                row = ttk.Frame(box)
                row.pack(fill="x")
                ttk.Checkbutton(row, text=name, variable=fv[name], command=changed).pack(side="left", padx=4)
                ttk.Label(row, text=f"{lo:.2f}-{hi:.2f}$", style="Muted.TLabel").pack(side="right", padx=6)
        bot = ttk.Frame(dlg)
        bot.grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=(6, 4))

        def set_all(on):
            for var in fv.values():
                var.set(on)
            changed()
        ttk.Button(bot, text="Забирать всех", command=lambda: set_all(True)).pack(side="left", padx=2)
        ttk.Button(bot, text="Отпускать всех", command=lambda: set_all(False)).pack(side="left", padx=2)
        opt2 = ttk.Frame(dlg)
        opt2.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 10))
        UNK = {"keep": "забирать", "release": "отпускать"}
        unk_var = tk.StringVar(value=UNK[cfg["fish_unknown"]])
        rec_v = tk.BooleanVar(value=cfg["keep_record"])

        def opt_changed(e=None):
            cfg["fish_unknown"] = next(k for k, t in UNK.items() if t == unk_var.get())
            cfg["keep_record"] = rec_v.get()
            persist()
        ttk.Label(opt2, text="Если название не прочиталось:").grid(row=0, column=0, sticky="w", padx=2, pady=3)
        ucb = ttk.Combobox(opt2, textvariable=unk_var, values=list(UNK.values()), state="readonly", width=12)
        ucb.grid(row=0, column=1, sticky="w", padx=6)
        ucb.bind("<<ComboboxSelected>>", opt_changed)
        ttk.Checkbutton(opt2, text="С плашкой «Рекордный улов» - забирать всегда", variable=rec_v,
                        command=opt_changed).grid(row=1, column=0, columnspan=2, sticky="w", padx=2, pady=3)
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    opt = ttk.LabelFrame(tab_set, text="Тонкая настройка")
    opt.grid(row=0, column=0, columnspan=4, sticky="ew", **pad)
    ttk.Checkbutton(opt, text="Поменять A и D местами (если бот тянет не туда)", variable=v["invert"],
                    command=lambda: save()).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(opt, text="Только когда активно окно игры", variable=v["focus_only"],
                    command=lambda: save()).grid(row=1, column=0, columnspan=3, sticky="w", **pad)
    hr = ttk.Frame(opt)
    hr.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
    hold_var = tk.StringVar(value=str(cfg["hold_after_lost"]))
    ttk.Label(hr, text="Если плашку с рыбкой не видно, держать прежнюю клавишу ещё").pack(side="left")
    ttk.Spinbox(hr, from_=0, to=5, increment=0.1, width=4, textvariable=hold_var, command=lambda: save()).pack(side="left", padx=4)
    ttk.Label(hr, text="сек").pack(side="left")
    zr = ttk.Frame(opt)
    zr.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
    zone_var = tk.StringVar()
    ttk.Button(zr, text="Выбрать зону воды", command=lambda: pick_zone()).pack(side="left")
    ttk.Button(zr, text="Сбросить", command=lambda: (cfg.update(zone=None), save(), show_zone())).pack(side="left", padx=4)
    ttk.Label(zr, textvariable=zone_var, style="Muted.TLabel").pack(side="left", padx=6)

    # ---------------------------------------------------------------- улов: какая рыба и сколько кг
    catf = ttk.LabelFrame(tab_cat, text="Какую рыбу поймал бот")
    catf.grid(row=0, column=0, columnspan=4, sticky="nsew", **pad)
    tab_cat.rowconfigure(0, weight=1)
    catf.rowconfigure(2, weight=1)
    catf.columnconfigure(0, weight=1)
    t_start = time.time()
    PERIODS = {"За этот запуск": None, "Сегодня": "day", "7 дней": 7, "30 дней": 30, "Всё время": 0}
    WHAT = {"Забрал себе": "keep", "Отпустил": "release", "Всё": "all"}
    ctop = ttk.Frame(catf)
    ctop.grid(row=0, column=0, sticky="ew", **pad)
    per_var, what_var = tk.StringVar(value="За этот запуск"), tk.StringVar(value="Забрал себе")
    for var, vals, w in ((per_var, PERIODS, 15), (what_var, WHAT, 12)):
        cb = ttk.Combobox(ctop, textvariable=var, values=list(vals), state="readonly", width=w)
        cb.pack(side="left", padx=(0, 6))
        cb.bind("<<ComboboxSelected>>", lambda e: show_catch())
    ttk.Button(ctop, text="Копировать", command=lambda: copy_catch()).pack(side="right", padx=2)
    ttk.Button(ctop, text="Очистить...", command=lambda: clear_catch()).pack(side="right", padx=2)
    cat_sum = tk.StringVar()
    ttk.Label(catf, textvariable=cat_sum, style="Status.TLabel").grid(row=1, column=0, sticky="w", **pad)
    COLS = (("n", "Штук", 60), ("kg", "Всего", 90), ("max", "Самая крупная", 110), ("avg", "Средний вес", 100))
    ctv = ttk.Treeview(catf, columns=[c for c, *_ in COLS], height=8, selectmode="browse")
    ctv.heading("#0", text="Рыба", anchor="w")
    ctv.column("#0", width=190, anchor="w")
    for c, title, w in COLS:
        ctv.heading(c, text=title)
        ctv.column(c, width=w, anchor="e", stretch=False)
    ctv.grid(row=2, column=0, sticky="nsew", **pad)
    cat_state = {"ver": -1, "per": None, "what": None, "rows": []}

    def catch_since():
        p = PERIODS[per_var.get()]
        if p is None:
            return t_start
        if p == "day":
            lt = time.localtime()
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return time.time() - p * 86400 if p else 0.0

    def show_catch(force=True):
        key = (CATCH["ver"], per_var.get(), what_var.get())
        if not force and key == (cat_state["ver"], cat_state["per"], cat_state["what"]):
            return
        cat_state.update(ver=key[0], per=key[1], what=key[2])
        rows, (n, kg, mx, nokg) = catch_summary(catch_rows(), catch_since(), WHAT[what_var.get()])
        cat_state["rows"] = rows
        ctv.delete(*ctv.get_children())
        for name, cnt, s_kg, s_max, s_no in rows:
            have = cnt - s_no
            ctv.insert("", "end", text=name, values=(cnt, fmt_kg(s_kg) if have else "-", fmt_kg(s_max) if have else "-",
                                                    fmt_kg(s_kg / have) if have else "-"))
        if not n:
            cat_sum.set("Пока ничего" + (" - рыба появится здесь после первого улова" if per_var.get() == "За этот запуск"
                                         else " за этот период"))
            return
        what = {"keep": "Забрал", "release": "Отпустил", "all": "Поймал"}[WHAT[what_var.get()]]
        cat_sum.set(f"{what}: {n} шт., {fmt_kg(kg)}" + (f"  ·  самая крупная {fmt_kg(mx)}" if mx else "") +
                    (f"  ·  вес не прочитан у {nokg}" if nokg else ""))

    def copy_catch():
        lines = [f"Улов ({per_var.get().lower()}, {what_var.get().lower()}): {cat_sum.get()}"]
        for name, cnt, s_kg, s_max, s_no in cat_state["rows"]:
            have = cnt - s_no
            lines.append(f"{name}\t{cnt} шт.\t{fmt_kg(s_kg) if have else '-'}\tмакс {fmt_kg(s_max) if have else '-'}")
        parent.clipboard_clear()
        parent.clipboard_append("\n".join(lines))
        log("Улов скопирован в буфер обмена")

    def clear_catch():
        from tkinter import messagebox
        if messagebox.askyesno("Улов", "Стереть весь журнал улова (за всё время)?", parent=parent.winfo_toplevel()):
            catch_clear()
            show_catch()
            log("Журнал улова очищен")

    dbgf = ttk.LabelFrame(tab_set, text="Отладка")
    dbgf.grid(row=1, column=0, columnspan=4, sticky="ew", **pad)
    ov_var = tk.BooleanVar(value=False)
    rec_var = tk.BooleanVar(value=False)
    rec_fight = tk.BooleanVar(value=True)
    ttk.Checkbutton(dbgf, text="Показывать оверлей поверх игры (что жмёт и что видит бот)",
                    variable=ov_var, command=lambda: toggle_overlay()).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(dbgf, text="Записывать отладочное видео", variable=rec_var,
                    command=lambda: toggle_rec()).grid(row=1, column=0, sticky="w", **pad)
    ttk.Checkbutton(dbgf, text="только вываживание", variable=rec_fight).grid(row=1, column=1, sticky="w", **pad)
    col_var = tk.BooleanVar(value=cfg["collect"])
    cr = ttk.Frame(dbgf)
    cr.grid(row=2, column=0, columnspan=3, sticky="w", **pad)

    def collect_changed():
        cfg["collect"] = col_var.get()
        persist()
        log("Сбор образцов рыб " + ("включён: снимок каждой новой рыбы - в папку fish_samples"
                                    if cfg["collect"] else "выключен"))
    ttk.Checkbutton(cr, text="Собирать образцы рыб (снимок каждой новой рыбы - для настройки)",
                    variable=col_var, command=collect_changed).pack(side="left")

    def open_samples():
        os.makedirs(SAMPLES_DIR, exist_ok=True)
        os.startfile(SAMPLES_DIR)
    ttk.Button(cr, text="Открыть папку", command=open_samples).pack(side="left", padx=6)
    rec_info = tk.StringVar(value="")
    ttk.Label(dbgf, textvariable=rec_info, style="Muted.TLabel", wraplength=640, justify="left").grid(
        row=3, column=0, columnspan=3, sticky="w", **pad)
    shared = {"fi": None, "overlay": None}

    def stats_provider():
        import widgets
        s_ = shared["fi"].stats if shared["fi"] is not None else {}
        k, r = s_.get("kept", 0), s_.get("released", 0)
        fi = shared["fi"]
        prog = None
        if fi is not None and fi.prog and time.time() - fi.prog[-1][0] < 1.0:
            prog = max(0.0, min(1.0, fi.prog[-1][1] / 520.0))
        return {"active": ctl["active"], "status": ctl["status"], "rate_of": "Поймано",
                "progress": prog, "held": fi.held if fi is not None else None, "kg": s_.get("kg", 0.0),
                "items": [("Забросов", s_.get("casts", 0)), ("Поклёвок", s_.get("bites", 0)),
                          ("Поймано", k + r)]}
    import widgets
    widgets.register("fish", stats_provider)

    def toggle_overlay():
        if ov_var.get() and shared["overlay"] is None:
            try:
                import fishing_debug as FD
                shared["overlay"] = FD.Overlay(parent.winfo_toplevel())
            except Exception as ex:
                log(f"Оверлей не запустился: {ex}")
                ov_var.set(False)
        elif not ov_var.get() and shared["overlay"] is not None:
            shared["overlay"].destroy()
            shared["overlay"] = None

    def toggle_rec():
        fi = shared["fi"]
        if fi is None:
            return
        if rec_var.get() and fi.recorder is None:
            import fishing_debug as FD
            fi.recorder = FD.Recorder(only_fight=rec_fight.get())
            rec_info.set(f"Пишу: {fi.recorder.video_path}")
            log("Отладочная запись включена")
        elif not rec_var.get() and fi.recorder is not None:
            r = fi.recorder
            fi.recorder = None
            r.close()
            rec_info.set(f"Сохранено: {r.video_path}  и  {r.csv_path}")
            log("Отладочная запись сохранена - пришли этот файл для разбора")

    def overlay_tick():
        try:
            if shared["overlay"] is not None and shared["fi"] is not None:
                shared["overlay"].draw(shared["fi"].dbg if ctl["active"] else {})
            parent.after(70, overlay_tick)
        except Exception:
            pass
    parent.after(500, overlay_tick)

    logbox = tk.Text(tab_log, height=9, width=74, state="disabled", font=("Consolas", 9))
    logbox.grid(row=0, column=0, columnspan=4, sticky="ew", **pad)

    def save():
        for k, var in v.items():
            cfg[k] = var.get()
        try:
            cfg["hold_after_lost"] = max(0.0, float(hold_var.get()))
            cfg["latency_ms"] = max(0, min(300, int(float(lat_var.get()))))
        except ValueError:
            pass
        cfg["after_catch"] = next(k for k, t in AFTER.items() if t == after_var.get())
        persist()

    def show_zone():
        zone_var.set("своя зона" if cfg["zone"] else "по умолчанию: середина экрана")

    def pick_zone():
        top = parent.winfo_toplevel()
        was = top.attributes("-topmost")
        top.withdraw()
        time.sleep(0.35)
        try:
            r = AF.pick_region(top, "Обведи участок воды, где появляются брызги и плашка с рыбкой")
        except Exception as ex:
            r = None
            log(f"Снимок экрана не получился: {ex}")
        finally:
            top.deiconify()
            top.attributes("-topmost", was)
        if r:
            img, x, y = r
            cfg["zone"] = [x, y, x + img.shape[1], y + img.shape[0]]
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
        res = {"scr": None}

        def screen():
            if res["scr"] is None:
                res["scr"] = AF.Screen()
            return res["scr"]

        def grab():
            return screen().grab()

        def grab_box(box):
            s = screen()
            x0, y0, x1, y1 = sc(box, s.mon["width"], s.mon["height"])
            shot = s.sct.grab({"left": s.mon["left"] + x0, "top": s.mon["top"] + y0,
                               "width": x1 - x0, "height": y1 - y0})
            return np.ascontiguousarray(np.asarray(shot)[:, :, :3]), x0

        fi = Fisher(cfg, inp, log, grab, grab_box)
        shared["fi"] = fi
        prev = False
        was = False
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
                fi.hold(None)
                fi.e_tries = 0
                fi.ui_seen = time.time()
                log("Рыбалка включена" if was else "Рыбалка выключена")
            if not ctl["active"] or SEWING_BUSY["on"]:
                fi.hold(None)
                BUSY["on"] = False
                ctl["status"] = ("Выключен" if switches.is_on("fish") else "Отключён в меню") \
                    if not ctl["active"] else "Пауза: работает швейный бот"
                time.sleep(0.1)
                continue
            if cfg["focus_only"] and not any(t in AF.foreground_title().lower()
                                             for t in ("gta", "rage", "majestic", "grand theft")):
                fi.hold(None)
                ctl["status"] = "Пауза: окно игры не активно"
                time.sleep(0.2)
                continue
            BUSY["on"] = True
            try:
                fi.step()
            except Exception as ex:
                fi.hold(None)
                log(f"Ошибка: {ex}")
                time.sleep(1)
            s_ = fi.stats
            ctl["status"] = fi.state
            ctl["info"] = (f"держу: {fi.held or '-'}   ·   забросов {s_['casts']}, поклёвок {s_['bites']}, "
                           f"забрал {s_['kept']} ({fmt_kg(s_['kg'])}), отпустил {s_['released']}")
            time.sleep(0.02 if fi.state in ("Тяну рыбу", "Жду поклёвку") else 0.05)
        fi.hold(None)
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
            st_lbl.configure(style="StatusOn.TLabel" if s in ("Тяну рыбу", "Жду поклёвку", "Заброс", "Улов")
                             else "Status.TLabel")
            info_var.set(ctl["info"] if ctl["active"] else f"Включить: кнопка ниже или {vk_name(cfg['toggle_vk'])}")
            show_catch(False)
        except tk.TclError:
            return
        parent.after(150, pump)

    def stop():
        ctl["quit"] = True
        ctl["active"] = False
        fi = shared.get("fi")
        if fi is not None and fi.recorder is not None:
            fi.recorder.close()
        if shared.get("overlay") is not None:
            shared["overlay"].destroy()

    show_zone()
    show_filter()
    show_catch()
    threading.Thread(target=worker, daemon=True).start()
    parent.after(150, pump)
    return stop
