# -*- coding: utf-8 -*-
"""Обновление через GitHub Releases: проверка, скачивание, замена exe и перезапуск."""
import os
import re
import sys
import json
import time
import subprocess
import urllib.request

from version import __version__, REPO

ASSET_NAME = "MajesticTools.exe"
API = "https://api.github.com/repos/{}/releases/latest"
UA = {"User-Agent": "MajesticTools-updater"}


def is_frozen():
    return getattr(sys, "frozen", False)


def parse_ver(v):
    nums = re.findall(r"\d+", v.lstrip("vV").split("-")[0])
    return tuple(int(x) for x in nums[:4]) or (0,)


def check():
    """None - обновлений нет, иначе dict(version, url, size, notes)."""
    if not REPO:
        raise RuntimeError("эта копия собрана не на GitHub, обновлять неоткуда")
    req = urllib.request.Request(API.format(REPO),
                                 headers=dict(UA, Accept="application/vnd.github+json"))
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if parse_ver(tag) <= parse_ver(__version__):
        return None
    for a in data.get("assets", []):
        if a.get("name") == ASSET_NAME:
            return {"version": tag.lstrip("vV"), "url": a["browser_download_url"],
                    "size": int(a.get("size") or 0), "notes": (data.get("body") or "").strip()}
    return None          # релиз есть, но exe ещё собирается


def download(info, progress=None):
    """Качает новый exe рядом с текущим. Возвращает путь к скачанному."""
    new = sys.executable + ".new"
    req = urllib.request.Request(info["url"], headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r, open(new, "wb") as f:
        total = info["size"] or int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total)
    with open(new, "rb") as f:
        head = f.read(2)
    if head != b"MZ" or (info["size"] and os.path.getsize(new) != info["size"]):
        os.remove(new)
        raise RuntimeError("скачанный файл повреждён, попробуй ещё раз")
    return new


def apply_and_restart(new):
    """Подменяет exe и запускает новую версию. Текущую программу после этого нужно закрыть."""
    exe = sys.executable
    old = exe + ".old"
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            old = f"{exe}.{int(time.time())}.old"
    os.replace(exe, old)              # запущенный exe Windows разрешает переименовать
    try:
        os.replace(new, exe)
    except Exception:
        os.replace(old, exe)
        raise
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env, close_fds=True)


def cleanup_old():
    """Удаляет старый exe, оставшийся после прошлого обновления."""
    if not is_frozen():
        return
    d = os.path.dirname(sys.executable)
    base = os.path.basename(sys.executable)
    for fn in os.listdir(d):
        if fn.startswith(base) and (fn.endswith(".old") or fn.endswith(".new")):
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass
