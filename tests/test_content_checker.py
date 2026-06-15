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
    """Полные шапка и подвал — все 8 элементов найдены, багов нет."""
    r = check_content(COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER, 'category')
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
    r = check_content(html, 'category')
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
    r = check_content(html, 'category')
    assert any(bug.key == 'ftr_email' for bug in r.bugs)


def test_phone_in_region_caught_via_tel_link():
    """Телефон ловится по tel:-ссылке, даже если видимый формат «+7 (495)…»."""
    html = COMMON + CARD_WITH_PRICE + FORM_NF + SMU_MARKER
    b = _by_key(check_content(html, 'category'))
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

    html_other = COMMON + CARD_WITH_PRICE                      # не-СМУ
    r_other = check_content(html_other, 'category')
    assert not _by_key(r_other)['form_nf'].required


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


# ── Описания столбцов ────────────────────────────────────────────────


def test_every_block_has_description():
    """У каждого столбца отчёта должно быть пояснение «что проверяется»."""
    for type_code in ('main', 'catalog', 'category', 'filter', 'product'):
        r = check_content(COMMON + CARD_WITH_PRICE + SMU_MARKER, type_code)
        for b in r.blocks:
            assert b.description, f'Нет описания для столбца {b.key} ({type_code})'
            assert b.key in BLOCK_DESCRIPTIONS


def test_empty_html_returns_no_blocks():
    r = check_content('', 'category')
    assert r.blocks == []
    assert r.bug_count == 0
