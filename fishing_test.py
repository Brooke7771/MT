# -*- coding: utf-8 -*-
"""
Отдельный тестовый запуск только рыбалки - доводим бота, не трогая остальную программу.

  py fishing_test.py                 - окно только с рыбалкой (как в программе)
  py fishing_test.py --video file.mp4 - прогнать запись: что бот видит и что бы нажал
  py fishing_test.py --video file.mp4 --out debug.mp4 - то же + видео с пометками бота

Сборка в exe:
  py -m PyInstaller -F -w --uac-admin -n FishingTest fishing_test.py
"""
import sys
import time


def replay(path, out=None):
    import cv2
    import fishing as FI
    import fishing_debug as FD
    writer = None
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    tr = FI.Tracker()
    i = 0
    in_fight = False
    last_key = None
    line = ""
    while True:
        ok, f = cap.read()
        if not ok:
            break
        t = i / fps
        i += 1
        if i % 2:
            continue
        H, W = f.shape[:2]
        if FI.minigame_on(f):
            if not in_fight:
                in_fight = True
                tr.reset()
                print(f"{t:7.1f}s  начало вываживания")
            d = tr.update(f, FI.sc(FI.ZONE, W, H), t)
            key = None if d is None else ("D" if d < 0 else "A")
            if out:
                dbg = dict(tr.dbg)
                dbg.update(state="Тяну рыбу", held=key, dir=tr.last_dir, source=tr.source, W=W, H=H, t=t)
                img = FD.annotate(f, dbg)
                if writer is None:
                    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), fps / 2, (img.shape[1], img.shape[0]))
                writer.write(img)
            if key != last_key:
                why = tr.source if key else "-"
                print(f"{t:7.1f}s  держу {key or 'ничего'}  (рыба {'влево' if d == -1 else 'вправо' if d == 1 else '?'}, по: {why})")
                last_key = key
            line += key or "."
        else:
            if in_fight:
                in_fight = False
                last_key = None
                print(f"{t:7.1f}s  конец вываживания")
                print("         " + line[::4])
                line = ""
            if FI.dialog_on(f):
                pass
    if writer is not None:
        writer.release()
        print("отладочное видео:", out)
    print("готово")


def gui():
    import tkinter as tk
    import switches
    switches.ENABLED["fish"] = True          # в тестовом запуске рыбалка всегда разрешена
    import theme as T
    import fishing
    root = tk.Tk()
    root.title("Тест рыбалки")
    root.minsize(760, 640)
    T.apply(root)
    root.attributes("-topmost", True)
    from tkinter import ttk
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True, padx=14, pady=10)
    stop = fishing.build_fishing_tab(frame)
    T.dark_titlebar(root)

    def close():
        stop()
        root.after(200, root.destroy)
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    if "--video" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
        replay(sys.argv[sys.argv.index("--video") + 1], out)
    else:
        gui()
