# -*- coding: utf-8 -*-
"""Сетевые запросы с нормальной проверкой сертификатов.

На некоторых Windows встроенный Python не находит нужный корневой сертификат
(Windows докачивает их только для своих программ, а антивирусы иногда подменяют
сертификаты своими). Поэтому берём сертификаты и из Windows, и из пакета certifi.
"""
import ssl
import urllib.request

_ctx = None


def ssl_context():
    global _ctx
    if _ctx is None:
        ctx = ssl.create_default_context()          # сертификаты Windows
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())  # + стандартный набор Mozilla
        except Exception:
            pass
        _ctx = ctx
    return _ctx


def urlopen(req, timeout=15):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
