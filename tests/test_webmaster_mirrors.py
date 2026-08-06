# -*- coding: utf-8 -*-
"""Один домен - одна запись: в Вебмастере сайт добавлен и как http, и как https."""
from webmaster_api import _без_зеркал


def test_https_вытесняет_http():
    хосты = [
        ('smolensk.mepen.ru', 'http:smolensk.mepen.ru:80', 'http://smolensk.mepen.ru'),
        ('smolensk.mepen.ru', 'https:smolensk.mepen.ru:443', 'https://smolensk.mepen.ru'),
        ('spb.mepen.ru', 'https:spb.mepen.ru:443', 'https://spb.mepen.ru'),
    ]
    res = _без_зеркал(хосты)
    assert len(res) == 2
    по_домену = {д: hid for д, hid, _ in res}
    assert по_домену['smolensk.mepen.ru'] == 'https:smolensk.mepen.ru:443'
    assert по_домену['spb.mepen.ru'] == 'https:spb.mepen.ru:443'


def test_порядок_записей_не_важен():
    прямой = [('a.ru', 'https:a.ru:443', ''), ('a.ru', 'http:a.ru:80', '')]
    обратный = list(reversed(прямой))
    assert _без_зеркал(прямой)[0][1] == 'https:a.ru:443'
    assert _без_зеркал(обратный)[0][1] == 'https:a.ru:443'


def test_только_http_остаётся_как_есть():
    res = _без_зеркал([('b.ru', 'http:b.ru:80', '')])
    assert res == [('b.ru', 'http:b.ru:80', '')]


def test_пустой_домен_отбрасывается():
    assert _без_зеркал([('', 'https:x:443', ''), ('c.ru', 'https:c.ru:443', '')]) \
        == [('c.ru', 'https:c.ru:443', '')]
