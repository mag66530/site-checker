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
from html.parser import HTMLParser
from typing import Optional, Callable

from text_checker import html_to_visible_text


# ── Вырезание скрытого/отключённого (то, чего покупатель НЕ видит) ───
# Цена/кнопка/наличие, спрятанные через disabled / display:none / hidden /
# visibility:hidden — хоть в атрибуте style, хоть правилом из CSS-файла —
# для покупателя всё равно что отсутствуют. Поэтому такие поддеревья
# выкидываем и считаем контент только по видимой части.
#
# CSS-правила (display:none и т.п.) приходят из подключённых на странице
# стилей — их подгружает раннер и передаёт сюда уже разобранными
# (parse_hidden_selectors). Так мы ловим «цена есть в коде, но скрыта стилями».

# Классы, которые ПО КОНВЕНЦИИ всегда означают «визуально скрыто» (фреймворки).
# Сюда НЕ кладём «disabled»: класс с таким именем сплошь и рядом всего лишь
# смысловой маркер (напр. «card-item-add-no-cart-block disabled» — вариант
# блока БЕЗ корзины), а сама кнопка «Купить в один клик» при этом видна. Что
# реально скрыто — решаем по CSS (display:none и т.п.), который мы читаем.
_HIDDEN_CLASSES = {
    'd-none', 'hidden', 'is-hidden', 'invisible',
    'sr-only', 'visually-hidden', 'visuallyhidden',
}
_VOID_TAGS = {
    'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'area',
    'base', 'col', 'embed', 'param', 'track', 'wbr',
}

# Сигналы «элемент невидим» в теле CSS-правила.
_RE_OPACITY0 = re.compile(r'opacity:0(?![.\d])')


def _decl_hides(decl: str) -> bool:
    """Делает ли блок объявлений элемент невидимым (display:none и т.п.)."""
    d = decl.lower().replace(' ', '').replace('\n', '').replace('\t', '')
    return ('display:none' in d or 'visibility:hidden' in d
            or bool(_RE_OPACITY0.search(d)))


def _compile_compound(c: str):
    """Простой селектор → (tag, frozenset(classes), id, attr-tests) | None.

    Поддерживаем только то, что встречается в правилах скрытия: тег, .класс,
    #id и [class*=/^=/$=/=...]. Всё прочее (псевдо, хитрые атрибуты) → None,
    т.е. правило пропускаем (консервативно, чтобы не спрятать лишнее)."""
    c = c.strip()
    if not c:
        return None
    tag = ''
    classes = set()
    cid = ''
    attrs = []
    i, n = 0, len(c)
    m = re.match(r'[a-zA-Z][\w-]*|\*', c)
    if m:
        t = m.group(0)
        tag = '' if t == '*' else t.lower()
        i = m.end()
    while i < n:
        ch = c[i]
        if ch == '.':
            m = re.match(r'\.([\w-]+)', c[i:])
            if not m:
                return None
            classes.add(m.group(1).lower())
            i += m.end()
        elif ch == '#':
            m = re.match(r'#([\w-]+)', c[i:])
            if not m:
                return None
            cid = m.group(1).lower()
            i += m.end()
        elif ch == '[':
            m = re.match(r'\[\s*class\s*([*^$|~]?)=\s*["\']?([^"\'\]]+)["\']?\s*\]',
                         c[i:], re.I)
            if not m:
                return None
            attrs.append((m.group(1) or '=', m.group(2).lower()))
            i += m.end()
        else:
            return None
    return (tag, frozenset(classes), cid, tuple(attrs))


def _compile_selector(sel: str):
    """Полный селектор → кортеж простых (от внешнего к ключевому) | None."""
    sel = sel.strip()
    if not sel or ':' in sel or '+' in sel or '~' in sel:
        return None              # псевдо/соседние — пропускаем (консервативно)
    comps = []
    for part in sel.replace('>', ' ').split():
        cc = _compile_compound(part)
        if cc is None:
            return None
        comps.append(cc)
    return tuple(comps) if comps else None


def parse_hidden_selectors(css_text: str) -> tuple:
    """CSS-текст → кортеж разобранных селекторов, которые ПРЯЧУТ элемент.

    Берём только правила верхнего уровня (внутрь @media/@keyframes не лезем —
    мобильные/анимационные скрытия для десктоп-проверки не применяем)."""
    if not css_text:
        return ()
    css = re.sub(r'/\*.*?\*/', '', css_text, flags=re.S)
    sels = []
    idx, n, buf = 0, len(css), []
    while idx < n:
        ch = css[idx]
        if ch == '{':
            prelude = ''.join(buf).strip()
            buf = []
            depth, idx = 1, idx + 1
            dstart = idx
            while idx < n and depth > 0:
                if css[idx] == '{':
                    depth += 1
                elif css[idx] == '}':
                    depth -= 1
                if depth > 0:
                    idx += 1
            block = css[dstart:idx]
            idx += 1
            if prelude.startswith('@'):
                continue              # @media/@keyframes/@font-face — мимо
            if _decl_hides(block):
                for s in prelude.split(','):
                    cs = _compile_selector(s)
                    if cs:
                        sels.append(cs)
        else:
            buf.append(ch)
            idx += 1
    return tuple(sels)


def _compound_matches(comp, el) -> bool:
    tag, classes, cid, attrs = comp
    etag, eclasses, eid, eraw = el
    if tag and tag != etag:
        return False
    if cid and cid != eid:
        return False
    if classes and not classes <= eclasses:
        return False
    for op, val in attrs:
        if op == '*' or op == '~' or op == '|':
            if val not in eraw:
                return False
        elif op == '^':
            if not eraw.startswith(val):
                return False
        elif op == '$':
            if not eraw.endswith(val):
                return False
        else:                          # '='
            if eraw != val:
                return False
    return True


def _selector_matches(sel, el, stack) -> bool:
    if not _compound_matches(sel[-1], el):
        return False
    ai = len(stack) - 1
    for comp in reversed(sel[:-1]):
        ok = False
        while ai >= 0:
            hit = _compound_matches(comp, stack[ai])
            ai -= 1
            if hit:
                ok = True
                break
        if not ok:
            return False
    return True


def _build_hidden_index(selectors):
    """Индекс селекторов по ключевому классу/id для быстрого матчинга."""
    by_class, by_id, fallback = {}, {}, []
    for sel in selectors:
        _, kclasses, kid, _ = sel[-1]
        if kclasses:
            by_class.setdefault(next(iter(kclasses)), []).append(sel)
        elif kid:
            by_id.setdefault(kid, []).append(sel)
        else:
            fallback.append(sel)
    return by_class, by_id, fallback


class _VisibleHTML(HTMLParser):
    """Собирает HTML только из ВИДИМЫХ элементов (скрытые поддеревья — мимо)."""

    def __init__(self, hidden_index=None):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.index = hidden_index           # (by_class, by_id, fallback) | None
        self.stack: list = []               # видимые предки (для CSS-матчинга)

    @staticmethod
    def _inline_hidden(attrs) -> bool:
        d = {k: (v or '') for k, v in attrs}
        if 'hidden' in d:
            return True
        style = d.get('style', '').lower().replace(' ', '')
        if ('display:none' in style or 'visibility:hidden' in style
                or _RE_OPACITY0.search(style)):
            return True
        cls = set(d.get('class', '').lower().split())
        return bool(cls & _HIDDEN_CLASSES)

    def _css_hidden(self, el) -> bool:
        if not self.index:
            return False
        by_class, by_id, fallback = self.index
        cands = fallback
        if el[2] and el[2] in by_id:
            cands = cands + by_id[el[2]]
        for c in el[1]:
            lst = by_class.get(c)
            if lst:
                cands = cands + lst
        for sel in cands:
            if _selector_matches(sel, el, self.stack):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag not in _VOID_TAGS:
                self.skip += 1
            return
        if tag in ('script', 'style', 'noscript', 'template'):
            self.skip = 1
            return
        d = dict(attrs)
        raw_class = (d.get('class') or '').lower()
        el = (tag, frozenset(raw_class.split()), (d.get('id') or '').lower(), raw_class)
        if self._inline_hidden(attrs) or self._css_hidden(el):
            if tag not in _VOID_TAGS:
                self.skip = 1
            return
        self.parts.append(f'<{tag} class="{d.get("class", "") or ""}">')
        if tag not in _VOID_TAGS:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        if self.skip:
            return
        d = dict(attrs)
        raw_class = (d.get('class') or '').lower()
        el = (tag, frozenset(raw_class.split()), (d.get('id') or '').lower(), raw_class)
        if self._inline_hidden(attrs) or self._css_hidden(el):
            return
        self.parts.append(f'<{tag}>')

    def handle_endtag(self, tag):
        if self.skip:
            if tag not in _VOID_TAGS:
                self.skip -= 1
            return
        self.parts.append(f'</{tag}>')
        if tag not in _VOID_TAGS and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _strip_hidden(html: str, css_hidden: tuple = ()) -> str:
    """HTML → только видимая часть.

    Убираем поддеревья, скрытые: атрибутом (hidden / style=display:none),
    классом из списка скрывающих, ИЛИ правилом CSS (css_hidden — селекторы
    из подключённых стилей + из <style> самой страницы)."""
    try:
        inline = ''
        for m in re.findall(r'<style[^>]*>(.*?)</style>', html, re.S | re.I):
            inline += '\n' + m
        all_sels = tuple(css_hidden) + parse_hidden_selectors(inline)
        index = _build_hidden_index(all_sels) if all_sels else None
        p = _VisibleHTML(index)
        p.feed(html)
        return ''.join(p.parts)
    except Exception:
        return html


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
    # «Видимая» часть страницы (без disabled/скрытых блоков) — для цены и
    # кнопок: то, что покупатель реально видит. Скрытая цена/кнопка = её нет.
    vis_html_lower: str = ''
    vis_text_lower: str = ''
    # Нижние блоки карточки товара («С этим товаром покупают», «Похожие»,
    # «Вас также может заинтересовать») — видимая часть от первого такого блока.
    rec_html_lower: str = ''
    rec_text_lower: str = ''


# ── Детекторы. Возвращают (present: bool, count: Optional[int]) ──────


def _d_h1(c: _Ctx):
    n = _count_tag(c.html_lower, 'h1')
    return n > 0, n


def _d_h2(c: _Ctx):
    n = _count_tag(c.html_lower, 'h2')
    return n > 0, n


# Крошки ищем ТОЛЬКО в реальной разметке элемента (class/id/aria/itemtype), а
# не где попало: иначе слово breadcrumb из href подключённого стиля
# (/bitrix/.../breadcrumb/.../style.css) даёт ложный «✓», даже когда самих
# крошек на странице нет (их «вшили» в H1). Так было — коллега это поймала.
_RE_BREADCRUMB = re.compile(r'(?:class|id|aria-label|itemtype)="[^"]*breadcrumb', re.I)


def _d_breadcrumbs(c: _Ctx):
    # Микроразметка BreadcrumbList или класс/атрибут breadcrumb на реальном
    # элементе — практически универсальный признак хлебных крошек.
    return bool(_RE_BREADCRUMB.search(c.html_lower)), None


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
        'card-item-add-to-cart-block' in c.vis_html_lower
        or 'an-ico-basket' in c.vis_html_lower
        or 'add-to-cart-btn' in c.vis_html_lower
        or 'popup_form' in c.vis_html_lower
        or 'в корзину' in c.vis_text_lower
    )
    return present, None


def _d_btn_add_cart(c: _Ctx):
    present = (
        'добавить в корзину' in c.vis_text_lower
        or 'в корзину' in c.vis_text_lower
        or 'card-item-add-to-cart-block' in c.vis_html_lower
        or 'an-ico-basket' in c.vis_html_lower
        or 'add-to-cart-btn' in c.vis_html_lower
        or 'popup_form' in c.vis_html_lower
    )
    return present, None


def _d_btn_oneclick(c: _Ctx):
    # «Купить в один клик»: текст в карточке + класс кнопки one-click.
    present = (
        'в один клик' in c.vis_text_lower
        or 'one-click-to-buy' in c.vis_html_lower
        or 'an-ico-one-click' in c.vis_html_lower
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
    # По видимому тексту: скрытый стилями статус наличия покупатель не видит.
    return 'в наличии' in c.vis_text_lower, None


# Карточка товара МПЭ — отдельный шаблон: <div itemtype="schema.org/Product"
# class="card-item ">. Маркер «class="card-item"» точный: на СМУ карточки
# зовутся catalog-product-card-item (класс начинается с catalog-), под-классы
# card-item-name/-img идут через дефис — под этот маркер не попадают; на
# разделах-витринах и карточке товара МПЭ его нет.
_RE_MPE_CARD = re.compile(r'class="card-item[ "]')


def _d_product_cards(c: _Ctx):
    # СМУ — catalog-product-card-item; ИМП — listing__cards / card-product;
    # МПЭ — card-item. Запасные маркеры — listing-card / «расчёт стоимости».
    n = c.html_lower.count('catalog-product-card-item')
    if n == 0:
        n = c.html_lower.count('listing-card')
    if n == 0:
        n = c.html_lower.count('listing__cards')   # контейнер выдачи ИМП
    if n == 0:
        n = c.html_lower.count('card-product')      # карточка товара ИМП
    if n == 0:
        n = len(_RE_MPE_CARD.findall(c.html_lower))  # карточка товара МПЭ
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


def _d_rec_block(c: _Ctx):
    # Нижние блоки карточки: «С этим товаром покупают», «Похожие»,
    # «Вас также может заинтересовать» и т.п. Справочно — есть/нет.
    return bool(c.rec_html_lower), None


def _d_rec_price(c: _Ctx):
    # У товаров в нижних блоках должна быть видимая цена (₽ или «по запросу»).
    # Если карточки снизу есть, а цены не видно — тот самый баг с пустыми ценами.
    rh = c.rec_html_lower
    if not rh:
        return True, None                 # нижнего блока нет — не баг
    has_cards = ('catalog-product-card-item' in rh or 'card-product' in rh
                 or 'listing-card' in rh or bool(_RE_MPE_CARD.search(rh)))
    if not has_cards:
        return True, None                 # не товарный блок — не баг
    has_price = bool(_PRICE_RE.search(c.rec_text_lower)) or 'по запросу' in c.rec_text_lower
    return has_price, None


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
    'rec_block':     'Нижние блоки карточки: «С этим товаром покупают», «Похожие», «Вас также может заинтересовать». Справочно — есть/нет.',
    'rec_price':     'У товаров в нижних блоках есть видимая цена (₽ или «по запросу»). Если карточки снизу есть, а цены не видно — баг (пустые цены снизу).',
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
    # Нижние блоки карточки — после основного товара («сначала карточка, потом
    # что ниже»): сам блок (справочно) + есть ли у его товаров цена.
    _b('rec_block',     'Блок «похожие / с этим покупают»', False, _d_rec_block),
    _b('rec_price',     'Цены в нижних блоках',        True,  _d_rec_price),
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
    if type_code == 'tech':
        return []        # тех. страницы проверяем на доступность (404/ошибки), без структуры
    # custom / неизвестный тип — только базовая структура
    return _TOP


# ── Точка входа ─────────────────────────────────────────────────────


def check_content(html: str, type_code: str, css_hidden: tuple = ()) -> ContentResult:
    """
    Проверить наличие ожидаемых блоков на странице данного типа.

    html       — сырой HTML страницы (как в http_checker body_text)
    type_code  — 'main' | 'catalog' | 'category' | 'filter' | 'product' | 'custom'
    css_hidden — разобранные селекторы скрытия из подключённых стилей
                 (parse_hidden_selectors). Нужны, чтобы цена/кнопка, спрятанные
                 правилом display:none из CSS-файла, считались невидимыми.
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

    # Видимая часть страницы (без disabled/скрытых блоков) — то, что реально
    # видит покупатель. Цена/кнопка считаются ТОЛЬКО по ней: скрытая или
    # отключённая цена/кнопка для покупателя всё равно что отсутствует.
    visible_html = _strip_hidden(html, css_hidden)
    visible_text = html_to_visible_text(visible_html)
    ctx.vis_html_lower = visible_html.lower()
    ctx.vis_text_lower = visible_text.lower()

    # «Ценовая» область (по видимому тексту). На карточке товара цена одна —
    # но внизу есть блок «с этим товаром покупают / похожие», где у чужих
    # карточек бывает «Цена по запросу». Чтобы это не примешивалось к цене
    # самого товара, на product берём текст ДО первого блока рекомендаций.
    ctx.price_text = visible_text
    ctx.price_text_lower = ctx.vis_text_lower
    if type_code == 'product':
        _related = (
            'с этим товаром', 'с этими товарами', 'с этим покупают',
            'похожие товар', 'похожие предложения', 'сопутствующ',
            'рекомендуем', 'рекомендованные', 'смотрите также',
            'вместе с этим', 'аналогичные товар', 'вам может понадоб',
            'с этим часто', 'вас также', 'также может заинтерес',
            'вам может понравиться', 'с этим товаром также',
        )
        cut = len(ctx.vis_text_lower)
        for _m in _related:
            i = ctx.vis_text_lower.find(_m)
            if 0 <= i < cut:
                cut = i
        ctx.price_text = visible_text[:cut]
        ctx.price_text_lower = ctx.vis_text_lower[:cut]
        # Область нижних блоков (рекомендации) — от первого такого маркера до
        # конца. Считаем по ней наличие блока и цены у нижних карточек.
        ctx.rec_text_lower = ctx.vis_text_lower[cut:] if cut < len(ctx.vis_text_lower) else ''
        hcut = len(ctx.vis_html_lower)
        for _m in _related:
            j = ctx.vis_html_lower.find(_m)
            if 0 <= j < hcut:
                hcut = j
        ctx.rec_html_lower = ctx.vis_html_lower[hcut:] if hcut < len(ctx.vis_html_lower) else ''

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
            or bool(_RE_MPE_CARD.search(ctx.html_lower))   # листинг МПЭ
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

    # Для пояснения «почему нет»: было ли это в коде (включая скрытое), но
    # покупатель не видит. ctx.text/html_lower — весь код; видимость уже учтена
    # в детекторах (они смотрят vis_*). Если в коде есть, а present=False —
    # значит скрыто/отключено.
    raw_has_price = bool(_PRICE_RE.search(ctx.text)) or 'по запросу' in ctx.text_lower
    raw_has_btn = (
        'card-item-add-to-cart-block' in ctx.html_lower
        or 'card-item-add-no-cart-block' in ctx.html_lower
        or 'an-ico-basket' in ctx.html_lower or 'add-to-cart-btn' in ctx.html_lower
        or 'one-click-to-buy' in ctx.html_lower or 'an-ico-one-click' in ctx.html_lower
        or 'popup_form' in ctx.html_lower
        or 'в корзину' in ctx.text_lower or 'в один клик' in ctx.text_lower
    )

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
        # Пояснение к багу цены/кнопки: «есть в коде, но покупатель не видит»
        # (скрыто стилями display:none / disabled) vs просто «нет в коде».
        note = ''
        if required and not present:
            if blk.key == 'price' and raw_has_price:
                note = 'в коде есть, но покупатель не видит (скрыто стилями/disabled)'
            elif blk.key == 'btn_order' and raw_has_btn:
                note = 'в коде есть, но покупатель не видит (скрыто стилями/disabled)'
        result.blocks.append(BlockResult(
            key=blk.key,
            label=blk.label,
            required=required,
            present=bool(present),
            count=count,
            description=BLOCK_DESCRIPTIONS.get(blk.key, ''),
            note=note,
        ))

    return result
