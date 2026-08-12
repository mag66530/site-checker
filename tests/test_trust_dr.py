"""trust_check.fetch_dr - разбор ответа Open PageRank.

Open PageRank переехал под Keywords Everywhere: адрес, метод и заголовок
авторизации поменялись целиком. Ключ старого образца при этом у части
аккаунтов ещё живёт, поэтому оба пути должны работать. Сеть не трогаем -
подменяем requests.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trust_check as tc


class _Ответ:
    def __init__(self, код=200, данные=None, текст='', headers=None):
        self.status_code = код
        self._данные = данные if данные is not None else {}
        self.text = текст
        self.headers = headers or {}

    def json(self):
        return self._данные


class _Requests:
    """Заглушка requests: помнит вызовы, отдаёт заготовленные ответы."""

    def __init__(self, ответ):
        self.ответ = ответ
        self.вызовы = []

    def post(self, url, **kw):
        self.вызовы.append(('post', url, kw))
        return self.ответ

    def get(self, url, **kw):
        self.вызовы.append(('get', url, kw))
        return self.ответ


def _подменить(monkeypatch, ответ):
    заглушка = _Requests(ответ)
    monkeypatch.setattr(tc, 'requests', заглушка)
    return заглушка


def test_новый_ключ_идёт_в_новый_api(monkeypatch):
    ответ = _Ответ(данные={'results': [
        {'domain': 'inmetprom.ru', 'found': True, 'open_page_rank': 3.42},
        {'domain': 'spb.inmetprom.ru', 'found': False},
    ]}, headers={'X-Domains-Remaining': '758'})
    з = _подменить(monkeypatch, ответ)

    out = tc.fetch_dr(['inmetprom.ru', 'spb.inmetprom.ru'],
                      'opr_live_af0f6f1d16c0333e22c1f3d683a51685')

    assert out == {'inmetprom.ru': 3.42, 'spb.inmetprom.ru': None}
    метод, url, kw = з.вызовы[0]
    assert метод == 'post' and url == tc.OPR_URL_KE
    assert kw['headers']['Authorization'].startswith('Bearer opr_live_')
    assert kw['json']['domains'] == ['inmetprom.ru', 'spb.inmetprom.ru']
    print('✓ ключ opr_live_… уходит bearer-токеном на новый адрес')


def test_поддомены_читаются_из_hosts(monkeypatch):
    """Запрошенный поддомен сворачивается к регистрируемому домену, а его
    собственный ранг лежит в hosts[] внутри результата этого домена. Пока
    читали только results[], DR был лишь у корневого домена."""
    ответ = _Ответ(данные={'results': [
        {'domain': 'inmetprom.ru', 'found': True, 'open_page_rank': 1.17,
         'hosts': [
             {'host': 'spb.inmetprom.ru', 'found': True, 'open_page_rank': 0.82},
             {'host': 'kazan.inmetprom.ru', 'found': False},
         ]},
    ]})
    _подменить(monkeypatch, ответ)

    out = tc.fetch_dr(['inmetprom.ru', 'spb.inmetprom.ru',
                       'kazan.inmetprom.ru'], 'opr_live_x')

    assert out == {'inmetprom.ru': 1.17,
                   'spb.inmetprom.ru': 0.82,
                   'kazan.inmetprom.ru': None}
    print('✓ ранги поддоменов берутся из hosts[]')


def test_ключ_не_принят_даёт_понятный_лог(monkeypatch):
    _подменить(monkeypatch, _Ответ(код=401, текст='unauthorized'))
    строки = []

    out = tc.fetch_dr(['inmetprom.ru'], 'opr_live_плохой', log=строки.append)

    assert out == {}
    assert any('401' in s for s in строки), строки
    print('✓ HTTP 401 объясняется, а не молчит')


def test_исчерпанный_лимит_не_роняет_прогон(monkeypatch):
    _подменить(monkeypatch, _Ответ(код=429, текст='quota'))
    строки = []

    out = tc.fetch_dr(['inmetprom.ru'], 'opr_live_x', log=строки.append)

    assert out == {}
    assert any('429' in s for s in строки), строки
    print('✓ лимит - предупреждение в лог, DR остаётся прочерком')


def test_старый_ключ_идёт_на_старый_адрес(monkeypatch):
    ответ = _Ответ(данные={'response': [
        {'domain': 'inmetprom.ru', 'status_code': 200, 'page_rank_decimal': 2.5},
    ]})
    з = _подменить(monkeypatch, ответ)

    out = tc.fetch_dr(['inmetprom.ru'], 'a' * 32)

    assert out == {'inmetprom.ru': 2.5}
    метод, url, kw = з.вызовы[0]
    assert метод == 'get' and url == tc.OPR_URL_LEGACY
    assert kw['headers']['API-OPR'] == 'a' * 32
    print('✓ ключ старого образца идёт прежним путём')


def test_без_ключа_сети_нет(monkeypatch):
    з = _подменить(monkeypatch, _Ответ())

    assert tc.fetch_dr(['inmetprom.ru'], '') == {}
    assert tc.fetch_dr(['inmetprom.ru'], None) == {}
    assert з.вызовы == []
    print('✓ без ключа в сеть не ходим')
