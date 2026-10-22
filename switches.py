# -*- coding: utf-8 -*-
"""Выключатели ботов в боковом меню. Выключенный бот не запускается ни кнопкой, ни горячей клавишей."""
import os
import sys
import json


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


PATH = os.path.join(app_dir(), "modules_config.json")
NAMES = {"bot": "Швейный бот", "afk": "Анти-АФК", "fish": "Рыбалка", "sand": "Шлифовка", "click": "Кликер", "wood": "Лесоруб",
         "mine": "Шахта", "gym": "Качалка", "farm": "Ферма", "rec": "Запись", "lift": "Лифт", "hammer": "Молоток"}
ENABLED = {"bot": True, "afk": False, "fish": False, "sand": False, "click": False, "wood": False,
           "mine": False, "gym": False, "farm": False, "rec": True, "lift": False, "hammer": False}     # новые боты по умолчанию выключены; запись - включена, как была
_listeners = []

try:
    with open(PATH, encoding="utf-8") as f:
        ENABLED.update({k: bool(v) for k, v in json.load(f).items() if k in ENABLED})
except Exception:
    pass


def is_on(key):
    return ENABLED.get(key, True)


def set_on(key, value):
    ENABLED[key] = bool(value)
    try:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(ENABLED, f)
    except Exception:
        pass
    for fn in list(_listeners):
        try:
            fn(key, value)
        except Exception:
            pass


def listen(fn):
    _listeners.append(fn)
