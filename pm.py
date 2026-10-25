# -*- coding: utf-8 -*-
"""Вкладка «PM»: таблица сотрудников по скриншотам планшета («Коллеги» и «Вызовы» → «История»).
Скрины: кнопкой, из буфера (Win+Shift+S → Ctrl+V) или из папки, за которой следим.
Распознавание - pm_ocr (OCR Windows, бесплатно, без интернета), снимки и сравнение - pm_track.
Игру не трогает: только читает готовые картинки."""
import os
import time
import json
import queue
import threading

import pm_ocr
import pm_track as PT
import pm_stats as PS

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
NEW_ROUND = 5 * 60          # скрин «Коллеги» позже прошлых на столько секунд - новая проверка
# клавиши для скрина игры (виртуальные коды Windows); игра их не занимает
SHOT_KEYS = {0: "выкл", 0x6A: "Numpad *", 0x6F: "Numpad /", 0x6E: "Numpad .", 0x2D: "Insert", 0x24: "Home",
             0x23: "End", 0x21: "PageUp", 0x22: "PageDown", 0x13: "Pause", 0x91: "ScrollLock"}
SHOT_KEYS.update({0x70 + i: f"F{i + 1}" for i in range(12)})
STATE = {"n": 0, "online": 0, "calls": 0, "ask": 0, "changes": 0, "busy": "", "shots": 0, "ok": True}


def _cfg_path():
    return os.path.join(PT.data_dir(), "pm_config.json")


def load_cfg():
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(c):
    try:
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def build_pm_tab(parent, afk=False):
    """afk=True - с автоматической проверкой (бот сам достаёт планшет, снимает скрины, смотрит баланс)."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import theme as T
    import widgets

    cfg = load_cfg()
    cfg["filter"] = PT.filter_cfg(cfg.get("filter"))
    cfg.setdefault("white", [])
    cfg.setdefault("shot_vk", 0x6A)
    cfg.setdefault("shot_beep", True)
    q = queue.Queue()
    work = queue.Queue()
    st = {"alive": True, "shots": [], "snap": None, "old": None, "old_label": "", "watch_seen": set(),
          "watch_t": 0.0, "edits": {}, "marks": PT.load_marks(), "base": PT.load_roster()}
    st["roster"] = st["base"]
    st["bal"] = {}               # uid -> результат проверки по «Управлению балансом»
    ok, ocr_msg = pm_ocr.available()
    STATE["ok"] = ok

    # ================================================================ верхняя панель
    bar = ttk.Frame(parent)
    bar.pack(fill="x", padx=8, pady=(0, 4))
    ttk.Button(bar, text="Добавить скрины…", style="Accent.TButton", command=lambda: add_files()).pack(side="left")
    ttk.Button(bar, text="Вставить из буфера (Ctrl+V)", command=lambda: paste()).pack(side="left", padx=4)
    ttk.Button(bar, text="Очистить", command=lambda: clear()).pack(side="left", padx=4)
    ttk.Button(bar, text="Новый порядок", command=lambda: new_order()).pack(side="left", padx=4)
    watch_var = tk.BooleanVar(value=bool(cfg.get("watch_on")))
    folder = {"path": cfg.get("watch_dir", "")}
    ttk.Checkbutton(bar, text="Следить за папкой:", variable=watch_var, command=lambda: watch_toggle()).pack(
        side="left", padx=(16, 4))
    def short(d):                                                   # длинный путь не должен выталкивать кнопки
        return d if len(d) <= 40 else "…" + d[-38:]
    folder_lbl = ttk.Label(bar, text=short(folder["path"]) or "не выбрана", style="Muted.TLabel")
    folder_lbl.pack(side="left")
    ttk.Button(bar, text="Выбрать…", command=lambda: pick_folder()).pack(side="left", padx=4)
    bar2 = ttk.Frame(parent)
    bar2.pack(fill="x", padx=8, pady=(0, 4))
    ttk.Label(bar2, text="Скрин игры по клавише:").pack(side="left")
    key_var = tk.StringVar(value=SHOT_KEYS.get(cfg["shot_vk"], "Numpad *"))
    key_cb = ttk.Combobox(bar2, textvariable=key_var, values=list(SHOT_KEYS.values()), state="readonly", width=10)
    key_cb.pack(side="left", padx=4)
    ttk.Button(bar2, text="Скрин через 3 с", command=lambda: shot_later()).pack(side="left", padx=4)
    beep_var = tk.BooleanVar(value=bool(cfg["shot_beep"]))
    ttk.Checkbutton(bar2, text="Звук", variable=beep_var,
                    command=lambda: (cfg.update(shot_beep=bool(beep_var.get())), save_cfg(cfg))).pack(side="left", padx=(8, 0))
    filter_var = tk.BooleanVar(value=bool(cfg["filter"]["on"]))
    ttk.Checkbutton(bar2, text="Фильтр", variable=filter_var, command=lambda: filter_toggle()).pack(side="left", padx=(24, 4))
    ttk.Button(bar2, text="Настроить фильтр…", command=lambda: filter_dialog()).pack(side="left", padx=4)
    white_btn = ttk.Button(bar2, text="Вайтлист…", command=lambda: white_dialog())
    white_btn.pack(side="left", padx=4)
    bar_rec = ttk.Frame(parent)
    bar_rec.pack(fill="x", padx=8, pady=(0, 4))
    import pm_screenrec
    rec_stop = pm_screenrec.build_rec_bar(bar_rec, cfg, save_cfg, lambda m: msg.set(m),
                                          {k: v for k, v in SHOT_KEYS.items()})
    afk_ui = {}
    if afk:
        import pm_afk
        cfg["afk"] = pm_afk.afk_cfg(cfg.get("afk"))
        if not isinstance(cfg.get("afk_rules"), list):
            cfg["afk_rules"] = pm_afk.default_rules()
        have = {r_.get("event") for r_ in cfg["afk_rules"]}
        cfg["afk_rules"] += [r_ for r_ in pm_afk.default_rules() if r_["event"] in pm_afk.COUNTED and r_["event"] not in have]
        save_cfg(cfg)
        bar3 = ttk.Frame(parent)
        bar3.pack(fill="x", padx=8, pady=(0, 4))
        afk_ui["btn"] = ttk.Button(bar3, text="Автопроверка: выкл", command=lambda: afk_toggle())
        afk_ui["btn"].pack(side="left")
        ttk.Button(bar3, text="Проверить сейчас", command=lambda: afk_ui["run"].check_now()).pack(side="left", padx=4)
        ttk.Button(bar3, text="Настройки…", command=lambda: afk_settings()).pack(side="left", padx=4)
        ttk.Button(bar3, text="Правила наблюдения…", command=lambda: rules_dialog()).pack(side="left", padx=4)
        ttk.Button(bar3, text="Проверить по балансу", style="Accent.TButton",
                   command=lambda: balance_check()).pack(side="left", padx=(16, 4))
        ttk.Label(bar3, text="(окно - внизу: «Работает - вызов или баланс за…»)", style="Muted.TLabel").pack(side="left")
        afk_ui["var"] = tk.StringVar(value="Автоматическая проверка выключена: бот сам достаёт планшет, снимает скрины "
                                           "и смотрит баланс по таймеру")
        ttk.Label(parent, textvariable=afk_ui["var"], style="Muted.TLabel").pack(anchor="w", padx=10)
        afk_ui["stats"] = tk.StringVar(value=pm_afk.stats_text(pm_afk.load_state().get("stats")))
        ttk.Label(parent, textvariable=afk_ui["stats"], style="Muted.TLabel").pack(anchor="w", padx=10)
    msg = tk.StringVar(value=("Добавь скрины «Коллеги», «Вызовы» (История) и группы в телефоне. Можно несколько: они склеятся."
                              if ok else "⚠ " + ocr_msg))
    ttk.Label(parent, textvariable=msg, style="Muted.TLabel" if ok else "Warn.TLabel").pack(anchor="w", padx=10)

    body = ttk.Frame(parent)
    body.pack(fill="both", expand=True, padx=8, pady=4)
    # ---- слева: скрины
    left = ttk.LabelFrame(body, text="Скрины")
    left.pack(side="left", fill="y", padx=(0, 6))
    lb = tk.Listbox(left, width=34, bg=T.FIELD, fg=T.TEXT, selectbackground=T.ACCENT, relief="flat",
                    highlightthickness=0, activestyle="none", font=("Segoe UI", 9))
    lb.pack(fill="both", expand=True, padx=6, pady=6)
    ttk.Button(left, text="Убрать выбранный", command=lambda: remove_shot()).pack(fill="x", padx=6, pady=(0, 6))

    # ---- справа: результат
    nb = ttk.Notebook(body)
    nb.pack(side="left", fill="both", expand=True)
    tabs = {}
    for k_, t_ in (("emp", "Сотрудники"), ("calls", "Вызовы"), ("diff", "Изменения"), ("f", "Тексты /f")):
        f = ttk.Frame(nb)
        nb.add(f, text=t_)
        tabs[k_] = f
    try:
        nb.select(int(cfg.get("tab", 0)))
    except (tk.TclError, ValueError):
        pass
    nb.bind("<<NotebookTabChanged>>", lambda e: (cfg.update(tab=nb.index("current")), save_cfg(cfg)))

    cols = ("num", "name", "cn", "uid", "cu", "rank", "dept", "status", "calls", "group", "brief", "comment")
    et = ttk.Treeview(tabs["emp"], columns=cols, show="headings", selectmode="browse")
    for c, h, w, a in (("num", "№", 36, "e"), ("name", "Ник", 160, "w"), ("cn", "", 24, "center"),
                       ("uid", "Статик", 64, "e"), ("cu", "", 24, "center"),
                       ("rank", "Ранг", 44, "e"), ("dept", "Отдел", 56, "center"), ("status", "Статус", 130, "w"), ("calls", "Работа", 170, "w"),
                       ("group", "Группа", 60, "center"), ("brief", "Брифинг", 64, "center"),
                       ("comment", "Комментарий", 120, "w")):
        et.heading(c, text=h)
        et.column(c, width=w, anchor=a, stretch=c not in ("cn", "cu"))
    esb = ttk.Scrollbar(tabs["emp"], orient="vertical", command=et.yview)
    et.configure(yscrollcommand=esb.set)
    et.pack(side="left", fill="both", expand=True, pady=4)
    esb.pack(side="right", fill="y", pady=4)
    et.tag_configure("odd", background=T.FIELD_ALT)
    et.tag_configure("doubt", foreground="#ffb347")
    et.tag_configure("off", foreground=T.MUTED)
    et.tag_configure("calls", foreground=T.OK)
    et.tag_configure("bal", foreground="#4fd1c5")
    et.tag_configure("left", foreground="#e06c6c")

    ct = ttk.Treeview(tabs["calls"], columns=("emp", "uid", "cit", "reason", "status", "date"), show="headings")
    for c, h, w, a in (("emp", "Сотрудник", 200, "w"), ("uid", "UID", 80, "e"), ("cit", "Гражданин", 90, "e"),
                       ("reason", "Причина", 120, "w"), ("status", "Статус", 110, "w"), ("date", "Дата", 140, "w")):
        ct.heading(c, text=h)
        ct.column(c, width=w, anchor=a)
    ct.tag_configure("odd", background=T.FIELD_ALT)
    ct.tag_configure("pm", foreground=T.OK)
    ct.pack(fill="both", expand=True, pady=4)

    def text_box(parent_):
        t = tk.Text(parent_, bg=T.FIELD, fg=T.TEXT, insertbackground=T.TEXT, relief="flat", wrap="word",
                    font=("Segoe UI", 10), padx=10, pady=8, highlightthickness=0)
        t.pack(fill="both", expand=True, pady=4)
        return t
    diff_txt = text_box(tabs["diff"])
    import pm_ftext
    ftab = pm_ftext.build_f_tab(tabs["f"], cfg, save_cfg, lambda m: msg.set(m))

    # ---- низ: действия
    bot = ttk.Frame(parent)
    bot.pack(side="bottom", fill="x", padx=8, pady=(2, 8), before=body)
    win_var = tk.StringVar(value=str(cfg.get("window", 15)))
    ttk.Label(bot, text="Работает - вызов или баланс за").pack(side="left")
    ttk.Entry(bot, textvariable=win_var, width=4).pack(side="left", padx=4)
    ttk.Label(bot, text="мин. (0 - всё)").pack(side="left")
    for text, cmd in (("Копировать таблицу", lambda: copy(PT.table_text(view(st["snap"])) if st["snap"] else "")),
                      ("Копировать изменения", lambda: copy(diff_txt.get("1.0", "end").strip())),
                      ("Копировать /f", lambda: ftab.copy_all())):
        ttk.Button(bot, text=text, command=cmd).pack(side="left", padx=(10, 0))
    ttk.Button(bot, text="Сохранить снимок", style="Accent.TButton", command=lambda: save_snap()).pack(side="right")
    ttk.Button(bot, text="Сбросить брифинг", command=lambda: reset_brief()).pack(side="right", padx=(0, 6))
    base_var = tk.StringVar()
    base_cb = ttk.Combobox(bot, textvariable=base_var, width=22, state="readonly")
    base_cb.pack(side="right", padx=6)
    ttk.Label(bot, text="Сравнить со снимком:").pack(side="right")

    # ================================================================ скрины
    def add_paths(paths):
        for p in paths:
            if not p.lower().endswith(IMG_EXT) or any(s["path"] == p for s in st["shots"]):
                continue
            s = {"path": p, "name": os.path.basename(p), "res": None, "state": "в очереди", "t": time.time()}
            st["shots"].append(s)
            work.put(s)
        shots_refresh()

    def add_files():
        ps = filedialog.askopenfilenames(parent=parent, title="Скрины планшета",
                                         filetypes=[("Картинки", "*.png *.jpg *.jpeg *.bmp")],
                                         initialdir=folder["path"] or None)
        add_paths(list(ps))

    def paste(e=None):
        try:
            from PIL import ImageGrab
            got = ImageGrab.grabclipboard()
        except Exception as ex:
            msg.set(f"Буфер: {ex}")
            return
        if isinstance(got, list):
            add_paths(got)
        elif got is not None:
            d = os.path.join(PT.data_dir(), "pm_clipboard")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, time.strftime("%Y%m%d_%H%M%S") + f"_{len(st['shots'])}.png")
            got.save(p)
            add_paths([p])
        else:
            msg.set("В буфере нет картинки: сделай скрин (Win+Shift+S) и нажми ещё раз")
    parent.winfo_toplevel().bind("<Control-v>", lambda e: paste() if parent.winfo_ismapped() else None, add="+")
    parent.winfo_toplevel().bind("<Control-V>", lambda e: paste() if parent.winfo_ismapped() else None, add="+")

    def clear():
        st["shots"].clear()
        st["edits"].clear()
        st["base"] = st["roster"]                                      # следующая проверка - в том же порядке
        shots_refresh()
        rebuild()

    def new_order():
        if not st["roster"].get("order"):
            return
        if messagebox.askyesno("Порядок списка", "Забыть порядок прошлых проверок? Таблица выстроится заново "
                               "по текущим скринам, «Вышел» у пропавших исчезнет.", parent=parent):
            PT.reset_roster()
            st["base"] = st["roster"] = {"order": [], "emp": {}}
            rebuild()

    def new_round(s):
        """Пришёл скрин «Коллеги» через NEW_ROUND после прошлых - это новая проверка: старые скрины убираем
        (порядок строк остаётся от прошлой)."""
        prev = [x for x in st["shots"] if x is not s and x["res"] and x["res"].get("kind") == "colleagues"]
        if s["res"].get("kind") != "colleagues" or not prev or s["t"] - max(x["t"] for x in prev) < NEW_ROUND:
            return
        st["shots"][:] = [x for x in st["shots"] if x["t"] > s["t"] - NEW_ROUND]
        st["edits"].clear()
        st["base"] = st["roster"]

    def remove_shot():
        s = lb.curselection()
        if s:
            del st["shots"][s[0]]
            shots_refresh()
            rebuild()

    KIND = {"colleagues": "Коллеги", "calls": "Вызовы", "group": "Группа"}

    def shots_refresh():
        lb.delete(0, "end")
        for s in st["shots"]:
            r = s["res"]
            if r is None:
                lab = f"⏳ {s['name']}"
            elif r.get("kind"):
                lab = f"✓ {KIND[r['kind']]}: {len(r['rows'])} стр. · {s['name']}"
            else:
                lab = f"⚠ {s['name']}: {r.get('error', '')}"
            lb.insert("end", lab)
            if r is not None and not r.get("kind"):
                lb.itemconfigure("end", foreground="#ffb347")
        STATE["shots"] = len(st["shots"])

    # ---- папка
    def pick_folder():
        d = filedialog.askdirectory(parent=parent, title="Папка, куда сохраняются скрины", initialdir=folder["path"] or None)
        if d:
            folder["path"] = d
            folder_lbl.configure(text=short(d))
            cfg["watch_dir"] = d
            save_cfg(cfg)
            if watch_var.get():
                watch_toggle()

    def watch_toggle():
        cfg["watch_on"] = bool(watch_var.get())
        save_cfg(cfg)
        if watch_var.get():
            if not folder["path"]:
                pick_folder()
                if not folder["path"]:
                    watch_var.set(False)
                    return
            st["watch_seen"] = set(_list_imgs(folder["path"]))       # старые файлы не берём
            st["watch_t"] = time.time()
            msg.set(f"Слежу за папкой: новые скрины добавятся сами")

    def _list_imgs(d):
        try:
            return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(IMG_EXT)]
        except OSError:
            return []

    def watch_tick():
        if watch_var.get() and folder["path"]:
            new = []
            for p in _list_imgs(folder["path"]):
                try:
                    if p not in st["watch_seen"] and time.time() - os.path.getmtime(p) > 1.0:   # дописан
                        new.append(p)
                except OSError:
                    pass
            if new:
                st["watch_seen"].update(new)
                add_paths(sorted(new, key=os.path.getmtime))

    # ================================================================ распознавание (фоновый поток)
    def worker():
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        while st["alive"]:
            try:
                s = work.get(timeout=0.5)
            except queue.Empty:
                continue
            if s is None:
                break
            q.put(("busy", f"Распознаю {s['name']}…"))
            try:
                s["res"] = pm_ocr.recognize(s["path"])
            except Exception as ex:
                s["res"] = {"kind": None, "rows": [], "error": f"ошибка: {ex}"}
            q.put(("done", s))
    if ok:
        threading.Thread(target=worker, daemon=True).start()
    if watch_var.get() and folder["path"]:                          # при запуске старые файлы папки не берём
        st["watch_seen"] = set(_list_imgs(folder["path"]))

    # ================================================================ скрин игры по клавише
    st["shot_vk"] = cfg["shot_vk"]

    def shot_dir():
        if not folder["path"]:                                       # папки нет - своя в «Изображениях», за ней и следим
            folder["path"] = os.path.join(os.path.expanduser("~"), "Pictures", "Majestic PM")
            cfg["watch_dir"] = folder["path"]
            save_cfg(cfg)
            q.put(("folder", folder["path"]))
        os.makedirs(folder["path"], exist_ok=True)
        return folder["path"]

    def take_shot():
        """Снимок основного экрана (игра на весь экран) в папку скринов. Вызывается в фоновом потоке."""
        try:
            import mss
            from PIL import Image
            try:
                import ctypes
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            p = os.path.join(shot_dir(), time.strftime("PM_%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png")
            img.save(p)
            if cfg.get("shot_beep"):
                try:
                    import winsound
                    winsound.MessageBeep(0)
                except Exception:
                    pass
            q.put(("shot", p))
        except Exception as ex:
            q.put(("busy", f"Скрин не сохранился: {ex}"))

    def hot_worker():
        import ctypes
        user32 = ctypes.windll.user32
        was = False
        while st["alive"]:
            vk = st["shot_vk"]
            down = bool(vk) and bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not was:
                take_shot()
            was = down
            time.sleep(0.03)
    threading.Thread(target=hot_worker, daemon=True).start()

    def shot_later():
        msg.set("Скрин через 3 секунды: переключись в игру")
        parent.after(3000, lambda: threading.Thread(target=take_shot, daemon=True).start())

    def key_pick(e=None):
        vk = next((k for k, v in SHOT_KEYS.items() if v == key_var.get()), 0)
        st["shot_vk"] = cfg["shot_vk"] = vk
        save_cfg(cfg)
        msg.set(f"Скрин игры: клавиша {key_var.get()}" if vk else "Скрин по клавише выключен")
    key_cb.bind("<<ComboboxSelected>>", key_pick)

    # ================================================================ результат
    def window():
        try:
            return max(0, int(win_var.get()))
        except ValueError:
            return 15

    def rebuild():
        results = [s["res"] for s in st["shots"] if s["res"] and s["res"].get("kind")]
        if not results:
            st["snap"] = None
            for w in (et, ct):
                w.delete(*w.get_children())
            diff_txt.delete("1.0", "end")
            ftab.refresh(None)
            STATE.update(n=0, online=0, calls=0, ask=0, changes=0)
            return
        snap = PT.merge(results, window())
        seen = set(cfg.get("depts_seen", []))                          # отделы, которые видели - для фильтра
        new_d = {e["dept"] for e in snap["employees"].values() if e.get("dept") and e["dept"] != "?"} - seen
        if new_d:
            cfg["depts_seen"] = sorted(seen | new_d)
            save_cfg(cfg)
        PT.apply_roster(snap, st["base"])                              # порядок прошлой проверки, пропавшие - «Вышел»
        for uid, ed in st["edits"].items():                          # ручные правки поверх распознанного
            if uid in snap["employees"]:
                snap["employees"][uid].update(ed)
                snap["employees"][uid]["doubt"] = []
        if snap["employees"]:
            st["roster"] = PT.roster_of(snap)
            try:
                PT.save_roster(st["roster"])
            except OSError as ex:
                msg.set(f"Порядок не сохранился: {ex}")
        apply_marks(snap)
        st["snap"] = snap
        try:                                                           # история каждого (окно статистики)
            ts = [s["t"] for s in st["shots"] if s["res"] and s["res"].get("kind") == "colleagues"]
            PS.record_snap(snap, max(ts) if ts else None)
        except Exception:
            pass
        fill(snap)

    def view(s):
        return PT.filtered(s, cfg["filter"], cfg["white"])

    def work_text(e, b):
        """Колонка «Работа»: катает вызовы (по «Истории») или что делал последним по «Управлению балансом»
        за то же окно, что и вызовы."""
        if e.get("on_calls"):
            return f"катает ({e['calls']})"
        if not b:
            return ""
        if b.get("n"):
            t = (b.get("last_type") or "работал").replace("Вылечил игрока", "лечил").replace("Продажа таблетки", "продал таблетку")
            return f"{t[:1].lower() + t[1:]} ({b['n']}{'+' if b.get('full') else ''})"
        ago = b.get("ago")
        if ago is None:
            return "баланс: пусто"
        return "баланс: " + (f"{ago} мин назад" if ago < 120 else f"{ago // 60} ч назад")

    def fill(full):
        for uid, e in full["employees"].items():              # проверка по балансу - и в таблицу, и в тексты /f
            e["bal"] = st["bal"].get(uid)
        snap, old = view(full), view(st["old"])
        sel = et.selection()
        et.delete(*et.get_children())
        for i, (uid, e) in enumerate(PT.sorted_emp(snap)):
            tags = ["odd"] if i % 2 else []
            if e.get("doubt"):
                tags.append("doubt")
            elif e["status"] == "left":
                tags.append("left")
            elif e["status"] == "offline":
                tags.append("off")
            elif e.get("on_calls"):
                tags.append("calls")
            elif (st["bal"].get(uid) or {}).get("n"):
                tags.append("bal")
            et.insert("", "end", iid=uid, tags=tags, values=(
                i + 1, ("★ " if PT.in_white(uid, e, cfg["white"]) else "") + e["name"] + ("  ⚠" if e.get("doubt") else ""),
                "⧉", uid, "⧉", e.get("rank") or "", e.get("dept") or "",
                PT.status_text(e), work_text(e, st["bal"].get(uid)),
                ("✓" if e.get("in_group") else "—") if snap.get("has_group") else "",
                {"yes": "✓ есть", "no": "✗ нет"}.get(e.get("brief") or "", "·"), e.get("comment") or ""))
        if sel and et.exists(sel[0]):
            et.selection_set(sel[0])
        ct.delete(*ct.get_children())
        for i, c in enumerate(snap["calls"]):
            tags = ["odd"] if i % 2 else []
            if c["uid"] and c["uid"] in full["employees"]:
                tags.append("pm")
            ct.insert("", "end", tags=tags, values=(c["employee"] or "-", c["uid"] or "-", c["citizen"] or "",
                                                    c["reason"], c["status"], c["date"]))
        diff_txt.delete("1.0", "end")
        diff_txt.insert("1.0", PT.changes_text(snap, old, st["old_label"]))
        ftab.refresh(snap)
        emps = snap["employees"].values()
        online = [e for e in emps if e["status"] in pm_ocr.ONLINE]
        STATE.update(n=len(snap["employees"]), online=len(online), calls=sum(1 for e in online if e.get("on_calls")),
                     ask=sum(1 for e in emps if e["status"] in pm_ocr.ASK and not e.get("on_calls")),
                     changes=len(PT.diff(old, snap)) if old else 0)
        info = [f"Сотрудников: {len(full['employees'])}", f"в сети: {len(online)}"]
        if snap.get("hidden"):
            info.append(f"фильтр скрыл: {snap['hidden']}")
        if snap["has_calls"]:
            info.append(f"вызовов на скринах: {len(snap['calls'])} (самый свежий {snap['calls_newest']})")
        else:
            info.append("скринов «Вызовы» нет")
        if snap.get("has_group"):
            n_in = sum(1 for e in emps if e.get("in_group"))
            info.append(f"в группе: {len(snap['group'])}" + (f" из {snap['group_count']}" if snap.get("group_count") else "")
                        + f", из них в списке PM: {n_in}")
        if snap.get("roster_gone"):
            info.append(f"вышли (нет на скринах): {len(snap['roster_gone'])}")
        if snap.get("roster_new"):
            info.append(f"новых внизу: {len(snap['roster_new'])}")
        if snap["unknown"]:
            info.append(f"строк без статика: {len(snap['unknown'])}")
        if any(e.get("doubt") for e in emps):
            info.append("⚠ жёлтые - проверь (Enter или правый щелчок - исправить)")
        info.append("двойной щелчок - статистика")
        msg.set("  ·  ".join(info))

    # ---- правка вручную
    def edit(e=None):
        uid = (et.selection() or [None])[0]
        if not uid or not st["snap"]:
            return
        emp = st["snap"]["employees"][uid]
        dlg = tk.Toplevel(parent)
        dlg.title("Исправить")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.grab_set()
        v_name, v_rank = tk.StringVar(value=emp["name"]), tk.StringVar(value=str(emp.get("rank") or ""))
        v_dept = tk.StringVar(value=emp.get("dept") or "")
        names = [pm_ocr.STATUS_NAME[k] for k in ("active", "away", "arena", "jail", "offline")]
        v_st = tk.StringVar(value=pm_ocr.STATUS_NAME.get(emp["status"], names[0]))
        v_calls = tk.BooleanVar(value=bool(emp.get("on_calls")))
        pad = {"padx": 12, "pady": 5}
        ttk.Label(dlg, text=f"Статик {uid}", style="Big.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(dlg, text="Ник:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(dlg, textvariable=v_name, width=28).grid(row=1, column=1, **pad)
        ttk.Label(dlg, text="Ранг:").grid(row=2, column=0, sticky="w", **pad)
        rf = ttk.Frame(dlg)
        rf.grid(row=2, column=1, sticky="w", **pad)
        ttk.Entry(rf, textvariable=v_rank, width=6).pack(side="left")
        ttk.Label(rf, text="Отдел:").pack(side="left", padx=(12, 4))
        ttk.Entry(rf, textvariable=v_dept, width=8).pack(side="left")
        ttk.Label(dlg, text="Статус:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Combobox(dlg, textvariable=v_st, values=names, state="readonly", width=20).grid(row=3, column=1, sticky="w", **pad)
        ttk.Checkbutton(dlg, text="Катает вызовы", variable=v_calls).grid(row=4, column=1, sticky="w", **pad)

        def ok_():
            code = next(k for k, v in pm_ocr.STATUS_NAME.items() if v == v_st.get())
            ed = {"name": v_name.get().strip(), "status": code, "on_calls": bool(v_calls.get()),
                  "dept": v_dept.get().strip().upper()}
            if v_rank.get().strip().isdigit():
                ed["rank"] = int(v_rank.get())
            st["edits"][uid] = ed
            dlg.destroy()
            rebuild()
        ttk.Button(dlg, text="Сохранить", style="Accent.TButton", command=ok_).grid(row=5, column=0, columnspan=2,
                                                                                   sticky="ew", padx=12, pady=10)
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass
    # ---- брифинг (щелчок по ячейке) и комментарий (двойной щелчок)
    def apply_marks(snap):
        for uid, e in snap["employees"].items():
            m = st["marks"].get(uid) or {}
            e["brief"], e["comment"] = m.get("brief", ""), m.get("comment", "")

    def set_mark(uid, **kw):
        st["marks"].setdefault(uid, {}).update(kw)
        try:
            PT.save_marks(st["marks"])
        except OSError as ex:
            msg.set(f"Не сохранилось: {ex}")
        if st["snap"]:
            apply_marks(st["snap"])
            fill(st["snap"])

    def cell_of(e):
        uid = et.identify_row(e.y)
        col = et.identify_column(e.x)
        if not uid or not col:
            return None, None
        return uid, cols[int(col[1:]) - 1]

    def copy_field(uid, what):
        if not st["snap"] or uid not in st["snap"]["employees"]:
            return
        name = st["snap"]["employees"][uid]["name"]
        copy({"name": name, "uid": uid, "both": f"{name} | {uid}"}[what])

    def on_click(e):
        uid, col = cell_of(e)
        if col in ("cn", "cu") and uid:
            copy_field(uid, "name" if col == "cn" else "uid")
            return "break"
        if col == "brief" and uid:
            cur = (st["marks"].get(uid) or {}).get("brief", "")
            set_mark(uid, brief={"": "yes", "yes": "no", "no": ""}[cur])
        elif col == "comment" and uid:
            et.after(1, lambda: edit_comment(uid))

    def on_double(e):
        uid, col = cell_of(e)
        if col in ("cn", "cu"):
            return "break"
        if col == "brief":
            on_click(e)                                               # быстрый второй щелчок - тоже переключение
            return "break"
        if col == "comment":
            return "break"
        stats_open(uid)
        return "break"

    def stats_open(uid=None):
        """Окно статистики сотрудника (двойной щелчок по строке)."""
        uid = uid or (et.selection() or [None])[0]
        if not uid:
            return
        emp = (st["snap"] or {}).get("employees", {}).get(uid) or {}
        PS.open_window(parent, uid, emp, st["marks"].get(uid) or {}, (st["bal"].get(uid) or {}).get("shot"))

    def edit_comment(uid):
        if not et.exists(uid) or not et.bbox(uid, "comment"):
            return
        for old in [w for w in et.winfo_children() if isinstance(w, tk.Entry)]:
            old.destroy()
        x, y, w, h = et.bbox(uid, "comment")
        var = tk.StringVar(value=(st["marks"].get(uid) or {}).get("comment", ""))
        ent = tk.Entry(et, textvariable=var, bg=T.FIELD, fg=T.TEXT, insertbackground=T.TEXT, relief="flat",
                       font=("Segoe UI", 9), highlightthickness=1, highlightbackground=T.ACCENT)
        ent.place(x=x, y=y, width=max(w, 220), height=h)
        ent.focus_set()
        ent.icursor("end")

        def done(save=True):
            if save:
                set_mark(uid, comment=var.get().strip())
            ent.destroy()
        ent.bind("<Return>", lambda e: done())
        ent.bind("<Escape>", lambda e: done(False))
        ent.bind("<FocusOut>", lambda e: done())

    def reset_brief():
        if not any(m.get("brief") for m in st["marks"].values()):
            return
        if messagebox.askyesno("Брифинг", "Сбросить отметки брифинга у всех? Комментарии останутся.", parent=parent):
            for m in st["marks"].values():
                m["brief"] = ""
            set_mark("", brief="")

    # ---- фильтр
    def refill():
        save_cfg(cfg)
        white_btn.configure(text=f"Вайтлист ({len(cfg['white'])})…")
        if st["snap"]:
            fill(st["snap"])

    def filter_toggle():
        cfg["filter"]["on"] = bool(filter_var.get())
        refill()

    CHOICES = {"calls": (("all", "все"), ("yes", "катает"), ("no", "не катает")),
               "group": (("all", "все"), ("yes", "в группе"), ("no", "не в группе")),
               "brief": (("all", "все"), ("yes", "есть"), ("no", "нет"), ("none", "не отмечен")),
               "comment": (("all", "все"), ("yes", "есть"), ("no", "нет"))}

    def filter_dialog():
        if st.get("fdlg") and st["fdlg"].winfo_exists():
            st["fdlg"].lift()
            return
        f = cfg["filter"]
        dlg = st["fdlg"] = tk.Toplevel(parent)
        dlg.title("Фильтр: кого показывать в списках")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        dlg.resizable(False, False)
        pad = {"padx": 12, "pady": 4}
        ttk.Checkbutton(dlg, text="Фильтр включён", variable=filter_var, command=filter_toggle).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(12, 6))
        ttk.Label(dlg, text="Статус:").grid(row=1, column=0, sticky="nw", **pad)
        sv = {}
        box = ttk.Frame(dlg)
        box.grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        for i, k in enumerate(PT.STATUS_ORDER):
            sv[k] = tk.BooleanVar(value=k in f["status"])
            ttk.Checkbutton(box, text=pm_ocr.STATUS_NAME[k], variable=sv[k]).grid(row=i // 3, column=i % 3, sticky="w",
                                                                              padx=(0, 14), pady=2)
        ttk.Label(dlg, text="Отдел:").grid(row=12, column=0, sticky="nw", **pad)
        dv = {}
        dbox = ttk.Frame(dlg)
        dbox.grid(row=12, column=1, columnspan=3, sticky="w", **pad)
        depts = sorted(set(cfg.get("depts_seen", [])) | set(f["depts"]))
        for i, d_ in enumerate(depts):
            dv[d_] = tk.BooleanVar(value=d_ in f["depts"])
            ttk.Checkbutton(dbox, text=d_, variable=dv[d_]).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 14), pady=2)
        ttk.Label(dbox, text="ничего не отмечено - все отделы" if depts else "отделы появятся после скринов «Коллеги»",
                  style="Muted.TLabel").grid(row=(len(depts) + 3) // 4, column=0, columnspan=4, sticky="w")
        ttk.Label(dlg, text="Ранг:").grid(row=2, column=0, sticky="w", **pad)
        rk = ttk.Frame(dlg)
        rk.grid(row=2, column=1, columnspan=3, sticky="w", **pad)
        v_lo, v_hi = tk.StringVar(value=f["rank_min"]), tk.StringVar(value=f["rank_max"])
        ttk.Label(rk, text="от").pack(side="left")
        ttk.Entry(rk, textvariable=v_lo, width=5).pack(side="left", padx=4)
        ttk.Label(rk, text="до").pack(side="left")
        ttk.Entry(rk, textvariable=v_hi, width=5).pack(side="left", padx=4)
        ttk.Label(rk, text="(пусто - любой)", style="Muted.TLabel").pack(side="left", padx=6)
        cv = {}
        for r, (k, lab) in enumerate((("calls", "Вызовы:"), ("group", "Группа:"), ("brief", "Брифинг:"),
                                      ("comment", "Комментарий:")), start=3):
            ttk.Label(dlg, text=lab).grid(row=r, column=0, sticky="w", **pad)
            cv[k] = tk.StringVar(value=dict(CHOICES[k]).get(f[k], "все"))
            ttk.Combobox(dlg, textvariable=cv[k], values=[n for _, n in CHOICES[k]], state="readonly", width=14).grid(
                row=r, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(dlg, text="Ник или статик:").grid(row=7, column=0, sticky="w", **pad)
        v_name = tk.StringVar(value=f["name"])
        ttk.Entry(dlg, textvariable=v_name, width=24).grid(row=7, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(dlg, text="Скрыть не в сети дольше:").grid(row=8, column=0, sticky="w", **pad)
        oh = ttk.Frame(dlg)
        oh.grid(row=8, column=1, columnspan=3, sticky="w", **pad)
        v_off = tk.StringVar(value=f["off_hours"])
        ttk.Entry(oh, textvariable=v_off, width=5).pack(side="left")
        ttk.Label(oh, text="ч (пусто - не скрывать)", style="Muted.TLabel").pack(side="left", padx=6)
        v_wa, v_wo = tk.BooleanVar(value=f["white_always"]), tk.BooleanVar(value=f["white_only"])
        ttk.Checkbutton(dlg, text="Вайтлист показывать всегда (мимо фильтра)", variable=v_wa).grid(
            row=9, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(dlg, text="Показывать только вайтлист", variable=v_wo).grid(
            row=10, column=0, columnspan=4, sticky="w", **pad)

        def apply_(*a):
            f.update(status=[k for k in PT.STATUS_ORDER if sv[k].get()], rank_min=v_lo.get().strip(),
                     rank_max=v_hi.get().strip(), name=v_name.get(), off_hours=v_off.get().strip(),
                     white_always=bool(v_wa.get()), white_only=bool(v_wo.get()),
                     depts=[d_ for d_, v_ in dv.items() if v_.get()])
            for k in CHOICES:
                f[k] = next(c for c, n in CHOICES[k] if n == cv[k].get())
            if not f["on"]:                                          # меняют фильтр - значит, он нужен
                f["on"] = True
                filter_var.set(True)
            refill()

        def reset():
            on = f["on"]
            f.clear()
            f.update(PT.filter_cfg({"on": on}))
            dlg.destroy()
            refill()
            filter_dialog()
        for v in list(sv.values()) + list(cv.values()) + list(dv.values()) + [v_lo, v_hi, v_name, v_off, v_wa, v_wo]:
            v.trace_add("write", apply_)
        bt = ttk.Frame(dlg)
        bt.grid(row=13, column=0, columnspan=4, sticky="ew", padx=12, pady=(8, 12))
        ttk.Button(bt, text="Сбросить", command=reset).pack(side="left")
        ttk.Button(bt, text="Готово", style="Accent.TButton", command=dlg.destroy).pack(side="right")
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    # ---- вайтлист
    def white_add(uid, name):
        for x in cfg["white"]:
            if (uid and x.get("uid") == uid) or (not uid and not x.get("uid") and x.get("name", "").lower() == name.lower()):
                return False
        cfg["white"].append({"uid": uid, "name": name})
        refill()
        return True

    def white_remove(uid, e=None):
        nk = ((e or {}).get("name") or "").lower()
        cfg["white"] = [x for x in cfg["white"]
                        if not ((x.get("uid") and x["uid"] == uid) or (not x.get("uid") and x.get("name", "").lower() == nk))]
        refill()

    def white_dialog():
        if st.get("wdlg") and st["wdlg"].winfo_exists():
            st["wdlg"].lift()
            return
        dlg = st["wdlg"] = tk.Toplevel(parent)
        dlg.title("Вайтлист")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        ttk.Label(dlg, text="Кто всегда в списках, даже если фильтр его скрывает.\n"
                            "Добавить: статик или ник, либо правый щелчок по строке таблицы.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(12, 6))
        wl = tk.Listbox(dlg, width=46, height=14, bg=T.FIELD, fg=T.TEXT, selectbackground=T.ACCENT, relief="flat",
                        highlightthickness=0, activestyle="none", font=("Segoe UI", 10), selectmode="extended")
        wl.pack(fill="both", expand=True, padx=12)

        def redraw():
            wl.delete(0, "end")
            for x in cfg["white"]:
                wl.insert("end", f"{x.get('uid') or '—':>7}   {x.get('name') or ''}")
        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=12, pady=8)
        v = tk.StringVar()
        ent = ttk.Entry(row, textvariable=v, width=26)
        ent.pack(side="left")

        def add_():
            t = v.get().strip()
            if not t:
                return
            emps = (st["snap"] or {}).get("employees", {})
            if t.isdigit():
                uid, name = t, (emps.get(t) or {}).get("name", "")
            else:
                uid = next((u for u, e in emps.items() if (e.get("name") or "").lower() == t.lower()), "")
                name = emps[uid]["name"] if uid else t
            white_add(uid, name)
            v.set("")
            redraw()

        def add_sel():
            for uid in et.selection():
                e = st["snap"]["employees"].get(uid) if st["snap"] else None
                if e:
                    white_add(uid, e["name"])
            redraw()

        def remove_():
            idx = set(wl.curselection())
            cfg["white"] = [x for i, x in enumerate(cfg["white"]) if i not in idx]
            refill()
            redraw()
        ent.bind("<Return>", lambda e: add_())
        ttk.Button(row, text="Добавить", command=add_).pack(side="left", padx=4)
        ttk.Button(row, text="+ выбранный в таблице", command=add_sel).pack(side="left", padx=4)
        row2 = ttk.Frame(dlg)
        row2.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(row2, text="Убрать выделенных", command=remove_).pack(side="left")
        ttk.Button(row2, text="Готово", style="Accent.TButton", command=dlg.destroy).pack(side="right")
        redraw()
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    # ---- правый щелчок по строке
    menu = tk.Menu(et, tearoff=0, bg=T.PANEL, fg=T.TEXT, activebackground=T.ACCENT, activeforeground=T.TEXT, bd=0)

    def on_menu(e):
        uid = et.identify_row(e.y)
        if not uid or not st["snap"] or uid not in st["snap"]["employees"]:
            return
        et.selection_set(uid)
        emp = st["snap"]["employees"][uid]
        menu.delete(0, "end")
        menu.add_command(label="📊 Статистика…", command=lambda: stats_open(uid))
        menu.add_separator()
        if PT.in_white(uid, emp, cfg["white"]):
            menu.add_command(label="Убрать из вайтлиста", command=lambda: white_remove(uid, emp))
        else:
            menu.add_command(label="★ В вайтлист", command=lambda: white_add(uid, emp["name"]))
        menu.add_command(label="Исправить…", command=edit)
        menu.add_separator()
        menu.add_command(label="Копировать ник", command=lambda: copy_field(uid, "name"))
        menu.add_command(label="Копировать статик", command=lambda: copy_field(uid, "uid"))
        menu.add_command(label="Копировать «ник | статик»", command=lambda: copy_field(uid, "both"))
        b = st["bal"].get(uid)
        if b and b.get("shot") and os.path.exists(b["shot"]):
            menu.add_command(label="Открыть скрин баланса", command=lambda: os.startfile(b["shot"]))
        menu.tk_popup(e.x_root, e.y_root)
    et.bind("<Button-3>", on_menu)
    white_btn.configure(text=f"Вайтлист ({len(cfg['white'])})…")

    et.bind("<Button-1>", on_click, add="+")
    et.bind("<Double-1>", on_double)
    et.bind("<Return>", edit)

    # ---- снимки
    def bases_refresh(select_latest=True):
        lst = PT.snapshots()
        st["bases"] = lst
        base_cb.configure(values=[lab for _, lab in lst] or ["нет снимков"])
        if select_latest:
            if lst:
                base_var.set(lst[0][1])
                st["old"], st["old_label"] = PT.load(lst[0][0]), lst[0][1]
            else:
                base_var.set("нет снимков")
                st["old"], st["old_label"] = None, ""

    def base_pick(e=None):
        for p, lab in st.get("bases", []):
            if lab == base_var.get():
                st["old"], st["old_label"] = PT.load(p), lab
        if st["snap"]:
            fill(st["snap"])
    base_cb.bind("<<ComboboxSelected>>", base_pick)

    def save_snap():
        if not st["snap"]:
            messagebox.showinfo("Снимок", "Сначала добавь скрины", parent=parent)
            return
        PT.save(st["snap"])
        bases_refresh()
        msg.set("Снимок сохранён: следующая проверка сравнится с ним")
        try:
            import stats
            stats.event(f"PM: снимок сохранён ({len(st['snap']['employees'])} сотрудников)", "ok")
        except Exception:
            pass
        fill(st["snap"])

    def copy(text):
        if not text:
            msg.set("Нечего копировать")
            return
        parent.clipboard_clear()
        parent.clipboard_append(text)
        msg.set("Скопировано в буфер обмена ✓")

    win_var.trace_add("write", lambda *a: (cfg.update(window=window()), save_cfg(cfg), rebuild()))

    # ================================================================ обмен с потоком
    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "busy":
                    STATE["busy"] = m[1]
                    msg.set(m[1])
                elif m[0] == "shot":
                    st["watch_seen"].add(m[1])
                    add_paths([m[1]])
                    msg.set(f"Скрин сохранён: {m[1]}")
                elif m[0] == "afk_status":
                    if afk_ui:
                        afk_ui["var"].set(m[1])
                elif m[0] == "afk":
                    afk_done(m[1], m[2], m[3], m[4] if len(m) > 4 else None, m[5] if len(m) > 5 else None)
                elif m[0] == "balance":
                    balance_done(*m[1:])
                elif m[0] == "folder":
                    folder_lbl.configure(text=short(m[1]))
                elif m[0] == "done":
                    STATE["busy"] = ""
                    if m[1] in st["shots"]:
                        new_round(m[1])
                    shots_refresh()
                    rebuild()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if st["alive"]:
            st["tick"] = st.get("tick", 0) + 1
            if st["tick"] % 10 == 0:
                watch_tick()
            parent.after(200, pump)

    # ================================================================ АФК-режим (только основная программа)
    def afk_toggle():
        run = afk_ui["run"]
        run.set_on(not run.on)
        afk_ui["btn"].configure(text="Автопроверка: ВКЛ" if run.on else "Автопроверка: выкл",
                                style="Accent.TButton" if run.on else "TButton")

    def bal_minutes():
        w = window()                                   # то же окно, что «катает» (0 - всё, что на экране)
        return w if w > 0 else 10 ** 6

    def pick_people(snap):
        """Кого смотреть в «Управлении балансом»: в сети (активен / нет на месте) и не катает вызовы, с учётом фильтра."""
        return [(uid, e["name"]) for uid, e in PT.sorted_emp(view(snap))
                if e.get("status") in ("active", "away") and not e.get("on_calls")]

    def balance_check():
        """Кто в сети и не катает вызовы -> бот смотрит каждого в «Управлении балансом» (лечил, продавал таблетки)."""
        if not st["snap"]:
            msg.set("Сначала нужна проверка состава («Коллеги» и «Вызовы»)")
            return
        people = pick_people(st["snap"])
        if not people:
            msg.set("Проверять некого: все в сети катают вызовы")
            return
        if afk_ui.get("run") is None:
            msg.set("Автоматическая проверка не запущена")
            return
        afk_ui["run"].balance_now(people, bal_minutes())
        msg.set(f"Проверка по балансу: {len(people)} чел. (~{len(people) * 4} с) - переключись в игру")

    def balance_done(res, err, dur, folder):
        st["bal"].update(res)
        try:
            PS.record_balance(res)
        except Exception:
            pass
        work = [b["name"] for b in res.values() if b.get("n")]
        idle = [b["name"] for b in res.values() if not b.get("n")]
        win = window()
        txt = (f"Баланс{f' за {win} мин' if win else ''}: работали {len(work)}, без действий {len(idle)} "
               f"({dur:.0f} с, скрины - правый щелчок по строке)")
        if err:
            txt += f" · прервано: {err}"
        msg.set(txt)
        if afk_ui:
            afk_ui["var"].set(txt)
        if st["snap"]:
            fill(st["snap"])

    def afk_done(shots, err, dur, waiting=None, bal=None):
        """Пришла автоматическая проверка: скрины -> таблица (как новая проверка), баланс -> колонка «Работа»,
        правила -> уведомления."""
        good = [(p, r) for p, r in shots if r and r.get("kind")]
        if not good:
            afk_ui["var"].set(f"Автопроверка не удалась - {err or 'нет скринов'}")
            return
        if bal is not None:
            st["bal"] = dict(bal)                       # новая проверка - старый баланс не тянем
            try:
                PS.record_balance(bal)
            except Exception:
                pass
        prev_snap = st["snap"]
        now = time.time()
        st["shots"][:] = [{"path": p, "name": os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p),
                           "res": r, "state": "", "t": now} for p, r in good]
        st["edits"].clear()
        st["base"] = st["roster"]
        if prev_snap:
            st["old"], st["old_label"] = prev_snap, "прошлая проверка"
        shots_refresh()
        rebuild()
        snap = st["snap"]
        hits = []
        if snap:
            state = pm_afk.load_state()
            hits = pm_afk.evaluate(snap, cfg["afk_rules"], state, cfg["white"], now, waiting)
            pm_afk.save_state(state)
            afk_ui["stats"].set("Вызовы: " + (pm_afk.stats_text(state.get("stats")) or "пока нет данных"))
        stamp = time.strftime("%H:%M")
        nb_ = (f" · баланс: работали {sum(1 for b in bal.values() if b.get('n'))} из {len(bal)}" if bal else "")
        afk_ui["var"].set(f"Автопроверка {stamp} за {dur:.0f} с" + nb_ + (f" (не всё: {err})" if err else "") +
                          (f" · событий: {len(hits)}" if hits else " · событий нет"))
        if hits:
            afk_notify(hits, stamp)

    def afk_notify(hits, stamp):
        import market_watch as MW
        if any(r.get("popup") for r, _ in hits):
            afk_popup([t for r, t in hits if r.get("popup")], stamp)
        if any(r.get("sound") for r, _ in hits):
            MW.play_sound("beep")
        tg = [t for r, t in hits if r.get("tg")]
        if tg:
            def send():
                st_line = afk_ui["stats"].get()
                ok_, m = MW.telegram(f"PM · проверка {stamp}\n" + "\n".join("• " + t for t in tg) +
                                     (f"\n{st_line}" if st_line else ""))
                if not ok_:
                    q.put(("afk_status", m))
            threading.Thread(target=send, daemon=True).start()

    def afk_popup(lines, stamp):
        """Окно поверх всех, которое НЕ забирает фокус у игры (иначе следующая проверка ждала бы игру)."""
        old = afk_ui.get("pop")
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        w = tk.Toplevel(parent)
        afk_ui["pop"] = w
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=T.ACCENT)
        inner = tk.Frame(w, bg=T.CARD)
        inner.pack(fill="both", expand=True, padx=2, pady=2)
        head = tk.Frame(inner, bg=T.CARD)
        head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text=f"PM · проверка {stamp}", bg=T.CARD, fg=T.TEXT, font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(head, text="✕", command=w.destroy, bg=T.CARD, fg=T.MUTED, relief="flat", bd=0,
                  activebackground=T.CARD, font=("Segoe UI", 11)).pack(side="right")
        more = len(lines) - 12
        tk.Label(inner, text="\n".join("• " + t for t in lines[:12]) + (f"\n… и ещё {more}" if more > 0 else ""),
                 bg=T.CARD, fg=T.TEXT, justify="left", font=("Segoe UI", 10), wraplength=440).pack(
            anchor="w", padx=14, pady=(0, 12))
        w.update_idletasks()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"+{sw - w.winfo_reqwidth() - 24}+{sh - w.winfo_reqheight() - 80}")
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(w.winfo_id()) or w.winfo_id()
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x80 | 0x8)   # без фокуса, без панели задач
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)   # не попадает на снимки (скрины планшета)
        except Exception:
            pass

    def afk_settings():
        a = cfg["afk"]
        dlg = tk.Toplevel(parent)
        dlg.title("Автоматическая проверка: настройки")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        pad = {"padx": 12, "pady": 4}
        v_int = tk.StringVar(value=str(a["interval"]))
        names = [pm_afk.KEY_NAMES.get(k, k) for k in pm_afk.TABLET_KEYS]
        v_key = tk.StringVar(value=pm_afk.KEY_NAMES.get(a["tablet_key"], a["tablet_key"]))
        v_dept = tk.StringVar(value=a["dept"])
        v_col, v_calls, v_grp, v_onl, v_wait, v_bal = (tk.BooleanVar(value=a[k]) for k in
                                                       ("colleagues", "calls", "group", "only_online", "waiting", "balance"))
        v_idle, v_idle_s = tk.BooleanVar(value=a["idle_guard"]), tk.StringVar(value=str(a["idle_sec"]))
        r = 0
        ttk.Label(dlg, text="Проверять каждые").grid(row=r, column=0, sticky="w", **pad)
        f1 = ttk.Frame(dlg)
        f1.grid(row=r, column=1, sticky="w", **pad)
        ttk.Spinbox(f1, from_=2, to=240, width=5, textvariable=v_int).pack(side="left")
        ttk.Label(f1, text="мин.").pack(side="left", padx=4)
        r += 1
        ttk.Label(dlg, text="Клавиша планшета:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Combobox(dlg, textvariable=v_key, values=names, state="readonly", width=16).grid(row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Label(dlg, text="Отдел в «Коллеги»:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Combobox(dlg, textvariable=v_dept, values=[""] + cfg.get("depts_seen", []), width=16).grid(
            row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Label(dlg, text="пусто - все отделы (дольше: весь список)", style="Muted.TLabel").grid(
            row=r, column=1, sticky="w", padx=12)
        r += 1
        ttk.Label(dlg, text="Что снимать:").grid(row=r, column=0, sticky="nw", **pad)
        f2 = ttk.Frame(dlg)
        f2.grid(row=r, column=1, sticky="w", **pad)
        ttk.Checkbutton(f2, text="«Коллеги»", variable=v_col).pack(anchor="w")
        ttk.Checkbutton(f2, text="    только кто в сети (сортировка «Статус в сети», дальше не листает)",
                        variable=v_onl).pack(anchor="w")
        ttk.Checkbutton(f2, text="«Вызовы» → «История» (за время между проверками + 3 мин)", variable=v_calls).pack(anchor="w")
        ttk.Checkbutton(f2, text="    «Доступные» - сколько вызовов ждут", variable=v_wait).pack(anchor="w")
        ttk.Checkbutton(f2, text="«Управление балансом» - кто без вызовов: что делал (лечил, таблетки...)",
                        variable=v_bal).pack(anchor="w")
        cb = ttk.Checkbutton(f2, text="Группа в телефоне", variable=v_grp)
        cb.pack(anchor="w")
        cb.state(["disabled"])
        ttk.Label(f2, text="группа - пока нет: пришли запись, как открываешь её в телефоне", style="Muted.TLabel").pack(anchor="w")
        r += 1
        f3 = ttk.Frame(dlg)
        f3.grid(row=r, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(f3, text="Не начинать проверку, если я трогал клавиатуру/мышь последние", variable=v_idle).pack(side="left")
        ttk.Spinbox(f3, from_=10, to=600, increment=10, width=5, textvariable=v_idle_s).pack(side="left", padx=4)
        ttk.Label(f3, text="с").pack(side="left")
        r += 1
        ttk.Label(dlg, text="Проверка идёт, только когда игра на переднем плане; во время неё анти-АФК стоит на паузе.\n"
                            "Баланс смотрится за то же время, что «катает» (поле внизу вкладки).\n"
                            "Планшет бот закрывает сам; если что-то пошло не так - закрывает и пробует в следующий раз.",
                  style="Muted.TLabel", justify="left").grid(row=r, column=0, columnspan=2, sticky="w", **pad)

        def ok_():
            try:
                a["interval"] = max(2, min(240, int(float(v_int.get()))))
                a["idle_sec"] = max(10, min(600, int(float(v_idle_s.get()))))
            except ValueError:
                pass
            a["tablet_key"] = next((k for k in pm_afk.TABLET_KEYS if pm_afk.KEY_NAMES.get(k, k) == v_key.get()), "Down")
            a["dept"] = v_dept.get().strip()
            a["colleagues"], a["calls"], a["group"] = bool(v_col.get()), bool(v_calls.get()), False
            a["only_online"], a["waiting"], a["balance"] = bool(v_onl.get()), bool(v_wait.get()), bool(v_bal.get())
            a["idle_guard"] = bool(v_idle.get())
            save_cfg(cfg)
            dlg.destroy()
        r += 1
        ttk.Button(dlg, text="Сохранить", style="Accent.TButton", command=ok_).grid(
            row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=10)
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    def rules_dialog():
        dlg = tk.Toplevel(parent)
        dlg.title("Правила наблюдения")
        dlg.configure(bg=T.BG)
        dlg.transient(parent.winfo_toplevel())
        ttk.Label(dlg, text="Правило = кого смотрим + что ловим. После каждой автоматической проверки сработавшие правила "
                            "присылают уведомление (одно на случай, пока он длится).", style="Muted.TLabel",
                  wraplength=760, justify="left").pack(anchor="w", padx=12, pady=(10, 4))
        tv = ttk.Treeview(dlg, columns=("on", "name", "ev", "who", "how"), show="headings", height=9, selectmode="browse")
        for c, h, w_ in (("on", "Вкл", 44), ("name", "Название", 190), ("ev", "Событие", 200), ("who", "Кого", 180),
                         ("how", "Куда", 130)):
            tv.heading(c, text=h)
            tv.column(c, width=w_, anchor="center" if c == "on" else "w")
        tv.pack(fill="both", expand=True, padx=12, pady=4)

        def ev_text(r_):
            t = pm_afk.EVENTS[r_["event"]]
            return t + (f" {r_['minutes']} мин" if r_["event"] in pm_afk.TIMED else
                        f" {r_['minutes']}" if r_["event"] in pm_afk.COUNTED else "")

        def fill_():
            tv.delete(*tv.get_children())
            for r_ in cfg["afk_rules"]:
                how = ", ".join(n for k, n in (("popup", "окно"), ("sound", "звук"), ("tg", "Telegram")) if r_.get(k)) or "-"
                tv.insert("", "end", iid=r_["id"], values=("✓" if r_["on"] else "—", r_["name"], ev_text(r_),
                                                           pm_afk.who_text(r_["who"]), how))

        def sel_():
            s_ = tv.selection()
            return next((r_ for r_ in cfg["afk_rules"] if s_ and r_["id"] == s_[0]), None)

        def toggle_():
            r_ = sel_()
            if r_:
                r_["on"] = not r_["on"]
                save_cfg(cfg)
                fill_()

        def delete_():
            r_ = sel_()
            if r_ and messagebox.askyesno("Правило", f"Удалить «{r_['name']}»?", parent=dlg):
                cfg["afk_rules"].remove(r_)
                save_cfg(cfg)
                fill_()
        bar_ = ttk.Frame(dlg)
        bar_.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bar_, text="Добавить…", style="Accent.TButton", command=lambda: rule_edit(None, fill_, dlg)).pack(side="left")
        ttk.Button(bar_, text="Изменить…", command=lambda: sel_() and rule_edit(sel_(), fill_, dlg)).pack(side="left", padx=4)
        ttk.Button(bar_, text="Вкл / выкл", command=toggle_).pack(side="left", padx=4)
        ttk.Button(bar_, text="Удалить", command=delete_).pack(side="left", padx=4)
        tv.bind("<Double-1>", lambda e: sel_() and rule_edit(sel_(), fill_, dlg))
        fill_()
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    def rule_edit(rule, done, owner):
        r_ = rule or pm_afk.new_rule("Новое правило", "no_calls", 20)
        who = r_["who"]
        dlg = tk.Toplevel(owner)
        dlg.title("Правило")
        dlg.configure(bg=T.BG)
        dlg.transient(owner)
        dlg.grab_set()
        pad = {"padx": 12, "pady": 4}
        v_name = tk.StringVar(value=r_["name"])
        v_ev = tk.StringVar(value=pm_afk.EVENTS[r_["event"]])
        v_min = tk.StringVar(value=str(r_["minutes"]))
        v_white = tk.BooleanVar(value=who.get("white_only"))
        v_uids = tk.StringVar(value=who.get("uids", ""))
        v_depts = tk.StringVar(value=", ".join(who.get("depts", [])))
        v_r1, v_r2 = tk.StringVar(value=str(who.get("rank_min", ""))), tk.StringVar(value=str(who.get("rank_max", "")))
        v_ch = {k: tk.BooleanVar(value=r_.get(k, True)) for k in ("popup", "sound", "tg")}
        r = 0
        ttk.Label(dlg, text="Название:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(dlg, textvariable=v_name, width=34).grid(row=r, column=1, columnspan=3, sticky="w", **pad)
        r += 1
        ttk.Label(dlg, text="Что ловим:").grid(row=r, column=0, sticky="w", **pad)
        evcb = ttk.Combobox(dlg, textvariable=v_ev, values=list(pm_afk.EVENTS.values()), state="readonly", width=30)
        evcb.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        minf = ttk.Frame(dlg)
        minf.grid(row=r, column=3, sticky="w", **pad)
        ttk.Spinbox(minf, from_=1, to=600, width=5, textvariable=v_min).pack(side="left")
        unit = ttk.Label(minf, text="мин.")
        unit.pack(side="left", padx=4)

        def ev_changed(e=None):
            ev = next(k for k, t in pm_afk.EVENTS.items() if t == v_ev.get())
            num = ev in pm_afk.TIMED or ev in pm_afk.COUNTED
            unit.configure(text="шт." if ev in pm_afk.COUNTED else "мин.")
            for w_ in minf.winfo_children():
                w_.state(["!disabled"] if num else ["disabled"])
        evcb.bind("<<ComboboxSelected>>", ev_changed)
        r += 1
        ttk.Label(dlg, text="«Не берёт вызовы» - в сети («Сейчас активен»), но ни одного вызова за N минут (нужна «История»). "
                            "«В ожидании» и «непринятых за час» - общие, «кого смотрим» для них не важно.",
                  style="Muted.TLabel", wraplength=560, justify="left").grid(row=r, column=1, columnspan=3, sticky="w", padx=12)
        r += 1
        ttk.Label(dlg, text="Кого смотрим:").grid(row=r, column=0, sticky="nw", **pad)
        wf = ttk.Frame(dlg)
        wf.grid(row=r, column=1, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(wf, text="только вайтлист", variable=v_white).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(wf, text="статики или ники:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(wf, textvariable=v_uids, width=36).grid(row=1, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Label(wf, text="отделы:").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(wf, textvariable=v_depts, width=16).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(wf, text="ранг от").grid(row=2, column=2, sticky="e")
        rf = ttk.Frame(wf)
        rf.grid(row=2, column=3, sticky="w", padx=6)
        ttk.Entry(rf, textvariable=v_r1, width=4).pack(side="left")
        ttk.Label(rf, text="до").pack(side="left", padx=4)
        ttk.Entry(rf, textvariable=v_r2, width=4).pack(side="left")
        ttk.Label(wf, text="через запятую; пустые поля - без ограничения (все сотрудники со скринов)",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=4, sticky="w")
        r += 1
        ttk.Label(dlg, text="Куда сообщать:").grid(row=r, column=0, sticky="w", **pad)
        cf = ttk.Frame(dlg)
        cf.grid(row=r, column=1, columnspan=3, sticky="w", **pad)
        for k, t in (("popup", "окно поверх игры"), ("sound", "звук"), ("tg", "Telegram (бот из «Готовки»)")):
            ttk.Checkbutton(cf, text=t, variable=v_ch[k]).pack(side="left", padx=(0, 10))

        def ok_():
            r_["name"] = v_name.get().strip() or "Правило"
            r_["event"] = next(k for k, t in pm_afk.EVENTS.items() if t == v_ev.get())
            try:
                r_["minutes"] = max(1, int(float(v_min.get())))
            except ValueError:
                pass
            r_["who"] = {"white_only": bool(v_white.get()), "uids": v_uids.get().strip(),
                         "depts": [d.strip().upper() for d in v_depts.get().split(",") if d.strip()],
                         "rank_min": v_r1.get().strip(), "rank_max": v_r2.get().strip()}
            for k, var in v_ch.items():
                r_[k] = bool(var.get())
            if rule is None:
                cfg["afk_rules"].append(r_)
            save_cfg(cfg)
            done()
            dlg.destroy()
        r += 1
        ttk.Button(dlg, text="Сохранить", style="Accent.TButton", command=ok_).grid(
            row=r, column=0, columnspan=4, sticky="ew", padx=12, pady=10)
        ev_changed()
        try:
            T.dark_titlebar(dlg)
        except Exception:
            pass

    if afk:
        afk_ui["run"] = pm_afk.Runner(lambda: cfg["afk"], window, q.put, pick=pick_people)

    def provider():
        s = dict(STATE)
        return {"active": False, "label": (s["busy"] and "Распознаю…" or ("Готов" if s["ok"] else "Нет OCR"),
                                           "ok" if s["ok"] else "warn"),
                "status": s["busy"] or "PM", "pm": s,
                "items": [("Сотрудников", s["n"])]}
    widgets.register("pm", provider)

    def stop():
        st["alive"] = False
        ftab.stop()
        rec_stop()
        work.put(None)
        if afk_ui.get("run"):
            afk_ui["run"].stop()

    bases_refresh()
    if watch_var.get() and folder["path"]:
        watch_toggle()
    parent.after(200, pump)
    return stop
