# -*- coding: utf-8 -*-
"""Тёмная красно-чёрная тема для всего окна (ttk + обычные tk-виджеты).
Кнопки, поля, карточки, вкладки и полосы прокрутки - скруглённые картинки (Pillow, 9-slice):
ttk растягивает середину картинки, а углы оставляет как есть. Нет Pillow - остаётся плоский вид clam."""
import sys

BG = "#111115"          # фон страниц
SIDEBAR = "#0a0a0d"     # боковое меню
PANEL = "#17171c"       # заголовки таблиц, кнопки
CARD = "#16161c"        # карточки (рамки с настройками)
CARD_EDGE = "#25252e"
FIELD = "#1e1e24"       # поля ввода, строки таблиц
FIELD_ALT = "#19191e"   # чередование строк
BORDER = "#2c2c34"
TEXT = "#ececf1"
MUTED = "#8c8c99"
ACCENT = "#e3263d"
ACCENT_HOVER = "#ff3d55"
ACCENT_DIM = "#5c1019"  # выделенная строка
OK = "#3ecf6e"
WARN = "#ff5a5a"

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 18, "bold")
FONT_MONO = ("Consolas", 9)

BTN = "#202028"         # обычная кнопка
BTN_HOVER = "#2a2a34"
BTN_PRESS = "#191920"
BTN_EDGE = "#2f2f3a"


def dark_titlebar(root):
    """Тёмная полоса заголовка окна (Windows 10 2004+ / Windows 11)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):           # DWMWA_USE_IMMERSIVE_DARK_MODE (новый и старый номер)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val),
                                                         ctypes.sizeof(val)) == 0:
                break
        r, g, b = int(SIDEBAR[1:3], 16), int(SIDEBAR[3:5], 16), int(SIDEBAR[5:7], 16)
        color = ctypes.c_int(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color),
                                                   ctypes.sizeof(color))   # цвет заголовка (Win11)
    except Exception:
        pass


_IMGS = []
# фоны, на которых лежат скруглённые элементы: картинки «запекаются» на этот фон без прозрачности -
# полупрозрачные картинки Tk на Windows рисует очень медленно (страница перерисовывалась секунды)
BGS = {"": BG, "Card.": CARD, "Side.": SIDEBAR}


def _rgba(c, a=255):
    return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), a)


def _photo(im, w, h, bg):
    from PIL import Image, ImageTk
    im = im.resize((w, h), Image.LANCZOS)
    base = Image.new("RGBA", (w, h), _rgba(bg))
    base.alpha_composite(im)
    ph = ImageTk.PhotoImage(base.convert("RGB"))
    _IMGS.append(ph)                 # держим ссылки, иначе картинки пропадут
    return ph


def _img(w, h, r, fill=None, outline=None, width=1, inset=0, bg=BG):
    """Скруглённый прямоугольник w x h на фоне bg (рисуется в 4 раза крупнее и сглаживается)."""
    from PIL import Image, ImageDraw
    S = 4
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((inset * S, inset * S, (w - inset) * S - 1, (h - inset) * S - 1), radius=r * S,
                        fill=_rgba(fill) if fill else None,
                        outline=_rgba(outline) if outline else None, width=width * S if outline else 0)
    return _photo(im, w, h, bg)


def _chevron(w, h, up, color, bg=FIELD):
    from PIL import Image, ImageDraw
    S = 4
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy, a = w * S / 2, h * S / 2, 3.2 * S
    pts = [(cx - a, cy + a / 2), (cx, cy - a / 2), (cx + a, cy + a / 2)] if up else \
          [(cx - a, cy - a / 2), (cx, cy + a / 2), (cx + a, cy - a / 2)]
    d.line(pts, fill=_rgba(color), width=int(1.6 * S), joint="curve")
    return _photo(im, w, h, bg)


def _switch_style(st):
    """Флажки в виде современных переключателей (картинки рисуются через Pillow).
    Нет Pillow - остаётся обычный вид темы."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return

    def make(on, hover, bg):
        S, w, h, gap = 4, 36, 20, 8
        im = Image.new("RGBA", ((w + gap) * S, h * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        fill = (ACCENT_HOVER if hover else ACCENT) if on else ("#34343e" if hover else "#2a2a33")
        d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=h * S // 2, fill=fill)
        r = h * S // 2 - 3 * S
        cx = w * S - h * S // 2 if on else h * S // 2
        cy = h * S // 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#ffffff" if on else ("#a8a8b4" if hover else "#8c8c99"))
        return _photo(im, w + gap, h, bg)

    def radio(on, hover, bg):
        S, w = 4, 18
        im = Image.new("RGBA", ((w + 6) * S, w * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse((S, S, (w - 1) * S, (w - 1) * S), fill=_rgba("#2a2a33"),
                  outline=_rgba(ACCENT if on else ("#55555f" if hover else "#3a3a45")), width=int(1.5 * S))
        if on:
            d.ellipse((5 * S, 5 * S, (w - 5) * S, (w - 5) * S), fill=_rgba(ACCENT))
        return _photo(im, w + 6, w, bg)

    try:
        for pre, bg in BGS.items():
            el = pre.replace(".", "") + "Switch.indicator"
            st.element_create(el, "image", make(False, False, bg), ("selected", "active", make(True, True, bg)),
                              ("selected", make(True, False, bg)), ("active", make(False, True, bg)))
            st.layout(pre + "TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
                (el, {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]})]})])
            el = pre.replace(".", "") + "Dot.indicator"
            st.element_create(el, "image", radio(False, False, bg), ("selected", radio(True, False, bg)),
                              ("active", radio(False, True, bg)))
            st.layout(pre + "TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
                (el, {"side": "left", "sticky": ""}),
                ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Radiobutton.label", {"sticky": "nswe"})]})]})])
    except Exception:
        pass


def _round_style(st):
    """Скруглённые кнопки, поля ввода, карточки, вкладки и полосы прокрутки."""
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    R = 7
    up, dn = _chevron(18, 13, True, MUTED), _chevron(18, 13, False, MUTED)
    up_h, dn_h = _chevron(18, 13, True, ACCENT_HOVER), _chevron(18, 13, False, ACCENT_HOVER)
    st.element_create("RSpin.uparrow", "image", up, ("pressed", up_h), ("active", up_h), sticky="e")
    st.element_create("RSpin.downarrow", "image", dn, ("pressed", dn_h), ("active", dn_h), sticky="e")
    cdn, cdn_h = _chevron(22, 22, False, MUTED), _chevron(22, 22, False, ACCENT_HOVER)
    st.element_create("RCombo.downarrow", "image", cdn, ("pressed", cdn_h), ("active", cdn_h), sticky="e")
    inner = [("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]
    for pre, bg in BGS.items():
        p = pre.replace(".", "")
        # --- кнопки
        b = lambda fill, edge: _img(240, 30, R, fill=fill, outline=edge, bg=bg)
        st.element_create(p + "RButton.bg", "image", b(BTN, BTN_EDGE),
                          ("disabled", b(PANEL, "#22222a")), ("pressed", b(BTN_PRESS, ACCENT)),
                          ("active", b(BTN_HOVER, "#45454f")), border=R + 2, padding=(2, 1), width=20, height=20, sticky="nswe")
        st.element_create(p + "RAccent.bg", "image", b(ACCENT, ACCENT),
                          ("disabled", b("#4a1a22", "#4a1a22")), ("pressed", b("#b81d30", "#b81d30")),
                          ("active", b(ACCENT_HOVER, ACCENT_HOVER)), border=R + 2, padding=(2, 1), width=20, height=20, sticky="nswe")
        st.layout(pre + "TButton", [(p + "RButton.bg", {"sticky": "nswe", "children": inner})])
        st.layout(pre + "Accent.TButton", [(p + "RAccent.bg", {"sticky": "nswe", "children": inner})])
        # --- поля ввода: Entry / Spinbox / Combobox
        f = lambda edge, fill=FIELD: _img(240, 30, 6, fill=fill, outline=edge, bg=bg)
        el = p + "RField.field"
        st.element_create(el, "image", f("#2c2c36"), ("disabled", f("#24242c", PANEL)),
                          ("focus", f(ACCENT)), ("hover", f("#3e3e4a")), border=8, padding=(8, 4), width=20, height=20,
                          sticky="nswe")
        st.layout(pre + "TEntry", [(el, {"sticky": "nswe", "children": [
            ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
        st.layout(pre + "TCombobox", [(el, {"sticky": "nswe", "children": [
            ("RCombo.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]})]})])
        st.layout(pre + "TSpinbox", [(el, {"sticky": "nswe", "children": [
            ("null", {"side": "right", "sticky": "ns", "children": [
                ("RSpin.uparrow", {"side": "top", "sticky": "e"}),
                ("RSpin.downarrow", {"side": "bottom", "sticky": "e"})]}),
            ("Spinbox.padding", {"sticky": "nswe", "children": [("Spinbox.textarea", {"sticky": "nswe"})]})]})])
        # --- ползунок (Scale): тонкая дорожка + круглая ручка
        st.element_create(p + "RScale.trough", "image", _img(400, 8, 3, fill="#2a2a33", inset=1, bg=bg),
                          border=(5, 0), width=20, sticky="ew")
        knob = lambda c: _img(18, 18, 9, fill=c, outline="#ffffff", inset=1, bg=bg)
        st.element_create(p + "RScale.slider", "image", knob(ACCENT), ("pressed", knob(ACCENT_HOVER)),
                          ("active", knob(ACCENT_HOVER)), sticky="")
        st.layout(pre + "Horizontal.TScale", [(p + "RScale.trough", {"sticky": "ew", "children": [
            (p + "RScale.slider", {"side": "left", "sticky": ""})]})])
    # --- карточки (рамки с подписью): подпись над карточкой, сама карточка - скруглённая
    st.element_create("RCard.border", "image", _img(600, 300, 10, fill=CARD, outline=CARD_EDGE),
                      border=12, width=26, height=26, sticky="nswe")
    st.layout("TLabelframe", [("RCard.border", {"sticky": "nswe"})])
    # карточка внутри карточки: тот же фон, углы - под фон карточки
    st.element_create("CardRCard.border", "image", _img(600, 300, 10, fill=CARD, outline="#2c2c36", bg=CARD),
                      border=12, width=26, height=26, sticky="nswe")
    st.layout("Card.TLabelframe", [("CardRCard.border", {"sticky": "nswe"})])
    # у вкладок рамка только вокруг страниц (элемент client), ярлыки - над ней
    st.element_create("Notebook.client", "image", _img(800, 400, 10, fill=BG, outline=CARD_EDGE),
                      border=12, padding=(6, 6), width=26, height=26, sticky="nswe")
    st.layout("TNotebook", [("Notebook.client", {"sticky": "nswe"})])
    # --- вкладки: «таблетки», выбранная - красная
    t = lambda fill, edge=None: _img(160, 34, 8, fill=fill, outline=edge, inset=2)
    st.element_create("RTab.tab", "image", t(PANEL, "#22222a"),
                      ("selected", t(ACCENT_DIM, ACCENT)), ("active", t("#202028", "#33333d")),
                      border=10, padding=(4, 2), width=22, height=22, sticky="nswe")
    st.layout("TNotebook.Tab", [("RTab.tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
            ("Notebook.label", {"side": "top", "sticky": ""})]})]})])
    # --- полосы прокрутки: тонкий скруглённый ползунок без стрелок
    vt = lambda c: _img(10, 300, 4, fill=c, inset=1)
    ht = lambda c: _img(300, 10, 4, fill=c, inset=1)
    st.element_create("RV.thumb", "image", vt("#34343e"), ("pressed", vt(ACCENT)), ("active", vt("#4a4a56")),
                      border=(0, 6), width=10, height=20, sticky="nswe")
    st.element_create("RH.thumb", "image", ht("#34343e"), ("pressed", ht(ACCENT)), ("active", ht("#4a4a56")),
                      border=(6, 0), width=20, height=10, sticky="nswe")
    st.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("RV.thumb", {"expand": "1", "sticky": "nswe"})]})])
    st.layout("Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {"sticky": "we", "children": [
        ("RH.thumb", {"expand": "1", "sticky": "nswe"})]})])
    return True


# виджеты внутри карточек получают свой стиль "Card.<стиль>" с фоном карточки
_CARD_SKIP = {"Treeview", "TNotebook", "TScrollbar", "TProgressbar", "TSeparator", "TLabelframe"}
# для них нужен готовый макет "Card.<стиль>" (картинки на фоне карточки), иначе не трогаем
_CARD_LAYOUT = {"TButton", "Accent.TButton", "TEntry", "TCombobox", "TSpinbox", "TCheckbutton",
                "TRadiobutton", "Horizontal.TScale"}
_NEED_LAYOUT = {"TButton", "TEntry", "TCombobox", "TSpinbox", "TCheckbutton", "TRadiobutton", "TScale"}
_TK_RECOLOR = {"Frame", "Label", "Canvas", "Checkbutton", "Radiobutton", "Message"}


def _in_card(w):
    try:
        p = w.master
        while p is not None:
            c = p.winfo_class()
            if c == "TLabelframe":
                return True
            if c in ("TNotebook", "Toplevel", "Tk"):
                return False
            p = p.master
    except Exception:
        pass
    return False


def cardify(w, st=None, deep=True):
    """Фон виджета (и, если deep, всех вложенных) - под карточку, если он лежит внутри TLabelframe."""
    from tkinter import ttk
    st = st or ttk.Style(w)
    stack = [w]
    while stack:
        x = stack.pop()
        try:
            if deep:
                stack.extend(x.winfo_children())
            _card_one(x, st)
        except Exception:
            pass


def _card_one(x, st):
    cls = x.winfo_class()
    if cls in ("TNotebook", "Toplevel", "Tk") or not _in_card(x):
        return
    if cls == "TLabelframe":
        if not str(x.cget("style")):
            x.configure(style="Card.TLabelframe")
        return
    if cls.startswith("T"):
        if cls in _CARD_SKIP:
            return
        s = str(x.cget("style")) or cls
        if s == "TScale":
            s = str(x.cget("orient")).capitalize() + ".TScale"
        if s.startswith("Card.") or ("." not in s and s != cls):
            return
        if cls in _NEED_LAYOUT and s not in _CARD_LAYOUT:
            return
        new = "Card." + s
        if new not in _CARD_DONE:
            st.configure(new, background=CARD)
            if cls in ("TCheckbutton", "TRadiobutton"):
                st.map(new, background=[("active", CARD)])
            _CARD_DONE.add(new)
        x.configure(style=new)
    elif cls in _TK_RECOLOR:
        if str(x.cget("bg")).lower() == BG:
            x.configure(bg=CARD)
            if cls in ("Checkbutton", "Radiobutton"):
                x.configure(activebackground=CARD, selectcolor=FIELD)


_CARD_DONE = set()


def apply(root):
    from tkinter import ttk

    root.configure(bg=BG)
    # обычные tk-виджеты (Text, выпадающие списки) берут цвета отсюда
    for pat, val in (("*Text.background", FIELD), ("*Text.foreground", TEXT),
                     ("*Text.insertBackground", TEXT), ("*Text.selectBackground", ACCENT_DIM),
                     ("*Text.relief", "flat"), ("*Text.highlightThickness", 1),
                     ("*Text.highlightBackground", BORDER), ("*Text.highlightColor", ACCENT),
                     ("*Text.borderWidth", 8),
                     ("*Listbox.relief", "flat"), ("*Listbox.highlightThickness", 1),
                     ("*Listbox.highlightBackground", BORDER), ("*Listbox.highlightColor", ACCENT),
                     ("*Listbox.activeStyle", "none"),
                     ("*TCombobox*Listbox.background", FIELD), ("*TCombobox*Listbox.foreground", TEXT),
                     ("*TCombobox*Listbox.selectBackground", ACCENT),
                     ("*TCombobox*Listbox.selectForeground", "#ffffff"),
                     ("*TCombobox*Listbox.font", FONT),
                     ("*TCombobox*Listbox.highlightThickness", 0), ("*TCombobox*Listbox.borderWidth", 4),
                     ("*Menu.background", PANEL), ("*Menu.foreground", TEXT),
                     ("*Menu.activeBackground", ACCENT), ("*Menu.activeForeground", "#ffffff"),
                     ("*Menu.relief", "flat"), ("*Menu.borderWidth", 4), ("*Menu.activeBorderWidth", 0),
                     ("*Menu.font", FONT)):
        root.option_add(pat, val)

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                 lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=ACCENT,
                 selectbackground=ACCENT_DIM, selectforeground=TEXT, insertcolor=TEXT,
                 arrowcolor=TEXT, font=FONT)
    st.map(".", foreground=[("disabled", MUTED)])
    rounded = _round_style(st)

    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Muted.TLabel", foreground=MUTED)
    st.configure("Warn.TLabel", foreground=WARN)
    st.configure("Ok.TLabel", foreground=OK)
    st.configure("Big.TLabel", font=("Segoe UI", 12, "bold"))
    st.configure("Status.TLabel", font=("Segoe UI", 16, "bold"), foreground=MUTED)
    st.configure("StatusOn.TLabel", font=("Segoe UI", 16, "bold"), foreground=OK)
    st.configure("StatusErr.TLabel", font=("Segoe UI", 16, "bold"), foreground=WARN)
    st.configure("Key.TLabel", background="#1b1b22", foreground=TEXT, font=FONT_BOLD,
                 padding=(12, 6), anchor="center", relief="flat")
    st.configure("Mono.TLabel", font=FONT_MONO)

    st.configure("TButton", background=BTN, foreground=TEXT, bordercolor=BORDER,
                 lightcolor=PANEL, darkcolor=PANEL, padding=(10, 5), relief="flat", anchor="center")
    st.map("TButton", background=[("pressed", BORDER), ("active", "#24242b")],
           foreground=[("disabled", "#5d5d68"), ("active", "#ffffff")],
           bordercolor=[("active", ACCENT), ("focus", BORDER)])
    st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                 lightcolor=ACCENT, darkcolor=ACCENT, font=FONT_BOLD, padding=(12, 7))
    st.map("Accent.TButton", background=[("pressed", "#b81d30"), ("active", ACCENT_HOVER)],
           foreground=[("disabled", "#a06a72")],
           bordercolor=[("active", ACCENT_HOVER)], lightcolor=[("active", ACCENT_HOVER)],
           darkcolor=[("active", ACCENT_HOVER)])

    _switch_style(st)
    st.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=FIELD,
                 indicatorforeground="#ffffff", indicatorrelief="flat", padding=3)
    st.map("TCheckbutton", background=[("active", BG)],
           indicatorbackground=[("selected", ACCENT), ("active", BORDER)],
           foreground=[("active", "#ffffff")])
    st.configure("TRadiobutton", background=BG, foreground=TEXT, padding=3)
    st.map("TRadiobutton", background=[("active", BG)], foreground=[("active", "#ffffff")])

    st.configure("Side.TCheckbutton", background=SIDEBAR, foreground=MUTED, font=("Segoe UI", 9), padding=2)
    st.map("Side.TCheckbutton", background=[("active", SIDEBAR)], foreground=[("active", TEXT)])
    st.configure("Side.TButton", background=SIDEBAR, padding=(10, 4), font=("Segoe UI", 9))

    for w in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(w, fieldbackground=FIELD, foreground=TEXT, background=PANEL, bordercolor=BORDER,
                     lightcolor=FIELD, darkcolor=FIELD, arrowcolor=TEXT, insertcolor=TEXT,
                     padding=(2, 1) if rounded else 4)
        st.map(w, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
               fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
               foreground=[("readonly", TEXT)], background=[("active", BORDER)],
               arrowcolor=[("active", ACCENT_HOVER)])
    st.configure("TCombobox", selectbackground=FIELD, selectforeground=TEXT)
    st.configure("TSpinbox", arrowsize=12)

    if rounded:
        st.configure("Horizontal.TScale", background=BG)
    else:
        st.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD, bordercolor=BORDER,
                     lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Horizontal.TScale", background=[("active", ACCENT_HOVER)])
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 4))
    st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 7), borderwidth=0, font=FONT_BOLD)
    if rounded:
        st.configure("TNotebook.Tab", padding=(14, 6))
    st.map("TNotebook.Tab", background=[("selected", ACCENT_DIM), ("active", "#202027")],
           foreground=[("selected", "#ffffff"), ("active", TEXT)],
           expand=[("selected", (0, 0, 0, 0))], padding=[("selected", (14, 6) if rounded else (16, 8))])

    st.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                 bordercolor=CARD_EDGE, lightcolor=CARD_EDGE, darkcolor=CARD_EDGE, rowheight=26, font=FONT)
    st.map("Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", "#ffffff")])
    st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, bordercolor=PANEL,
                 lightcolor=PANEL, darkcolor=PANEL, relief="flat", font=("Segoe UI", 9, "bold"),
                 padding=(6, 6))
    st.map("Treeview.Heading", background=[("active", "#202027")], foreground=[("active", TEXT)])

    if rounded:
        # подпись - над карточкой, мелким светлым шрифтом; внутри - отступы
        st.configure("TLabelframe", background=BG, labeloutside=True, labelmargins=(4, 0, 0, 6),
                     padding=(14, 10, 14, 12))
        st.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 10))
        st.configure("Card.TLabelframe", background=CARD, labeloutside=True, labelmargins=(4, 0, 0, 6),
                     padding=(12, 8, 12, 10))
        st.configure("Card.TLabelframe.Label", background=CARD, foreground=MUTED, font=("Segoe UI Semibold", 9))
    else:
        st.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                     relief="solid", borderwidth=1, padding=8)
        st.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=FONT_BOLD)

    st.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                 arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL, width=10, arrowsize=10)
    st.configure("Horizontal.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                 arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL, width=10, arrowsize=10)
    st.configure("TSeparator", background=BORDER)
    st.configure("TProgressbar", background=ACCENT, troughcolor=FIELD, bordercolor=FIELD,
                 lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

    if rounded:
        # всё, что появляется внутри карточек (и сразу, и позже), получает фон карточки
        root.bind_all("<Map>", lambda e: _on_map(e, st), add="+")
    return st


_SEEN = set()


def _on_map(e, st):
    """Каждый виджет получает своё событие Map - обрабатываем только его и один раз."""
    w = e.widget
    if isinstance(w, str):
        return
    k = str(w)
    if k in _SEEN:
        return
    _SEEN.add(k)
    try:
        _card_one(w, st)
    except Exception:
        pass


def stripe(tree):
    """Цвета строк таблицы: чередование и "готово"."""
    tree.tag_configure("odd", background=FIELD_ALT)
    tree.tag_configure("done", foreground=OK)
