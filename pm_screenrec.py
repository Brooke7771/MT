# -*- coding: utf-8 -*-
"""Запись экрана для доказательств (PM): основной монитор в полном разрешении, H.264 (.mp4) через
кодировщик Windows (Media Foundation); если его нет - MPEG-4 (mp4v). Снимок и запись - в разных потоках;
если снимок не успевает за частотой кадров, недостающие кадры повторяются, и видео идёт с реальной скоростью.
Замерено (1080p): H.264 ~6-10 мс на кадр, ~170 МБ в минуту при 30 к/с (качество высокое, PSNR ~44,6)."""
import os
import time
import queue
import threading

QUALITY = {"1080p, 30 к/с (лучше всего)": (1.0, 30), "1080p, 20 к/с": (1.0, 20), "720p, 30 к/с (легче)": (2 / 3, 30)}


def default_dir():
    d = os.path.join(os.path.expanduser("~"), "Videos", "Majestic PM")
    return d


class ScreenRec:
    def __init__(self):
        self.on = False
        self.path = None
        self.t0 = 0.0
        self.frames = 0
        self.dropped = 0
        self.err = ""
        self.codec = ""
        self._q = None

    def elapsed(self):
        return time.time() - self.t0 if self.on else 0.0

    def start(self, folder, quality):
        if self.on:
            return
        scale, fps = QUALITY.get(quality, (1.0, 30))
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "PM_" + time.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4")
        self.on, self.frames, self.dropped, self.err = True, 0, 0, ""
        self.t0 = time.time()
        self._q = queue.Queue(maxsize=60)
        threading.Thread(target=self._capture, args=(scale, fps), daemon=True).start()

    def stop(self):
        self.on = False

    def _capture(self, scale, fps):
        import numpy as np
        import cv2
        import mss
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        sct = mss.mss()
        mon = sct.monitors[1]
        w, h = int(mon["width"] * scale) // 2 * 2, int(mon["height"] * scale) // 2 * 2
        writer = None
        for api, cc, name in ((cv2.CAP_MSMF, "avc1", "H.264"), (cv2.CAP_ANY, "mp4v", "MPEG-4")):
            try:
                wr = cv2.VideoWriter(self.path, api, cv2.VideoWriter_fourcc(*cc), fps, (w, h))
            except Exception:
                continue
            if wr.isOpened():
                writer, self.codec = wr, name
                break
        if writer is None:
            self.err, self.on = "не удалось открыть видеокодек", False
            return
        wt = threading.Thread(target=self._write, args=(writer,), daemon=True)
        wt.start()
        n = 0
        try:
            while self.on:
                img = np.asarray(sct.grab(mon))[:, :, :3]
                if scale != 1.0:
                    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
                due = int((time.time() - self.t0) * fps) + 1       # сколько кадров уже должно быть в видео
                reps = max(1, due - n)
                try:
                    self._q.put_nowait((np.ascontiguousarray(img), reps))
                    n += reps
                except queue.Full:
                    self.dropped += 1
                nxt = self.t0 + n / fps
                d = nxt - time.time()
                if d > 0:
                    time.sleep(d)
        except Exception as ex:
            self.err = str(ex)
        finally:
            self.on = False
            self._q.put(None)
            wt.join(10)
            try:
                sct.close()
            except Exception:
                pass

    def _write(self, writer):
        try:
            while True:
                it = self._q.get()
                if it is None:
                    break
                img, reps = it
                for _ in range(reps):
                    writer.write(img)
                    self.frames += 1
        except Exception as ex:
            self.err = str(ex)
        finally:
            writer.release()


def build_rec_bar(parent, cfg, save_cfg, msg, keys):
    """Ряд кнопок записи экрана во вкладке PM. keys - {vk: имя} для горячей клавиши. -> stop()."""
    import tkinter as tk
    from tkinter import ttk, filedialog
    import ctypes

    rec = ScreenRec()
    cfg.setdefault("rec_dir", default_dir())
    cfg.setdefault("rec_quality", next(iter(QUALITY)))
    cfg.setdefault("rec_vk", 0)
    st = {"alive": True, "prev": False}
    ttk.Label(parent, text="Запись экрана:").pack(side="left", padx=(16, 4))
    btn = ttk.Button(parent, text="⏺ Начать", command=lambda: toggle())
    btn.pack(side="left")
    q_var = tk.StringVar(value=cfg["rec_quality"] if cfg["rec_quality"] in QUALITY else next(iter(QUALITY)))
    ttk.Combobox(parent, textvariable=q_var, values=list(QUALITY), state="readonly", width=24).pack(side="left", padx=4)
    hk_var = tk.StringVar(value=keys.get(cfg["rec_vk"], "выкл"))
    ttk.Label(parent, text="клавиша:").pack(side="left")
    ttk.Combobox(parent, textvariable=hk_var, values=list(keys.values()), state="readonly", width=9).pack(side="left", padx=4)
    ttk.Button(parent, text="Открыть папку", command=lambda: open_dir()).pack(side="left", padx=2)
    ttk.Button(parent, text="…", width=2, command=lambda: pick_dir()).pack(side="left")
    t_var = tk.StringVar()
    ttk.Label(parent, textvariable=t_var, style="Muted.TLabel").pack(side="left", padx=6)

    def save(*a):
        cfg["rec_quality"] = q_var.get()
        cfg["rec_vk"] = next((k for k, v in keys.items() if v == hk_var.get()), 0)
        save_cfg(cfg)
    q_var.trace_add("write", save)
    hk_var.trace_add("write", save)

    def toggle():
        if rec.on:
            rec.stop()
            btn.configure(text="⏺ Начать")
            parent.after(1500, lambda: msg(f"Видео сохранено: {rec.path}" if not rec.err else f"Запись: {rec.err}"))
            return
        try:
            rec.start(cfg["rec_dir"], q_var.get())
        except OSError as ex:
            msg(f"Запись не началась: {ex}")
            return
        btn.configure(text="■ Стоп")
        msg("Идёт запись экрана - «Стоп» или та же клавиша, чтобы закончить")

    def open_dir():
        d = cfg["rec_dir"]
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except OSError as ex:
            msg(f"Папка не открылась: {ex}")

    def pick_dir():
        nd = filedialog.askdirectory(title="Куда сохранять записи экрана", initialdir=cfg["rec_dir"])
        if nd:
            cfg["rec_dir"] = nd
            save_cfg(cfg)
            msg(f"Записи экрана - в {nd}")

    def tick():
        if not st["alive"]:
            return
        try:
            if rec.on:
                s = int(rec.elapsed())
                t_var.set(f"● {s // 60}:{s % 60:02d}  {rec.codec}")
            elif btn.cget("text") != "⏺ Начать":         # остановилась сама (ошибка)
                btn.configure(text="⏺ Начать")
                t_var.set("")
                msg(f"Запись остановлена: {rec.err or 'готово'}")
            else:
                t_var.set("")
            vk = cfg.get("rec_vk") or 0
            if vk:
                down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not st["prev"]:
                    toggle()
                st["prev"] = down
            parent.after(150, tick)
        except tk.TclError:
            pass
    parent.after(300, tick)

    def stop():
        st["alive"] = False
        rec.stop()
    return stop
