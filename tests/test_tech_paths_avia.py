"""Тех. страницы АПС: список заведён и в той форме, какую отдаёт сайт.

Тех. страницы проверяются на КАЖДОМ прогоне целиком, мимо выборки
(runner_30min: get_tech_paths). Пока списка не было, весь блок служебных
страниц у АПС просто отсутствовал в отчёте.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import sources as S


def test_список_заведён():
    пути = S.get_tech_paths('avia')

    assert set(пути) == {'/about', '/delivery', '/contacts', '/sprav'}
    print('✓ четыре служебные страницы АПС на месте')


def test_адреса_без_завершающего_слеша():
    """Со слешем сайт отвечает 301 - со слешем в отчёт полез бы редирект."""
    кривые = [p for p in S.get_tech_paths('avia') if p.endswith('/')]

    assert not кривые, кривые
    print('✓ пути слеша на конце не имеют - как отдаёт сам сайт')


def test_подписи_человеческие():
    подписи = {p: S.tech_page_label(p) for p in S.get_tech_paths('avia')}

    assert подписи['/about'] == 'О компании'
    assert подписи['/contacts'] == 'Контакты'
    assert подписи['/delivery'] == 'Доставка'
    assert подписи['/sprav'] == 'Справочники'
    # Ни одна подпись не осталась голым путём.
    assert not [p for p, л in подписи.items() if л == p], подписи
    print('✓ в отчёте будут названия, а не адреса')


def test_чужие_проекты_не_затронуты():
    assert len(S.get_tech_paths('imp')) == 24
    assert len(S.get_tech_paths('smu')) == 10
    assert len(S.get_tech_paths('sm')) == 6
    assert S.get_tech_paths('mpk') == []      # у МПК списка по-прежнему нет
    print('✓ правка адресная: у остальных проектов всё как было')
