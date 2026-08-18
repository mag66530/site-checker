# -*- coding: utf-8 -*-
"""Короткий листинг без блока фильтров - не находка, а норма.

Поймано на МПК (Magento + Amasty Shop By): блок фильтров рисуется, только если
есть что фильтровать. У проекта 476 категорий, среди них много мелких - на
каждой такой прогон писал красное «фильтр не найден на странице», хотя сайт
ведёт себя штатно. Явный кейс из конфига (селектор задан руками) по-прежнему
обязан находиться - иначе это ошибка конфига, и её надо видеть.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filters_run as F


class _Локатор:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class _Страница:
    """Минимальная заглушка Playwright-страницы: открытие категории, счётчик
    карточек и отсутствие фильтра."""

    def __init__(self, карточек):
        self._карточек = карточек
        self.url = 'https://site.ru/cat'

    def goto(self, *a, **k):
        class _R:
            status = 200
        return _R()

    def wait_for_timeout(self, *a, **k):
        pass

    def locator(self, sel):
        # карточки товара находим, блок фильтров - нет
        return _Локатор(self._карточек if sel == '.item-wrapper' else 0)

    def eval_on_selector_all(self, *a, **k):
        return []

    def evaluate(self, *a, **k):
        return None

    def inner_text(self, *a, **k):
        return ''

    def content(self):
        return '<html></html>'


_КЕЙС = {'name': 'категория прогона', 'category': 'https://site.ru/cat',
         'card': '.item-wrapper', 'filter': 'a.amshopby-attr', 'apply': None}


def test_мелкая_категория_прогона_пропускается():
    case = dict(_КЕЙС, _auto=True)
    r = F.run_case(_Страница(4), case, log=lambda *a: None)
    assert r['verdict'] == 'skipped'
    assert 'фильтровать нечего' in r['detail']


def test_большая_категория_без_фильтра_остаётся_находкой():
    """На полноценном листинге пропавший фильтр - настоящая проблема."""
    case = dict(_КЕЙС, _auto=True)
    r = F.run_case(_Страница(F.MIN_CARDS_FOR_FILTER + 5), case, log=lambda *a: None)
    assert r['verdict'] == 'filter_absent'


def test_кейс_из_конфига_не_прощаем():
    """У ручного кейса селектор задан человеком: если он не находится - это
    ошибка конфига, её надо показать, сколько бы товаров ни было."""
    r = F.run_case(_Страница(2), dict(_КЕЙС), log=lambda *a: None)
    assert r['verdict'] == 'filter_absent'


def test_конфиг_мпк_на_месте():
    import json
    from pathlib import Path
    p = Path(__file__).parent.parent / 'catalogs' / 'filters-mpk.json'
    cases = json.loads(p.read_text(encoding='utf-8'))['cases']
    assert cases and all(c['card'] == '.item-wrapper' for c in cases)
    # Фильтр у Amasty - ссылка, а не чекбокс: кнопки «Показать» нет.
    assert all(c['filter'] == 'a.amshopby-attr' and c['apply'] is None
               for c in cases)
