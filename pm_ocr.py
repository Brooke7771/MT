# -*- coding: utf-8 -*-
"""PM: распознавание скриншотов планшета («Коллеги» и «Вызовы» → «История») встроенным OCR Windows.

Бесплатно и без интернета: Windows.Media.Ocr (есть в Windows 10/11, языки ru и en).
Тип экрана и положение таблицы ищутся по чёрным заголовкам колонок («Должность»/«Статус» или
«Сотрудник»/«Описание»), остальная разметка пересчитывается от них - поэтому подходят и полные
скрины 1920x1080, и обрезанные, и другой масштаб. Каждая колонка читается отдельно своим движком:
ники, статики и ранги - английским (русский путает l/I, D/0), статусы и причины - русским.
Статус в сети берётся по цвету точки справа (текст часто перекрывают подсказки).
Программа не трогает игру: только читает готовые картинки."""
import re
import asyncio
import threading

from PIL import Image

# разметка по эталонному скрину 1920x1080 (координаты экрана; дальше - линейный пересчёт по двум якорям)
LAYOUT = {
    "colleagues": {
        "anchors": (("должность", 689), ("статус", 1407)), "header_y": 269, "row_h": 45,
        "cols": {"num_name": (376, 594, "en"), "uid": (594, 668, "en"),
                 "post": (680, 890, "en"), "rank_dept": (898, 1075, "en"), "today": (1088, 1234, "ru"),
                 "month": (1238, 1370, "ru"), "status": (1376, 1518, "ru")},
        "dot": (1521, 1538), "ref": "uid", "dept": (985, 1075),
    },
    "calls": {
        "anchors": (("сотрудник", 388), ("описание", 1289)), "header_y": 276, "row_h": 45,
        "cols": {"employee": (378, 616, "en"), "uid": (618, 708, "en"), "citizen": (722, 860, "en"),
                 "number": (862, 976, "en"), "reason": (978, 1098, "ru"), "status": (1100, 1226, "ru"),
                 "desc": (1228, 1416, "ru"), "date": (1418, 1550, "en")},
        "ref": "date",
    },
}
STATUS_NAME = {"active": "Сейчас активен", "away": "Нет на месте", "arena": "На арене", "jail": "В тюрьме", "offline": "Не в сети",
               "left": "Вышел", "unknown": "?"}
ONLINE = {"active", "away", "arena", "jail"}           # в игре (для «зашёл/вышел»)
ASK = {"active", "away"}                               # кому писать /f, если не катает вызовы
DATE_RE = re.compile(r"(\d{1,2})[.,](\d{1,2})[.,](\d{2}|\d{4})\s*([01]?\d|2[0-3])[:.;]([0-5]\d)")
TIME_RE = re.compile(r"(\d{1,2})[:.;](\d{2})\s*$")

_tls = threading.local()


class OcrError(Exception):
    pass


# ====================================================================== движок OCR
def available():
    """-> (ok, сообщение). Есть ли OCR Windows с русским и английским."""
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        miss = [l for l in ("ru", "en-US") if not OcrEngine.is_language_supported(Language(l))]
        if miss:
            return False, ("В Windows нет языка распознавания: " + ", ".join(miss) +
                           ". Параметры → Время и язык → Язык → добавить русский/английский (с распознаванием текста)")
        return True, "OCR Windows готов"
    except Exception as ex:
        return False, f"Распознавание Windows недоступно: {ex}"


def _engine(lang):
    eng = getattr(_tls, "eng", None)
    if eng is None:
        eng = _tls.eng = {}
    if lang not in eng:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        e = OcrEngine.try_create_from_language(Language(lang))
        if e is None:
            raise OcrError(f"Нет языка OCR: {lang}")
        eng[lang] = e
    return eng[lang]


def ocr(im, lang="ru"):
    """Слова картинки PIL: [(текст, x, y, w, h)] в координатах этой картинки."""
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
    from winrt.windows.storage.streams import DataWriter
    im = im.convert("RGBA")
    wr = DataWriter()
    wr.write_bytes(im.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(wr.detach_buffer(), BitmapPixelFormat.RGBA8, im.width, im.height)
    eng = _engine("en-US" if lang == "en" else lang)

    async def run():
        return await eng.recognize_async(bmp)
    res = asyncio.run(run())
    out = []
    for line in res.lines:
        for w in line.words:
            b = w.bounding_rect
            out.append((w.text, b.x, b.y, b.width, b.height))
    return out


# ====================================================================== разбор скрина
def _norm(s):
    return s.lower().replace("ё", "е").strip(".,:;")


def find_table(words):
    """По словам всего скрина: (тип, a, b, header_y) - x_экрана = a*x_эталона + b. Или None."""
    best = None
    for kind, L in LAYOUT.items():
        (w1, x1), (w2, x2) = L["anchors"]
        c1 = [w for w in words if _norm(w[0]) == w1]
        c2 = [w for w in words if _norm(w[0]) == w2]
        for p in c1:
            for q in c2:
                cy1, cy2 = p[2] + p[4] / 2, q[2] + q[4] / 2
                if abs(cy1 - cy2) > max(p[4], q[4]) or q[1] <= p[1]:
                    continue
                a = (q[1] - p[1]) / float(x2 - x1)
                if not 0.4 < a < 2.5:
                    continue
                cand = (kind, a, p[1] - a * x1, (cy1 + cy2) / 2)
                if best is None or abs(a - 1) < abs(best[1] - 1):
                    best = cand
    return best


def _table_bottom(img, xa, xb, y0, a):
    """Низ белой таблицы: первая строка пикселей ниже заголовка, где средняя яркость по ширине таблицы
    падает (рамка планшета или игра). Текст строк так не затемняет - он занимает малую долю ширины."""
    import numpy as np
    W, H = img.size
    xa, xb = int(max(0, xa)), int(min(W, xb))
    band = np.asarray(img.crop((xa, int(y0), xb, H)).convert("L"), dtype=np.float32).mean(axis=1)
    dark = np.where(band < 150)[0]
    return int(y0 + dark[0]) - 1 if len(dark) else H - 1


def _digits(s):
    s = s.translate(str.maketrans({"O": "0", "o": "0", "О": "0", "о": "0", "D": "0", "I": "1", "l": "1", "|": "1",
                                   "і": "1", "!": "1", "З": "3", "з": "3", "S": "5", "B": "8", "В": "8"}))
    return re.sub(r"\D", "", s)


def _clean_name(s):
    s = re.sub(r"[^A-Za-zА-Яа-яЁё' \-]", "", s).strip()
    return re.sub(r"\s+", " ", s)


# ---- свои цифры: OCR Windows не читает одиночный символ («9» в колонке ранга), поэтому ранг узнаём
# по образцам цифр, взятым с того же скрина из уже прочитанных статиков (шрифт и масштаб совпадают)
GLYPH = (12, 18)


def _glyphs(im):
    """Тёмные символы на светлом фоне -> список нормированных картинок (numpy) слева направо."""
    import numpy as np
    import cv2
    g = np.asarray(im.convert("L"))
    if g.size == 0:
        return []
    mask = (g < 150).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    comps = [stats[i] for i in range(1, n) if stats[i][3] >= 4 and stats[i][4] >= 6]
    if not comps:
        return []
    hmax = max(c[3] for c in comps)
    comps = sorted((c for c in comps if c[3] >= hmax * 0.6), key=lambda c: c[0])
    out = []
    for x, y, w, h, _ in comps:
        crop = mask[y:y + h, x:x + w].astype(np.float32)
        out.append(cv2.resize(crop, GLYPH, interpolation=cv2.INTER_AREA))
    return out


class Digits:
    def __init__(self):
        self.t = {}

    def learn(self, im, text):
        gs = _glyphs(im)
        if len(gs) != len(text) or not text.isdigit():
            return
        for g, ch in zip(gs, text):
            self.t.setdefault(ch, []).append(g)

    def read(self, im):
        """-> (строка цифр, уверенность 0..1)."""
        import numpy as np
        if not self.t:
            return "", 0.0
        means = {ch: np.mean(v, axis=0) for ch, v in self.t.items()}
        s, conf = "", 1.0
        for g in _glyphs(im):
            best, bd, second = None, 1e9, 1e9
            for ch, m in means.items():
                d = float(np.mean((g - m) ** 2))
                if d < bd:
                    best, bd, second = ch, d, bd
                elif d < second:
                    second = d
            s += best
            conf = min(conf, 1.0 - bd / max(second, 1e-6))
        return s, conf


def _dot_status(img, xa, xb, yc, h):
    """Цвет точки статуса справа в строке -> active/away/arena/jail/offline/unknown."""
    px = img.load()
    W, H = img.size
    best, bs = None, -1
    for y in range(int(yc - h * 0.25), int(yc + h * 0.25) + 1):
        for x in range(int(xa), int(xb) + 1):
            if 0 <= x < W and 0 <= y < H:
                r, g, b = px[x, y][:3]
                mx, mn = max(r, g, b), min(r, g, b)
                dk = 255 - mx
                score = (mx - mn) * 2 + max(0, 230 - (r + g + b) / 3)     # насыщенность или темнее белого фона
                if score > bs:
                    best, bs = (r, g, b), score
    if best is None or bs < 25:
        return "unknown"
    r, g, b = best
    mx, mn = max(best), min(best)
    if mx - mn < 30:
        return "offline"                                       # серая
    import colorsys
    hue = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360
    if 75 <= hue <= 170:
        return "active"
    if 35 <= hue < 75:
        return "away"
    if 180 <= hue <= 260:
        return "arena"
    if hue < 35 or hue > 330:
        return "jail"                                          # оранжевая
    return "unknown"


def recognize(path_or_img):
    """Скрин -> {"kind": "colleagues"|"calls", "rows": [...]} или {"kind": None, "error": ...}.
    Строки «Коллеги»: num, name, uid, rank, post, today, month, status, offline_since, doubt (список полей).
    Строки «Вызовы»: employee, uid, citizen, reason, status, desc, date, time, doubt."""
    img = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
    img = img.convert("RGB")
    # 1) общий проход - найти заголовок таблицы (уменьшаем большие скрины для скорости)
    k = min(1.0, 2000.0 / max(img.size))
    small = img.resize((int(img.width * k), int(img.height * k))) if k < 1 else img
    words = [(t, x / k, y / k, w / k, h / k) for t, x, y, w, h in ocr(small, "ru")]
    tab = find_table(words)
    if not tab:
        grp = _group(img, words)
        if grp:
            return grp
        return {"kind": None, "rows": [], "error": "Не нашёл таблицу «Коллеги», «Вызовы» (История) или группу в телефоне"}
    kind, a, b, hy = tab
    L = LAYOUT[kind]
    X = lambda x: a * x + b
    y0 = hy + a * 22                                            # верх первой строки
    xs = [v[0] for v in L["cols"].values()] + [v[1] for v in L["cols"].values()]
    y1 = _table_bottom(img, X(min(xs)), X(max(xs)), y0, a)
    rh = L["row_h"] * a
    up = max(1.0, 2.2 / a)                                      # мелкий текст увеличиваем
    cols = {}
    for c, (xa, xb, lang) in L["cols"].items():
        box = (int(max(0, X(xa))), int(max(0, y0)), int(min(img.width, X(xb))), int(min(img.height, y1)))
        if box[2] - box[0] < 5 or box[3] - box[1] < 5:
            cols[c] = []
            continue
        crop = img.crop(box)
        if up > 1:
            crop = crop.resize((int(crop.width * up), int(crop.height * up)), Image.LANCZOS)
        cols[c] = [(t, box[0] + x / up, box[1] + y / up, w / up, h / up) for t, x, y, w, h in ocr(crop, lang)]
    # 2) строки - по колонке, которая есть в каждой строке
    ref = sorted(cols[L["ref"]], key=lambda w: w[2] + w[4] / 2)
    groups = []
    for w in ref:
        cy = w[2] + w[4] / 2
        if groups and abs(groups[-1][0] - cy) < rh * 0.35:
            groups[-1][1].append(w)
        else:
            groups.append([cy, [w]])
    if not groups:
        return {"kind": kind, "rows": [], "error": "Таблица пустая или не прочиталась"}
    hmed = sorted(max(x[4] for x in g[1]) for g in groups)[len(groups) // 2]
    digits = Digits()
    if kind == "colleagues":                                    # образцы цифр - из прочитанных статиков
        for t, x, y, w, h in cols["uid"]:
            pad = 2 * a
            digits.learn(img.crop((int(x - pad), int(y - pad), int(x + w + pad), int(y + h + pad))), _digits(t))
    rows, cys = [], []
    for cy, g in groups:
        top, bot = min(x[2] for x in g), max(x[2] + x[4] for x in g)
        if bot - top < hmed * 0.72 or top < y0 + 1 or bot > y1 - 1:
            continue                                            # строка срезана прокруткой - пропускаем

        def cell(c):
            ws = sorted((x for x in cols[c] if abs(x[2] + x[4] / 2 - cy) < rh * 0.4), key=lambda x: x[1])
            return " ".join(x[0] for x in ws).strip()
        if kind == "colleagues":
            rows.append(_colleague(img, L, X, cy, rh, cell, digits))
            cys.append(cy)
        else:
            rows.append(_call(cell))
    if kind == "colleagues" and rows:
        for row, d in zip(rows, _depts(img, X(L["dept"][0]), X(L["dept"][1]), cys, rh, a)):
            row["dept"] = d
    return {"kind": kind, "rows": rows, "error": ""}


# ---- отдел: короткое слово («PM») OCR Windows читает ненадёжно - только на одном масштабе из нескольких.
# Поэтому ячейки отдела группируем по картинке (шрифт один, отделов немного), каждую группу выкладываем
# сеткой (OCR не берёт картинки больше 2600 px) и читаем на нескольких масштабах, пока не прочтётся.
LATIN = str.maketrans("АВЕКМНОРСТХУавекмнорстху", "ABEKMHOPCTXYABEKMHOPCTXY")
DEPT_SCALES = (1.5, 1.3, 1.7, 1.2, 2.0, 1.0)


def _depts(img, xa, xb, cys, rh, a):
    import numpy as np
    cells, vecs = [], []
    for cy in cys:
        c = img.crop((int(xa), int(cy - rh * 0.35), int(xb), int(cy + rh * 0.35)))
        g = np.asarray(c.convert("L"), dtype=np.float32)
        ink = g < 140
        if ink.sum() < 6:
            cells.append(None)
            vecs.append(None)
            continue
        ys, xs = np.where(ink)
        t = Image.fromarray((ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype(np.uint8))
        cells.append(c)
        vecs.append((np.asarray(t.resize((48, 14), Image.BILINEAR), dtype=np.float32) / 255, t.width / max(1, t.height)))
    groups = []                                                 # [[индексы], образец]
    for i, v in enumerate(vecs):
        if v is None:
            continue
        for gr in groups:
            ref = vecs[gr[0]]
            if abs(ref[1] - v[1]) < 0.25 * ref[1] and float(np.mean((ref[0] - v[0]) ** 2)) < 0.02:
                gr.append(i)
                break
        else:
            groups.append([i])
    out = [""] * len(cys)
    for gr in groups:
        text = _dept_text([cells[i] for i in gr[:15]], a)
        for i in gr:
            out[i] = text
    return out


def _dept_text(cells, a, per=5):
    from collections import Counter
    cw, ch = cells[0].size
    for sc in DEPT_SCALES:
        up = sc / a
        w, h = max(1, int(cw * up)), max(1, int(ch * up))
        gx, gy = 40, 20
        n = len(cells)
        sheet = Image.new("RGB", (min(n, per) * (w + gx) + gx, ((n + per - 1) // per) * (h + gy) + gy), (255, 255, 255))
        for i, c in enumerate(cells):
            sheet.paste(c.resize((w, h), Image.LANCZOS), (gx + (i % per) * (w + gx), gy + (i // per) * (h + gy)))
        for lang in ("en", "ru"):
            got = Counter()
            for t, x, y, ww, hh in ocr(sheet, lang):
                t = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", t.translate(LATIN)).upper()
                if t:
                    got[t] += 1
            if got:
                return got.most_common(1)[0][0]
    return "?"


# ---- группа в игровом телефоне: карточки «Имя» (жирно, чёрным) и «Фамилия» (серым) под ним, справа метки отделов.
# Опоры: «Индикатор над головой» над списком и кнопка «Расформировать группу» (у участника - «Покинуть группу») под ним (между ними ~425 px при 1080p).
GROUP_SPAN = 425.0


def _group(img, words):
    ind = [w for w in words if _norm(w[0]) == "индикатор"]
    dis = [w for w in words if _norm(w[0]).startswith(("расформир", "покинут"))]   # лидер группы / участник
    if not ind or not dis:
        return None
    wi, wd = ind[0], min(dis, key=lambda w: abs(w[1] - ind[0][1]))
    yi, yb = wi[2] + wi[4] / 2, wd[2] + wd[4] / 2
    k = (yb - yi) / GROUP_SPAN
    if not 0.4 < k < 3:
        return None
    x0, x1 = wi[1] + 48 * k, wi[1] + 165 * k
    y0, y1 = yi + 16 * k, yb - 42 * k
    box = (int(max(0, x0)), int(max(0, y0)), int(min(img.width, x1)), int(min(img.height, y1)))
    crop = img.crop(box)
    up = max(1.0, 2.5 / k)
    big = crop.resize((int(crop.width * up), int(crop.height * up)), Image.LANCZOS)
    ws = [(t, x / up, y / up, w / up, h / up) for t, x, y, w, h in ocr(big, "en")]
    gray = crop.convert("L").load()
    lines = []
    for w in sorted(ws, key=lambda w: w[2] + w[4] / 2):
        cy = w[2] + w[4] / 2
        if lines and abs(lines[-1]["cy"] - cy) < 8 * k:
            lines[-1]["w"].append(w)
        else:
            lines.append({"cy": cy, "w": [w]})
    for ln in lines:
        ln["w"].sort(key=lambda w: w[1])
        ln["text"] = _clean_name(" ".join(w[0] for w in ln["w"]))
        top = min(w[2] for w in ln["w"])
        bot = max(w[2] + w[4] for w in ln["w"])
        ln["cut"] = top < 2 or bot > crop.height - 2
        # имя чёрное, фамилия серая: самый тёмный пиксель в рамке слова
        dark = 255
        for w in ln["w"]:
            for yy in range(int(w[2]), int(w[2] + w[4])):
                for xx in range(int(w[1]), int(w[1] + w[3]), 2):
                    if 0 <= xx < crop.width and 0 <= yy < crop.height:
                        dark = min(dark, gray[xx, yy])
        ln["first"] = dark < 85
    rows = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if ln["first"] and not ln["cut"] and ln["text"]:
            if nxt and not nxt["first"] and nxt["cy"] - ln["cy"] < 32 * k and not nxt["cut"]:
                rows.append({"name": f"{ln['text']} {nxt['text']}".strip(), "first": ln["text"], "last": nxt["text"],
                             "doubt": [] if nxt["text"] else ["name"]})
                i += 2
                continue
            if not nxt or nxt["first"]:
                rows.append({"name": ln["text"], "first": ln["text"], "last": "", "doubt": ["name"]})
        i += 1
    # сколько всего в группе: число рядом со значком над поиском
    count = None
    for w in words:
        if w[0].isdigit() and abs((w[2] + w[4] / 2) - (yi - 56 * k)) < 12 * k and w[1] < wi[1] + 60 * k:
            count = int(w[0])
            break
    return {"kind": "group", "rows": rows, "count": count, "error": ""}


def _colleague(img, L, X, cy, rh, cell, digits):
    doubt = []
    m = re.match(r"\s*([0-9OoIl|]{1,4})\s+(.+)$", cell("num_name"))
    num, name = (_digits(m.group(1)), m.group(2)) if m else ("", cell("num_name"))
    name = _clean_name(name)
    uid = _digits(cell("uid"))
    rd = cell("rank_dept").split()
    # первое слово - ранг, только если похоже на число: одиночную цифру OCR не видит, и тогда первым идёт
    # отдел («SURG» иначе стал бы рангом 5, «MED» - 0)
    rank = _digits(rd[0]) if rd and re.fullmatch(r"[0-9Il|!]{1,2}", rd[0]) else ""
    rank = rank[:2]
    if not rank:                                                # одиночная цифра - по образцам
        r_, conf = digits.read(img.crop((int(X(900)), int(cy - rh * 0.3), int(X(966)), int(cy + rh * 0.3))))
        if r_ and len(r_) <= 2:
            rank = r_
            if conf < 0.25:
                doubt.append("rank")
    st_text = cell("status")
    if not (4 <= len(uid) <= 6):
        doubt.append("uid")
    if not rank or not 1 <= int(rank) <= 20:
        doubt.append("rank")
    if len(name) < 3 or " " not in name:
        doubt.append("name")
    m = DATE_RE.search(st_text.replace(" ", " "))
    dot = _dot_status(img, X(L["dot"][0]), X(L["dot"][1]), cy, rh)
    since = None
    if m:
        d, mo, y, hh, mm = m.groups()
        y = y if len(y) == 4 else "20" + y
        since = f"{int(d):02d}.{int(mo):02d}.{y} {int(hh):02d}:{mm}"
        status = "offline"
    elif "актив" in st_text.lower():
        status = "active" if dot in ("active", "unknown") else dot
    elif "мест" in st_text.lower():
        status = "away"
    elif "арен" in st_text.lower():
        status = "arena"
    elif "тюрьм" in st_text.lower():
        status = "jail"
    else:
        status = dot
    if status == "unknown":
        doubt.append("status")
    return {"num": int(num) if num else None, "name": name, "uid": uid or None, "rank": int(rank) if rank else None,
            "post": cell("post"), "today": cell("today"), "month": cell("month"), "status": status,
            "offline_since": since, "doubt": doubt}


def _call(cell):
    doubt = []
    emp = _clean_name(cell("employee"))
    uid = _digits(cell("uid"))
    if len(emp) < 3:
        emp = None
    if emp and not 3 <= len(uid) <= 6:
        doubt.append("uid")
    date = cell("date")
    m = DATE_RE.search(date)
    t = None
    if m:
        d, mo, y, hh, mm = m.groups()
        y = y if len(y) == 4 else "20" + y
        date = f"{int(d):02d}.{int(mo):02d}.{y} {int(hh):02d}:{mm}"
        t = f"{int(hh):02d}:{mm}"
    else:
        doubt.append("date")
    return {"employee": emp, "uid": uid if emp and uid else None, "citizen": _digits(cell("citizen")) or None,
            "reason": cell("reason"), "status": cell("status"), "desc": cell("desc"), "date": date, "time": t,
            "doubt": doubt}
