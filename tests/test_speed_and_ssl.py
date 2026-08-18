# -*- coding: utf-8 -*-
"""Пороги скорости ответа документа и проверка SSL-сертификата."""
import datetime as dt
import types

import ssl_checker as sslc
from http_checker import rate_speed, SPEED
from report_priorities import classify, ssl_findings, _speed_findings


# ── Скорость ─────────────────────────────────────────────────────────


def test_пороги_скорости():
    """Чек-лист: до 1с - норма, до 2.5с - предупреждение, дальше - чинить."""
    assert rate_speed(0) == SPEED.FAST
    assert rate_speed(999) == SPEED.FAST
    assert rate_speed(1000) == SPEED.NORMAL
    assert rate_speed(2499) == SPEED.NORMAL
    assert rate_speed(2500) == SPEED.SLOW
    assert rate_speed(7999) == SPEED.SLOW
    assert rate_speed(8000) == SPEED.VERY_SLOW
    assert rate_speed(None) is None


def _res(ms, rating):
    return types.SimpleNamespace(
        speed_rating=rating, elapsed_ms=ms, city='Москва',
        type_label='Категория', url='https://a.ru/catalog/truba/')


def test_быстрая_страница_молчит():
    assert _speed_findings(_res(800, 'fast')) == []
    assert _speed_findings(_res(None, None)) == []


def test_до_двух_с_половиной_это_предупреждение_а_не_ошибка():
    f = _speed_findings(_res(1800, 'normal'))[0]
    assert f.level == 'Предупреждение'
    assert 'дольше 1 секунды' in f.problem
    assert '1.8 с' in f.detail
    assert classify(f)['task_group'] == 'page_speed_warn'


def test_дольше_двух_с_половиной_это_ошибка():
    f = _speed_findings(_res(3200, 'slow'))[0]
    assert f.level == 'Ошибка'
    assert 'дольше 2.5 секунд' in f.problem
    assert classify(f)['task_group'] == 'page_speed_slow'


def test_очень_долгий_ответ_отдельным_текстом():
    f = _speed_findings(_res(9000, 'very_slow'))[0]
    assert f.level == 'Ошибка' and 'очень долго' in f.problem
    # приоритет выше, чем у просто медленной
    assert classify(f)['priority'] == 1


# ── SSL: имена сертификата ───────────────────────────────────────────


def test_имена_из_сертификата():
    cert = {'subject': ((('commonName', 'a.ru'),),),
            'subjectAltName': (('DNS', 'a.ru'), ('DNS', '*.a.ru'),
                               ('IP Address', '1.2.3.4'))}
    assert sslc.cert_hosts(cert) == ['a.ru', '*.a.ru']


def test_маска_покрывает_один_уровень():
    """*.a.ru подходит к spb.a.ru, но не к x.spb.a.ru - как в браузере."""
    имена = ['*.a.ru']
    assert sslc.host_matches('spb.a.ru', имена) is True
    assert sslc.host_matches('x.spb.a.ru', имена) is False
    assert sslc.host_matches('a.ru', имена) is False       # сам домен - нет
    assert sslc.host_matches('spb.a.ru', ['a.ru', '*.a.ru']) is True
    assert sslc.host_matches('', имена) is False


# ── SSL: разбор сертификата ──────────────────────────────────────────


СЕЙЧАС = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)


def _cert(not_after, not_before='Jan  1 00:00:00 2026 GMT', host='a.ru'):
    return {'subject': ((('commonName', host),),),
            'issuer': ((('organizationName', "Let's Encrypt"),),),
            'subjectAltName': (('DNS', host),),
            'notBefore': not_before, 'notAfter': not_after}


def test_нормальный_сертификат_молчит():
    r = sslc.analyze_cert(_cert('Dec  1 00:00:00 2026 GMT'), 'a.ru',
                          chain_len=3, now=СЕЙЧАС)
    assert r['issues'] == [] and r['warnings'] == []
    assert r['days_left'] > 30 and r['issuer'] == "Let's Encrypt"


def test_истёкший_сертификат_ошибка():
    r = sslc.analyze_cert(_cert('Aug  1 00:00:00 2026 GMT'), 'a.ru',
                          chain_len=3, now=СЕЙЧАС)
    assert any('истёк' in i for i in r['issues'])
    assert r['days_left'] < 0


def test_истекает_скоро_ошибка_а_не_предупреждение():
    """Меньше двух недель - продлевать надо сейчас, а не «когда-нибудь»."""
    r = sslc.analyze_cert(_cert('Aug 25 00:00:00 2026 GMT'), 'a.ru',
                          chain_len=3, now=СЕЙЧАС)
    assert any('истекает через 7 дн' in i for i in r['issues'])
    assert r['warnings'] == []


def test_истекает_через_месяц_предупреждение():
    r = sslc.analyze_cert(_cert('Sep 10 00:00:00 2026 GMT'), 'a.ru',
                          chain_len=3, now=СЕЙЧАС)
    assert r['issues'] == []
    assert any('истекает через 23 дн' in w for w in r['warnings'])


def test_чужой_домен_ошибка():
    r = sslc.analyze_cert(_cert('Dec  1 00:00:00 2026 GMT', host='other.ru'),
                          'a.ru', chain_len=3, now=СЕЙЧАС)
    assert any('выписан на другой домен' in i for i in r['issues'])


def test_неполная_цепочка_предупреждение():
    r = sslc.analyze_cert(_cert('Dec  1 00:00:00 2026 GMT'), 'a.ru',
                          chain_len=1, now=СЕЙЧАС)
    assert r['issues'] == []
    assert any('неполную цепочку' in w for w in r['warnings'])


def test_сертификат_из_будущего():
    r = sslc.analyze_cert(
        _cert('Dec  1 00:00:00 2027 GMT', not_before='Oct  1 00:00:00 2026 GMT'),
        'a.ru', chain_len=3, now=СЕЙЧАС)
    assert any('не вступил в силу' in i for i in r['issues'])


def test_сертификата_нет():
    r = sslc.analyze_cert({}, 'a.ru', now=СЕЙЧАС)
    assert any('не удалось прочитать' in i for i in r['issues'])


# ── SSL: вывод в отчёт ───────────────────────────────────────────────


def test_находки_ssl_идут_в_безопасность():
    check = {'hosts': [{
        'host': 'a.ru', 'available': True, 'error': None,
        'expires': '2026-08-25T00:00:00+00:00', 'days_left': 7,
        'issuer': "Let's Encrypt",
        'issues': ['SSL-сертификат истекает через 7 дн. - продлить сейчас'],
        'warnings': ['сервер отдаёт неполную цепочку SSL - часть мобильных '
                     'браузеров и роботов не подтвердит сертификат']}]}
    находки = ssl_findings(check)
    assert len(находки) == 2
    assert all(f.section == 'Безопасность' for f in находки)
    assert находки[0].level == 'Ошибка' and находки[1].level == 'Предупреждение'
    assert 'a.ru' in находки[0].url and '2026-08-25' in находки[0].detail
    группы = {classify(f)['task_group'] for f in находки}
    assert группы == {'ssl_cert', 'ssl_chain'}
    # общая задача про заголовки не должна перехватывать SSL
    assert all(classify(f)['task_group'] != 'security_headers' for f in находки)


def test_не_дозвонились_не_находка():
    """Таймаут/DNS - ограничение проверки, а не дефект сайта."""
    assert ssl_findings({'hosts': [
        {'host': 'a.ru', 'available': False, 'error': 'TimeoutError: '}]}) == []
    assert ssl_findings(None) == []
    assert ssl_findings({}) == []
