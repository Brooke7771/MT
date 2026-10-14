# -*- coding: utf-8 -*-
"""Накопительная статистика «за всё время»: сколько поймано, сколько комплектов, нажатий,
сколько времени отработал каждый бот. Хранится в stats.json в папке данных; стирается вместе
с файлами ботов при выгрузке ключа."""
import os
import sys
import json
import time

MAIN = {"fish": "Поймано", "bot": "Комплектов", "afk": "Нажатий"}     # главный счётчик каждого бота
_data = {}
_state = {"loaded": False, "dirty": False, "saved": 0.0}


def _dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


def path():
    return os.path.join(_dir(), "stats.json")


def _load():
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _data.update(d)
    except Exception:
        pass


def get(key):
    _load()
    d = _data.get(key) or {}
    return {"count": int(d.get("count", 0)), "secs": float(d.get("secs", 0.0))}


def add(key, count=0, secs=0.0):
    _load()
    d = _data.setdefault(key, {"count": 0, "secs": 0.0})
    d["count"] = int(d.get("count", 0)) + int(count)
    d["secs"] = float(d.get("secs", 0.0)) + float(secs)
    _state["dirty"] = True
    if time.time() - _state["saved"] > 20:
        save()


def save():
    if not _state["dirty"]:
        return
    try:
        with open(path(), "w", encoding="utf-8") as f:
            json.dump(_data, f)
        _state["dirty"] = False
        _state["saved"] = time.time()
    except Exception:
        pass


def reset():
    """После стирания файлов ботов: обнулить и в памяти."""
    _data.clear()
    _state.update(loaded=True, dirty=False)


# ---- лента событий для страницы «Главная»
EVENTS = []


def event(text, kind="info"):
    EVENTS.append((time.time(), text, kind))
    del EVENTS[:-30]
