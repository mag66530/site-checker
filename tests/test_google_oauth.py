# -*- coding: utf-8 -*-
"""Чистые функции OAuth-привязки Google-аккаунта проекта (без сети)."""
from urllib.parse import parse_qs, urlparse

import google_oauth


def test_auth_url_несёт_проект_и_offline_доступ():
    url = google_oauth.auth_url("cid.apps.googleusercontent.com",
                                "https://site-checker.streamlit.app", "sm")
    q = parse_qs(urlparse(url).query)
    assert q["state"] == ["gdrive:sm"]
    # без offline+consent Google не вернёт refresh-токен
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["redirect_uri"] == ["https://site-checker.streamlit.app"]
    assert "drive.file" in q["scope"][0]


def test_project_from_state():
    assert google_oauth.project_from_state("gdrive:mpe") == "mpe"
    assert google_oauth.project_from_state("что-то чужое") == ""
    assert google_oauth.project_from_state("") == ""


def test_access_token_без_данных_не_ходит_в_сеть():
    assert google_oauth.access_token("", "", "") == ""
    assert google_oauth.access_token("cid", "sec", "") == ""
