# -*- coding: utf-8 -*-
"""
twogis_check.py - данные карточки организации в 2ГИС (рейтинг, число отзывов,
даты отзывов) с ПУБЛИЧНОЙ страницы отзывов. Используется в проверке «приоритет
докупки отзывов» вместе с Яндекс.Бизнесом (см. yabusiness_check).

Данные берём со страницы 2gis.ru/<city>/firm/<id>/tab/reviews:
  • рейтинг     - JSON "general_rating"
  • число отзывов - JSON "general_review_count"
  • даты отзывов  - русские даты в разметке ленты

Сессия/авторизация не нужна (публичная страница). Firm-id и город берём из
прямой ссылки 2ГИС (в конфиге брендов).
"""
import re

_RE_FIRM = re.compile(r'/firm/(\d+)')
_RE_CITY = re.compile(r'2gis\.[a-z]+/([a-z_]+)/')
_RE_RATING = re.compile(r'"general_rating"\s*:\s*([\d.]+)')
_RE_COUNT = re.compile(r'"general_review_count"\s*:\s*(\d+)')
_RE_RUDATE = re.compile(
    r'(\d{1,2})\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|'
    r'октябр|ноябр|декабр)\w*(?:\s+(\d{4}))?')
_MONTHS = [('январ', 1), ('феврал', 2), ('март', 3), ('апрел', 4),
           ('ма[йя]', 5), ('июн', 6), ('июл', 7), ('август', 8),
           ('сентябр', 9), ('октябр', 10), ('ноябр', 11), ('декабр', 12)]


def parse_firm(url_or_id: str):
    """Из прямой ссылки 2ГИС вернуть (city_slug, firm_id). Принимает и голый
    firm-id (тогда city=None). None-id, если не распознали."""
    s = (url_or_id or '').strip()
    if s.isdigit():
        return None, s
    fid = _RE_FIRM.search(s)
    city = _RE_CITY.search(s)
    return (city.group(1) if city else None), (fid.group(1) if fid else None)


def _parse_rudate(day, word, year):
    import datetime
    month = next((n for pat, n in _MONTHS if re.match(pat, word)), None)
    if not month:
        return None
    y = int(year) if year else datetime.date.today().year
    try:
        return datetime.date(y, month, int(day)).isoformat()
    except ValueError:
        return None


def _extract(html: str) -> dict:
    """Из HTML страницы отзывов 2ГИС → {rating, review_count, review_dates}."""
    r = _RE_RATING.search(html)
    c = _RE_COUNT.search(html)
    dates = []
    for m in _RE_RUDATE.finditer(html):
        d = _parse_rudate(m.group(1), m.group(2), m.group(3))
        if d:
            dates.append(d)
    # даты в ленте дублируются (карточка отзыва + служебные) - дедуп с учётом
    # повторов невозможен по одной дате; оставляем как есть для оценки помесячно.
    return {
        'rating': float(r.group(1)) if r else None,
        'review_count': int(c.group(1)) if c else None,
        'review_dates': dates,
    }


async def afetch_2gis(ctx, sem, url_or_id: str) -> dict:
    """Async: рейтинг/число/даты отзывов организации в 2ГИС. Своя вкладка в
    общем контексте, ограничена семафором. → {url, firm_id, rating,
    review_count, review_dates, available}."""
    city, fid = parse_firm(url_or_id)
    if not fid:
        return {'url': url_or_id, 'firm_id': None, 'available': False,
                'rating': None, 'review_count': None, 'review_dates': []}
    city = city or 'moscow'
    async with sem:
        page = await ctx.new_page()
        data = {'rating': None, 'review_count': None, 'review_dates': []}
        try:
            await page.goto(
                f'https://2gis.ru/{city}/firm/{fid}/tab/reviews',
                wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(4000)
            # подгрузить ленту (2ГИС ленивая) - несколько прокруток
            for _ in range(6):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(700)
            data = _extract(await page.content())
        except Exception:
            pass
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return {'url': url_or_id, 'firm_id': fid,
                'available': data['rating'] is not None
                or data['review_count'] is not None, **data}
