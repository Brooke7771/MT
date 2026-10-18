# -*- coding: utf-8 -*-
"""Запись для разбора («откат»): видео экрана + журнал нажатий клавиш и мыши с точными метками
времени + метки, которые ставишь горячей клавишей («вот тут поклёвка»). Всё лежит в папке
recordings/<дата> рядом с данными программы:
  video.mp4    - кадры экрана (размер и частота выбираются; настоящее время каждого кадра - в frames.csv)
  frames.csv   - номер кадра; время от старта записи, с
  input.csv    - время; тип (down/up/move/mark); клавиша; код; x; y   (нажатия бота через SendInput тоже видны)
  marks.csv    - метки, поставленные горячей клавишей
  meta.json    - разрешение, частота, продолжительность, заметка «что записываю»
По такой записи видно, КАК игра реагирует на нажатия, и можно восстановить логику нового бота.
По умолчанию пишутся только игровые клавиши (не весь текст, который печатаешь)."""
import os
import sys
import csv
import json
import time
import queue
import shutil
import threading

import numpy as np
import cv2

IS_WIN = sys.platform == "win32"

# игровые клавиши: стрелки, WASD и соседи, цифры, Space, Esc, Shift/Ctrl/Alt, Tab, Enter, F1-F12, нампад, мышь
GAME_VKS = sorted(set(
    [0x01, 0x02, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x14, 0x1B, 0x20, 0x21, 0x22, 0x23, 0x24,
     0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E]
    + list(range(0x30, 0x3A))
    + [ord(c) for c in "ADWSEQFGIXCVZRT"]
    + list(range(0x60, 0x70)) + list(range(0x70, 0x7C)) + [0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5]))
ALL_VKS = list(range(1, 255))
MOUSE = {0x01: "МЫШЬ ЛЕВАЯ", 0x02: "МЫШЬ ПРАВАЯ", 0x04: "МЫШЬ СРЕДНЯЯ", 0x05: "МЫШЬ БОК1", 0x06: "МЫШЬ БОК2"}

STATE = {"rec": False, "t": 0.0, "frames": 0, "dropped": 0, "mb": 0.0, "events": 0, "marks": 0, "fps": 0.0,
         "folder": "", "last": ""}


def data_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def rec_root():
    return os.path.join(data_dir(), "recordings")


def key_name(vk):
    if vk in MOUSE:
        return MOUSE[vk]
    try:
        from sewing_bot import vk_name
        return vk_name(vk)
    except Exception:
        return f"VK_{vk:02X}"


def key_down(vk):
    return bool(IS_WIN and __import__("ctypes").windll.user32.GetAsyncKeyState(vk) & 0x8000)


class Session:
    def __init__(self, fps=15, scale=1.0, track=True, all_keys=False, max_min=10, notes="", version=""):
        self.fps, self.scale, self.track, self.all_keys = fps, scale, track, all_keys
        self.max_s = max_min * 60
        self.notes, self.version = notes, version
        self.dir = os.path.join(rec_root(), time.strftime("%Y%m%d_%H%M%S"))
        self.stop_ev = threading.Event()
        self.q = queue.Queue(maxsize=24)
        self.threads = []
        self.frames = 0
        self.dropped = 0
        self.events = 0
        self.marks = 0
        self.size = (0, 0)
        self.t0 = 0.0
        self.t_end = None
        self.error = ""
        self.lock = threading.Lock()
        self._rows = []
        self._mark_rows = []
        self.reason = ""

    # ---- управление
    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        free = shutil.disk_usage(self.dir).free
        if free < 2 * 2 ** 30:
            raise RuntimeError("Мало места на диске: нужно хотя бы 2 ГБ")
        self.t0 = time.perf_counter()
        self.wall = time.time()
        self.threads = [threading.Thread(target=self._capture, daemon=True),
                        threading.Thread(target=self._write, daemon=True)]
        if self.track:
            self.threads.append(threading.Thread(target=self._input, daemon=True))
        for t in self.threads:
            t.start()

    def t(self):
        return time.perf_counter() - self.t0

    def elapsed(self):
        return (self.t_end if self.t_end is not None else self.t())

    def mark(self, label=""):
        with self.lock:
            self.marks += 1
            n = self.marks
            t = self.t()
            self._mark_rows.append((f"{t:.3f}", n, label))
            self._rows.append((f"{t:.3f}", "mark", f"метка {n}", 0, "", ""))
        return n

    def size_mb(self):
        try:
            return os.path.getsize(os.path.join(self.dir, "video.mp4")) / 2 ** 20
        except OSError:
            return 0.0

    def stop(self, reason="вручную"):
        if self.t_end is not None:
            return
        self.reason = reason
        self.stop_ev.set()
        for t in self.threads:
            t.join(timeout=20)
        self.t_end = self.t()
        self._flush()
        dur = self.t_end
        meta = {"программа": self.version, "начало": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.wall)),
                "длительность_с": round(dur, 2), "кадров": self.frames, "потеряно_кадров": self.dropped,
                "частота_цель": self.fps, "частота_факт": round(self.frames / dur, 2) if dur > 0 else 0,
                "размер_кадра": list(self.size), "масштаб": self.scale, "событий_ввода": self.events,
                "меток": self.marks, "клавиши": "все" if self.all_keys else "игровые", "ввод_пишется": self.track,
                "остановлено": reason, "заметка": self.notes, "ошибка": self.error}
        try:
            with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ---- потоки
    def _capture(self):
        try:
            import ctypes
            import mss
            if IS_WIN:
                try:
                    ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
                except Exception:
                    pass
            sct = mss.mss()
            mon = sct.monitors[1]
            W, H = mon["width"], mon["height"]
            ow, oh = max(2, int(W * self.scale) // 2 * 2), max(2, int(H * self.scale) // 2 * 2)
            self.size = (ow, oh)
            period = 1.0 / self.fps
            nxt = time.perf_counter()
            last_disk = 0.0
            while not self.stop_ev.is_set():
                t = self.t()
                if t >= self.max_s:
                    self.reason = "достигнут лимит времени"
                    self.stop_ev.set()
                    break
                if t - last_disk > 5:
                    last_disk = t
                    if shutil.disk_usage(self.dir).free < 500 * 2 ** 20:
                        self.reason = "закончилось место на диске"
                        self.stop_ev.set()
                        break
                img = np.asarray(sct.grab(mon))[:, :, :3]
                if (ow, oh) != (W, H):
                    img = cv2.resize(img, (ow, oh), interpolation=cv2.INTER_AREA)
                try:
                    self.q.put_nowait((t, np.ascontiguousarray(img)))
                except queue.Full:
                    self.dropped += 1
                nxt += period
                wait = nxt - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                else:
                    nxt = time.perf_counter()          # отстаём: не копим долг, просто идём дальше
        except Exception as ex:
            self.error = f"захват: {ex}"
            self.stop_ev.set()
        finally:
            self.q.put(None)

    def _write(self):
        writer = None
        fcsv = None
        try:
            fcsv = open(os.path.join(self.dir, "frames.csv"), "w", encoding="utf-8", newline="")
            fw = csv.writer(fcsv, delimiter=";")
            fw.writerow(["кадр", "t"])
            while True:
                item = self.q.get()
                if item is None:
                    break
                t, img = item
                if writer is None:
                    h, w = img.shape[:2]
                    writer = cv2.VideoWriter(os.path.join(self.dir, "video.mp4"),
                                             cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
                    if not writer.isOpened():
                        raise RuntimeError("не удалось создать видеофайл")
                writer.write(img)
                fw.writerow([self.frames, f"{t:.3f}"])
                self.frames += 1
                if self.frames % 60 == 0:
                    fcsv.flush()
        except Exception as ex:
            self.error = f"запись: {ex}"
            self.stop_ev.set()
        finally:
            if writer is not None:
                writer.release()
            if fcsv is not None:
                fcsv.close()

    def _input(self):
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)       # точный таймер (иначе сон ~15 мс)
            except Exception:
                pass
            vks = ALL_VKS if self.all_keys else GAME_VKS
            prev = {}
            pt = wintypes.POINT()
            lastp, lastt = None, 0.0
            lflush = time.time()
            while not self.stop_ev.is_set():
                t = self.t()
                u.GetCursorPos(ctypes.byref(pt))
                x, y = pt.x, pt.y
                for vk in vks:
                    d = bool(u.GetAsyncKeyState(vk) & 0x8000)
                    if d != prev.get(vk, False):
                        prev[vk] = d
                        with self.lock:
                            self._rows.append((f"{t:.3f}", "down" if d else "up", key_name(vk), vk, x, y))
                            self.events += 1
                if (lastp is None or abs(x - lastp[0]) + abs(y - lastp[1]) >= 3) and t - lastt >= 0.02:
                    lastp, lastt = (x, y), t
                    with self.lock:
                        self._rows.append((f"{t:.3f}", "move", "", 0, x, y))
                if time.time() - lflush > 1.0:
                    lflush = time.time()
                    self._flush()
                time.sleep(0.003)
        except Exception as ex:
            self.error = f"ввод: {ex}"
        finally:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    def _flush(self):
        with self.lock:
            rows, self._rows = self._rows, []
            mrows, self._mark_rows = self._mark_rows, []
        try:
            p = os.path.join(self.dir, "input.csv")
            new = not os.path.exists(p)
            with open(p, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter=";")
                if new:
                    w.writerow(["t", "тип", "клавиша", "код", "x", "y"])
                w.writerows(rows)
            if mrows:
                p = os.path.join(self.dir, "marks.csv")
                new = not os.path.exists(p)
                with open(p, "a", encoding="utf-8", newline="") as f:
                    w = csv.writer(f, delimiter=";")
                    if new:
                        w.writerow(["t", "номер", "подпись"])
                    w.writerows(mrows)
        except OSError:
            pass


# ====================================================================== интерфейс
CONFIG_PATH = os.path.join(data_dir(), "recorder_config.json")


ALLOWED = {"fn": lambda: True}       # главное окно подставляет «есть мастер-ключ»: без него страницы нет и клавиши молчат


def rec_on():
    import switches
    return switches.is_on("rec") and ALLOWED["fn"]()


def build_rec_tab(parent):
    import tkinter as tk
    import switches
    from tkinter import ttk, messagebox
    import theme as T
    import widgets

    cfg = {"fps": 15, "scale": "Полный", "minutes": 10, "track": True, "keys": "игровые", "minimize": True,
           "start_vk": 0x6A, "mark_vk": 0x6E, "notes": ""}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    st = {"sess": None, "alive": True, "binding": None}
    q = queue.Queue()

    def save_cfg():
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
        except OSError:
            pass

    pad = {"padx": 8, "pady": 3}
    parent.columnconfigure(0, weight=1)
    status_var = tk.StringVar(value="Готов к записи")
    ttk.Label(parent, textvariable=status_var, style="Status.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", **pad)
    ttk.Label(parent, text="Пишет экран и нажатия клавиш/мыши, чтобы по записи можно было разобрать, как должен работать новый бот. "
                           "Во время записи программа сворачивается: играй как обычно, нажимай горячую клавишу, чтобы остановить.",
              style="Muted.TLabel", wraplength=760, justify="left").grid(row=1, column=0, columnspan=4, sticky="w", **pad)
    btn = ttk.Button(parent, text="Начать запись", style="Accent.TButton", command=lambda: toggle())
    btn.grid(row=2, column=0, columnspan=4, sticky="ew", **pad)

    # горячие клавиши
    hk = ttk.Frame(parent)
    hk.grid(row=3, column=0, columnspan=4, sticky="w", **pad)
    start_var, mark_var = tk.StringVar(), tk.StringVar()

    def paint_keys():
        start_var.set(key_name(cfg["start_vk"]))
        mark_var.set(key_name(cfg["mark_vk"]))
    paint_keys()
    ttk.Label(hk, text="Старт/стоп:").pack(side="left")
    ttk.Label(hk, textvariable=start_var, style="Key.TLabel", width=14).pack(side="left", padx=4)
    ttk.Button(hk, text="Назначить", command=lambda: bind("start_vk")).pack(side="left")
    ttk.Label(hk, text="    Метка («вот здесь»):").pack(side="left")
    ttk.Label(hk, textvariable=mark_var, style="Key.TLabel", width=14).pack(side="left", padx=4)
    ttk.Button(hk, text="Назначить", command=lambda: bind("mark_vk")).pack(side="left")

    # настройки
    box = ttk.LabelFrame(parent, text="Что и как писать")
    box.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
    fps_var = tk.IntVar(value=cfg["fps"])
    scale_var = tk.StringVar(value=cfg["scale"])
    min_var = tk.IntVar(value=cfg["minutes"])
    track_var = tk.BooleanVar(value=cfg["track"])
    keys_var = tk.StringVar(value=cfg["keys"])
    mini_var = tk.BooleanVar(value=cfg["minimize"])
    notes_var = tk.StringVar(value=cfg["notes"])
    r = ttk.Frame(box)
    r.pack(fill="x", padx=6, pady=4)
    ttk.Label(r, text="Кадров в секунду:").pack(side="left")
    ttk.Spinbox(r, from_=5, to=30, width=4, textvariable=fps_var).pack(side="left", padx=(4, 12))
    ttk.Label(r, text="Размер:").pack(side="left")
    ttk.Combobox(r, textvariable=scale_var, values=["Полный", "Половина"], width=10, state="readonly").pack(side="left", padx=(4, 12))
    ttk.Label(r, text="Лимит, мин:").pack(side="left")
    ttk.Spinbox(r, from_=1, to=60, width=4, textvariable=min_var).pack(side="left", padx=4)
    r2 = ttk.Frame(box)
    r2.pack(fill="x", padx=6, pady=2)
    ttk.Checkbutton(r2, text="Писать нажатия клавиш и мыши", variable=track_var).pack(side="left")
    ttk.Label(r2, text="   Клавиши:").pack(side="left")
    ttk.Combobox(r2, textvariable=keys_var, values=["игровые", "все"], width=9, state="readonly").pack(side="left", padx=4)
    ttk.Checkbutton(r2, text="Свернуть программу на время записи", variable=mini_var).pack(side="left", padx=12)
    r3 = ttk.Frame(box)
    r3.pack(fill="x", padx=6, pady=(2, 6))
    ttk.Label(r3, text="Что записываю (для меня):").pack(side="left")
    ttk.Entry(r3, textvariable=notes_var, width=70).pack(side="left", padx=6, fill="x", expand=True)
    ttk.Label(box, text="Не вводи пароли во время записи. «Игровые» клавиши: WASD, стрелки, цифры, Space, Esc, "
                        "Shift/Ctrl/Alt, F1-F12, нампад и мышь; обычный текст в чате так не пишется.",
              style="Muted.TLabel", wraplength=760, justify="left").pack(anchor="w", padx=8, pady=(0, 6))

    # список записей
    lst = ttk.LabelFrame(parent, text="Записи")
    lst.grid(row=5, column=0, columnspan=4, sticky="nsew", **pad)
    parent.rowconfigure(5, weight=1)
    tv = ttk.Treeview(lst, columns=("date", "dur", "frames", "size", "note"), show="headings", height=6, selectmode="browse")
    for c, h, w, a in (("date", "Дата", 140, "w"), ("dur", "Длит.", 70, "e"), ("frames", "Кадров", 80, "e"),
                       ("size", "Размер", 80, "e"), ("note", "Заметка", 360, "w")):
        tv.heading(c, text=h)
        tv.column(c, width=w, anchor=a)
    tv.pack(fill="both", expand=True, padx=6, pady=6)
    tv.tag_configure("odd", background=T.FIELD_ALT)
    bb = ttk.Frame(lst)
    bb.pack(fill="x", padx=6, pady=(0, 6))

    def folder_of(iid):
        return os.path.join(rec_root(), iid)

    def refill():
        tv.delete(*tv.get_children())
        try:
            names = sorted((n for n in os.listdir(rec_root()) if os.path.isdir(folder_of(n))), reverse=True)
        except OSError:
            names = []
        for i, n in enumerate(names):
            meta = {}
            try:
                with open(os.path.join(folder_of(n), "meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass
            try:
                mb = os.path.getsize(os.path.join(folder_of(n), "video.mp4")) / 2 ** 20
            except OSError:
                mb = 0
            d = int(meta.get("длительность_с", 0))
            tv.insert("", "end", iid=n, tags=("odd",) if i % 2 else (),
                      values=(meta.get("начало", n), f"{d // 60}:{d % 60:02d}", meta.get("кадров", "—"),
                              f"{mb:.0f} МБ", meta.get("заметка", "")))

    def open_folder():
        sel = tv.selection()
        path = folder_of(sel[0]) if sel else rec_root()
        os.makedirs(rec_root(), exist_ok=True)
        try:
            os.startfile(path)
        except Exception:
            pass

    def delete_rec():
        sel = tv.selection()
        if not sel:
            return
        if messagebox.askyesno("Удалить запись", f"Удалить запись {sel[0]} вместе с видео?", parent=parent):
            shutil.rmtree(folder_of(sel[0]), ignore_errors=True)
            refill()
    ttk.Button(bb, text="Открыть папку", command=open_folder).pack(side="left")
    ttk.Button(bb, text="Удалить запись", command=delete_rec).pack(side="left", padx=6)

    # ---------------- запись
    def apply_cfg():
        try:
            cfg.update(fps=max(5, min(30, int(fps_var.get()))), scale=scale_var.get(), minutes=max(1, int(min_var.get())),
                       track=bool(track_var.get()), keys=keys_var.get(), minimize=bool(mini_var.get()),
                       notes=notes_var.get().strip())
        except (tk.TclError, ValueError):
            pass
        save_cfg()

    def start():
        if not rec_on():
            status_var.set("Запись выключена в меню (переключатель у пункта «Запись»)")
            return
        apply_cfg()
        try:
            from version import __version__
        except Exception:
            __version__ = ""
        s = Session(fps=cfg["fps"], scale=1.0 if cfg["scale"] == "Полный" else 0.5, track=cfg["track"],
                    all_keys=cfg["keys"] == "все", max_min=cfg["minutes"], notes=cfg["notes"], version=__version__)
        try:
            s.start()
        except Exception as ex:
            messagebox.showerror("Запись", str(ex), parent=parent)
            return
        st["sess"] = s
        STATE.update(rec=True, folder=s.dir)
        btn.configure(text="Остановить запись")
        if cfg["minimize"]:
            try:
                parent.winfo_toplevel().iconify()
            except tk.TclError:
                pass

    def stop(reason="вручную"):
        s = st["sess"]
        if not s:
            return
        status_var.set("Сохраняю запись…")
        parent.update_idletasks()
        s.stop(reason or s.reason or "вручную")
        st["sess"] = None
        STATE.update(rec=False, last=s.dir)
        btn.configure(text="Начать запись")
        try:
            top = parent.winfo_toplevel()
            top.deiconify()
            top.lift()
        except tk.TclError:
            pass
        refill()
        text = (f"Запись сохранена: {s.dir}\nДлительность {s.elapsed():.0f} с, кадров {s.frames}, "
                f"событий ввода {s.events}, меток {s.marks}.")
        if s.error:
            text += f"\n\nПредупреждение: {s.error}"
        status_var.set(f"Сохранено: {os.path.basename(s.dir)}")
        messagebox.showinfo("Запись", text, parent=parent)

    def toggle():
        if st["sess"]:
            stop()
        else:
            start()

    # ---------------- горячие клавиши и привязка
    def bind(which):
        st["binding"] = which
        status_var.set("Нажми новую клавишу…")

        def wait():
            end = time.time() + 6
            while time.time() < end and st["alive"]:
                for vk in range(8, 255):
                    if vk in (0x01, 0x02, 0x04, 0x10, 0x11, 0x12) or vk in (cfg["start_vk"], cfg["mark_vk"]) and False:
                        continue
                    if key_down(vk):
                        q.put(("bound", which, vk))
                        return
                time.sleep(0.02)
            q.put(("bound", which, None))
        threading.Thread(target=wait, daemon=True).start()

    def hotkeys():
        prev = {"s": True, "m": True}
        while st["alive"]:
            if st["binding"] is None and rec_on():
                s = key_down(cfg["start_vk"])
                m = key_down(cfg["mark_vk"])
                if s and not prev["s"]:
                    q.put(("toggle",))
                if m and not prev["m"]:
                    q.put(("mark",))
                prev["s"], prev["m"] = s, m
            time.sleep(0.03)
    threading.Thread(target=hotkeys, daemon=True).start()
    switches.listen(lambda k, v: q.put(("off",)) if k == "rec" and not v else None)

    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "toggle":
                    toggle()
                elif m[0] == "off" and st["sess"]:
                    stop("выключена в меню")
                elif m[0] == "mark" and st["sess"]:
                    n = st["sess"].mark()
                    status_var.set(f"● Метка {n} · {st['sess'].elapsed():.1f} с")
                elif m[0] == "bound":
                    st["binding"] = None
                    if m[2]:
                        cfg[m[1]] = m[2]
                        save_cfg()
                        paint_keys()
                    status_var.set("Готов к записи")
        except queue.Empty:
            pass
        except tk.TclError:
            return
        s = st["sess"]
        if s:
            if s.stop_ev.is_set():
                stop(s.reason or "вручную")
            else:
                STATE.update(t=s.elapsed(), frames=s.frames, dropped=s.dropped, mb=s.size_mb(), events=s.events,
                             marks=s.marks, fps=(s.frames / s.elapsed()) if s.elapsed() > 1 else 0.0)
                status_var.set(f"● Идёт запись {int(s.elapsed()) // 60:02d}:{int(s.elapsed()) % 60:02d}"
                               f" · кадров {s.frames} · событий {s.events} · меток {s.marks}")
        if st["alive"]:
            parent.after(200, pump)

    def provider():
        s = dict(STATE)
        return {"active": s["rec"], "label": (("● REC", "warn") if s["rec"] else ("Готов", "ok")),
                "status": "Идёт запись" if s["rec"] else "Готов к записи", "rec": s,
                "items": [("Кадров", s["frames"])]}
    widgets.register("rec", provider)

    def stop_all():
        st["alive"] = False
        if st["sess"]:
            try:
                st["sess"].stop("закрытие программы")
            except Exception:
                pass

    refill()
    parent.after(200, pump)
    return stop_all
