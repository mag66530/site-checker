"""
schema_checker.py - микроразметка Schema.org и OpenGraph (пункт 1.12, ТЗ 3.5).

ТЗ 3.5.1 - OpenGraph на основных типах страниц: og:url, og:title,
og:description, og:image, og:type. Отсутствие поля = баг.

Проверяем не только наличие тега, но и ПРИГОДНОСТЬ значения (check_og):
  • значение пустое - тег есть, а толку нет (типовая поломка шаблона: тег
    в вёрстке стоит, переменная не подставилась) = баг;
  • og:url - абсолютный адрес и ведёт на эту же страницу;
  • og:image - абсолютный адрес, не data: (соцсети такое не покажут);
  • og:description - не длиннее 300 символов (обрежется в анонсе);
  • og:type - из известных значений OpenGraph.

ТЗ 3.5.2 - Schema.org. Формат: microdata (itemtype/itemprop) - основной,
JSON-LD - допустим, но «по обстоятельствам»: тип найден ТОЛЬКО в JSON-LD =
предупреждение, нет нигде = баг. Требования по типам страниц:
  • BreadcrumbList - везде, где есть вложенность (не главная)  [3.5.2.1]
  • Organization (или подтип LocalBusiness) - на всех страницах [3.5.2.2]
  • Product - на карточке товара                               [3.5.2.3]
  • Разметка листинга - OfferCatalog ПО ТЗ, но фактически на
    проектах листинги размечены ItemList/CollectionPage -
    принимаем любой из трёх                                    [3.5.2.4]
  • Фото размечены - itemprop="image" / ImageObject            [3.5.2.5]
  • Цены размечены - PriceSpecification/Offer/itemprop="price";
    отсутствие - ПРЕДУПРЕЖДЕНИЕ (товары «по запросу» без цены)  [3.5.2.6]
  • Характеристики - PropertyValue на товаре                   [3.5.2.7]

Плюс валидация ОБЯЗАТЕЛЬНЫХ ПОЛЕЙ по каждому типу (_validate_fields):
разбираем дерево microdata (свой парсер на stdlib) и JSON-LD-объекты и
проверяем поля внутри объекта - Product без name/offers/image, Offer без
price/priceCurrency, BreadcrumbList без itemListElement и т.п. Нет
критичного поля = баг, нет желательного = предупреждение. Это то, что
раньше показывали только внешние валидаторы.

Внешние валидаторы Яндекса/Google из ТЗ (validator.schema.org и пр.) -
ручные, у них нет пригодного API; полную «как поисковик» проверку каждого
значения они дают, здесь - наличие типов, обязательных полей и формат.
"""
import json
import re
from html.parser import HTMLParser
from typing import Optional

_RE_OG = re.compile(
    r'<meta\b[^>]*property\s*=\s*["\']og:(\w+)["\'][^>]*>', re.I)
_RE_OG_TAG = re.compile(r'<meta\b[^>]*>', re.I)
_RE_OG_PROP = re.compile(r'property\s*=\s*["\']og:(\w+)["\']', re.I)
_RE_OG_CONTENT = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.I)

# Длина og:description по чек-листу.
OG_DESC_MAX = 300

# Допустимые значения og:type. Список Open Graph: базовые + пространства имён
# (article, book, profile, video.*, music.*). Незнакомое значение - не ошибка
# (стандарт разрешает свои типы), но повод посмотреть глазами.
OG_TYPES = {
    'website', 'article', 'book', 'profile', 'product', 'product.group',
    'product.item', 'video.movie', 'video.episode', 'video.tv_show',
    'video.other', 'music.song', 'music.album', 'music.playlist',
    'music.radio_station',
}
_RE_ITEMTYPE = re.compile(
    r'itemtype\s*=\s*["\']https?://schema\.org/(\w+)', re.I)
_RE_ITEMPROP = re.compile(r'itemprop\s*=\s*["\'](\w+)["\']', re.I)
_RE_JSONLD = re.compile(
    r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)'
    r'</script>', re.I | re.S)

OG_REQUIRED = ('url', 'title', 'description', 'image', 'type')

# Эквиваленты: подтипы засчитываются за требуемый тип
_ORG_TYPES = {'Organization', 'LocalBusiness', 'Corporation', 'Store'}
_LISTING_TYPES = {'OfferCatalog', 'ItemList', 'CollectionPage'}
_PRICE_TYPES = {'PriceSpecification', 'UnitPriceSpecification', 'Offer',
                'AggregateOffer'}
_IMAGE_TYPES = {'ImageObject', 'ImageGallery'}

# На каких типах страниц что ОБЯЗАТЕЛЬНО (баг) и что ЖЕЛАТЕЛЬНО (предупр.)
_SEO_TYPES = ('main', 'catalog', 'category', 'filter', 'product')


def parse_og(html: str) -> dict:
    """{свойство: значение} по OpenGraph-тегам страницы.

    Читаем ИМЕННО значение (content), а не только факт наличия тега: пустой
    «<meta property="og:title" content="">» раньше засчитывался как
    заполненный, а на шаблонных сайтах это типовая ситуация - тег в шаблоне
    есть, переменная не подставилась.

    Повторные теги: берём ПЕРВОЕ непустое значение (og:image часто дублируют
    для разных размеров). ЧИСТАЯ функция - есть юнит-тест.
    """
    out: dict = {}
    for tag in _RE_OG_TAG.findall(html or ''):
        mp = _RE_OG_PROP.search(tag)
        if not mp:
            continue
        prop = mp.group(1).lower()
        mc = _RE_OG_CONTENT.search(tag)
        val = (mc.group(1) if mc else '').strip()
        if prop not in out or (not out[prop] and val):
            out[prop] = val
    return out


def _abs_url(v: str) -> bool:
    """Адрес абсолютный (со схемой или «//host»)."""
    s = (v or '').strip()
    return bool(s) and (s.startswith('//') or bool(re.match(r'[a-z][a-z0-9+.\-]*:', s, re.I)))


def check_og(og: dict, page_url: str = '') -> tuple:
    """Проверка ЗНАЧЕНИЙ OpenGraph. → (issues, warnings).

    Раньше проверялось только наличие пяти тегов. Здесь - что они пригодны:
    непустые, адреса абсолютные, og:url ведёт на эту же страницу, описание не
    длиннее 300 символов, тип из известных.
    ЧИСТАЯ функция - есть юнит-тест."""
    issues, warnings = [], []
    og = og or {}

    # Пустое значение при наличии тега - тег бесполезен. Отсутствие тега
    # ловится отдельно (og_missing), чтобы не выписывать две находки разом.
    for f in OG_REQUIRED:
        if f in og and not og[f]:
            issues.append(f'OpenGraph-тег og:{f} пустой - значение не подставилось')

    url_v = og.get('url') or ''
    if url_v:
        if not _abs_url(url_v):
            issues.append('og:url - относительный адрес, нужен полный '
                          '(со схемой и доменом)')
        elif page_url:
            def _norm(u):
                from urllib.parse import urlsplit
                sp = urlsplit(u if _abs_url(u) else 'https://' + u.lstrip('/'))
                host = (sp.netloc or '').lower().removeprefix('www.')
                return host + (sp.path or '/').rstrip('/')
            try:
                if _norm(url_v) != _norm(page_url):
                    warnings.append('og:url ведёт не на эту страницу - в анонсе '
                                    'соцсети покажут другой адрес')
            except Exception:      # noqa: BLE001
                pass

    img = og.get('image') or ''
    if img:
        if img.lower().startswith('data:'):
            issues.append('og:image встроен как data: - соцсети такую картинку '
                          'не покажут, нужна ссылка на файл')
        elif not _abs_url(img):
            issues.append('og:image - относительный адрес, нужен полный '
                          '(со схемой и доменом)')

    desc = og.get('description') or ''
    if desc and len(desc) > OG_DESC_MAX:
        warnings.append(f'og:description длиннее {OG_DESC_MAX} символов - '
                        f'в анонсе он обрежется')

    typ = (og.get('type') or '').strip().lower()
    if typ and typ not in OG_TYPES:
        warnings.append(f'og:type - незнакомое значение «{typ}»: обычно это '
                        f'website, article или product')

    return issues, warnings


def _jsonld_types(html: str) -> set:
    """Все @type из JSON-LD блоков (рекурсивно)."""
    types = set()
    for m in _RE_JSONLD.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                t = x.get('@type')
                if isinstance(t, str):
                    types.add(t)
                elif isinstance(t, list):
                    types.update(str(i) for i in t)
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return types


# ── Разбор объектов разметки (для проверки обязательных полей) ───────
# Без внешних зависимостей: microdata - свой парсер на stdlib html.parser
# (строит дерево itemscope/itemprop), JSON-LD - через json. Нужно, чтобы
# проверять НАЛИЧИЕ ПОЛЕЙ внутри конкретного объекта (Product.offers.price
# и т.п.), а не просто «где-то на странице есть itemprop=price».

_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'param', 'source', 'track', 'wbr'}


def _type_seg(itemtype: str) -> str:
    """Последний сегмент itemtype: https://schema.org/Product → Product."""
    seg = (itemtype or '').strip().split()[0] if itemtype else ''
    return re.sub(r'.*/', '', seg)


class _MicrodataParser(HTMLParser):
    """Строит дерево microdata-объектов: [{'type', 'props'}], где props -
    {имя: [значения|вложенные объекты]}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.roots = []
        self._stack = []          # [{'item', 'depth'}]
        self._depth = 0
        self._txt = None          # (item, prop, depth) - захват текста
        self._buf = []

    def _open(self, tag, attrs, void):
        a = dict(attrs)
        itemprop = a.get('itemprop')
        if 'itemscope' in a:
            item = {'type': _type_seg(a.get('itemtype')), 'props': {}}
            if self._stack and itemprop:
                self._stack[-1]['item']['props'].setdefault(
                    itemprop, []).append(item)
            else:
                self.roots.append(item)
            if not void:
                self._stack.append({'item': item, 'depth': self._depth})
        elif itemprop and self._stack:
            parent = self._stack[-1]['item']
            val = None
            if tag == 'meta':
                val = a.get('content', '')
            elif tag in ('a', 'link', 'area'):
                val = a.get('href', '')
            elif tag in ('img', 'audio', 'video', 'source', 'iframe', 'embed'):
                val = a.get('src', '')
            elif tag == 'object':
                val = a.get('data', '')
            elif tag == 'time':
                val = a.get('datetime')
            if val is not None:
                parent['props'].setdefault(itemprop, []).append((val or '').strip())
            else:
                self._txt = (parent, itemprop, self._depth)
                self._buf = []
        if not void:
            self._depth += 1

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, tag in _VOID)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, True)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        self._depth -= 1
        while self._stack and self._stack[-1]['depth'] >= self._depth:
            self._stack.pop()
        if self._txt and self._depth <= self._txt[2]:
            parent, prop, _ = self._txt
            txt = ' '.join(''.join(self._buf).split())
            if txt:
                parent['props'].setdefault(prop, []).append(txt)
            self._txt = None
            self._buf = []

    def handle_data(self, data):
        if self._txt:
            self._buf.append(data)


def _microdata_objects(html: str) -> list:
    p = _MicrodataParser()
    try:
        p.feed(html or '')
    except Exception:
        pass
    return p.roots


def _jsonld_objects(html: str) -> list:
    """Типизированные объекты (с @type) из всех JSON-LD блоков, рекурсивно.
    Возвращает [{'type', 'props'}] в том же виде, что и microdata."""
    out = []

    def _norm(node):
        if isinstance(node, dict):
            t = node.get('@type')
            typ = ''
            if isinstance(t, str):
                typ = t
            elif isinstance(t, list) and t:
                typ = str(t[0])
            if typ:
                props = {k: (v if isinstance(v, list) else [v])
                         for k, v in node.items() if not k.startswith('@')}
                out.append({'type': _type_seg(typ), 'props': props})
            for v in node.values():
                _norm(v)
        elif isinstance(node, list):
            for x in node:
                _norm(x)

    for m in _RE_JSONLD.finditer(html or ''):
        try:
            _norm(json.loads(m.group(1).strip()))
        except Exception:
            continue
    return out


def _walk_objects(items):
    """Все объекты рекурсивно (включая вложенные в props)."""
    for it in items:
        yield it
        for vals in it.get('props', {}).values():
            for x in vals:
                if isinstance(x, dict):
                    yield from _walk_objects([x])


# Обязательные (баг) и желательные (предупр.) поля по типам. Значение -
# список групп «любое из»: поле засчитано, если есть хотя бы один вариант.
# req - критично для сниппета (нет = баг), rec - желательно (нет = предупр.).
_FIELD_RULES = {
    'Product': {'req': [('название', ('name',)),
                        ('предложение/цена', ('offers', 'price')),
                        ('изображение', ('image',))],
                'rec': [('описание', ('description',))]},
    'Offer': {'req': [('цена', ('price', 'priceSpecification', 'lowPrice')),
                      ('валюта', ('priceCurrency',))],
              'rec': [('наличие', ('availability',))]},
    'AggregateOffer': {'req': [('цена', ('lowPrice', 'price')),
                               ('валюта', ('priceCurrency',))], 'rec': []},
    'BreadcrumbList': {'req': [('элементы', ('itemListElement',))], 'rec': []},
    'PropertyValue': {'req': [('название', ('name',)),
                              ('значение', ('value',))], 'rec': []},
    'Organization': {'req': [('название', ('name',))],
                     'rec': [('адрес/телефон', ('address', 'telephone')),
                             ('логотип', ('logo',))]},
    'LocalBusiness': {'req': [('название', ('name',))],
                      'rec': [('адрес/телефон', ('address', 'telephone'))]},
    'PostalAddress': {'req': [('адрес', ('streetAddress', 'addressLocality'))],
                      'rec': []},
    'VideoObject': {'req': [('название', ('name',))],
                    'rec': [('превью', ('thumbnailUrl',)),
                            ('описание', ('description',))]},
    'FAQPage': {'req': [('вопросы-ответы', ('mainEntity',))], 'rec': []},
    # Отзывы: без автора и оценки поиск звёзды не покажет, без даты - не
    # поймёт свежесть. Текст отзыва желателен.
    'Review': {'req': [('автор', ('author',)),
                       ('оценка', ('reviewRating', 'ratingValue'))],
               'rec': [('дата', ('datePublished', 'dateCreated')),
                       ('текст отзыва', ('reviewBody', 'description'))]},
    'AggregateRating': {'req': [('оценка', ('ratingValue',)),
                                ('число отзывов', ('reviewCount', 'ratingCount'))],
                        'rec': []},
}

# Видео на странице: свой <video> или встроенный плеер видеохостинга.
_RE_VIDEO_CONTENT = re.compile(
    r'<video\b|<iframe\b[^>]*src\s*=\s*["\'][^"\']*'
    r'(?:youtube\.com|youtu\.be|rutube\.ru|vk\.com/video|vkvideo|vimeo\.com)',
    re.I)
# FAQ-блок: типовые классы/заголовки «вопрос-ответ».
_RE_FAQ_CONTENT = re.compile(
    r'class\s*=\s*["\'][^"\']*\bfaq\b'
    r'|часто\s+задаваемые\s+вопросы|вопрос[\s-]*ответ|вопросы\s+и\s+ответы',
    re.I)


# ── Отзывы на карточке товара (чек-лист) ────────────────────────────
# Блок отзывов на товаре нужен и покупателю, и сниппету (звёзды в выдаче).
# Проверяем три вещи: блок есть, он размечен, и разметка правдоподобна.
#
# Про «правдоподобие». Отзывы на таких сайтах генерируются, и это нормально -
# но сгенерированные наспех видно сразу: одна дата у всех, один автор, у всех
# пятёрки. Такую разметку поисковики наказывают ручными мерами, то есть это
# вредит клиенту. Проверяем именно ЭТО, а не «насколько похоже на Фламп».

_RE_REVIEW_TEXT_BLOCK = re.compile(
    r'отзывы\s+(?:клиентов|покупателей|наших|о\s)', re.I)
_REVIEW_MIN = 3          # меньше отзывов - о разбросе судить нельзя
_REVIEW_SHORT = 25       # короче - отзыв «ни о чём», символов


def _prop_str(props: dict, *names) -> str:
    """Первое непустое строковое значение свойства объекта."""
    for n in names:
        for v in (props or {}).get(n) or []:
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                # вложенный объект: Person → name, Rating → ratingValue
                for k in ('name', 'ratingValue', 'value'):
                    for vv in v.get('props', {}).get(k, []) if 'props' in v \
                            else ([v.get(k)] if v.get(k) else []):
                        if isinstance(vv, str) and vv.strip():
                            return vv.strip()
    return ''


def collect_reviews(objs: list) -> list:
    """Объекты Review → [{'author', 'rating', 'date', 'body'}].
    ЧИСТАЯ функция - есть юнит-тест."""
    out = []
    for o in objs or []:
        if (o.get('type') or '') != 'Review':
            continue
        p = o.get('props') or {}
        out.append({
            'author': _prop_str(p, 'author'),
            'rating': _prop_str(p, 'reviewRating', 'ratingValue'),
            'date': _prop_str(p, 'datePublished', 'dateCreated')[:10],
            'body': _prop_str(p, 'reviewBody', 'description'),
        })
    return out


def check_reviews_plausible(reviews: list) -> list:
    """Признаки наспех сгенерированных отзывов. → список предупреждений.

    Судим только когда отзывов достаточно (_REVIEW_MIN): на двух отзывах
    «одинаковая дата» ничего не значит.
    ЧИСТАЯ функция - есть юнит-тест."""
    out = []
    n = len(reviews or [])
    if n < _REVIEW_MIN:
        return out

    даты = [r.get('date') for r in reviews if r.get('date')]
    if not даты:
        out.append('у отзывов не проставлены даты - поиск не покажет их '
                   'свежесть')
    elif len(set(даты)) == 1 and len(даты) == n:
        out.append('все отзывы с одной датой - выглядит как разовая '
                   'генерация, поисковики за такое наказывают')

    авторы = [r.get('author') for r in reviews if r.get('author')]
    if not авторы:
        out.append('у отзывов не указаны авторы - разметка неполная и '
                   'недостоверная')
    elif len(set(авторы)) == 1 and len(авторы) == n:
        out.append('все отзывы от одного автора')

    оценки = [r.get('rating') for r in reviews if r.get('rating')]
    if оценки and len(set(оценки)) == 1 and len(оценки) == n and n >= 5:
        out.append(f'у всех отзывов одинаковая оценка ({оценки[0]}) - '
                   f'разброс выглядит естественнее')

    короткие = sum(1 for r in reviews
                   if len((r.get('body') or '').strip()) < _REVIEW_SHORT)
    if короткие == n:
        out.append('тексты отзывов слишком короткие - пользы покупателю нет')
    return out


def check_product_reviews(html: str, objs: list) -> tuple:
    """Отзывы на карточке товара: есть ли блок, размечен ли, правдоподобен ли.
    → (issues, warnings)."""
    issues, warnings = [], []
    reviews = collect_reviews(objs)
    есть_агрегат = any((o.get('type') or '') == 'AggregateRating'
                       for o in objs or [])
    текстовый_блок = bool(_RE_REVIEW_TEXT_BLOCK.search(html or ''))

    if not reviews and not есть_агрегат:
        if текстовый_блок:
            warnings.append('блок отзывов на товаре не размечен '
                            '(schema.org/Review) - звёзды в сниппете не появятся')
        else:
            warnings.append('на карточке товара нет блока отзывов')
        return issues, warnings

    if есть_агрегат and not reviews:
        # Звёзды в сниппете есть, а отзывов, из которых они складываются, нет.
        # Google прямо считает это накруткой рейтинга и снимает сниппет.
        warnings.append('на товаре есть сводная оценка (AggregateRating), но '
                        'нет самих отзывов (Review) - поиск считает это '
                        'накруткой звёзд')
    if reviews and not есть_агрегат:
        warnings.append('у отзывов нет сводной оценки '
                        '(schema.org/AggregateRating)')
    warnings.extend(check_reviews_plausible(reviews))
    return issues, warnings


def _validate_fields(html: str):
    """Проверить обязательные/желательные поля у каждого объекта разметки.

    Возвращает (issues, warnings, details). Тексты issues/warnings - БЕЗ
    чисел: лист отчёта группирует страницы по точному тексту, число на
    каждой странице своё - раздробило бы группы. Числа - в details
    (['Offer/цена: 21 из 60', …]) и показываются в колонке-контексте."""
    objs = list(_walk_objects(_microdata_objects(html))) \
        + list(_walk_objects(_jsonld_objects(html)))
    # счётчики: (тип, метка поля, критично?) → (нет, всего)
    miss = {}
    total = {}
    for o in objs:
        rules = _FIELD_RULES.get(o.get('type'))
        if not rules:
            continue
        props = o.get('props') or {}
        total[o['type']] = total.get(o['type'], 0) + 1
        for crit, groups in (('req', rules['req']), ('rec', rules['rec'])):
            for label, alts in groups:
                has = any(props.get(a) for a in alts)
                if not has:
                    key = (o['type'], label, crit)
                    miss[key] = miss.get(key, 0) + 1

    issues, warnings, details = [], [], []
    for (typ, label, crit), n in sorted(
            miss.items(), key=lambda kv: (-kv[1], kv[0])):
        tot = total.get(typ, n)
        frag = f'в разметке {typ}: нет поля «{label}»'
        (issues if crit == 'req' else warnings).append(frag)
        if tot > 1:
            details.append(f'{typ}/{label}: {n} из {tot}')
    return issues, warnings, details


def check_markup(html: Optional[str], type_code: str, url: str = '') -> Optional[dict]:
    """Проверить OG + Schema.org одной страницы.

    Возвращает dict для CheckResult.markup (или None для нерелевантных
    страниц - тех. страницы кроме контактов не проверяем)."""
    html = html or ''
    is_contacts = type_code == 'tech' and 'contact' in (url or '').lower()
    if type_code not in _SEO_TYPES and not is_contacts:
        return None

    og = parse_og(html)
    og_found = set(og)
    micro = {m.group(1) for m in _RE_ITEMTYPE.finditer(html)}
    ld = _jsonld_types(html)
    props = {m.group(1).lower() for m in _RE_ITEMPROP.finditer(html)}

    issues, warnings = [], []

    # ── 3.5.1 OpenGraph: все 5 полей + пригодность значений ──
    og_missing = [f for f in OG_REQUIRED if f not in og_found]
    for f in og_missing:
        issues.append(f'нет OpenGraph-тега og:{f}')
    _og_i, _og_w = check_og(og, url)
    issues.extend(_og_i)
    warnings.extend(_og_w)

    def _have(type_set):
        """(в microdata?, в json-ld?) хотя бы один тип из набора."""
        return bool(micro & type_set), bool(ld & type_set)

    def _require(type_set, label, warn_only=False):
        """Требование типа: microdata = ок; только JSON-LD = предупреждение;
        нигде = баг (или предупреждение, если warn_only)."""
        in_micro, in_ld = _have(type_set)
        if in_micro:
            return
        if in_ld:
            warnings.append(f'{label} - только в JSON-LD (в microdata нет)')
        elif warn_only:
            warnings.append(f'нет разметки: {label}')
        else:
            issues.append(f'нет разметки: {label}')

    # ── 3.5.2.2 Данные компании - на всех страницах ──
    _require(_ORG_TYPES, 'данные компании (Organization/LocalBusiness)')

    # ── 3.5.2.1 Хлебные крошки - везде, где вложенность ──
    if type_code in ('catalog', 'category', 'filter', 'product') or is_contacts:
        _require({'BreadcrumbList'}, 'хлебные крошки (BreadcrumbList)')

    # ── 3.5.2.4 Листинги ──
    if type_code in ('catalog', 'category', 'filter'):
        _require(_LISTING_TYPES,
                 'листинг (OfferCatalog/ItemList/CollectionPage)')

    if type_code == 'product':
        # ── 3.5.2.3 Карточка товара ──
        _require({'Product'}, 'товар (Product)')
        # ── 3.5.2.7 Характеристики ──
        _require({'PropertyValue'}, 'характеристики (PropertyValue)')
        # ── 3.5.2.5 Фото: itemprop="image" или ImageObject ──
        if 'image' not in props and not (micro | ld) & _IMAGE_TYPES:
            issues.append('фото товара не размечено (itemprop=image/ImageObject)')
        # ── 3.5.2.6 Цены: предупреждение (товары «по запросу» без цены) ──
        if 'price' not in props and not (micro | ld) & _PRICE_TYPES:
            warnings.append('цена не размечена (Offer/PriceSpecification) - '
                            'норма для «цены по запросу»')
        # ── Отзывы на карточке: блок, разметка, правдоподобие ──
        _rv_i, _rv_w = check_product_reviews(
            html, list(_walk_objects(_microdata_objects(html)))
            + list(_walk_objects(_jsonld_objects(html))))
        issues.extend(_rv_i)
        warnings.extend(_rv_w)

    # ── Условные типы: требуем разметку, только когда сам контент есть ──
    # Видео на странице (свой <video> / встроенный плеер) → VideoObject.
    if _RE_VIDEO_CONTENT.search(html) and not (micro | ld) & {'VideoObject'}:
        warnings.append('видео на странице не размечено '
                        '(schema.org/VideoObject)')
    # FAQ-блок (класс faq / «часто задаваемые вопросы») → FAQPage.
    if _RE_FAQ_CONTENT.search(html) and not (micro | ld) & {'FAQPage'}:
        warnings.append('блок вопросов-ответов не размечен '
                        '(schema.org/FAQPage)')
    # Контакты/адрес (обычно в футере) → PostalAddress. Предупреждаем,
    # только если адреса нет ни типом, ни полями (addressLocality и т.п.).
    if not ((micro | ld) & {'PostalAddress'}
            or props & {'address', 'addresslocality', 'streetaddress'}):
        warnings.append('адрес/контакты не размечены '
                        '(schema.org/PostalAddress)')

    # ── Обязательные ПОЛЯ внутри объектов (валидация, а не только наличие
    # типа): Product без offers/name, Offer без price/currency, крошки без
    # itemListElement и т.п. Работает по разобранному дереву microdata +
    # JSON-LD - то, что раньше делали только внешние валидаторы. ──
    f_issues, f_warnings, f_details = _validate_fields(html)
    issues.extend(f_issues)
    warnings.extend(f_warnings)

    return {
        'og_missing': og_missing,
        'og': og,
        'micro_types': sorted(micro),
        'ld_types': sorted(ld),
        'field_details': f_details,
        'issues': issues,
        'warnings': warnings,
    }
