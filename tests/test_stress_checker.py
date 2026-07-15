"""Тесты stress_checker: детект бана, мутации URL, пробы через фейковую
сессию (без реальной сети), оркестрация с пропуском по бану."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stress_checker as sc
from stress_checker import (
    _looks_banned, _mutations, probe_parsing, probe_load,
    probe_url_duplicates,
)


# ── Фейковая сессия: отдаёт запрограммированные ответы по URL ──


class _FakeResp:
    def __init__(self, status, body=b''):
        self.status = status
        self._body = body
        self.content = self

    async def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    async def release(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """route(url) -> (status, body) | Exception. Дефолт 200."""
    def __init__(self, route):
        self._route = route
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self._route(url)
        if isinstance(r, Exception):
            raise r
        status, body = r
        return _FakeResp(status, body)


def _const(status, body=b''):
    return lambda url: (status, body)


# ── _looks_banned ──


def test_ban_by_captcha_body():
    assert _looks_banned(200, 'подтвердите, что вы не робот', 0)
    assert _looks_banned(503, 'attention required | cloudflare', 0)
    print('✓ капча/челлендж в теле = бан независимо от кода')


def test_ban_403_only_after_streak():
    # Одиночный 403 в начале - это закрытый раздел, не бан обхода.
    assert not _looks_banned(403, '', 0)
    assert not _looks_banned(403, '', 2)
    # 403 после серии успехов - защита закрыла доступ.
    assert _looks_banned(403, '', 3)
    assert _looks_banned(429, '', 5)
    print('✓ 403/429 = бан только после серии успешных страниц')


def test_no_ban_on_normal():
    assert not _looks_banned(200, 'обычная страница', 10)
    assert not _looks_banned(404, '', 10)
    assert not _looks_banned(500, '', 10)
    print('✓ 200/404/500 без маркеров - не бан')


# ── _mutations ──


def test_mutations_category():
    muts = dict((k, v) for k, v in
                _mutations('https://x.ru/catalog/truby/', 'category'))
    assert muts['сдвоенный сегмент пути'] == \
        'https://x.ru/catalog/truby/truby/'
    assert muts['двойной слэш в пути'] == 'https://x.ru/catalog//truby/'
    assert '?PAGEN_1=99999' in muts['глубокая пагинация (?PAGEN_1=99999)']
    assert muts['сдвоенный GET-параметр'].endswith('?sort=price&sort=price')
    assert 'сдвоенный сегмент /filter/' not in muts   # не фильтр
    print('✓ мутации категории: сегмент/слэш/пагинация/параметр')


def test_mutations_filter_adds_filter_double():
    muts = dict((k, v) for k, v in _mutations(
        'https://x.ru/catalog/truby/filter/w-is-10/apply/', 'filter'))
    assert muts['сдвоенный сегмент /filter/'] == \
        'https://x.ru/catalog/truby/filter/filter/w-is-10/apply/'
    print('✓ у фильтра добавляется сдвоенный /filter/')


# ── probe_parsing ──


def test_parsing_clean():
    sess = _FakeSession(_const(200, b'ok'))
    res = asyncio.run(probe_parsing(sess, ['u1', 'u2', 'u3'], None))
    assert res['checked'] == 3 and not res['server_errors']
    assert not res['banned'] and res['stopped'] is None
    print('✓ парсинг чистый: без 5xx и бана')


def test_parsing_catches_5xx():
    route = lambda u: (500, b'') if u == 'bad' else (200, b'ok')
    sess = _FakeSession(route)
    res = asyncio.run(probe_parsing(sess, ['a', 'bad', 'b'], None))
    assert res['server_errors'] == [{'url': 'bad', 'code': 500}]
    print('✓ парсинг ловит 5xx с адресом')


def test_parsing_stops_on_ban():
    # 3 успешных, затем 403 - бан, обход обрывается, 5-я не запрашивается.
    def route(u):
        return (403, b'') if u == 'u4' else (200, b'ok')
    sess = _FakeSession(route)
    res = asyncio.run(probe_parsing(
        sess, ['u1', 'u2', 'u3', 'u4', 'u5'], None))
    assert res['banned'] and res['banned']['after'] == 3
    assert res['stopped'] == 'ban'
    assert 'u5' not in sess.calls          # обход прерван
    print('✓ парсинг: бан после серии успехов обрывает обход')


# ── probe_load ──


def test_load_clean_and_baseline():
    sess = _FakeSession(_const(200, b'ok'))
    res = asyncio.run(probe_load(
        sess, ['p1'], baselines={'p1': 100}, proxy_url=None,
        concurrency=5, waves=2))
    p = res['pages'][0]
    assert p['server_5xx'] == 0 and p['sent'] == 10
    assert p['median_ms'] is not None
    print('✓ нагрузка чистая: 5×2=10 запросов, 5xx нет')


def test_load_circuit_breaker():
    # Все 500 - предохранитель должен оборвать после первой волны.
    sess = _FakeSession(_const(500, b''))
    res = asyncio.run(probe_load(
        sess, ['p1'], baselines={}, proxy_url=None,
        concurrency=10, waves=3))
    p = res['pages'][0]
    assert p['stopped'] is True
    assert p['sent'] == 10          # только первая волна, не 30
    assert p['server_5xx'] == 10
    print('✓ нагрузка: >30% ошибок в первой волне обрывает пробу')


# ── probe_url_duplicates ──


def test_duplicates_clean():
    sess = _FakeSession(_const(404, b''))     # кривой URL -> 404, это ок
    res = asyncio.run(probe_url_duplicates(
        sess, [('category', 'https://x.ru/catalog/truby/')], None))
    assert res['server_errors'] == [] and res['checked'] == 4
    print('✓ дубли: 404 на кривом URL - не находка')


def test_duplicates_catches_5xx():
    def route(u):
        return (500, b'') if u.endswith('truby/truby/') else (200, b'')
    sess = _FakeSession(route)
    res = asyncio.run(probe_url_duplicates(
        sess, [('category', 'https://x.ru/catalog/truby/')], None))
    assert len(res['server_errors']) == 1
    assert res['server_errors'][0]['kind'] == 'сдвоенный сегмент пути'
    print('✓ дубли: 5xx на кривом URL - находка с меткой')


if __name__ == '__main__':
    test_ban_by_captcha_body()
    test_ban_403_only_after_streak()
    test_no_ban_on_normal()
    test_mutations_category()
    test_mutations_filter_adds_filter_double()
    test_parsing_clean()
    test_parsing_catches_5xx()
    test_parsing_stops_on_ban()
    test_load_clean_and_baseline()
    test_load_circuit_breaker()
    test_duplicates_clean()
    test_duplicates_catches_5xx()
    print('Все тесты stress_checker пройдены.')
