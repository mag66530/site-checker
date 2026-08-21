"""Playwright-селектор не должен ронять быстрый путь «по коду».

Живой случай ИМП (прогон 21.08.2026): все шесть форм в отчёте стояли ✗, хотя
руками отправляются. В конфиге селекторы вида
`.send-question__form >> visible=true` - нормальные для Playwright, но
быстрый путь отдавал их в soup.select() как есть. soupsieve падал
(«SelectorSyntaxError: The combinator '>' … must have a selector before it»),
исключение уходило в общий except, форма писалась «Ошибкой» и БРАУЗЕРОМ УЖЕ
НЕ ПРОВЕРЯЛАСЬ - то есть проверки формы не было вовсе.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / 'forms_tester'))

import test_all as t             # noqa: E402
from bs4 import BeautifulSoup    # noqa: E402


# ── очистка селектора ────────────────────────────────────────────────

def test_фильтр_видимости_уводит_в_браузер():
    """visible=true в конфиге стоит там, где одинаковых форм несколько
    (у ИМП два .send-question__form на карточке товара). По статике видимость
    не определить, «первая попавшаяся» - молча не та форма."""
    for sel in ('.send-question__form >> visible=true',
                '.consultation__body_b >> visible=true',
                '.consultation__body:not(.consultation__body_b) >> visible=true',
                'form.x:visible'):
        assert t.css_для_soup(sel) == '', sel
    print('✓ формы «только видимая» уходят браузеру, а не проверяются по коду')


def test_цепочку_и_лишние_фильтры_чистим():
    assert t.css_для_soup('.modal >> .form-inner') == '.modal .form-inner'
    assert t.css_для_soup('.modal >> nth=0') == '.modal'
    assert t.css_для_soup('text=Купить') == ''
    assert t.css_для_soup('') == ''
    print('✓ цепочка склеивается, браузерные фильтры отбрасываются')


def test_обычный_css_не_трогаем():
    for sel in ('form#form-callback', '.consultation__body_b',
                '.consultation__body:not(.consultation__body_b)',
                'form[name="ORDER_FORM"]'):
        assert t.css_для_soup(sel) == sel, sel
    print('✓ обычный CSS остаётся как есть')


# ── soupsieve не должен падать на очищенных селекторах ───────────────

def test_очищенный_селектор_находит_форму_имп():
    """Разметка с карточки товара inmetprom.ru (классы проверены живьём)."""
    html = ('<form class="form-send consultation__body need-validate"></form>'
            '<form class="form-send consultation__body consultation__body_b"></form>')
    soup = BeautifulSoup(html, 'html.parser')

    синяя = soup.select(t.css_для_soup('.consultation__body:not(.consultation__body_b)'))
    товарная = soup.select(t.css_для_soup('.consultation__body_b'))

    assert len(синяя) == 1 and len(товарная) == 1
    print('✓ обе формы ИМП находятся по очищенному селектору')


def test_кривой_вариант_не_роняет_подбор():
    """Даже если какой-то вариант цепочки soupsieve не понимает - это не
    вердикт форме: пробуем следующий, иначе отдаём браузеру."""
    soup = BeautifulSoup('<div class="modal"><form class="b"></form></div>',
                         'html.parser')
    упало = False
    try:
        soup.select('.modal >> .b')          # сырой playwright-селектор
    except Exception:
        упало = True

    assert упало, 'сырой playwright-селектор действительно ломает soupsieve'
    assert len(soup.select(t.css_для_soup('.modal >> .b'))) == 1
    print('✓ подтверждено: сырой селектор ломает soupsieve, очищенный - нет')
