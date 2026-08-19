# -*- coding: utf-8 -*-
"""Токен Вебмастера: какое поле выигрывает.

Живой случай (ИМП): в кабинете заполнены ОБА поля - «OAuth-токен Вебмастера»
и «OAuth-токен Яндекса». Человек перевыпустил токен с недостающим правом
EXTERNAL_LINKS и положил в поле с точным названием, а прогон продолжал брать
старый из второго поля - в логе снова «нет права». Приоритет должен быть у
поля с точным названием."""
import ast
import re
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent


def _порядок(текст: str, окно: str):
    """Что раньше упоминается в выражении - webmaster_oauth или yandex_oauth."""
    i_wm = окно.find('webmaster_oauth')
    i_ya = окно.find('yandex_oauth')
    assert i_wm >= 0 and i_ya >= 0, окно
    return 'webmaster' if i_wm < i_ya else 'yandex'


def test_чек_лист_берёт_поле_вебмастера_первым():
    текст = (КОРЕНЬ / 'checklists' / 'checklist_30min.py').read_text(encoding='utf-8')
    m = re.search(r'_wm_token\s*=\s*\((.*?)\)\n\s*creds', текст, re.S)
    assert m, 'не нашли выбор токена'
    assert _порядок(текст, m.group(1)) == 'webmaster'


def test_расписание_берёт_поле_вебмастера_первым():
    текст = (КОРЕНЬ / 'run_scheduled.py').read_text(encoding='utf-8')
    m = re.search(r"'webmaster_oauth':\s*\((.{0,240}?)\),\n", текст, re.S)
    assert m, 'не нашли выбор токена'
    assert _порядок(текст, m.group(1)) == 'webmaster'


def test_проверка_индексации_берёт_поле_вебмастера_первым():
    текст = (КОРЕНЬ / 'index_pages_checker.py').read_text(encoding='utf-8')
    m = re.search(r'def _resolve_token.*?return \((.*?)\)\n\n', текст, re.S)
    assert m, 'не нашли выбор токена'
    assert _порядок(текст, m.group(1)) == 'webmaster'


def test_поля_подписаны_понятно():
    """Из названий должно быть ясно, какое поле главное."""
    текст = (КОРЕНЬ / 'auth' / 'ui.py').read_text(encoding='utf-8')
    поля = dict(re.findall(r'\("(webmaster_oauth|yandex_oauth)",\s*"([^"]+)"', текст))
    assert 'основной' in поля['webmaster_oauth']
    assert 'запасной' in поля['yandex_oauth']


def test_приоритет_одинаков_во_всех_точках():
    """Разный порядок в разных местах - источник трудноуловимых расхождений:
    интерфейс берёт один токен, фоновое расписание - другой."""
    точки = {
        'checklists/checklist_30min.py': r'_wm_token\s*=\s*\((.*?)\)\n\s*creds',
        'run_scheduled.py': r"'webmaster_oauth':\s*\((.{0,240}?)\),\n",
        'index_pages_checker.py': r'def _resolve_token.*?return \((.*?)\)\n\n',
    }
    порядки = set()
    for файл, rx in точки.items():
        текст = (КОРЕНЬ / файл).read_text(encoding='utf-8')
        m = re.search(rx, текст, re.S)
        assert m, файл
        порядки.add(_порядок(текст, m.group(1)))
    assert порядки == {'webmaster'}, f'разный приоритет: {порядки}'
