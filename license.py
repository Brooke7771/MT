# -*- coding: utf-8 -*-
"""
Доступ по ключам. Программа шлёт серверу ключ и отпечаток железа, сервер отвечает,
какие разделы открыть. Бесплатны всегда: очистка ОЗУ и калькулятор готовки.

Адрес сервера и мастер-ключи вписаны ниже (SERVER_URL, MASTER_KEYS). Мастер-ключи
можно добавить патчем программы, если потеряешь прежний.
"""
import os
import sys
import json
import time
import glob
import shutil
import hashlib
import platform
import subprocess

import net                       # общий SSL-клиент (как в updater/cooking)
import urllib.request

# ВПИШИ сюда адрес своего сервера Render (без слэша на конце):
SERVER_URL = "https://mt-server-oyxh.onrender.com"
# Мастер-ключ в коде НЕ хранится (репозиторий публичный). Админка включается, только если
# рядом с программой лежит файл admin_key.txt с мастер-ключом (та же строка, что ADMIN_SECRET
# на сервере; можно несколько, по одному в строке). У остальных пользователей файла нет.
MASTER_KEYS = []

OFFLINE_DAYS = 3                 # сколько суток без связи доступ держится по памяти
FREE = {"ram", "cook", "market", "pm"}           # всегда доступно без ключа
LOCKED = ["bot", "afk", "fish", "wood", "mine", "gym", "farm", "build", "test"]  # под ключом ("test" - страница с ботами на проверке)
ACCESS_NAMES = {"bot": "Швейный бот", "afk": "Анти-АФК", "fish": "Рыбалка", "wood": "Лесоруб", "mine": "Шахта", "gym": "Качалка", "farm": "Ферма",
                "build": "Стройка (шлифовка, лифт, молоток)", "test": "Тест (боты на проверке)",
                "ram": "Чистильщик ОЗУ", "cook": "Готовка", "market": "Рынок", "pm": "PM (таблица по скринам)"}


def app_dir():
    if getattr(sys, "frozen", False):
        import paths
        return paths.data_dir()
    return os.path.dirname(os.path.abspath(__file__))


KEY_PATH = os.path.join(app_dir(), "license_key.txt")      # введённый ключ
CACHE_PATH = os.path.join(app_dir(), "license_cache.json")  # последний ответ сервера


# ================================================================ отпечаток железа
def _wmic(section, prop):
    try:
        out = subprocess.run(["wmic", section, "get", prop], capture_output=True,
                             text=True, timeout=5, creationflags=0x08000000).stdout
        vals = [l.strip() for l in out.splitlines()[1:] if l.strip()]
        return vals[0] if vals else ""
    except Exception:
        return ""


def hwid():
    """Короткий стабильный код этого компьютера."""
    parts = [platform.node(), platform.machine()]
    if sys.platform == "win32":
        parts.append(_wmic("csproduct", "UUID"))
        parts.append(_wmic("cpu", "ProcessorId"))
        parts.append(_wmic("baseboard", "SerialNumber"))
        parts.append(os.environ.get("PROCESSOR_IDENTIFIER", ""))
    parts = [p for p in parts if p and "To be filled" not in p]
    digest = hashlib.sha256("|".join(parts).encode("utf-8", "ignore")).hexdigest()
    return digest[:32]


def hwid_short():
    h = hwid().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


# ================================================================ ключ на диске
def load_key():
    try:
        with open(KEY_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def save_key(key):
    try:
        with open(KEY_PATH, "w", encoding="utf-8") as f:
            f.write(key.strip())
    except Exception:
        pass


def clear_key():
    for p in (KEY_PATH, CACHE_PATH):
        try:
            os.remove(p)
        except OSError:
            pass


# ---- выгрузка ключа: уведомление окна и стирание файлов ботов
_hooks = []
_wipe = {"pending": False}
BOT_FILES = ("fishing_config.json", "fish_catch.json", "sewing_bot_config.json", "antiafk_config.json", "sewing_debug.png",
             "stats.json", "sanding_config.json", "clicker_config.json", "woodcut_config.json",
             "mining_config.json", "gym_config.json", "farm_config.json", "stroyka_config.json", "sewing_beta_config.json",
             "grill_config.json")
BOT_DIRS = ("antiafk_templates", "fish_samples")
BOT_GLOBS = ("debug_fishing_*",)


def on_change(fn):
    """Окно регистрирует функцию, которую зовут после смены/выгрузки ключа."""
    _hooks.append(fn)


def notify():
    for fn in list(_hooks):
        try:
            fn()
        except Exception:
            pass


def request_wipe():
    _wipe["pending"] = True


def wipe_pending():
    return _wipe["pending"]


def wipe_bot_files():
    """Стирает настройки, образцы и записи ботов (после выгрузки ключа)."""
    d = app_dir()
    for name in BOT_FILES:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    for name in BOT_DIRS:
        shutil.rmtree(os.path.join(d, name), ignore_errors=True)
    for pat in BOT_GLOBS:
        for p in glob.glob(os.path.join(d, pat)):
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        import stats
        stats.reset()
    except Exception:
        pass
    _wipe["pending"] = False


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


# ================================================================ запрос к серверу
def _post(path, payload, headers=None, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(SERVER_URL + path, data=data,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "MajesticTools", **(headers or {})})
    with net.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def check(key=None):
    """Спрашивает сервер про ключ. -> dict с ok, accesses, name, reason."""
    key = (key if key is not None else load_key()).strip()
    if not key:
        return {"ok": False, "reason": "no_key", "accesses": []}
    try:
        res = _post("/api/check", {"key": key, "hwid": hwid()})
        res.setdefault("accesses", [])
        if res.get("ok"):
            _save_cache({"key": key, "res": res, "ts": time.time()})
        else:
            try:
                os.remove(CACHE_PATH)       # сервер отказал: старый "ок" больше не действует
            except OSError:
                pass
        return res
    except Exception as ex:
        # нет связи - разрешаем по последнему удачному ответу этого же ключа,
        # но не дольше OFFLINE_DAYS суток с последней успешной проверки
        cache = _load_cache()
        fresh = time.time() - cache.get("ts", 0) < OFFLINE_DAYS * 86400
        if cache.get("key") == key and cache.get("res", {}).get("ok") and fresh:
            res = dict(cache["res"])
            res["offline"] = True
            return res
        return {"ok": False, "reason": "offline", "accesses": [], "error": str(ex)}


def granted(res):
    """Множество разделов, которые можно показать (с учётом бесплатных)."""
    acc = set(FREE)
    if res and res.get("ok"):
        acc |= set(res.get("accesses", []))
    if has_master():                    # владельцу (мастер-ключ на этом ПК) видны все закрытые разделы
        acc |= set(LOCKED)
    return acc


# ================================================================ админка
def valid_masters():
    keys = [m for m in MASTER_KEYS if m and "ВПИШИ" not in m]
    try:
        with open(os.path.join(app_dir(), "admin_key.txt"), encoding="utf-8-sig") as f:
            keys += [ln.strip() for ln in f if ln.strip()]
    except OSError:
        pass
    return keys


def has_master():
    return bool(valid_masters())


def is_master(token):
    return token.strip() in valid_masters()


def admin(path, token, payload=None):
    req = urllib.request.Request(SERVER_URL + "/api/admin/" + path,
                                 data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "X-Admin": token.strip(), "User-Agent": "MajesticTools"},
                                 method="POST")
    with net.urlopen(req, timeout=25) as r:
        return json.load(r)


def admin_get(path, token):
    req = urllib.request.Request(SERVER_URL + "/api/admin/" + path,
                                 headers={"X-Admin": token.strip(), "User-Agent": "MajesticTools"})
    with net.urlopen(req, timeout=25) as r:
        return json.load(r)
