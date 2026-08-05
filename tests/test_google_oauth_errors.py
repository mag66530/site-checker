# -*- coding: utf-8 -*-
"""Расшифровка ошибок обмена кода: Google отвечает скупо, человеку нужна причина."""
import pytest

import google_oauth


class _Ответ:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"{}"

    def json(self):
        return self._payload


def _подмена(monkeypatch, status, payload):
    monkeypatch.setattr(google_oauth.requests, "post",
                        lambda *a, **k: _Ответ(status, payload))


def test_код_уже_использован(monkeypatch):
    _подмена(monkeypatch, 400, {"error": "invalid_grant",
                                "error_description": "Bad Request"})
    with pytest.raises(RuntimeError) as e:
        google_oauth.exchange_code("cid", "sec", "code", "https://app")
    assert "уже использован" in str(e.value)


def test_несовпадение_адреса_возврата(monkeypatch):
    _подмена(monkeypatch, 400, {"error": "redirect_uri_mismatch",
                                "error_description": "Bad Request"})
    with pytest.raises(RuntimeError) as e:
        google_oauth.exchange_code("cid", "sec", "code", "https://app")
    assert "адрес возврата" in str(e.value) and "https://app" in str(e.value)


def test_чужая_пара_ключей(monkeypatch):
    _подмена(monkeypatch, 401, {"error": "invalid_client"})
    with pytest.raises(RuntimeError) as e:
        google_oauth.exchange_code("cid", "sec", "code", "https://app")
    assert "Client ID / Client secret" in str(e.value)


def test_успех_возвращает_токены(monkeypatch):
    _подмена(monkeypatch, 200, {
        "access_token": "at", "refresh_token": "rt",
        "scope": "https://www.googleapis.com/auth/drive.file openid"})
    monkeypatch.setattr(google_oauth.requests, "get",
                        lambda *a, **k: _Ответ(200, {"email": "x@y.ru"}))
    res = google_oauth.exchange_code("cid", "sec", "code", "https://app")
    assert res["refresh_token"] == "rt" and res["email"] == "x@y.ru"


def test_флажок_доступа_к_диску_сняли(monkeypatch):
    """Google выдаёт токен даже без права на Диск - ловим это сразу, а не на
    первой записи («insufficient authentication scopes»)."""
    _подмена(monkeypatch, 200, {"access_token": "at", "refresh_token": "rt",
                                "scope": "openid email"})
    monkeypatch.setattr(google_oauth.requests, "get",
                        lambda *a, **k: _Ответ(200, {"email": "x@y.ru"}))
    with pytest.raises(RuntimeError) as e:
        google_oauth.exchange_code("cid", "sec", "code", "https://app")
    assert "доступ к Google Диску не выдан" in str(e.value)
