# -*- coding: utf-8 -*-
"""Каталоги проекта не должны содержать домены чужого проекта.

Живой случай: в карте присутствия МПЭ для Баку стоял metpromintex.az - домен
проекта МПИ. Прогон чек-листа по МПЭ уходил на чужой сайт, и его страницы
попадали в отчёт МПЭ."""
import csv
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПРОЕКТЫ = sorted(p.stem for p in (КОРЕНЬ / 'projects').glob('*.json'))


def _проект(pid):
    return json.loads((КОРЕНЬ / 'projects' / f'{pid}.json').read_text(encoding='utf-8'))


def _строки(путь):
    if not путь.exists():
        return []
    with open(путь, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _хост(url):
    if not url:
        return ''
    if '://' not in url:
        url = 'https://' + url
    return (urlparse(url).hostname or '').lower().removeprefix('www.')


def _свои_домены(pid):
    """Всё, что проект законно считает своим: корень + домены из его КП."""
    свои = {_хост(_проект(pid).get('root_domain', ''))}
    for р in _строки(КОРЕНЬ / 'catalogs' / f'{pid}-kp.csv'):
        свои.add(_хост(р.get('domain', '')))
    return {д for д in свои if д}


@pytest.mark.parametrize('pid', ПРОЕКТЫ)
def test_поддомены_принадлежат_своему_проекту(pid):
    файл = КОРЕНЬ / 'catalogs' / f'{pid}-subdomains.csv'
    if not файл.exists():
        pytest.skip(f'у проекта {pid} нет карты присутствия')

    чужие_домены = {}
    for другой in ПРОЕКТЫ:
        if другой != pid:
            for д in _свои_домены(другой):
                чужие_домены.setdefault(д, другой)

    свои = _свои_домены(pid)
    найдено = []
    for р in _строки(файл):
        host = _хост(р.get('url', ''))
        владелец = чужие_домены.get(host)
        # корневой домен проекта в конце хоста - поддомен свой (msk.mepen.ru)
        родной = any(host == д or host.endswith('.' + д) for д in свои)
        if владелец and not родной:
            найдено.append(f"{host} (город {р.get('city', '?')}) - домен проекта {владелец}")

    assert not найдено, (
        f'в catalogs/{pid}-subdomains.csv домены чужих проектов:\n  ' + '\n  '.join(найдено))
