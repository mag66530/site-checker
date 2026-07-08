"""
schema_checker.py - микроразметка Schema.org и OpenGraph (пункт 1.12, ТЗ 3.5).

ТЗ 3.5.1 - OpenGraph на основных типах страниц: og:url, og:title,
og:description, og:image, og:type. Отсутствие поля = баг.

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

Валидаторы Яндекса/Google из ТЗ - ручные инструменты; здесь проверяется
наличие и полнота разметки, не валидность каждого поля.
"""
import json
import re
from typing import Optional

_RE_OG = re.compile(
    r'<meta\b[^>]*property\s*=\s*["\']og:(\w+)["\'][^>]*>', re.I)
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


def check_markup(html: Optional[str], type_code: str, url: str = '') -> Optional[dict]:
    """Проверить OG + Schema.org одной страницы.

    Возвращает dict для CheckResult.markup (или None для нерелевантных
    страниц - тех. страницы кроме контактов не проверяем)."""
    html = html or ''
    is_contacts = type_code == 'tech' and 'contact' in (url or '').lower()
    if type_code not in _SEO_TYPES and not is_contacts:
        return None

    og_found = {m.group(1).lower() for m in _RE_OG.finditer(html)}
    micro = {m.group(1) for m in _RE_ITEMTYPE.finditer(html)}
    ld = _jsonld_types(html)
    props = {m.group(1).lower() for m in _RE_ITEMPROP.finditer(html)}

    issues, warnings = [], []

    # ── 3.5.1 OpenGraph: все 5 полей ──
    og_missing = [f for f in OG_REQUIRED if f not in og_found]
    for f in og_missing:
        issues.append(f'нет OpenGraph-тега og:{f}')

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

    return {
        'og_missing': og_missing,
        'micro_types': sorted(micro),
        'ld_types': sorted(ld),
        'issues': issues,
        'warnings': warnings,
    }
