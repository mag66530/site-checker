# -*- coding: utf-8 -*-
"""Страница товара, свёрстанная не «карточкой», а таблицей (МТТ).

Прогон писал по каждой такой странице «нет хлебных крошек, нет цены, нет
кнопки заказа», хотя на met-trans.ru всё это есть - просто названо иначе:

  крошки  - <nav class="layout-product__path"><ul class="product-path">
            (слова breadcrumb в вёрстке нет вообще);
  цена    - в таблице типоразмеров: «25,170 Р» + itemprop="price" content=…
            (запятая как разделитель тысяч, рубль одной буквой «Р»);
  заказ   - товар отмечают галочкой, заявка уходит кнопкой «Отправить заявку».
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from content_checker import _PRICE_RE, check_content

СТРАНИЦА_МТТ = (
    '<div class="header-top"><a href="tel:+74957553826">+7 (495) 755-38-26</a>'
    '<a href="mailto:moscow@met-trans.ru">moscow@met-trans.ru</a>'
    '<button class="button">Москва</button>'
    '<span class="header__request-a-callback--link">Закажите обратный звонок</span>'
    '</div>'
    '<div class="page-container"><div class="layout-product">'
    '<nav class="layout-product__path"><ul class="product-path">'
    '<li class="product-path__item"><a class="link_gray" href="/">Главная</a></li>'
    '<li class="product-path__item"><a class="link_gray" '
    'href="/truboprovodnaya-armatura">Трубопроводная арматура</a></li>'
    '<li class="product-path__item"><a class="link_gray" '
    'href="/kondensatootvodchik">Конденсатоотводчик</a></li>'
    '</ul></nav>'
    '<h1>Конденсатоотводчик 100 мм</h1>'
    '<div class="orderModalCtrl"><button type="button" '
    'class="button button_theme_action">Отправить заявку</button></div>'
    '<table class="product-list__table"><tbody>'
    '<tr itemprop="offers" itemscope itemtype="http://schema.org/Offer">'
    '<td class="product-list__description" itemprop="description">'
    'Конденсатоотводчик 100 мм стальной</td>'
    '<td class="product-list__price" itemprop="price" content="25948">'
    '<span>25,170 <span class="rub">Р</span></span></td>'
    '<td class="product-list__check"><input type="checkbox"></td></tr>'
    '</tbody></table>'
    '</div></div>'
)


def _по_ключам(r):
    return {b.key: b for b in r.blocks}


def test_товар_мтт_без_ложных_багов():
    r = check_content(
        СТРАНИЦА_МТТ, 'product',
        url='https://met-trans.ru/kondensatootvodchik/kondensatootvodchik-100-mm',
        city='Москва')
    b = _по_ключам(r)
    assert b['breadcrumbs'].present, 'крошки названы «путь», но они есть'
    assert b['price'].present and b['price_real'].present
    assert b['btn_order'].present, '«Отправить заявку» - это и есть заказ'
    assert not [x.key for x in r.bugs
                if x.key in ('breadcrumbs', 'price', 'btn_order')]


def test_шапка_мтт_с_одним_звонком():
    """В шапке МТТ только «Закажите обратный звонок» - обращение возможно."""
    b = _по_ключам(check_content(СТРАНИЦА_МТТ, 'main', city='Москва'))
    assert b['hdr_request'].present and b['hdr_callback'].present


def test_цена_с_запятой_и_буквой_р():
    for s in ['25,170 Р', '1,250,000 Р', '25 170 Р', '990 р']:
        assert _PRICE_RE.search(s), s
    # не цена: буква Р - начало слова, а не рубль
    for s in ['150 РАЗМЕР', '10 Ряд', '2 Р2']:
        assert not _PRICE_RE.search(s), s


def test_пустой_блок_пути_не_считается_крошками():
    """Раздел верхнего уровня МТТ: <nav class="layout-product__path"></nav> -
    крошек нет, и это настоящая находка, а не «нашлись по классу»."""
    пусто = ('<div class="layout-home"><nav class="layout-product__path">'
             '</nav><h1>Трубопроводная арматура в Москве</h1></div>')
    b = _по_ключам(check_content(пусто, 'category',
                                 url='https://met-trans.ru/truboprovodnaya-armatura'))
    assert not b['breadcrumbs'].present


def test_форма_не_нашли_что_искали_не_считается_кнопкой_заказа():
    """СМУ: в контенте есть <button>Оставить заявку</button>, но это форма
    обратной связи feedback-search, а не заказ товара - баг должен остаться."""
    html = ('<header><a href="tel:+74951234567">+7 (495) 123-45-67</a>'
            '<div class="make-request" id="txt-back-form">Оставить заявку</div>'
            '</header>'
            '<div class="breadcrumb">x</div><h1>Арматура</h1><span>1 200 ₽</span>'
            '<form class="feedback"><div>Не нашли что искали?</div>'
            '<input name="type" value="feedback-search">'
            '<button type="submit" class="btn btn-blue">Оставить заявку</button>'
            '</form>'
            '<footer><a href="mailto:a@b.ru">a@b.ru</a></footer>')
    b = _по_ключам(check_content(html, 'product',
                                 url='https://stalmetural.ru/catalog/a/b/'))
    assert not b['btn_order'].present


def test_заявка_только_в_шапке_не_закрывает_кнопку_заказа():
    """Когда шапка размечена <header>, её «Оставить заявку» не должна
    подменять кнопку заказа у самого товара."""
    html = ('<header><a href="tel:+74951234567">+7 (495) 123-45-67</a>'
            '<button>Оставить заявку</button></header>'
            '<div class="breadcrumb">x</div><h1>Товар</h1>'
            '<span>1 200 ₽</span>')
    b = _по_ключам(check_content(html, 'product',
                                 url='https://site.ru/catalog/t/'))
    assert not b['btn_order'].present
