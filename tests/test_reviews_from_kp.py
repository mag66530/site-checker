# -*- coding: utf-8 -*-
"""Отзывы берутся из карты присутствия проекта.

Раньше проверке нужен был отдельный catalogs/reviews-<pid>.csv, которого ни у
одного проекта, кроме СМУ, не было - а ссылки на карточки Яндекса и 2ГИС
клиент и так ведёт в КП (колонки yandex_map_url / twogis_map_url)."""
import csv

import pytest

import review_priority as R


@pytest.fixture
def кп(tmp_path, monkeypatch):
    """Подменяем каталоги проекта на временные."""
    (tmp_path / 'catalogs').mkdir()
    monkeypatch.setattr(R, 'BASE', tmp_path)
    return tmp_path / 'catalogs'


def _написать(путь, строки, поля):
    with open(путь, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=поля)
        w.writeheader()
        w.writerows(строки)


def test_филиалы_читаются_из_кп(кп):
    _написать(кп / 'x-kp.csv', [
        {'city': 'Москва', 'country': 'Россия',
         'yandex_map_url': 'https://yandex.ru/maps/org/x/1/',
         'twogis_map_url': 'https://2gis.ru/moscow/firm/70000001'},
        {'city': 'Тверь', 'country': 'Россия',
         'yandex_map_url': 'https://yandex.ru/maps/org/x/2/',
         'twogis_map_url': ''},
    ], ['city', 'country', 'yandex_map_url', 'twogis_map_url'])

    филиалы = R.load_branches('x')
    assert [b['city'] for b in филиалы] == ['Москва', 'Тверь']
    assert филиалы[0]['2gis_url'].endswith('70000001')
    assert филиалы[1]['2gis_url'] == ''          # одной ссылки достаточно


def test_города_без_ссылок_пропускаются(кп):
    """Проверять нечего - в отчёт такой город не тащим."""
    _написать(кп / 'x-kp.csv', [
        {'city': 'Москва', 'yandex_map_url': 'https://yandex.ru/maps/org/x/1/',
         'twogis_map_url': ''},
        {'city': 'Клин', 'yandex_map_url': '', 'twogis_map_url': ''},
    ], ['city', 'yandex_map_url', 'twogis_map_url'])
    assert [b['city'] for b in R.load_branches('x')] == ['Москва']


def test_свой_список_важнее_кп(кп):
    """reviews-<pid>.csv переопределяет КП: там могут быть только важные филиалы."""
    _написать(кп / 'x-kp.csv', [
        {'city': 'Москва', 'yandex_map_url': 'https://yandex.ru/maps/org/x/1/',
         'twogis_map_url': ''}],
        ['city', 'yandex_map_url', 'twogis_map_url'])
    _написать(кп / 'reviews-x.csv', [
        {'city': 'Сочи', 'country': 'Россия',
         'yandex_url': 'https://yandex.ru/maps/org/x/9/', '2gis_url': ''}],
        ['city', 'country', 'yandex_url', '2gis_url'])
    assert [b['city'] for b in R.load_branches('x')] == ['Сочи']


def test_нет_ни_кп_ни_списка(кп):
    assert R.load_branches('нетакого') is None


def test_кп_без_единой_ссылки_это_нет_данных(кп):
    _написать(кп / 'x-kp.csv',
              [{'city': 'Москва', 'yandex_map_url': '', 'twogis_map_url': ''}],
              ['city', 'yandex_map_url', 'twogis_map_url'])
    assert R.load_branches('x') is None


# ── Разбор ссылок и трактовка нулей ──────────────────────────────────

def test_ссылка_2гис_из_поиска_разбирается():
    """В КП попадаются ссылки вида /search/…/firm/<id>?m=… - id там есть."""
    import twogis_check
    город, fid = twogis_check.parse_firm(
        'https://2gis.ru/moscow/search/%D0%BC%D0%B5%D1%82/firm/70000001058778086'
        '?m=37.74%2C55.72%2F11.38')
    assert (город, fid) == ('moscow', '70000001058778086')


def test_нулевой_рейтинг_это_отсутствие_оценок():
    """Яндекс отдаёт 0 у карточки без оценок - как «худший рейтинг» это читать
    нельзя, иначе филиал без отзывов встанет выше реально плохих."""
    итог = R.compute_priority([
        {'city': 'A', 'yandex': {'rating': None, 'count': 0},
         'twogis': {'rating': None, 'count': 0}},
    ])
    b = итог['branches'][0]
    assert b['rating'] is None
    assert b['no_reviews'] is True and b['unknown'] is False


def test_ссылки_не_открылись_это_неизвестность():
    b = R.compute_priority([
        {'city': 'A', 'yandex': {'rating': None, 'count': None},
         'twogis': {'rating': None, 'count': None}},
    ])['branches'][0]
    assert b['unknown'] is True and b['no_reviews'] is False


def test_отзывы_есть_а_оценок_нет():
    b = R.compute_priority([
        {'city': 'A', 'yandex': {'rating': None, 'count': 0},
         'twogis': {'rating': None, 'count': 4}},
    ])['branches'][0]
    assert b['no_rating_yet'] is True
    assert b['no_reviews'] is False and b['unknown'] is False


def test_рейтинг_берётся_худший_из_двух():
    b = R.compute_priority([
        {'city': 'A', 'yandex': {'rating': 4.9, 'count': 10},
         'twogis': {'rating': 4.1, 'count': 10}},
    ])['branches'][0]
    assert b['rating'] == 4.1 and b['low_rating'] is True
