# -*- coding: utf-8 -*-
"""Автокликер Вебмастера и диагностика 403.

Живой случай (МПИ): кликер прошёл по всем поддоменам и отчитался «блоков 0»,
хотя ошибки на сайтах были. В логе видно, что Вебмастер увёл на
passport.yandex.ru - то есть разбиралась страница входа. Проверка авторизации
была только в _collect_sites, а при запуске с --project она не вызывается.

Второй случай (ИМП): беклинки не читались, а сообщение винило «права
владельца». Права были подтверждены на всех 242 хостах - не хватало scope
EXTERNAL_LINKS у OAuth-приложения."""
import asyncio

import pytest

import webmaster_recheck as W


class _Стр:
    """Заглушка страницы Playwright: важен только url."""

    def __init__(self, url):
        self.url = url

    async def query_selector_all(self, sel):
        return []

    async def goto(self, url, **kw):
        return None

    async def wait_for_timeout(self, ms):
        return None


def test_страница_входа_не_считается_отсутствием_ошибок():
    """Раньше «блоков 0» на странице логина выглядело как «ошибок нет»."""
    page = _Стр('https://passport.yandex.ru/auth?mode=auth&retpath=...')
    with pytest.raises(W.NotAuthorized):
        asyncio.run(W._process_problems(page, dry_run=True))


def test_обычная_страница_разбирается_как_раньше():
    page = _Стр('https://webmaster.yandex.ru/site/https:a.ru:443/'
                'optimization/checklist/')
    stat = asyncio.run(W._process_problems(page, dry_run=True))
    assert stat['problems'] == 0 and stat['clicked'] == 0


def test_проверка_авторизации_ловит_редирект():
    page = _Стр('https://passport.yandex.ru/auth')
    assert asyncio.run(W._check_auth(page)) is False


def test_проверка_авторизации_пропускает_залогиненного():
    page = _Стр('https://webmaster.yandex.ru/sites/')
    assert asyncio.run(W._check_auth(page)) is True


# ── 403 от API Вебмастера: недостающий scope, а не права на сайт ──────

class _Ответ:
    def __init__(self, text):
        self.status_code = 403
        self.text = text


def _вызвать_403(текст, monkeypatch):
    import webmaster_api as A
    monkeypatch.setattr(A.requests, 'get',
                        lambda *a, **k: _Ответ(текст), raising=False)
    with pytest.raises(PermissionError) as e:
        A._get('токен', '/user/')
    return str(e.value)


def test_403_называет_недостающее_право(monkeypatch):
    msg = _вызвать_403(
        '{"error_message":"Access to this resource is not allowed with scopes '
        'available for this application. Required scope: EXTERNAL_LINKS, '
        'application scopes: [ALL_SCOPES, HOST_LIST, COMMON]"}', monkeypatch)
    assert 'EXTERNAL_LINKS' in msg
    assert 'ALL_SCOPES, HOST_LIST, COMMON' in msg
    assert 'oauth.yandex.ru' in msg          # где чинить
    assert 'Права на сайт тут ни при чём' in msg


def test_403_без_подсказки_яндекса_отдаёт_его_ответ(monkeypatch):
    msg = _вызвать_403('{"error_message":"Forbidden"}', monkeypatch)
    assert 'Forbidden' in msg                # текст Яндекса не теряем
