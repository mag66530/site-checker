# -*- coding: utf-8 -*-
"""
google_map_check.py - контакты организации с публичной карточки Google Maps
(google.com/maps/place/...), сверка с КП. Используется в «Проверке КП».

Google не отдаёт эти поля microdata/JSON, как Яндекс.Карты - зато у каждой
строки карточки (адрес/телефон/сайт) есть служебный атрибут data-item-id на
кнопке/ссылке-обёртке. Это стабильный якорь (тот же используют публичные
Google Maps-скрейперы годами), в отличие от самого класса текста внутри
(«Io6YTe fontBodyMedium kR99db fdkmkc» - обфусцированный CSS-in-JS, может
смениться при любой переверстке):
  • адрес   - [data-item-id="address"];
  • телефон - [data-item-id^="phone:"] (сам номер - в видимом тексте, не в
    атрибуте: там "phone:tel:+7...", с пробелами/скобками не совпадает с КП);
  • сайт    - [data-item-id="authority"] (видимый текст - голый домен);
  • имя     - og:title (мета-тег head, не зависит от того, какие строки
    показаны в карточке).

Google Maps может показать баннер согласия на cookie перед карточкой
(«Прежде чем перейти в Google» / "Before you continue") - без клика по
«Принять все» карточка не откроется; пробуем несколько локализаций кнопки.

Как и у Яндекса/2ГИС: не удалось прочитать - available=False, штатный исход.
"""
import re

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 всегда есть в этом проекте
    BeautifulSoup = None

_RE_TITLE = re.compile(r'<meta content="([^"]{1,160})" property="og:title"')

_CONSENT_LABELS = ('Принять все', 'Accept all', 'Я согласен', 'I agree')

# Невидимые форматирующие символы (zero-width space, U+200B и родня) и
# символы из Private Use Area (U+E000-U+F8FF, там живут лигатуры иконочных
# шрифтов вида "открыть в новом окне") нужно чистить явно кодами \u, не
# литералами - иначе в файле остаются невидимые/мусорные байты, которые не
# видно и не проверить глазами. Раньше именно такой мусор (из get_text() по
# всей кнопке с data-item-id, а не только по видимому div со значением)
# ломал побайтовое сравнение внешне одинаковых строк (сайт на карте vs КП).
_RE_INVISIBLE = re.compile('[​‌‍﻿]')
_RE_ICON_GLYPH = re.compile('[-]')


def _clean(s: str) -> str:
    s = _RE_INVISIBLE.sub('', s)
    s = _RE_ICON_GLYPH.sub('', s)
    return s.strip()


def _find_by_item_id(soup, predicate):
    for el in soup.find_all(attrs={'data-item-id': True}):
        if predicate(el['data-item-id']):
            return el
    return None


def _value_text(el) -> str:
    """Текст ИМЕННО видимого значения, не всего элемента с data-item-id -
    внутри кнопки/ссылки, помимо самого значения, часто есть соседние иконки
    (напр. «открыть в новом окне»); их текст-лигатуры иначе попадали бы в
    результат вперемешку с адресом/телефоном/сайтом. Видимое значение всегда
    лежит в div.Io6YTe - если он есть, берём текст строго из него."""
    if el is None:
        return ''
    inner = el.find('div', class_='Io6YTe')
    return _clean((inner or el).get_text(strip=True))


def extract(html: str) -> dict:
    """HTML карточки (после подгрузки панели - см. afetch) → {name, phone,
    address, site, available}."""
    if BeautifulSoup is None:  # pragma: no cover
        return {'name': '', 'phone': '', 'address': '', 'site': '', 'available': False}
    soup = BeautifulSoup(html, 'html.parser')

    m_name = _RE_TITLE.search(html)
    name = _clean(m_name.group(1)) if m_name else ''

    address = _value_text(_find_by_item_id(soup, lambda v: v == 'address'))
    phone = _value_text(_find_by_item_id(soup, lambda v: v.startswith('phone:')))
    site = _value_text(_find_by_item_id(soup, lambda v: v == 'authority'))

    return {
        'name': name, 'phone': phone, 'address': address, 'site': site,
        'available': bool(name or phone or address or site),
    }


async def _accept_consent(page) -> None:
    for label in _CONSENT_LABELS:
        try:
            btn = page.get_by_role('button', name=label, exact=False)
            if await btn.count():
                await btn.first.click(timeout=2000)
                return
        except Exception:  # noqa: BLE001
            continue


async def afetch(ctx, sem, url: str) -> dict:
    """Async: карточка Google Maps по ссылке из КП. Своя вкладка в общем
    контексте (тот же паттерн, что yandex_map_check/twogis_map_check)."""
    if not url:
        return {'url': url, 'available': False, 'name': '', 'phone': '',
                'address': '', 'site': '', 'error': 'ссылки нет'}
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            await _accept_consent(page)
            await page.wait_for_timeout(3000)
            html = await page.content()
            data = extract(html)
            data['url'] = url
            data['error'] = None if data['available'] else 'карточка не распозналась'
            return data
        except Exception as e:  # noqa: BLE001
            return {'url': url, 'available': False, 'name': '', 'phone': '',
                    'address': '', 'site': '', 'error': str(e)}
        finally:
            try:
                await page.close()
            except Exception:
                pass
