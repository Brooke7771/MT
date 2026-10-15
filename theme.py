# -*- coding: utf-8 -*-
"""Тёмная красно-чёрная тема для всего окна (ttk + обычные tk-виджеты)."""
import sys

BG = "#111115"          # фон страниц
SIDEBAR = "#0a0a0d"     # боковое меню
PANEL = "#17171c"       # заголовки таблиц, кнопки
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


def _switch_style(st):
    """Флажки в виде современных переключателей (картинки рисуются через Pillow).
    Нет Pillow - остаётся обычный вид темы."""
    try:
        from PIL import Image, ImageDraw, ImageTk
    except Exception:
        return

    def make(on):
        S, w, h, gap = 4, 36, 20, 8
        im = Image.new("RGBA", ((w + gap) * S, h * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=h * S // 2,
                            fill=ACCENT if on else "#2a2a33")
        r = h * S // 2 - 3 * S
        cx = w * S - h * S // 2 if on else h * S // 2
        cy = h * S // 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#ffffff" if on else "#8c8c99")
        return ImageTk.PhotoImage(im.resize((w + gap, h), Image.LANCZOS))

    try:
        off, on = make(False), make(True)
        _IMGS[:] = [off, on]                    # держим ссылки, иначе картинки пропадут
        st.element_create("Switch.indicator", "image", off, ("selected", on))
        st.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Switch.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
    except Exception:
        pass


def apply(root):
    from tkinter import ttk

    root.configure(bg=BG)
    # обычные tk-виджеты (Text, выпадающие списки) берут цвета отсюда
    for pat, val in (("*Text.background", FIELD), ("*Text.foreground", TEXT),
                     ("*Text.insertBackground", TEXT), ("*Text.selectBackground", ACCENT_DIM),
                     ("*Text.relief", "flat"), ("*Text.highlightThickness", 1),
                     ("*Text.highlightBackground", BORDER), ("*Text.highlightColor", ACCENT),
                     ("*Text.borderWidth", 6),
                     ("*TCombobox*Listbox.background", FIELD), ("*TCombobox*Listbox.foreground", TEXT),
                     ("*TCombobox*Listbox.selectBackground", ACCENT),
                     ("*TCombobox*Listbox.selectForeground", "#ffffff"),
                     ("*TCombobox*Listbox.font", FONT)):
        root.option_add(pat, val)

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                 lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=ACCENT,
                 selectbackground=ACCENT_DIM, selectforeground=TEXT, insertcolor=TEXT,
                 arrowcolor=TEXT, font=FONT)
    st.map(".", foreground=[("disabled", MUTED)])

    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Muted.TLabel", foreground=MUTED)
    st.configure("Warn.TLabel", foreground=WARN)
    st.configure("Ok.TLabel", foreground=OK)
    st.configure("Big.TLabel", font=("Segoe UI", 12, "bold"))
    st.configure("Status.TLabel", font=("Segoe UI", 16, "bold"), foreground=MUTED)
    st.configure("StatusOn.TLabel", font=("Segoe UI", 16, "bold"), foreground=OK)
    st.configure("StatusErr.TLabel", font=("Segoe UI", 16, "bold"), foreground=WARN)
    st.configure("Key.TLabel", background=FIELD, foreground=TEXT, font=FONT_BOLD,
                 padding=(10, 5), anchor="center", relief="flat")
    st.configure("Mono.TLabel", font=FONT_MONO)

    st.configure("TButton", background=PANEL, foreground=TEXT, bordercolor=BORDER,
                 lightcolor=PANEL, darkcolor=PANEL, padding=(12, 6), relief="flat")
    st.map("TButton", background=[("pressed", BORDER), ("active", "#24242b")],
           bordercolor=[("active", ACCENT), ("focus", BORDER)])
    st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                 lightcolor=ACCENT, darkcolor=ACCENT, font=FONT_BOLD, padding=(14, 8))
    st.map("Accent.TButton", background=[("pressed", "#b81d30"), ("active", ACCENT_HOVER)],
           bordercolor=[("active", ACCENT_HOVER)], lightcolor=[("active", ACCENT_HOVER)],
           darkcolor=[("active", ACCENT_HOVER)])

    _switch_style(st)
    st.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=FIELD,
                 indicatorforeground="#ffffff", indicatorrelief="flat", padding=2)
    st.map("TCheckbutton", background=[("active", BG)],
           indicatorbackground=[("selected", ACCENT), ("active", BORDER)],
           foreground=[("active", "#ffffff")])

    st.configure("Side.TCheckbutton", background=SIDEBAR, foreground=MUTED, font=("Segoe UI", 9), padding=2)
    st.map("Side.TCheckbutton", background=[("active", SIDEBAR)], foreground=[("active", TEXT)])

    for w in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(w, fieldbackground=FIELD, foreground=TEXT, background=PANEL, bordercolor=BORDER,
                     lightcolor=FIELD, darkcolor=FIELD, arrowcolor=TEXT, insertcolor=TEXT, padding=4)
        st.map(w, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
               fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
               foreground=[("readonly", TEXT)], background=[("active", BORDER)],
               arrowcolor=[("active", ACCENT_HOVER)])
    st.configure("TCombobox", selectbackground=FIELD, selectforeground=TEXT)

    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 8), borderwidth=0, font=FONT_BOLD)
    st.map("TNotebook.Tab", background=[("selected", ACCENT_DIM), ("active", "#202027")],
           foreground=[("selected", "#ffffff"), ("active", TEXT)])

    st.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                 bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, rowheight=24, font=FONT)
    st.map("Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", "#ffffff")])
    st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, bordercolor=BORDER,
                 lightcolor=PANEL, darkcolor=PANEL, relief="flat", font=("Segoe UI", 9, "bold"),
                 padding=(6, 5))
    st.map("Treeview.Heading", background=[("active", "#202027")], foreground=[("active", TEXT)])

    st.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                 relief="solid", borderwidth=1, padding=8)
    st.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=FONT_BOLD)

    st.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                 arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL)
    st.configure("TSeparator", background=BORDER)
    return st


def stripe(tree):
    """Цвета строк таблицы: чередование и "готово"."""
    tree.tag_configure("odd", background=FIELD_ALT)
    tree.tag_configure("done", foreground=OK)
