# -*- coding: utf-8 -*-
"""Прозвон адресов из карты сайта: код ответа + noindex (на фейковой сессии)."""
import asyncio

import sitemap_audit as sa


class _Тело:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n=None):
        return self._data[:n] if n else self._data


class _Ответ:
    def __init__(self, status=200, headers=None, body=b''):
        self.status = status
        self.headers = headers or {}
        self.content = _Тело(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Сессия:
    """Отдаёт заранее заданный ответ по URL; помнит, как её вызывали."""

    def __init__(self, ответы):
        self.ответы = ответы
        self.вызовы = []

    def get(self, url, **kw):
        self.вызовы.append((url, kw))
        о = self.ответы.get(url)
        if isinstance(о, Exception):
            raise о
        return о if о is not None else _Ответ(200)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _прозвон(monkeypatch, ответы, urls, **kw):
    сессия = _Сессия(ответы)
    monkeypatch.setattr(sa, 'asyncio', asyncio)

    import aiohttp
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda *a, **k: сессия)
    res = asyncio.run(sa.probe_sitemap_urls(urls, **kw))
    return res, сессия


_NOINDEX_HTML = (b'<html><head><meta name="robots" content="noindex, follow">'
                 b'</head><body></body></html>')


def test_редирект_из_карты_это_находка(monkeypatch):
    """301 в карте - находка сама по себе: в карте лежит устаревший адрес.
    Поэтому за редиректом НЕ идём (allow_redirects=False)."""
    res, сессия = _прозвон(monkeypatch, {
        'https://a.ru/old/': _Ответ(301, {'Location': '/new/'}),
        'https://a.ru/ok/': _Ответ(200, {}, b'<html><head></head></html>'),
    }, ['https://a.ru/old/', 'https://a.ru/ok/'])
    assert res['checked'] == 2
    assert res['bad_status'] == [{'url': 'https://a.ru/old/', 'status': 301}]
    assert res['noindex'] == []
    assert all(kw.get('allow_redirects') is False for _u, kw in сессия.вызовы)


def test_404_это_находка_а_обрыв_связи_нет(monkeypatch):
    """Плохой код ответа - задача клиенту. Сетевой сбой - наше ограничение:
    свалить их вместе значит отправить в работу живые адреса, до которых не
    доехали мы сами."""
    res, сессия = _прозвон(monkeypatch, {
        'https://a.ru/gone/': _Ответ(404),
        'https://a.ru/dead/': RuntimeError('таймаут'),
    }, ['https://a.ru/gone/', 'https://a.ru/dead/'])
    assert res['bad_status'] == [{'url': 'https://a.ru/gone/', 'status': 404}]
    assert res['unreachable'] == [{'url': 'https://a.ru/dead/', 'why': 'таймаут'}]
    # на сетевой сбой даём вторую попытку, на 404 - нет
    попытки = [u for u, _kw in сессия.вызовы]
    assert попытки.count('https://a.ru/dead/') == 2
    assert попытки.count('https://a.ru/gone/') == 1


def test_пустой_текст_таймаута_не_уходит_в_отчёт(monkeypatch):
    """У таймаутов aiohttp str(e) пустой - в отчёте было «нет ответа: »."""
    class _Таймаут(Exception):
        def __str__(self):
            return ''

    res, _ = _прозвон(monkeypatch, {'https://a.ru/slow/': _Таймаут()},
                      ['https://a.ru/slow/'])
    assert res['unreachable'] == [{'url': 'https://a.ru/slow/',
                                   'why': '_Таймаут'}]


def test_вторая_попытка_спасает_случайный_таймаут(monkeypatch):
    """Первый запрос упал, второй ответил 200 - находки быть не должно."""
    сессия = _Сессия({})
    состояние = {'n': 0}

    def get(url, **kw):
        сессия.вызовы.append((url, kw))
        состояние['n'] += 1
        if состояние['n'] == 1:
            raise RuntimeError('обрыв')
        return _Ответ(200, {}, b'<html><head></head></html>')

    сессия.get = get
    import aiohttp
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda *a, **k: сессия)
    res = asyncio.run(sa.probe_sitemap_urls(['https://a.ru/x/']))
    assert res['bad_status'] == [] and res['unreachable'] == []
    assert res['checked'] == 1 and len(сессия.вызовы) == 2


def test_noindex_в_meta_и_в_заголовке(monkeypatch):
    res, _ = _прозвон(monkeypatch, {
        'https://a.ru/meta/': _Ответ(200, {}, _NOINDEX_HTML),
        'https://a.ru/hdr/': _Ответ(200, {'X-Robots-Tag': 'noindex'},
                                    b'<html><head></head></html>'),
        'https://a.ru/ok/': _Ответ(200, {}, b'<html><head></head></html>'),
    }, ['https://a.ru/meta/', 'https://a.ru/hdr/', 'https://a.ru/ok/'])
    сигналы = {n['url']: n['signal'] for n in res['noindex']}
    assert 'meta robots' in сигналы['https://a.ru/meta/']
    assert 'X-Robots-Tag' in сигналы['https://a.ru/hdr/']
    assert 'https://a.ru/ok/' not in сигналы
    assert res['bad_status'] == []


def test_защита_сайта_не_выдаётся_за_битый_адрес(monkeypatch):
    """403/429/503 - про антибот и лимиты, а не про адрес в карте. Врать,
    что страницы нет, нельзя: это уйдёт клиенту в работу."""
    res, _ = _прозвон(monkeypatch, {
        'https://a.ru/x/': _Ответ(403),
        'https://a.ru/y/': _Ответ(503),
    }, ['https://a.ru/x/', 'https://a.ru/y/'])
    assert res['bad_status'] == [] and res['noindex'] == []
    assert res['blocked'] == 2


def test_прозвон_ограничен_срезом(monkeypatch):
    urls = [f'https://a.ru/{i}/' for i in range(500)]
    res, сессия = _прозвон(monkeypatch, {}, urls, limit=10)
    assert res['checked'] == 10 and res['sample_of'] == 500
    assert len(сессия.вызовы) == 10


def test_бюджет_времени_обрывает_прозвон_без_выводов(monkeypatch):
    """Что не успели за отведённое время - «не проверено», а не находка:
    иначе медленный сайт превращался бы в список битых адресов."""
    res, сессия = _прозвон(monkeypatch, {}, ['https://a.ru/1/', 'https://a.ru/2/'],
                           budget_s=0)
    assert res['checked'] == 0 and res['skipped'] == 2
    assert res['bad_status'] == [] and res['unreachable'] == []
    assert сессия.вызовы == []          # ни одного запроса не сделали


def test_пустой_список_и_нулевой_лимит(monkeypatch):
    res, сессия = _прозвон(monkeypatch, {}, [], limit=10)
    assert res == {'checked': 0, 'sample_of': 0, 'bad_status': [],
                   'noindex': [], 'unreachable': [], 'blocked': 0,
                   'skipped': 0, 'error': None}
    assert сессия.вызовы == []
    res2, сессия2 = _прозвон(monkeypatch, {}, ['https://a.ru/'], limit=0)
    assert res2['checked'] == 0 and сессия2.вызовы == []
