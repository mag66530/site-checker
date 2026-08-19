# -*- coding: utf-8 -*-
"""Фильтр-тест: где искать фильтр и что кейсы прогона наследуют от конфига.

Живой случай МТТ. В логе чек-листа все 20 страниц уходили в «не листинг товаров
(нет карточек) - пропущено», хотя на сайте и карточки, и рабочие фильтры. Две
причины подряд:

  1. Прогон подставлял в фильтр-тест КАТЕГОРИИ. У МТТ раздел - это список
     подразделов: ни таблицы типоразмеров, ни фильтра там нет. Фильтр живёт на
     странице типоразмеров, которую прогон считает товаром.
  2. Когда страницы поправили, вылезло «селектор фильтра не найден»: расширение
     кейсов копировало ключи ПОИМЁННО, и ключ «открыть» (у МТТ параметры за
     выпадающим списком) в авто-кейсы не попадал - попап не открывался.
"""
import json
import os
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import filters_run as F


def test_где_искать_фильтр_читается_из_конфига():
    assert F.страницы_проверки('mtt') == 'товары'
    # У остальных поведение прежнее - раздел/категория.
    assert F.страницы_проверки('mpk') == 'категории'
    assert F.страницы_проверки('нет-такого-проекта') == 'категории'


def test_кейсы_прогона_наследуют_все_настройки():
    """Именно из-за потери «открыть» проверка МТТ не находила фильтр."""
    шаблон = {'name': 'из конфига', 'category': 'https://site.ru/cat',
              'card': '.row', 'filter': 'label.item', 'apply': '.go',
              'открыть': '.param', 'open_wait_ms': 1200, 'wait_ms': 4000}
    кейсы = F._expand_cases_for_categories([шаблон], ['https://site.ru/a',
                                                      'https://site.ru/b'])
    assert len(кейсы) == 2
    for к in кейсы:
        assert к['открыть'] == '.param'
        assert к['open_wait_ms'] == 1200
        assert к['card'] == '.row' and к['filter'] == 'label.item'
        assert к['_auto'] is True
    assert [к['category'] for к in кейсы] == ['https://site.ru/a',
                                              'https://site.ru/b']


def test_конфиг_мтт_описывает_страницы_типоразмеров():
    данные = json.loads((КОРЕНЬ / 'catalogs' / 'filters-mtt.json')
                        .read_text(encoding='utf-8'))
    assert данные['проверять_на'] == 'товары'
    for c in данные['cases']:
        assert c['открыть'], 'без «открыть» значения фильтра не появятся'
        assert c['card'] == 'table.product-list__table tbody tr'


def test_прогон_берёт_страницы_по_ключу():
    """runner_30min должен спрашивать filters_run, откуда брать страницы."""
    src = (КОРЕНЬ / 'runner_30min.py').read_text(encoding='utf-8')
    assert 'страницы_проверки(pid)' in src
    assert "'product' if _где == 'товары' else 'category'" in src
