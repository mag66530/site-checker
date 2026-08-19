# -*- coding: utf-8 -*-
"""Сессия браузера должна открываться в Playwright.

Живой случай (ИМП): сессию переложили в поле «Сессия браузера», и прогон упал
сразу - «Protocol error (Storage.setCookies): Invalid cookie fields». Chrome
свежих версий кладёт в экспорт поля partitionKey и _crHasCrossSiteAncestor,
которых Playwright не знает: из-за одного лишнего поля негодной считается вся
сессия."""
import base64
import json

from autoclick_browser import sanitize_state, session_file_from_secret


def _cookie(**kw):
    основа = {'name': 'Session_id', 'value': 'x', 'domain': '.yandex.ru',
              'path': '/', 'expires': 1800000000.0, 'httpOnly': True,
              'secure': True, 'sameSite': 'Lax'}
    основа.update(kw)
    return основа


def test_поля_chrome_отбрасываются():
    состояние = {'cookies': [_cookie(partitionKey={'topLevelSite': 'https://a'},
                                     _crHasCrossSiteAncestor=False)]}
    c = sanitize_state(состояние)['cookies'][0]
    assert 'partitionKey' not in c and '_crHasCrossSiteAncestor' not in c
    assert c['name'] == 'Session_id' and c['value'] == 'x'


def test_нужные_поля_сохраняются():
    c = sanitize_state({'cookies': [_cookie()]})['cookies'][0]
    assert set(c) == {'name', 'value', 'domain', 'path', 'expires',
                      'httpOnly', 'secure', 'sameSite'}


def test_неизвестный_samesite_приводится_к_допустимому():
    """Chrome пишет 'unspecified'/None - Playwright знает только три значения."""
    for плохое in ('unspecified', None, '', 'no_restriction'):
        c = sanitize_state({'cookies': [_cookie(sameSite=плохое)]})['cookies'][0]
        assert c['sameSite'] in ('Strict', 'Lax', 'None')


def test_битый_expires_становится_сессионным():
    c = sanitize_state({'cookies': [_cookie(expires='никогда')]})['cookies'][0]
    assert c['expires'] == -1


def test_cookie_без_имени_выбрасывается():
    состояние = {'cookies': [_cookie(name=''), _cookie()]}
    assert len(sanitize_state(состояние)['cookies']) == 1


def test_origins_не_теряются():
    o = [{'origin': 'https://a', 'localStorage': []}]
    assert sanitize_state({'cookies': [], 'origins': o})['origins'] == o


def test_пустое_состояние_не_падает():
    assert sanitize_state({}) == {'cookies': [], 'origins': []}
    assert sanitize_state(None) == {'cookies': [], 'origins': []}


def test_файл_сессии_пишется_очищенным(tmp_path):
    """session_file_from_secret отдаёт путь к уже пригодному storage_state."""
    состояние = {'cookies': [_cookie(partitionKey={'x': 1})], 'origins': []}
    b64 = base64.b64encode(json.dumps(состояние).encode()).decode()
    путь = session_file_from_secret(b64)
    d = json.load(open(путь, encoding='utf-8'))
    assert 'partitionKey' not in d['cookies'][0]
