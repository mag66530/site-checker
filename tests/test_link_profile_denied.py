# -*- coding: utf-8 -*-
"""Отказ 403 на внешних ссылках: причина и скорость.

Живой случай (ИМП): прогон 6 минут обходил все 242 хоста, получая 403, и
писал в отчёт «дело НЕ в токене, вероятно доступ делегированный». Это
оказалось неверно - права владельца были подтверждены на всех хостах, а
отказывало OAuth-приложение, которому не выдали право EXTERNAL_LINKS."""
import link_profile as LP


def _хосты(n):
    return [{'ascii_host_url': f'https://s{i}.example.ru/',
             'host_id': f'https:s{i}.example.ru:443'} for i in range(n)]


def _прогон(monkeypatch, ошибка_403: str, хостов=242):
    """Ставим API, который на любые ссылки отвечает 403. Считаем обращения."""
    счёт = {'n': 0}

    def _get(token, path, proxy=None, params=None):
        if path.endswith('/user/'):
            return {'user_id': 1}
        if '/hosts' in path and 'links' not in path:
            return {'hosts': _хосты(хостов)}
        счёт['n'] += 1
        raise PermissionError(ошибка_403)

    # link_profile импортирует _get внутри функции - подменяем в источнике
    import webmaster_api
    monkeypatch.setattr(webmaster_api, '_get', _get)
    res = LP.fetch_link_profile('imp', 'токен', log=lambda m: None)
    return res, счёт['n']


_ОТКАЗ = ('Нет доступа (403): OAuth-приложению не выдано право EXTERNAL_LINKS '
          '(есть только: ALL_SCOPES, HOST_LIST, COMMON). Права на сайт тут ни '
          'при чём - добавьте это право приложению на oauth.yandex.ru.')


def test_обход_прекращается_после_нескольких_отказов(monkeypatch):
    """Права выдаются приложению целиком - обходить 242 хоста бессмысленно."""
    res, обращений = _прогон(monkeypatch, _ОТКАЗ)
    assert обращений <= LP._DENIED_STOP * 2, (
        f'сделано {обращений} запросов - обход не остановился')


def test_в_отчёт_попадает_ответ_яндекса_а_не_догадка(monkeypatch):
    res, _ = _прогон(monkeypatch, _ОТКАЗ)
    note = res.get('note') or ''
    assert 'EXTERNAL_LINKS' in note, note
    assert 'oauth.yandex.ru' in note
    # старая формулировка уводила в сторону - её быть не должно
    assert 'делегированный' not in note
    assert 'дело НЕ в токене' not in note


def test_несистемные_ошибки_обход_не_останавливают(monkeypatch):
    """Сетевой сбой на одном хосте - не повод бросать остальные."""
    res, обращений = _прогон(monkeypatch, 'Таймаут соединения', хостов=10)
    assert обращений >= 10, 'обход прервался на обычной ошибке'
    assert not (res.get('note') or '')
