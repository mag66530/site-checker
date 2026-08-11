# -*- coding: utf-8 -*-
"""Сессия браузера должна содержать вход в ОБА сервиса.

Живой случай (SHOPMET): экспорт выгрузил только cookies Google - в Яндексе
вход не был выполнен. Экспорт об этом не сказал, поле в кабинете заполнено,
а автокликер Вебмастера потом обходил все сайты, разбирая страницу входа, и
отчитывался «блоков 0»."""
import ast
import base64
import json
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent


def _session_info():
    """Берём функцию со страницы автокликеров: файл рисует UI при импорте,
    поэтому вытаскиваем только нужное определение."""
    src = (КОРЕНЬ / 'checklists' / 'autoclickers.py').read_text(encoding='utf-8')
    узел = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == '_session_info')
    ns: dict = {}
    exec(compile(ast.Module([узел], []), 'ac', 'exec'), ns)
    return ns['_session_info']


def _b64(*names):
    cookies = [{'name': n, 'value': 'x', 'domain': '.example'} for n in names]
    return base64.b64encode(
        json.dumps({'cookies': cookies}).encode()).decode()


def test_полная_сессия():
    инфо = _session_info()(_b64('Session_id', 'yandex_login', 'SID'))
    assert инфо['yandex'] and инфо['google']
    assert инфо['всего'] == 3


def test_только_google_видно_сразу():
    """Ровно случай SHOPMET: Google есть, Яндекса нет."""
    инфо = _session_info()(_b64('SID', 'SSID', '__Secure-1PSID'))
    assert инфо['google'] is True
    assert инфо['yandex'] is False


def test_только_яндекс():
    инфо = _session_info()(_b64('Session_id', 'sessionid2'))
    assert инфо['yandex'] is True and инфо['google'] is False


def test_технические_cookies_не_считаются_входом():
    """yandexuid ставится и незалогиненному - вход им подтверждать нельзя."""
    инфо = _session_info()(_b64('yandexuid', 'NID', '_ga'))
    assert инфо['yandex'] is False and инфо['google'] is False


def test_логин_яндекса_виден():
    src = json.dumps({'cookies': [
        {'name': 'Session_id', 'value': 'x'},
        {'name': 'yandex_login', 'value': 'metpromintex'},
        {'name': 'SID', 'value': 'y'}]}).encode()
    инфо = _session_info()(base64.b64encode(src).decode())
    assert инфо['login'] == 'metpromintex'


def test_битая_строка_не_роняет_страницу():
    assert _session_info()('не base64 вовсе') == {}


def test_экспорт_проверяет_вход_в_оба_сервиса():
    """Сам скрипт экспорта тоже обязан предупреждать, а не молчать."""
    src = (КОРЕНЬ / 'session_export.py').read_text(encoding='utf-8')
    assert 'Session_id' in src and '__Secure-1PSID' in src
    assert 'ВХОД ВЫПОЛНЕН НЕ ВЕЗДЕ' in src
