# -*- coding: utf-8 -*-
"""Где программа хранит свои файлы. Собранный exe можно держать где угодно (например, на
рабочем столе): настройки, ключ, кэш и записи лежат в %LOCALAPPDATA%\\MajesticTools.
При запуске из исходников (py ...) всё остаётся рядом с кодом."""
import os
import sys
import shutil

# что лежало рядом с exe в старых версиях - переносим один раз
_OLD = ("license_key.txt", "license_cache.json", "admin_key.txt", "modules_config.json",
        "fishing_config.json", "antiafk_config.json", "sewing_bot_config.json",
        "ram_cleaner_config.json", "cooking_config.json", "cooking_timers.json",
        "antiafk_templates", "cache")
_done = {}
APP = "MajesticTools"          # имя папки данных; у отдельной программы PM своё (pm_tools.py)


def data_dir():
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    if "d" in _done:
        return _done["d"]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.dirname(sys.executable)
    _done["d"] = d
    if APP == "MajesticTools":
        _migrate(d)
    return d


def _migrate(d):
    src = os.path.dirname(sys.executable)
    if os.path.normcase(src) == os.path.normcase(d):
        return
    for name in _OLD:
        a, b = os.path.join(src, name), os.path.join(d, name)
        if os.path.exists(a) and not os.path.exists(b):
            try:
                (shutil.copytree if os.path.isdir(a) else shutil.copy2)(a, b)
            except OSError:
                pass
