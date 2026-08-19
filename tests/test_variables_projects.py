# -*- coding: utf-8 -*-
"""Проверка КП должна знать все проекты из projects/*.json.

Живой случай: SHOPMET был во всех каталогах и в интерфейсе, но запуск падал -
`variables_run.py: error: argument --project: invalid choice: 'sm'`, потому что
список проектов в скрипте был прибит гвоздями и его забыли дополнить."""
import json
from pathlib import Path

import variables_run

КОРЕНЬ = Path(__file__).resolve().parent.parent


def test_проекты_берутся_из_каталога_projects():
    ожидаем = set()
    for f in (КОРЕНЬ / 'projects').glob('*.json'):
        ожидаем.add(json.loads(f.read_text(encoding='utf-8'))['id'])
    assert set(variables_run.PROJECT_NAMES) == ожидаем


def test_у_каждого_проекта_есть_название():
    for pid, имя in variables_run.PROJECT_NAMES.items():
        assert имя and имя != pid, f'у проекта {pid} нет названия в projects/{pid}.json'


def test_shopmet_доступен_для_запуска():
    assert variables_run.PROJECT_NAMES.get('sm') == 'SM - SHOPMET'
