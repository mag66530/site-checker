"""
content_checker.py — структурная проверка контента страницы.

Идея: HTTP-проверка отвечает на вопрос «страница открывается?» (код 200).
Этот модуль отвечает на вопрос «а на странице есть всё, что должно быть?» —
цена, кнопки «в корзину», формы, заголовок H1, хлебные крошки и т.д.

Работает на том же HTML, что уже скачан в http_checker (тот же body_text,
что уходит в text_checker). Ничего заново не качаем.

Детекторы построены на ТЕГАХ + ВИДИМОМ ТЕКСТЕ + МИКРОРАЗМЕТКЕ, а не на
CSS-классах конкретного сайта. Так проверка работает одинаково на всех
проектах (СМУ / ИМП / МПЭ — единый движок), и её не надо перенастраивать
под вёрстку каждого.

Главный принцип (по ТЗ):
  • Обязательный блок (required=True) отсутствует → это БАГ.
    H1, хлебные крошки, цена (хоть «Цена по запросу»), кнопки «в корзину» /
    «купить», ключевые формы.
  • Опциональный блок (required=False) отсутствует → просто «нет», НЕ баг.
    Фильтры, теги, пагинация, SEO-текст, H2, отзывы, FAQ — их могли не
    сделать, или мало товаров. Это нормально.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Callable

from text_checker import html_to_visible_text


# ── Результаты ──────────────────────────────────────────────────────


@dataclass
class BlockResult:
    key: str                       # машинный id ('h1', 'price', ...)
    label: str                     # человеческое имя ('Заголовок H1')
    required: bool                 # обязательный? (нет → баг)
    present: bool                  # найден на странице?
    count: Optional[int] = None    # для счётных блоков (карточки, формы, H2)
    note: str = ''                 # доп. деталь (напр. название формы)
    description: str = ''          # что конкретно проверяется (для шапки отчёта)


@dataclass
class ContentResult:
    type_code: str
    page_kind: str = ''            # для списков: 'listing' | 'section' | 'empty'
    blocks: list[BlockResult] = field(default_factory=list)
    is_soft_404: bool = False      # 200, но контент — «страница не найдена»

    @property
    def bugs(self) -> list[BlockResult]:
        """Обязательные блоки, которых нет, — это баги."""
        return [b for b in self.blocks if b.required and not b.present]

    @property
    def bug_count(self) -> int:
        # Soft-404 — это одна проблема (страница-404), а не «нет цены/кнопки».
        if self.is_soft_404:
            return 1
        return len(self.bugs)

    @property
    def has_bugs(self) -> bool:
        return self.bug_count > 0

    def get(self, key: str) -> Optional[BlockResult]:
        return next((b for b in self.blocks if b.key == key), None)


# ── Низкоуровневые помощники для детекторов ─────────────────────────


# Цена: число + ₽ (с учётом пробелов/неразрывных пробелов), либо «руб»
_PRICE_RE = re.compile(r'\d[\d\s\u00a0]{0,12}(?:₽|руб)', re.IGNORECASE)
_PHONE_RE = re.compile(
    # Узбекистан: +998 (90) 006-84-48 / tel:998900068448
    r'\+?998[\s\-(]*\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'
    # Беларусь: +375 (44) 588-81-48
    r'|\+375[\s\-(]*\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'
    # Россия/Казахстан: +7/8/7 (495) 123-45-67, либо tel:74951234567 без «+»
    r'|(?:\+7|\b8|\b7)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'
)
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
# Адрес: уличные маркеры со всеми ходовыми сокращениями.
# ИМП пишет «Рязанский пр., 86/1c1» — поэтому ловим и «пр.», и «пр-кт».
_ADDRESS_RE = re.compile(
    r'улиц|\bул\.|проспект|пр-?кт|\bпр\.|\bпр-т|шоссе|\bш\.\s|переул|\bпер\.|'
    r'набережн|\bнаб\.|бульвар|\bб-р|\bбул\.|площад|\bпл\.|проезд|\bд\.\s?\d',
    re.IGNORECASE,
)


def _extract_region(html: str, tag: str, side: str, fallback_frac: float = 0.28) -> str:
    """
    Вырезать HTML-регион шапки/подвала.

    Сначала пробуем семантический тег <header>/<footer> (есть на СМУ и
    большинстве современных сайтов). Если тега нет — берём приблизительный
    регион по положению: шапка ≈ начало страницы, подвал ≈ конец. Этого
    достаточно: нужные маркеры (телефон, «оставить заявку», адрес…) в
    середине листинга не встречаются.
    """
    m = re.search(rf'<{tag}\b[^>]*>(.*?)</{tag}>', html, re.IGNORECASE | re.DOTALL)
    if m:
        if side != 'bottom':
            return m.group(0)
        # Подвал: контактный блок (телефон/почта/адрес) часто свёрстан ВЫШЕ
        # семантического <footer> (у МПЭ в <footer> только меню и копирайт).
        # Поэтому берём не только сам тег, а ещё ~24 КБ перед ним — там и лежит
        # «нижний» блок контактов.
        pad = 24000
        start = max(0, m.start() - pad)
        return html[start:m.end()]
    n = len(html)
    cut = max(1, int(n * fallback_frac))
    return html[:cut] if side == 'top' else html[-cut:]


def _count_tag(html_lower: str, tag: str) -> int:
    """Сколько непустых тегов <tag>...</tag> на странице."""
    found = re.findall(rf'<{tag}\b[^>]*>(.*?)</{tag}>', html_lower, re.DOTALL)
    return sum(1 for f in found if re.sub(r'<[^>]+>', '', f).strip())


def _has_tag(html_lower: str, tag: str) -> bool:
    return f'<{tag}' in html_lower


def _count_text(text_lower: str, marker: str) -> int:
    return text_lower.count(marker)


@dataclass
class _Ctx:
    """Предрассчитанный контекст страницы для детекторов."""
    html: str
    html_lower: str
    text: str
    text_lower: str
    # Регионы шапки/подвала — чтобы проверять «телефон в шапке» и «телефон в
    # подвале» по отдельности, а не «телефон где-то на странице».
    # *_html — сырой HTML региона (телефон/почту ищем тут: там tel:/mailto:),
    # *_text — видимый текст (текстовые кнопки/метки ищем тут).
    header_html: str = ''
    header_text: str = ''
    header_text_lower: str = ''
    footer_html: str = ''
    footer_text: str = ''
    footer_text_lower: str = ''
    # «Ценовая» область: на карточке товара — текст ДО блока рекомендаций
    # («с этим товаром покупают», «похожие»), чтобы цена/«по запросу» из
    # чужих карточек снизу не считались ценой самого товара. На листинге и
    # прочих типах = весь текст страницы.
    price_text: str = ''
    price_text_lower: str = ''


# ── Детекторы. Возвращают (present: bool, count: Optional[int]) ──────


def _d_h1(c: _Ctx):
    n = _count_tag(c.html_lower, 'h1')
    return n > 0, n


def _d_h2(c: _Ctx):
    n = _count_tag(c.html_lower, 'h2')
    return n > 0, n


def _d_breadcrumbs(c: _Ctx):
    # Микроразметка BreadcrumbList или класс/атрибут breadcrumb —
    # практически универсальный признак хлебных крошек.
    present = (
        'breadcrumb' in c.html_lower
        or 'breadcrumblist' in c.html_lower
    )
    return present, None


# ── Шапка: обязательные элементы (проверяются ВНУТРИ региона шапки) ──
# По требованию: в шапке должны быть телефон, «заказать звонок»,
# «оставить заявку» и выбор города.


def _d_hdr_phone(c: _Ctx):
    # Сырой HTML региона: ловит и tel:-ссылку, и форматированный «+7 (499)…».
    return bool(_PHONE_RE.search(c.header_html)), None


def _d_hdr_callback(c: _Ctx):
    t = c.header_text_lower
    present = (
        'заказать звонок' in t
        or 'обратный звонок' in t
        or 'заказать обратный' in t
        or 'перезвоните мне' in t
    )
    return present, None


def _d_hdr_request(c: _Ctx):
    # СМУ: «Оставить заявку». ИМП: «Заявка» / «Быстрый заказ» / «Оформите
    # быстрый заказ». Ловим любой запрос-CTA в шапке (не только дословное
    # «оставить заявку»), иначе на ИМП был бы ложный баг.
    t = c.header_text_lower
    present = (
        'заявк' in t            # заявка/заявку/оставить заявку
        or 'быстрый заказ' in t
        or 'оформить заказ' in t
        or 'оформите заказ' in t
        or 'оставить заявку' in t
    )
    return present, None


def _d_hdr_city(c: _Ctx):
    # СМУ/ИМП.ru: текст «Город…», «Ваш город», «Выбрать город».
    # ИМП.by: переключатель городов без слова «город» — гео-иконка
    # (icon-geo-mark) + список городов; ловим по вёрстке.
    t = c.header_text_lower
    h = c.header_html.lower()
    present = (
        'город' in t or 'выбрать город' in t or 'ваш город' in t
        or 'geo-mark' in h or 'icon-geo' in h
        or 'select-city' in h or 'city-select' in h or 'cityselect' in h
        or 'js-city' in h or 'choose-city' in h
    )
    return present, None


# ── Подвал: телефон, e-mail, «написать нам», адрес ──


def _d_ftr_phone(c: _Ctx):
    return bool(_PHONE_RE.search(c.footer_html)), None


def _d_ftr_email(c: _Ctx):
    # Сырой HTML: ловит mailto: и текстовый адрес почты.
    return bool(_EMAIL_RE.search(c.footer_html)), None


def _d_ftr_writeus(c: _Ctx):
    t = c.footer_text_lower
    present = (
        'написать нам' in t
        or 'напишите нам' in t
        or 'написать письмо' in t
    )
    return present, None


def _d_ftr_address(c: _Ctx):
    # Пока — наличие адреса в подвале (метка «Адрес» или уличный маркер).
    # Сверку конкретного адреса с КП по каждому городу добавим, когда придёт КП.
    t = c.footer_text_lower
    present = 'адрес' in t or bool(_ADDRESS_RE.search(c.footer_text))
    return present, None


def _d_price(c: _Ctx):
    # Число с ₽/руб (товар с ценой) ИЛИ «по запросу» (товар без цены).
    # Ищем в «ценовой» области (на карточке — без блока рекомендаций снизу).
    present = (
        bool(_PRICE_RE.search(c.price_text))
        or 'по запросу' in c.price_text_lower
    )
    return present, None


def _d_price_real(c: _Ctx):
    # Настоящая цена — число с ₽/руб (в ценовой области).
    return bool(_PRICE_RE.search(c.price_text)), None


def _d_price_request(c: _Ctx):
    # «Цена по запросу» — товар без цены (в ценовой области).
    return 'по запросу' in c.price_text_lower, None


def _d_tag_tiles(c: _Ctx):
    # Плитка тегов «Часто ищут» — блок ссылок-тегов на популярные подборки.
    present = (
        'часто ищут' in c.text_lower
        or 'tag-cloud' in c.html_lower
        or 'tags-block' in c.html_lower
        or 'popular-tags' in c.html_lower
        or 'seo-tags' in c.html_lower
    )
    return present, None


def _d_btn_cart(c: _Ctx):
    # «В корзину»: СМУ — иконка-корзина (an-ico-basket), текст в <noindex>;
    # ИМП — кнопка add-to-cart-btn; МПЭ — popup_form («Заявка», «Расчитать
    # заказ» открывают форму). Ловим по вёрстке и по тексту.
    present = (
        'card-item-add-to-cart-block' in c.html_lower
        or 'an-ico-basket' in c.html_lower
        or 'add-to-cart-btn' in c.html_lower
        or 'popup_form' in c.html_lower
        or 'в корзину' in c.text_lower
    )
    return present, None


def _d_btn_add_cart(c: _Ctx):
    present = (
        'добавить в корзину' in c.text_lower
        or 'в корзину' in c.text_lower
        or 'card-item-add-to-cart-block' in c.html_lower
        or 'an-ico-basket' in c.html_lower
        or 'add-to-cart-btn' in c.html_lower
        or 'popup_form' in c.html_lower
    )
    return present, None


def _d_btn_oneclick(c: _Ctx):
    # «Купить в один клик»: текст в карточке + класс кнопки one-click.
    present = (
        'в один клик' in c.text_lower
        or 'one-click-to-buy' in c.html_lower
        or 'an-ico-one-click' in c.html_lower
    )
    return present, None


def _d_btn_order_listing(c: _Ctx):
    # Главная коммерческая проверка списка: есть ХОТЯ БЫ ОДНА кнопка заказа.
    # «В корзину» (товар с ценой) и «Купить в один клик» (товар по запросу) —
    # на сайте взаимоисключающие, поэтому обязательна не каждая, а любая из них.
    cart, _ = _d_btn_cart(c)
    one, _ = _d_btn_oneclick(c)
    return (cart or one), None


def _d_btn_order_product(c: _Ctx):
    cart, _ = _d_btn_add_cart(c)
    one, _ = _d_btn_oneclick(c)
    return (cart or one), None


def _d_availability(c: _Ctx):
    return 'в наличии' in c.text_lower, None


def _d_product_cards(c: _Ctx):
    # СМУ — catalog-product-card-item; ИМП — listing__cards / card-product.
    # Запасные маркеры — listing-card / «расчёт стоимости».
    n = c.html_lower.count('catalog-product-card-item')
    if n == 0:
        n = c.html_lower.count('listing-card')
    if n == 0:
        n = c.html_lower.count('listing__cards')   # контейнер выдачи ИМП
    if n == 0:
        n = c.html_lower.count('card-product')      # карточка товара ИМП
    if n == 0:
        n = _count_text(c.text_lower, 'расчёт стоимости')
    return n > 0, n


def _d_filters(c: _Ctx):
    present = (
        'подбор параметров' in c.text_lower
        or c.html_lower.count('<select') >= 2
    )
    return present, None


def _d_sort(c: _Ctx):
    return 'сортировать' in c.text_lower or 'по популярности' in c.text_lower, None


def _d_pagination(c: _Ctx):
    # Bitrix-пагинация даёт ссылки ?PAGEN_x=, либо класс pagination.
    present = (
        'pagen' in c.html_lower
        or 'pagination' in c.html_lower
        or 'data-page' in c.html_lower
    )
    return present, None


def _d_form_not_found(c: _Ctx):
    present = (
        'не нашли что искали' in c.text_lower
        or 'подберем нужную продукцию' in c.text_lower
    )
    return present, None


def _d_reviews(c: _Ctx):
    return 'отзывы клиентов' in c.text_lower, None


def _d_faq(c: _Ctx):
    present = (
        'faqpage' in c.html_lower
        or 'часто задаваемые' in c.text_lower
        or ('вопрос' in c.text_lower and 'ответ' in c.text_lower
            and 'вопрос-ответ' in c.text_lower)
    )
    return present, None


def _d_payment(c: _Ctx):
    return 'способы оплаты' in c.text_lower or 'способ оплаты' in c.text_lower, None


def _d_consultation(c: _Ctx):
    present = 'консультаци' in c.text_lower and 'специалист' in c.text_lower
    return present, None


def _d_found_cheaper(c: _Ctx):
    present = 'нашли дешевле' in c.text_lower or 'отправить ссылку' in c.text_lower
    return present, None


def _d_specs(c: _Ctx):
    present = 'характеристик' in c.text_lower or 'артикул' in c.text_lower
    return present, None


def _d_forms(c: _Ctx):
    n = c.html_lower.count('<form')
    return n > 0, n


def _d_search(c: _Ctx):
    present = ('type="search"' in c.html_lower
               or 'найти' in c.text_lower
               or 'поиск' in c.text_lower)
    return present, None


def _d_seo_text(c: _Ctx):
    # «Есть осмысленный текст-описание»: хотя бы один <p> длиннее 200 символов.
    for m in re.findall(r'<p\b[^>]*>(.*?)</p>', c.html_lower, re.DOTALL):
        plain = re.sub(r'<[^>]+>', '', m).strip()
        if len(plain) > 200:
            return True, None
    return False, None


# ── Описание блока ──────────────────────────────────────────────────


# Что конкретно проверяет каждый детектор. Эти тексты уходят в шапку
# листа «Структура страниц» (комментарий к столбцу) — чтобы у читателя
# отчёта не оставалось вопроса «а что значит этот столбец?».
# Везде проверяется НАЛИЧИЕ блока, не его наполнение.
BLOCK_DESCRIPTIONS = {
    'h1':            'Непустой тег <h1>. Проверяется наличие, не текст. Число = сколько H1 на странице.',
    'breadcrumbs':   'Хлебные крошки: микроразметка BreadcrumbList или класс breadcrumb в вёрстке.',
    'hdr_phone':     'Телефон в шапке: номер +7… внутри региона <header>. Обязателен.',
    'hdr_callback':  'Кнопка «Заказать звонок» (или «обратный звонок») в шапке. Обязательна.',
    'hdr_request':   'Запрос-CTA в шапке: «Оставить заявку» / «Заявка» / «Быстрый заказ». Обязателен.',
    'hdr_city':      'Выбор города в шапке («Город: …», «Ваш город»). Обязателен.',
    'ftr_phone':     'Телефон в подвале: номер +7… внутри региона <footer>. Обязателен.',
    'ftr_email':     'E-mail в подвале (адрес почты). Обязателен.',
    'ftr_writeus':   'Кнопка «Написать нам» в подвале. Обязательна.',
    'ftr_address':   'Адрес в подвале (метка «Адрес» или улица/проспект/шоссе…). Наличие; сверку с КП по городам добавим позже.',
    'h2':            'Количество непустых подзаголовков <h2>. Отсутствие — не баг.',
    'seo_text':      'Текст-описание: хотя бы один абзац <p> длиннее 200 символов.',
    'price':         'Цена в любом виде: число с ₽/руб ИЛИ «по запросу». Если нет ни того ни другого — баг.',
    'price_real':    'Реальная цена: число с ₽ или руб. Информационный столбец — «—» значит на странице только «по запросу».',
    'price_request': '«Цена по запросу» на странице. Информационный столбец, не баг.',
    'btn_order':     'Хотя бы одна кнопка заказа: «В корзину» ИЛИ «Купить в 1 клик» (они взаимоисключающие). Ни одной — баг.',
    'btn_cart':      'Кнопка «В корзину»: вёрстка корзины (an-ico-basket / add-to-cart) или текст «в корзину».',
    'btn_oneclick':  'Кнопка «Купить в 1 клик»: текст «в один клик» или класс one-click.',
    'availability':  'Статус наличия: текст «в наличии» на странице (бейдж на карточках или ссылки «В наличии»).',
    'product_cards': 'Количество карточек товаров в вёрстке листинга. Число = сколько карточек на первой странице.',
    'filters':       'Блок фильтров: «Подбор параметров» или два и более выпадающих списка.',
    'sort':          'Переключатель сортировки: «Сортировать» / «по популярности».',
    'pagination':    'Пагинация: ссылки ?PAGEN_ (Bitrix) или класс pagination. На листинге в одну страницу её нет — это не баг.',
    'tag_tiles':     'Плитка тегов «Часто ищут». Отсутствие — не баг, просто страница не проработана тегами.',
    'form_nf':       'Форма «Не нашли что искали» (есть только на СМУ, на других проектах не требуется).',
    'reviews':       'Блок «Отзывы клиентов».',
    'faq':           'FAQ: микроразметка FAQPage или блок «Часто задаваемые вопросы».',
    'payment':       'Блок «Способы оплаты».',
    'consultation':  'Блок консультации специалиста.',
    'found_cheaper': 'Кнопка «Нашли дешевле» / «Отправить ссылку».',
    'specs':         'Характеристики товара или артикул.',
    'forms':         'Количество тегов <form> на странице.',
    'search':        'Поиск по сайту: поле type="search" или текст «Найти»/«Поиск».',
}


@dataclass
class _Block:
    key: str
    label: str
    required: bool
    detect: Callable[[_Ctx], tuple]


def _b(key, label, required, detect):
    return _Block(key, label, required, detect)


# Шапка (4 обязательных элемента) и подвал (4) — общий набор для переиспользования
_HEADER = [
    _b('hdr_phone',    'Шапка: телефон',         True, _d_hdr_phone),
    _b('hdr_callback', 'Шапка: заказать звонок', True, _d_hdr_callback),
    _b('hdr_request',  'Шапка: оставить заявку', True, _d_hdr_request),
    _b('hdr_city',     'Шапка: город',           True, _d_hdr_city),
]
_FOOTER = [
    _b('ftr_phone',    'Подвал: телефон',      True, _d_ftr_phone),
    _b('ftr_email',    'Подвал: e-mail',       True, _d_ftr_email),
    _b('ftr_writeus',  'Подвал: написать нам', True, _d_ftr_writeus),
    _b('ftr_address',  'Подвал: адрес',        True, _d_ftr_address),
]

# Столбцы расставлены В ПОРЯДКЕ, КАК ИДЁТ НА СТРАНИЦЕ (сверху вниз):
# крошки → H1 → … контент типа страницы … → SEO-текст внизу.
# Шапку/подвал тут НЕ проверяем — это сквозные блоки, их сверяем один раз на
# главной (если сломаны там — сломаны везде; не плодим ошибку на сотни строк).
_TOP = [
    _b('breadcrumbs', 'Хлебные крошки',   True,  _d_breadcrumbs),
    _b('h1',          'Заголовок H1',     True,  _d_h1),
]
_BOTTOM = [
    _b('h2',          'Подзаголовки H2',  False, _d_h2),
    _b('seo_text',    'SEO-текст',        False, _d_seo_text),
]
_BOTTOM_CATALOG = [
    _b('seo_text',    'SEO-текст',        False, _d_seo_text),
]

# ЛИСТИНГ — порядок: фильтры/сортировка над списком, затем карточки с ценой и
# кнопкой, наличие, пагинация под списком, плитка тегов, отзывы/FAQ, форма.
_LISTING = [
    _b('filters',       'Фильтры',                    False, _d_filters),
    _b('sort',          'Сортировка',                 False, _d_sort),
    _b('product_cards', 'Карточки товаров',          True,  _d_product_cards),
    _b('price',         'Цена (есть)',                True,  _d_price),
    _b('price_real',    'Цена в рублях',              False, _d_price_real),
    _b('price_request', 'Цена по запросу',            False, _d_price_request),
    _b('btn_order',     'Кнопка заказа',              True,  _d_btn_order_listing),
    _b('btn_cart',      'Кнопка «В корзину»',         False, _d_btn_cart),
    _b('btn_oneclick',  'Кнопка «Купить в 1 клик»',   False, _d_btn_oneclick),
    _b('availability',  'Наличие',                    False, _d_availability),
    _b('pagination',    'Пагинация',                  False, _d_pagination),
    _b('tag_tiles',     'Плитка тегов (часто ищут)',  False, _d_tag_tiles),
    _b('reviews',       'Отзывы',                     False, _d_reviews),
    _b('faq',           'FAQ',                        False, _d_faq),
    _b('form_nf',       'Форма «Не нашли что искали»', True,  _d_form_not_found),
]

# РАЗДЕЛ — витрина подкатегорий, БЕЗ товаров. Товарные блоки не проверяем вообще.
_SECTION = [
    _b('tag_tiles',     'Плитка тегов (часто ищут)',  False, _d_tag_tiles),
    _b('form_nf',       'Форма «Не нашли что искали»', True,  _d_form_not_found),
]

# ПУСТОЙ РАЗДЕЛ — «Раздел пуст.»: ни товаров, ни подкатегорий → это БАГ.
_EMPTY = [
    _b('product_cards', 'Карточки товаров',           True,  _d_product_cards),
    _b('tag_tiles',     'Плитка тегов (часто ищут)',  False, _d_tag_tiles),
    _b('form_nf',       'Форма «Не нашли что искали»', True,  _d_form_not_found),
]

# Карточка товара — порядок: цена и кнопки сверху (блок покупки), наличие,
# характеристики, оплата, консультация, «нашли дешевле».
_PRODUCT = [
    _b('price',         'Цена (есть)',                True,  _d_price),
    _b('price_real',    'Цена в рублях',              False, _d_price_real),
    _b('price_request', 'Цена по запросу',            False, _d_price_request),
    _b('btn_order',     'Кнопка заказа',              True,  _d_btn_order_product),
    _b('btn_cart',      'Кнопка «В корзину»',         False, _d_btn_add_cart),
    _b('btn_oneclick',  'Кнопка «Купить в 1 клик»',   False, _d_btn_oneclick),
    _b('availability',  'Наличие',                    False, _d_availability),
    _b('specs',         'Характеристики',             False, _d_specs),
    _b('payment',       'Способы оплаты',             False, _d_payment),
    _b('consultation',  'Консультация',               False, _d_consultation),
    _b('found_cheaper', '«Нашли дешевле»',            False, _d_found_cheaper),
]

# КАТАЛОГ-корень — верхний уровень, показывает разделы. Товарных блоков нет.
_CATALOG = [
    _b('tag_tiles',     'Плитка тегов (часто ищут)',  False, _d_tag_tiles),
]

# Главная: шапка (сверху) → H1 → формы/поиск → подвал (снизу). Порядок как на
# странице. Шапка и подвал обязательны, H1 на главной строго не требуем.
_MAIN_PROFILE = [
    *_HEADER,
    _b('h1',     'Заголовок H1', False, _d_h1),
    _b('forms',  'Формы',        False, _d_forms),
    _b('search', 'Поиск',        False, _d_search),
    *_FOOTER,
]


def _profile_for(type_code: str, page_kind: str = '') -> list[_Block]:
    if type_code == 'product':
        return _TOP + _PRODUCT + _BOTTOM
    if type_code in ('category', 'filter'):
        if page_kind == 'listing':
            return _TOP + _LISTING + _BOTTOM
        if page_kind == 'empty':
            return _TOP + _EMPTY + _BOTTOM
        return _TOP + _SECTION + _BOTTOM   # раздел-витрина
    if type_code == 'catalog':
        return _TOP + _CATALOG + _BOTTOM_CATALOG
    if type_code == 'main':
        return _MAIN_PROFILE
    # custom / неизвестный тип — только базовая структура
    return _TOP


# ── Точка входа ─────────────────────────────────────────────────────


def check_content(html: str, type_code: str) -> ContentResult:
    """
    Проверить наличие ожидаемых блоков на странице данного типа.

    html       — сырой HTML страницы (как в http_checker body_text)
    type_code  — 'main' | 'catalog' | 'category' | 'filter' | 'product' | 'custom'
    """
    result = ContentResult(type_code=type_code)
    if not html or not isinstance(html, str):
        return result

    ctx = _Ctx(
        html=html,
        html_lower=html.lower(),
        text=html_to_visible_text(html),
        text_lower='',
    )
    ctx.text_lower = ctx.text.lower()

    # Регионы шапки/подвала: сырой HTML (для телефона/почты — там tel:/mailto:)
    # и видимый текст (для текстовых кнопок и меток). Тег <header>/<footer>,
    # либо приблизительно начало/конец страницы, если тегов нет.
    ctx.header_html = _extract_region(html, 'header', 'top')
    ctx.header_text = html_to_visible_text(ctx.header_html)
    ctx.header_text_lower = ctx.header_text.lower()
    ctx.footer_html = _extract_region(html, 'footer', 'bottom')
    ctx.footer_text = html_to_visible_text(ctx.footer_html)
    ctx.footer_text_lower = ctx.footer_text.lower()

    # «Ценовая» область. На карточке товара цена одна — но внизу есть блок
    # «с этим товаром покупают / похожие», где у чужих карточек бывает
    # «Цена по запросу». Чтобы это не примешивалось к цене самого товара,
    # на product берём текст ДО первого блока рекомендаций. На остальных
    # типах (листинг и т.д.) ценовая область = весь текст.
    ctx.price_text = ctx.text
    ctx.price_text_lower = ctx.text_lower
    if type_code == 'product':
        _related = (
            'с этим товаром', 'с этими товарами', 'с этим покупают',
            'похожие товар', 'похожие предложения', 'сопутствующ',
            'рекомендуем', 'рекомендованные', 'смотрите также',
            'вместе с этим', 'аналогичные товар', 'вам может понадоб',
            'с этим часто',
        )
        cut = len(ctx.text_lower)
        for _m in _related:
            i = ctx.text_lower.find(_m)
            if 0 <= i < cut:
                cut = i
        ctx.price_text = ctx.text[:cut]
        ctx.price_text_lower = ctx.text_lower[:cut]

    # Подтип страницы-списка (категория / тег) — определяем по вёрстке:
    #   listing — есть карточки товаров (catalog-product-card-item) → строгая
    #             проверка товарных блоков;
    #   section — есть вкладки/поиск по подкатегориям (catalog-cat-tabs /
    #             tab-search): это раздел-витрина (Бронза, Капролон, Арматура,
    #             Рельсы ведут в подкатегории) → товарные блоки не обязательны;
    #   empty   — «Раздел пуст.» и нет ни товаров, ни подкатегорий → это БАГ
    #             («Карточки товаров» остаётся обязательной и загорится красным).
    page_kind = ''
    if type_code in ('category', 'filter'):
        has_cards = (
            'catalog-product-card-item' in ctx.html_lower
            or 'listing-card' in ctx.html_lower
            or 'listing__cards' in ctx.html_lower      # листинг ИМП
        )
        has_subcats = (
            'catalog-cat-tabs' in ctx.html_lower
            or 'tab-search' in ctx.html_lower
        )
        is_empty = 'раздел пуст' in ctx.text_lower
        if has_cards:
            page_kind = 'listing'
        elif is_empty:
            page_kind = 'empty'
        elif has_subcats:
            page_kind = 'section'
        else:
            page_kind = 'section'   # нет явных признаков товаров — мягко, не баг
    result.page_kind = page_kind

    # Soft-404: страница отдала 200, но по контенту это «страница не найдена».
    # Тогда «нет цены/кнопок» — следствие, а не суть; в отчёте пишем «404».
    _404_MARKERS = (
        'страница не найдена', 'страница, которую вы ищете', 'ошибка 404',
        '404 ошибка', 'page not found', 'такой страницы не существует',
        'нет такой страницы', 'запрашиваемая страница не найдена',
    )
    title = ''
    mt = re.search(r'<title[^>]*>(.*?)</title>', ctx.html, re.IGNORECASE | re.DOTALL)
    if mt:
        title = re.sub(r'<[^>]+>', '', mt.group(1)).lower()
    h1_text = ''
    mh = re.search(r'<h1[^>]*>(.*?)</h1>', ctx.html_lower, re.DOTALL)
    if mh:
        h1_text = re.sub(r'<[^>]+>', '', mh.group(1))
    result.is_soft_404 = (
        any(m in ctx.text_lower for m in _404_MARKERS)
        or '404' in title or 'не найден' in title
        or '404' in h1_text or 'не найден' in h1_text
    )

    # Проект по хосту в HTML — у каждого свой набор элементов шапки/подвала/форм.
    # Чего у проекта нет ПО ДИЗАЙНУ — не проверяем и не выводим столбцом
    # (иначе ложный баг: у ИМП/МПЭ нет «Заказать звонок», у МПЭ — «Написать нам»,
    # форма «Не нашли что искали» есть только у СМУ).
    if 'stalmetural' in ctx.html_lower:
        absent = set()
    elif 'inmetprom' in ctx.html_lower:
        absent = {'hdr_callback', 'form_nf'}
    elif 'mepen' in ctx.html_lower:
        absent = {'hdr_callback', 'ftr_writeus', 'form_nf'}
    else:
        absent = {'form_nf'}

    for blk in _profile_for(type_code, page_kind):
        if blk.key in absent:
            continue        # этого элемента у проекта нет по дизайну — не показываем
        try:
            present, count = blk.detect(ctx)
        except Exception:
            present, count = False, None
        required = blk.required
        # Каталог-корень — верхний уровень иерархии, хлебных крошек там
        # может не быть (например, главная каталога ИМП) — это не баг.
        if type_code == 'catalog' and blk.key == 'breadcrumbs':
            required = False
        result.blocks.append(BlockResult(
            key=blk.key,
            label=blk.label,
            required=required,
            present=bool(present),
            count=count,
            description=BLOCK_DESCRIPTIONS.get(blk.key, ''),
        ))

    return result
