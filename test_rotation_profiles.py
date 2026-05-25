"""Тесты http_checker — классификация статусов и оценка скорости."""
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
    """Ретраим только то, что может стать ОК. 4xx — устойчивый."""
    assert should_retry(STATUS.TIMEOUT) is True
    assert should_retry(STATUS.NETWORK) is True
    assert should_retry(STATUS.SERVER_ERROR) is True
    assert should_retry(STATUS.OK) is False
    assert should_retry(STATUS.REDIRECT) is False
    assert should_retry(STATUS.NOT_FOUND) is False
    assert should_retry(STATUS.CLIENT_ERROR) is False
    print('✓ Логика ретраев: 5xx/timeout/network — да, 4xx — нет')


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


if __name__ == '__main__':
    test_classify_2xx()
    test_classify_3xx()
    test_classify_4xx()
    test_classify_5xx()
    test_classify_errors()
    test_should_retry()
    test_rate_speed()
    print('\n✅ Все тесты http_checker.py прошли')
