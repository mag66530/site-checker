"""Тесты http_checker – классификация статусов и оценка скорости."""
import sys
sys.path.insert(0, '/home/claude/site-checker-py')

from http_checker import classify, should_retry, rate_speed, STATUS, SPEED


def test_classify_2xx():
    assert classify(200, None) == STATUS.OK
    assert classify(201, None) == STATUS.OK
    assert classify(299, None) == STATUS.OK
    print('✓ 2xx → ok')


def test_classify_3xx():
    assert classify(301, None) == STATUS.REDIRECT
    assert classify(302, None) == STATUS.REDIRECT
    assert classify(307, None) == STATUS.REDIRECT
    print('✓ 3xx → redirect')


def test_classify_4xx():
    assert classify(404, None) == STATUS.NOT_FOUND
    assert classify(400, None) == STATUS.CLIENT_ERROR
    assert classify(403, None) == STATUS.CLIENT_ERROR
    assert classify(403, 'something') == STATUS.CLIENT_ERROR  # body есть
    print('✓ 4xx → not_found / client_error')


def test_classify_5xx():
    assert classify(500, None) == STATUS.SERVER_ERROR
    assert classify(502, None) == STATUS.SERVER_ERROR
    assert classify(503, None) == STATUS.SERVER_ERROR
    print('✓ 5xx → server_error')


def test_classify_errors():
    assert classify(None, 'timeout') == STATUS.TIMEOUT
    assert classify(None, 'network') == STATUS.NETWORK
    print('✓ timeout/network → корректные статусы')


def test_should_retry():
    """Ретраим только то, что может стать ОК. 4xx – устойчивый."""
    assert should_retry(STATUS.TIMEOUT) is True
    assert should_retry(STATUS.NETWORK) is True
    assert should_retry(STATUS.SERVER_ERROR) is True
    assert should_retry(STATUS.OK) is False
    assert should_retry(STATUS.REDIRECT) is False
    assert should_retry(STATUS.NOT_FOUND) is False
    assert should_retry(STATUS.CLIENT_ERROR) is False
    print('✓ Логика ретраев: 5xx/timeout/network – да, 4xx – нет')


def test_rate_speed():
    assert rate_speed(1000) == SPEED.FAST
    assert rate_speed(2499) == SPEED.FAST
    assert rate_speed(2500) == SPEED.NORMAL
    assert rate_speed(3999) == SPEED.NORMAL
    assert rate_speed(4000) == SPEED.SLOW
    assert rate_speed(7999) == SPEED.SLOW
    assert rate_speed(8000) == SPEED.VERY_SLOW
    assert rate_speed(20000) == SPEED.VERY_SLOW
    assert rate_speed(None) is None
    print('✓ Оценка скорости: fast/normal/slow/very_slow')


# ── «Ссылки реально открываются» (404) – check_content_links ──────────


import asyncio
from http_checker import check_content_links


class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Подделка aiohttp-сессии: отдаёт заранее заданные коды для HEAD/GET и
    запоминает, какие URL запрашивались (чтобы проверить фильтрацию)."""
    def __init__(self, head_codes=None, get_codes=None, default=200):
        self.head_codes = head_codes or {}
        self.get_codes = get_codes or {}
        self.default = default
        self.calls = []

    def head(self, url, **kw):
        self.calls.append(('head', url))
        return _FakeResp(self.head_codes.get(url, self.get_codes.get(url, self.default)))

    def get(self, url, **kw):
        self.calls.append(('get', url))
        return _FakeResp(self.get_codes.get(url, self.default))


def test_check_content_links_flags_only_internal_404():
    html = (
        '<header><a href="/in-header/">h</a></header>'      # шапка – не звоним
        '<main>'
        '<a href="/ok/">ok</a>'
        '<a href="/dead/">dead</a>'
        '<a href="https://other.com/ext/">внешний</a>'      # внешний – не звоним
        '<a href="#x">a</a><a href="mailto:a@b.ru">m</a>'
        '</main>'
    )
    base = 'https://shop.ru/about/'
    codes = {'https://shop.ru/ok/': 200, 'https://shop.ru/dead/': 404,
             'https://shop.ru/in-header/': 404}
    sess = _FakeSession(head_codes=codes, get_codes=codes)
    res = asyncio.run(check_content_links(sess, html, base))
    assert res is not None
    assert {b['url'] for b in res['broken']} == {'https://shop.ru/dead/'}
    assert res['checked'] == 2                              # /ok/ + /dead/
    asked = {u for _, u in sess.calls}
    assert 'https://other.com/ext/' not in asked            # внешний не звонили
    assert 'https://shop.ru/in-header/' not in asked        # из шапки не звонили


def test_check_content_links_head_to_get_fallback():
    """Сервер не поддерживает HEAD (405) – перепроверяем GET и ловим 404."""
    html = '<main><a href="/x/">x</a></main>'
    sess = _FakeSession(head_codes={'https://shop.ru/x/': 405},
                        get_codes={'https://shop.ru/x/': 404})
    res = asyncio.run(check_content_links(sess, html, 'https://shop.ru/'))
    assert {b['url'] for b in res['broken']} == {'https://shop.ru/x/'}
    assert ('get', 'https://shop.ru/x/') in sess.calls      # дошли до GET


def test_check_content_links_403_not_broken():
    """403 (доступ закрыт) – не «битая ссылка»: страница существует."""
    html = '<main><a href="/secret/">s</a><a href="/ok/">o</a></main>'
    sess = _FakeSession(head_codes={'https://shop.ru/secret/': 403,
                                    'https://shop.ru/ok/': 200})
    res = asyncio.run(check_content_links(sess, html, 'https://shop.ru/'))
    assert res['broken'] == [] and res['checked'] == 2


def test_check_content_links_none_when_no_links():
    sess = _FakeSession()
    res = asyncio.run(check_content_links(
        sess, '<main><a href="#x">a</a></main>', 'https://shop.ru/'))
    assert res is None and sess.calls == []


if __name__ == '__main__':
    test_classify_2xx()
    test_classify_3xx()
    test_classify_4xx()
    test_classify_5xx()
    test_classify_errors()
    test_should_retry()
    test_rate_speed()
    print('\n✅ Все тесты http_checker.py прошли')
