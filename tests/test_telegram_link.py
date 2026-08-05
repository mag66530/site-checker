# -*- coding: utf-8 -*-
"""Привязка Telegram: разбор /start и то, что привязка чужого кода не выдаётся
за успех нажавшего (реальный случай: кнопку нажал один, подтвердился другой)."""
import telegram_link


def test_collect_starts_разбирает_код_и_чат(monkeypatch):
    updates = [
        {"update_id": 10, "message": {"text": "/start uAAA",
                                      "chat": {"id": 111, "username": "kolya"}}},
        {"update_id": 11, "message": {"text": "привет", "chat": {"id": 222}}},
        {"update_id": 12, "message": {"text": "/start@bot uBBB",
                                      "chat": {"id": 333, "first_name": "Оля"}}},
    ]
    monkeypatch.setattr(telegram_link, "_api", lambda *a, **k: updates)
    telegram_link._offset.clear()
    res = telegram_link.collect_starts("token")
    assert res == {"uAAA": {"chat_id": "111", "username": "kolya"},
                   "uBBB": {"chat_id": "333", "username": "Оля"}}
    # смещение сдвинуто за последний апдейт - повторно те же не заберём
    assert telegram_link._offset["token"] == 13


def test_try_link_all_возвращает_кого_привязал(monkeypatch):
    """Ключевое: вернуть НАБОР пользователей, а не «сколько». По нему интерфейс
    отличает «подключился ты» от «подключился кто-то другой»."""
    monkeypatch.setattr(telegram_link, "collect_starts",
                        lambda *a, **k: {"uAAA": {"chat_id": "111", "username": "k"}})
    monkeypatch.setattr(telegram_link, "send_hello", lambda *a, **k: True)
    monkeypatch.setattr(telegram_link, "_подпись_аккаунта", lambda uid: "")

    import sys
    import types
    fake = types.ModuleType("auth.db")
    fake.telegram_link_by_code = lambda code, chat, user="": "user-7"
    pkg = types.ModuleType("auth")
    pkg.db = fake
    monkeypatch.setitem(sys.modules, "auth", pkg)
    monkeypatch.setitem(sys.modules, "auth.db", fake)

    assert telegram_link.try_link_all("token") == {"user-7"}


def test_link_url_и_код():
    code = telegram_link.make_code("user-1")
    assert code.startswith("u") and len(code) == 17
    assert telegram_link.link_url("bot", code) == f"https://t.me/bot?start={code}"
    assert telegram_link.link_url("", code) == ""
