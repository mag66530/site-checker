# -*- coding: utf-8 -*-
"""Отзывы на карточке товара: блок есть, размечен, правдоподобен.

«Правдоподобие» проверяем не ради маскировки под чужие сервисы, а потому что
наспех сгенерированные отзывы (одна дата, один автор, у всех пятёрки) ловят
ручные санкции - то есть вредят клиенту.
"""
import json

import schema_checker as sc
from report_priorities import classify, _markup_findings


def _jsonld(obj):
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False)
            + '</script></head><body>Отзывы клиентов</body></html>')


def _review(author='Иван', rating='5', date='2026-08-01',
            body='Труба пришла быстро, качество отличное, брали второй раз'):
    return {'@type': 'Review', 'author': {'@type': 'Person', 'name': author},
            'reviewRating': {'@type': 'Rating', 'ratingValue': rating},
            'datePublished': date, 'reviewBody': body}


def _товар(reviews, aggregate=True):
    obj = {'@context': 'https://schema.org', '@type': 'Product',
           'name': 'Труба', 'image': 'https://a.ru/1.jpg',
           'offers': {'@type': 'Offer', 'price': '100',
                      'priceCurrency': 'RUB'},
           'review': reviews}
    if aggregate:
        obj['aggregateRating'] = {'@type': 'AggregateRating',
                                  'ratingValue': '4.8', 'reviewCount': str(len(reviews))}
    return _jsonld(obj)


def _отзывные(html):
    r = sc.check_markup(html, 'product', 'https://a.ru/catalog/truba/t-1/')
    return [t for t in r['issues'] + r['warnings']
            if 'отзыв' in t.lower()]


# ── Сбор отзывов ─────────────────────────────────────────────────────


def test_отзывы_собираются_из_разметки():
    html = _товар([_review('Иван', '5', '2026-08-01'),
                   _review('Пётр', '4', '2026-07-12')])
    objs = list(sc._walk_objects(sc._jsonld_objects(html)))
    rv = sc.collect_reviews(objs)
    assert len(rv) == 2
    assert rv[0]['author'] == 'Иван' and rv[0]['rating'] == '5'
    assert rv[0]['date'] == '2026-08-01' and rv[0]['body']


def test_собираются_и_из_microdata():
    html = ('<div itemscope itemtype="https://schema.org/Review">'
            '<span itemprop="author">Иван</span>'
            '<span itemprop="reviewRating">5</span>'
            '<span itemprop="datePublished">2026-08-01</span>'
            '<p itemprop="reviewBody">Хорошая труба, доставили в срок, берём ещё</p>'
            '</div>')
    objs = list(sc._walk_objects(sc._microdata_objects(html)))
    rv = sc.collect_reviews(objs)
    assert len(rv) == 1 and rv[0]['author'] == 'Иван'


# ── Блок есть / размечен ─────────────────────────────────────────────


def test_нет_блока_отзывов():
    html = '<html><head></head><body>Труба профильная</body></html>'
    т = ' | '.join(_отзывные(html))
    assert 'нет блока отзывов' in т


def test_блок_есть_но_не_размечен():
    """Заголовок «Отзывы клиентов» в тексте, а разметки нет."""
    html = '<html><head></head><body><h2>Отзывы клиентов</h2>Хорошо</body></html>'
    т = ' | '.join(_отзывные(html))
    assert 'не размечен' in т
    assert 'нет блока отзывов' not in т


def test_нет_сводной_оценки():
    html = _товар([_review('Иван'), _review('Пётр', '4'),
                   _review('Сидор', '3')], aggregate=False)
    т = ' | '.join(_отзывные(html))
    assert 'сводной оценки' in т


def test_сводная_оценка_без_отзывов():
    """Звёзды есть, отзывов нет - Google считает это накруткой и снимает
    сниппет. Поймано живьём на SHOPMET."""
    html = _jsonld({'@context': 'https://schema.org', '@type': 'Product',
                    'name': 'Труба', 'image': 'https://a.ru/1.jpg',
                    'offers': {'@type': 'Offer', 'price': '100',
                               'priceCurrency': 'RUB'},
                    'aggregateRating': {'@type': 'AggregateRating',
                                        'ratingValue': '4.9',
                                        'reviewCount': '132'}})
    т = ' | '.join(_отзывные(html))
    assert 'накруткой звёзд' in т
    assert 'нет блока отзывов' not in т


def test_нормальные_отзывы_молчат():
    html = _товар([
        _review('Иван', '5', '2026-08-01'),
        _review('Пётр', '4', '2026-07-12', 'Всё устроило, доставка в срок, рекомендую'),
        _review('Мария', '5', '2026-06-30', 'Качество хорошее, цена адекватная'),
        _review('Олег', '3', '2026-05-15', 'Товар нормальный, но ждал дольше обещанного'),
    ])
    assert _отзывные(html) == []


# ── Правдоподобие ────────────────────────────────────────────────────


def test_мало_отзывов_не_судим():
    """На двух отзывах «одна дата» ничего не значит."""
    rv = [{'author': 'Иван', 'rating': '5', 'date': '2026-08-01', 'body': 'о' * 40},
          {'author': 'Иван', 'rating': '5', 'date': '2026-08-01', 'body': 'о' * 40}]
    assert sc.check_reviews_plausible(rv) == []


def test_одна_дата_у_всех():
    rv = [{'author': f'А{i}', 'rating': str(3 + i % 3), 'date': '2026-08-01',
           'body': 'о' * 40} for i in range(4)]
    т = ' | '.join(sc.check_reviews_plausible(rv))
    assert 'с одной датой' in т


def test_один_автор_у_всех():
    rv = [{'author': 'Иван', 'rating': str(3 + i % 3),
           'date': f'2026-0{i + 1}-01', 'body': 'о' * 40} for i in range(4)]
    т = ' | '.join(sc.check_reviews_plausible(rv))
    assert 'от одного автора' in т


def test_одинаковая_оценка_только_от_пяти_отзывов():
    """На четырёх пятёрках ещё рано делать вывод, на пяти - уже видно."""
    def _rv(n):
        return [{'author': f'А{i}', 'rating': '5', 'date': f'2026-0{i + 1}-01',
                 'body': 'о' * 40} for i in range(n)]
    assert not any('одинаковая оценка' in t
                   for t in sc.check_reviews_plausible(_rv(4)))
    assert any('одинаковая оценка (5)' in t
               for t in sc.check_reviews_plausible(_rv(5)))


def test_нет_дат_и_авторов():
    rv = [{'author': '', 'rating': '5', 'date': '', 'body': 'о' * 40}
          for _ in range(3)]
    т = ' | '.join(sc.check_reviews_plausible(rv))
    assert 'не проставлены даты' in т and 'не указаны авторы' in т


def test_слишком_короткие_тексты():
    rv = [{'author': f'А{i}', 'rating': str(3 + i % 3),
           'date': f'2026-0{i + 1}-01', 'body': 'Норм'} for i in range(3)]
    т = ' | '.join(sc.check_reviews_plausible(rv))
    assert 'слишком короткие' in т


def test_часть_коротких_не_находка():
    rv = [{'author': f'А{i}', 'rating': str(3 + i % 3),
           'date': f'2026-0{i + 1}-01',
           'body': 'Норм' if i == 0 else 'о' * 40} for i in range(3)]
    assert not any('короткие' in t for t in sc.check_reviews_plausible(rv))


# ── Вывод в отчёт ────────────────────────────────────────────────────


def test_находки_по_отзывам_идут_в_разметку_со_своими_задачами():
    html = _товар([_review('Иван', '5', '2026-08-01'),
                   _review('Иван', '5', '2026-08-01'),
                   _review('Иван', '5', '2026-08-01')], aggregate=False)
    markup = sc.check_markup(html, 'product', 'https://a.ru/catalog/truba/t-1/')
    находки = [f for f in _markup_findings(
        markup, city='Москва', page_type='Товар',
        url='https://a.ru/catalog/truba/t-1/') if 'отзыв' in f.problem.lower()]
    assert находки
    assert all(f.section == 'Разметка' for f in находки)
    группы = {classify(f)['task_group'] for f in находки}
    assert 'reviews_quality' in группы
    assert 'reviews_markup' in группы
    assert 'schema_generic' not in группы


def test_отзывы_проверяются_только_на_товаре():
    """На категории блока отзывов не требуем - там его и не должно быть."""
    html = '<html><head></head><body>Категория</body></html>'
    r = sc.check_markup(html, 'category', 'https://a.ru/catalog/truba/')
    assert not any('отзыв' in t.lower() for t in r['issues'] + r['warnings'])
