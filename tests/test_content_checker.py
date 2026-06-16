"""
Тесты content_checker — структурная проверка страниц.

Сценарии из правки заказчика:
  • листинг / раздел / пустой раздел различаются и проверяются по-разному;
  • в разделах нет товарных столбцов (карточек, цен, кнопок, пагинации);
  • цена разделена: «есть вообще» (обязательная) и «в рублях»/«по запросу»;
  • кнопка заказа обязательна как «хотя бы одна из», сами кнопки — справочно;
  • плитка тегов «Часто ищут» — присутствие фиксируется, отсутствие не баг.
"""
import pytest

from content_checker import check_content, BLOCK_DESCRIPTIONS

# ── Готовые куски HTML ───────────────────────────────────────────────

# Шапка со всеми обязательными элементами (телефон, заказать звонок,
# оставить заявку, город) и подвал (телефон, e-mail, написать нам, адрес).
COMMON = (
    '<header>'
    '<a href="tel:+74951234567">+7 (495) 123-45-67</a>'
    '<button>Заказать звонок</button><button>Оставить заявку</button>'
    '<span>Город: Москва</span>'
    '</header>'
    '<div class="breadcrumb">крошки</div>'
    '<h1>Категория</h1>'
    '<footer>'
    '<a href="tel:+74951234567">+7 (495) 123-45-67</a>'
    '<a href="mailto:info@example.ru">info@example.ru</a>'
    '<a>Написать нам</a><span>Адрес: ул. Ленина, 1</span>'
    '</footer>'
)

CARD_WITH_PRICE = (
    '<div class="catalog-product-card-item">'
    '<a href="/catalog/cat/tovar-1/">Товар</a>'
    '<span>1 200 ₽</span><span>В наличии</span>'
    '<span class="an-ico-basket"></span>'
    '</div>'
)

CARD_PRICE_REQUEST = (
    '<div class="catalog-product-card-item">'
    '<a href="/catalog/cat/tovar-2/">Товар</a>'
    '<span>Цена по запросу</span>'
    '<button class="one-click-to-buy">Купить в один клик</button>'
    '</div>'
)

SMU_MARKER = '<a href="https://stalmetural.ru/">stalmetural</a>'
FORM_NF = '<div>Не нашли что искали?</div>'


def _by_key(result):
    return {b.key: b for b in result.blocks}


# ── Шапка и подвал (обязательные элементы) ───────────────────────────


def test_header_footer_all_present():
    """Полные шапка и подвал — все 8 элементов найдены, багов нет.

    Шапка/подвал — сквозные блоки, проверяются только на главной ('main')."""
    r = check_content(COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER, 'main')
    b = _by_key(r)
    for key in ('hdr_phone', 'hdr_callback', 'hdr_request', 'hdr_city',
                'ftr_phone', 'ftr_email', 'ftr_writeus', 'ftr_address'):
        assert b[key].present and b[key].required, f'{key} должен быть найден и обязателен'
    assert all(bug.key not in (
        'hdr_phone', 'hdr_callback', 'hdr_request', 'hdr_city',
        'ftr_phone', 'ftr_email', 'ftr_writeus', 'ftr_address') for bug in r.bugs)


def test_header_missing_request_is_bug():
    """Нет «Оставить заявку» в шапке → красный баг именно по этому столбцу."""
    header_no_request = (
        '<header><a href="tel:+74951234567">+7 (495) 123-45-67</a>'
        '<button>Заказать звонок</button><span>Город: Москва</span></header>'
    )
    html = (header_no_request + '<div class="breadcrumb">x</div><h1>K</h1>'
            '<footer><a href="tel:+74951234567">+7</a>'
            '<a href="mailto:a@b.ru">a@b.ru</a>Написать нам Адрес: ул. Мира 5</footer>'
            + CARD_WITH_PRICE + FORM_NF + SMU_MARKER)
    r = check_content(html, 'main')
    b = _by_key(r)
    assert not b['hdr_request'].present
    assert any(bug.key == 'hdr_request' for bug in r.bugs)
    # остальные элементы шапки на месте
    assert b['hdr_phone'].present and b['hdr_callback'].present and b['hdr_city'].present


def test_footer_missing_email_is_bug():
    """Нет e-mail в подвале → баг по столбцу «Подвал: e-mail»."""
    footer_no_email = (
        '<footer><a href="tel:+74951234567">+7 (495) 123-45-67</a>'
        'Написать нам Адрес: ул. Мира 5</footer>'
    )
    header_full = (
        '<header><a href="tel:+74951234567">+7 (495) 1-23</a>Заказать звонок '
        'Оставить заявку Город: Москва</header>'
    )
    html = (header_full + '<div class="breadcrumb">x</div><h1>K</h1>' + footer_no_email
            + CARD_WITH_PRICE + FORM_NF + SMU_MARKER)
    r = check_content(html, 'main')
    assert any(bug.key == 'ftr_email' for bug in r.bugs)


def test_phone_in_region_caught_via_tel_link():
    """Телефон ловится по tel:-ссылке, даже если видимый формат «+7 (495)…»."""
    html = COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER
    b = _by_key(check_content(html, 'main'))
    assert b['hdr_phone'].present
    assert b['ftr_phone'].present


# ── Листинг ──────────────────────────────────────────────────────────


def test_listing_detected_with_full_checks():
    html = COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER
    r = check_content(html, 'category')
    assert r.page_kind == 'listing'
    b = _by_key(r)
    assert b['product_cards'].present and b['product_cards'].count == 1
    assert b['price'].present and b['price'].required
    assert b['btn_order'].present and b['btn_order'].required
    assert b['availability'].present and not b['availability'].required
    assert r.bug_count == 0


def test_listing_price_request_only():
    """Товар «по запросу»: цена засчитана, рублёвая отдельно показана как нет."""
    html = COMMON + CARD_PRICE_REQUEST + FORM_NF + SMU_MARKER
    r = check_content(html, 'category')
    b = _by_key(r)
    assert b['price'].present, '«по запросу» — это тоже цена, не баг'
    assert not b['price_real'].present, 'рублёвой цены нет'
    assert b['price_request'].present
    assert not b['price_real'].required and not b['price_request'].required


def test_listing_no_order_buttons_is_bug():
    """Нет НИ «В корзину», НИ «Купить в 1 клик» → «Кнопка заказа» — баг."""
    html = (
        COMMON
        + '<div class="catalog-product-card-item"><span>1 200 ₽</span></div>'
        + FORM_NF + SMU_MARKER
    )
    r = check_content(html, 'category')
    b = _by_key(r)
    assert not b['btn_order'].present
    assert b['btn_order'].required
    assert any(bug.key == 'btn_order' for bug in r.bugs)
    # Сами кнопки — справочные столбцы, не баги
    assert not b['btn_cart'].required and not b['btn_oneclick'].required


def test_listing_one_button_is_enough():
    """Любая одна кнопка (корзина ИЛИ 1 клик) закрывает «Кнопку заказа»."""
    html = COMMON + CARD_PRICE_REQUEST + FORM_NF + SMU_MARKER  # только 1 клик
    r = check_content(html, 'category')
    b = _by_key(r)
    assert b['btn_order'].present
    assert not b['btn_cart'].present
    assert b['btn_oneclick'].present


# ── Раздел-витрина ───────────────────────────────────────────────────


def test_section_has_no_product_columns():
    """Раздел (вкладки подкатегорий, без карточек): товарных проверок нет вообще."""
    html = COMMON + '<div class="catalog-cat-tabs">Подкатегории</div>' + FORM_NF + SMU_MARKER
    r = check_content(html, 'category')
    assert r.page_kind == 'section'
    keys = {b.key for b in r.blocks}
    for product_key in ('product_cards', 'price', 'btn_order', 'availability',
                        'pagination', 'filters', 'sort'):
        assert product_key not in keys, f'В разделе не должно быть столбца {product_key}'
    assert 'tag_tiles' in keys
    assert r.bug_count == 0


def test_empty_section_is_bug():
    """«Раздел пуст.» без товаров и подкатегорий — это баг."""
    html = COMMON + '<p>Раздел пуст.</p>' + FORM_NF + SMU_MARKER
    r = check_content(html, 'category')
    assert r.page_kind == 'empty'
    assert any(bug.key == 'product_cards' for bug in r.bugs)


# ── Каталог-корень ───────────────────────────────────────────────────


def test_catalog_root_minimal_columns():
    """Каталог: без карточек/фильтров/сортировки/H2 — лишних столбцов нет."""
    html = COMMON + SMU_MARKER
    r = check_content(html, 'catalog')
    keys = {b.key for b in r.blocks}
    for absent in ('product_cards', 'price', 'btn_order', 'filters', 'sort',
                   'pagination', 'h2'):
        assert absent not in keys, f'В каталоге не должно быть столбца {absent}'
    assert 'tag_tiles' in keys
    # Хлебные крошки на корне каталога не обязательны
    b = _by_key(r)
    assert not b['breadcrumbs'].required


# ── Плитка тегов ─────────────────────────────────────────────────────


def test_tag_tiles_absence_is_not_bug():
    html = COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER
    r = check_content(html, 'category')
    b = _by_key(r)
    assert not b['tag_tiles'].present
    assert not b['tag_tiles'].required
    assert all(bug.key != 'tag_tiles' for bug in r.bugs)


def test_tag_tiles_detected():
    html = COMMON + CARD_WITH_PRICE + '<div class="tags-block">Часто ищут</div>' + FORM_NF + SMU_MARKER
    r = check_content(html, 'category')
    assert _by_key(r)['tag_tiles'].present


# ── Форма «Не нашли что искали» ──────────────────────────────────────


def test_form_nf_required_only_on_smu():
    html_smu = COMMON + CARD_WITH_PRICE + SMU_MARKER          # СМУ, формы нет
    r_smu = check_content(html_smu, 'category')
    assert _by_key(r_smu)['form_nf'].required
    assert any(bug.key == 'form_nf' for bug in r_smu.bugs)

    # Не-СМУ: формы «Не нашли что искали» нет по дизайну → столбца быть не должно
    html_other = COMMON + CARD_WITH_PRICE
    r_other = check_content(html_other, 'category')
    assert 'form_nf' not in _by_key(r_other)


def test_project_absent_elements_not_shown():
    """У ИМП нет «Заказать звонок», у МПЭ — ещё и «Написать нам»:
    этих столбцов в отчёте быть не должно (не ложный баг)."""
    imp = '<a href="https://inmetprom.ru/">inmetprom</a>'
    mpe = '<a href="https://mepen.ru/">mepen</a>'
    base = ('<header><a href="tel:+74951234567">+7</a>Заявка Город</header>'
            '<h1>Гл</h1><footer><a href="mailto:a@b.ru">a@b.ru</a>ул. Ленина 1</footer>')
    imp_keys = {b.key for b in check_content(base + imp, 'main').blocks}
    assert 'hdr_callback' not in imp_keys
    assert 'ftr_writeus' in imp_keys      # у ИМП «Написать нам» есть
    mpe_keys = {b.key for b in check_content(base + mpe, 'main').blocks}
    assert 'hdr_callback' not in mpe_keys
    assert 'ftr_writeus' not in mpe_keys  # у МПЭ его нет


def test_belarus_header_variant():
    """ИМП.by: телефон +375 и переключатель городов по гео-иконке (без слова
    «город») должны распознаваться."""
    html = (
        '<a href="https://inmetprom.by/">inmetprom.by</a>'
        '<header>'
        '<svg class="svg-icon icon-geo-mark"></svg><span>Гомель</span>'
        '<a href="tel:+375445888148">+375 (44) 588-81-48</a>'
        'Оставить заявку</header>'
        '<h1>Гл</h1>'
        '<footer><a href="tel:+375445888148">+375 (44) 588-81-48</a>'
        '<a href="mailto:gomel@inmetprom.by">gomel@inmetprom.by</a>'
        'ул. Советская, 1</footer>'
    )
    b = _by_key(check_content(html, 'main'))
    assert b['hdr_phone'].present, 'телефон +375 должен ловиться'
    assert b['hdr_city'].present, 'город по гео-иконке должен ловиться'
    assert b['ftr_phone'].present
    assert 'hdr_callback' not in b, 'у ИМП нет «Заказать звонок»'


def test_soft_404_detected():
    """Страница отдала 200, но контент — «страница не найдена» → soft-404,
    одна проблема (404), а не «нет цены»."""
    html = (COMMON + SMU_MARKER
            + '<h1>Страница не найдена</h1><p>Ошибка 404</p>')
    r = check_content(html, 'category')
    assert r.is_soft_404
    assert r.bug_count == 1


# ── Карточка товара ──────────────────────────────────────────────────


def test_product_page():
    html = (
        COMMON + SMU_MARKER
        + '<span>5 400 руб</span><button>Добавить в корзину</button>'
        + '<div>Характеристики</div><div>Способы оплаты</div>'
    )
    r = check_content(html, 'product')
    b = _by_key(r)
    assert b['price'].present and b['btn_order'].present
    assert b['specs'].present and b['payment'].present
    assert r.bug_count == 0


def test_product_without_price_is_bug():
    html = COMMON + SMU_MARKER + '<button>Добавить в корзину</button>'
    r = check_content(html, 'product')
    assert any(bug.key == 'price' for bug in r.bugs)


def test_hidden_price_button_is_bug():
    """Цена и кнопка СПРЯТАНЫ стилем display:none → покупатель их не видит →
    баг с пояснением «в коде есть, но покупатель не видит»."""
    html = (COMMON + SMU_MARKER
            + '<div class="cost-val" style="display:none">3 627 руб.</div>'
            + '<div class="card-item-add-no-cart-block" style="display:none">'
            + '<div class="one-click-to-buy">Купить в один клик</div></div>'
            + '<div>Характеристики</div>')
    b = _by_key(check_content(html, 'product'))
    assert not b['price'].present and 'не видит' in b['price'].note
    assert not b['btn_order'].present and 'не видит' in b['btn_order'].note


def test_mpe_listing_is_recognized():
    """Листинг МПЭ — другой шаблон (card-item + schema Product, цена в
    .price-row, кнопка «в корзину» в .add). Должен распознаваться как листинг
    и проверяться: карточки/цена/кнопка. Форма «Не нашли» на МПЭ не требуется."""
    cards = ''.join(
        '<div itemscope itemtype="http://schema.org/Product" class="card-item ">'
        f'<a class="name h4"><span itemprop="name">Инконель {i}</span></a>'
        '<div class="price price-row h4" itemprop="offers">'
        f'<span itemprop="price">15557.00</span><span> ₽ </span></div>'
        '<div class="settings"><div class="add"><p>в корзину</p></div></div>'
        '</div>' for i in range(6))
    html = ('<a href="https://mepen.ru/">mepen</a>'
            '<div class="breadcrumb">крошки</div><h1>Инконель</h1>' + cards)
    r = check_content(html, 'category')
    assert r.page_kind == 'listing', f'ожидали listing, получили {r.page_kind!r}'
    b = _by_key(r)
    assert b['product_cards'].present and b['product_cards'].count == 6
    assert b['price'].present and b['btn_order'].present
    # форму «Не нашли что искали» на МПЭ не требуем — её не должно быть в багах
    assert not any(bug.key == 'form_nf' for bug in r.bugs)


def test_disabled_class_alone_does_not_hide():
    """Класс «disabled» сам по себе НЕ прячет: на сайте это часто смысловой
    маркер (card-item-add-no-cart-block disabled), а кнопка «Купить в один
    клик» при этом видна (реальный прод stalmetural.ru, товары «по запросу»).
    Видимая кнопка-один-клик → заказ ЕСТЬ, это не баг."""
    html = (COMMON + SMU_MARKER
            + '<div class="catalog-product-card-item">'
            + '<a href="/catalog/c/t/">Круг ванадиевый 103 мм</a>'
            + '<div class="cost-val">Цена по запросу</div>'
            + '<div class="card-item-add-no-cart-block disabled">'
            + '<div class="btn btn-transparent-blue one-click-to-buy-catalog">'
            + '<i class="an-ico an-ico-one-click"></i><span>Купить в один клик</span></div>'
            + '</div></div>' + FORM_NF)
    b = _by_key(check_content(html, 'category'))
    assert b['btn_order'].present, 'видимая кнопка «в один клик» — заказ есть, не баг'
    assert b['price'].present       # «по запросу» — цена есть


def test_visible_price_button_ok():
    """Видимые цена и кнопка → без багов."""
    html = (COMMON + SMU_MARKER
            + '<div class="cost-val">3 627 руб.</div>'
            + '<button class="add-to-cart-btn">В корзину</button>'
            + '<div>Характеристики</div>')
    b = _by_key(check_content(html, 'product'))
    assert b['price'].present and b['btn_order'].present


def test_css_display_none_hides_price_button_listing():
    """Цена/кнопка ЕСТЬ в HTML, но скрыты правилом display:none из CSS-файла
    (как на тест-стенде test2.stalmetural.ru) → покупатель не видит → баг."""
    from content_checker import parse_hidden_selectors
    cards = ''.join(
        f'<div class="catalog-product-card-item"><a href="/catalog/c/t{i}/">Товар {i}</a>'
        f'<div class="cost-val">156 000 ₽</div>'
        f'<div class="card-item-add-to-cart-block"><span class="an-ico-basket"></span>'
        f'<span>В корзину</span></div></div>'
        for i in range(5))
    html = COMMON + SMU_MARKER + cards + FORM_NF
    css = parse_hidden_selectors(
        '.cost-val,[class*="cost-"]{display:none!important}'
        '.card-item-add-to-cart-block{display:none!important}')
    # без CSS — всё «видно», багов по цене/кнопке нет
    b0 = _by_key(check_content(html, 'category'))
    assert b0['price'].present and b0['btn_order'].present
    # с CSS-скрытием — цена и кнопка считаются невидимыми → баг с пояснением
    b = _by_key(check_content(html, 'category', css_hidden=css))
    assert not b['price'].present and 'не видит' in b['price'].note
    assert not b['btn_order'].present and 'не видит' in b['btn_order'].note
    # карточки при этом всё равно посчитаны (контейнеры не скрыты)
    assert b['product_cards'].present and b['product_cards'].count == 5


def test_css_hidden_ancestor_qualified_not_overhide():
    """Правило `.catalog-list .cost-val{display:none}` НЕ должно прятать цену,
    если предка `.catalog-list` на странице нет (сетка, а не список)."""
    from content_checker import parse_hidden_selectors
    html = (COMMON + SMU_MARKER
            + '<div class="catalog-product-card-item"><a href="/catalog/c/t/">Товар</a>'
            + '<div class="cost-val">156 000 ₽</div>'
            + '<div class="card-item-add-to-cart-block"><span>В корзину</span></div></div>'
            + FORM_NF)
    css = parse_hidden_selectors('.catalog-list .cost-val{display:none}')
    b = _by_key(check_content(html, 'category', css_hidden=css))
    assert b['price'].present, 'цена не под .catalog-list — прятать нельзя'


def test_parse_hidden_selectors_basics():
    """display:none/visibility:hidden ловим; видимые правила и @media — нет."""
    from content_checker import parse_hidden_selectors
    assert len(parse_hidden_selectors('.a{display:none}')) == 1
    assert len(parse_hidden_selectors('.a{visibility:hidden}')) == 1
    assert len(parse_hidden_selectors('.a{color:red}')) == 0
    assert len(parse_hidden_selectors('.a{opacity:0.5}')) == 0   # не полностью прозрачно
    # внутрь @media не лезем (мобильные скрытия для десктоп-проверки не берём)
    assert len(parse_hidden_selectors('@media(max-width:600px){.a{display:none}}')) == 0
    # список селекторов раскрывается
    assert len(parse_hidden_selectors('.a,.b{display:none}')) == 2


def test_product_price_ignores_recommendations_block():
    """На карточке товара «Цена по запросу» из блока «с этим товаром покупают»
    не должна примешиваться к цене самого товара (там одна цена)."""
    html = (COMMON + SMU_MARKER
            + '<div>Цена за 1 кг 3 627.00 руб. <button>В корзину</button> Характеристики</div>'
            + '<div>С этим товаром покупают: Свинец Цена по запросу. Цинк Цена по запросу.</div>')
    b = _by_key(check_content(html, 'product'))
    assert b['price_real'].present
    assert not b['price_request'].present


# ── Описания столбцов ────────────────────────────────────────────────


def test_every_block_has_description():
    """У каждого столбца отчёта должно быть пояснение «что проверяется»."""
    for type_code in ('main', 'catalog', 'category', 'filter', 'product'):
        r = check_content(COMMON + CARD_WITH_PRICE + SMU_MARKER, type_code)
        for b in r.blocks:
            assert b.description, f'Нет описания для столбца {b.key} ({type_code})'
            assert b.key in BLOCK_DESCRIPTIONS


def test_phone_formats_and_footer_above_tag():
    """МПЭ-кейс: телефон в tel: без «+» и формате «+7 (495)»; контакты подвала
    свёрстаны ВЫШЕ тега <footer> (внутри него — только меню)."""
    html = (
        '<header><a href="tel:74957991438">7 (495) 799-14-38</a>'
        'Заявка Выберите город</header>'
        '<h1>Главная</h1>'
        # контактный блок ДО <footer>
        '<div class="contacts"><a href="tel:74957991438">7 (495) 799-14-38</a>'
        '<a href="mailto:moscow@mepen.ru">moscow@mepen.ru</a>'
        '<span>г. Москва, ул. Примерная, 5</span><a>Написать нам</a></div>'
        '<footer>Каталог Карта сайта © 2026</footer>'
    )
    b = _by_key(check_content(html, 'main'))
    assert b['hdr_phone'].present, 'телефон в tel: без + должен ловиться'
    assert b['ftr_phone'].present, 'телефон над <footer> должен ловиться'
    assert b['ftr_email'].present
    assert b['ftr_address'].present


def test_header_footer_only_on_main():
    """Сквозные блоки шапки/подвала проверяются только на главной.

    На категории/листинге/товаре/каталоге их в отчёте быть не должно —
    иначе одна и та же ошибка размножится на сотни строк."""
    hf_keys = {'hdr_phone', 'hdr_callback', 'hdr_request', 'hdr_city',
               'ftr_phone', 'ftr_email', 'ftr_writeus', 'ftr_address'}
    html = COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER
    for tc in ('category', 'product', 'catalog'):
        keys = {b.key for b in check_content(html, tc).blocks}
        assert not (keys & hf_keys), f'шапка/подвал не должны проверяться на {tc}'
    # А на главной — должны
    main_keys = {b.key for b in check_content(html, 'main').blocks}
    assert hf_keys <= main_keys, 'на главной шапка/подвал обязаны быть'


def test_empty_html_returns_no_blocks():
    r = check_content('', 'category')
    assert r.blocks == []
    assert r.bug_count == 0
