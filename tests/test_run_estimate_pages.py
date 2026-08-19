# -*- coding: utf-8 -*-
"""Оценка времени для форм/целей/КП/скорости: реагирует на настройки."""
from run_estimate import (estimate_forms_seconds, estimate_goals_seconds,
                          estimate_kp_seconds, estimate_pagespeed_seconds,
                          format_estimate)


def _мин(пара):
    return пара[0] / 60, пара[1] / 60


def test_формы_растут_от_числа_форм_и_городов():
    одна = estimate_forms_seconds(1, 1)
    семь = estimate_forms_seconds(7, 1)
    семь_три_города = estimate_forms_seconds(7, 3)
    assert одна[1] < семь[0], 'семь форм должны быть заведомо дольше одной'
    assert семь[1] < семь_три_города[0], 'три города - кратно дольше'
    # реальный ориентир: SHOPMET, 7 форм, 1 город - было 8 мин 18 с
    lo, hi = _мин(семь)
    assert lo <= 8.3 <= hi, f'8 мин должно попадать в диапазон {lo:.1f}-{hi:.1f}'


def test_формы_админка_добавляет_время():
    без = estimate_forms_seconds(3, 1)
    с_админкой = estimate_forms_seconds(3, 1, admin=True)
    assert с_админкой[0] > без[0] and с_админкой[1] > без[1]


def test_цели_учитывают_страницы_сайты_и_формы():
    мало = estimate_goals_seconds(9, 1)
    много_сайтов = estimate_goals_seconds(9, 3)
    с_формами = estimate_goals_seconds(9, 1, with_forms=True, forms_count=7)
    assert много_сайтов[0] > мало[0]
    assert с_формами[0] > мало[0], 'полный прогон форм дороже одного заказа'


def test_кп_карты_дороже_сайта():
    только_сайт = estimate_kp_seconds(21, check_site=True, maps=0)
    с_картами = estimate_kp_seconds(21, check_site=True, maps=2)
    assert с_картами[0] > только_сайт[1] * 3
    # реальный ориентир: 21 город без карт - было ~25 с
    assert только_сайт[0] <= 25 <= только_сайт[1]


def test_скорость_линейна_числу_адресов():
    десять = estimate_pagespeed_seconds(10)
    двадцать = estimate_pagespeed_seconds(20)
    assert двадцать[0] > десять[0] * 1.7


def test_формат_читаемый():
    assert 'мин' in format_estimate(*estimate_forms_seconds(7, 1))
    assert format_estimate(0, 0)          # пустой прогон не ломает вывод
