# -*- coding: utf-8 -*-
"""
twogis_map_check.py - контакты организации с публичной карточки 2ГИС
(2gis.ru/<city>/firm/<id>), сверка с КП. Та же страница, что уже дёргает
twogis_check.py ради рейтинга, - берём оттуда же ещё имя/телефон/адрес/сайт.

2ГИС не отдаёт эти поля microdata-тегами (в отличие от Яндекс.Карт) - разбираем
внедрённый в страницу JSON (initialState). Якоря, устойчивые к смене вёрстки:
  • имя      - og:title вида «Отзывы о <Имя>, <рубрика>, …» (мета-тег head,
    не зависит от JS-рендера ленты);
  • телефон  - "print_text":"...","type":"phone","value":"+7…" - именно с
    print_text рядом, иначе цепляются служебные номера поддержки 2ГИС
    (свои "type":"phone" без print_text встречаются на КАЖДОЙ карточке);
  • адрес    - "address_name":"..." - тот же ключ, что в двух других КАРТАХ
    Яндекса не значит: у 2ГИС он называется так же по совпадению, не связано;
  • сайт     - "url":"http(s)://...","text":"<домен-как-текст>" - у 2ГИС
    ссылка на сайт - единственная, где text выглядит как голый домен (у vk/wa
    и т.п. text - название соцсети, а не домен).

Как и у Яндекса: не удалось прочитать - available=False, штатный исход.
"""
import re

_RE_TITLE = re.compile(r'og:title"\s+content="Отзывы о ([^,"]{1,80}),')
_RE_PHONE = re.compile(r'"print_text":"[^"]{3,40}","type":"phone","value":"(\+\d{6,15})"')
_RE_ADDRESS = re.compile(r'"address_name":"([^"]{1,160})"')
_RE_SITE = re.compile(r'"url":"(https?://[^"]{3,200})","text":"([a-zA-Z0-9.\-]+\.[a-z]{2,})"')


def extract(html: str) -> dict:
    """HTML карточки (после подгрузки ленты - см. afetch) → {name, phone,
    address, site, available}."""
    m_name = _RE_TITLE.search(html)
    m_phone = _RE_PHONE.search(html)
    m_addr = _RE_ADDRESS.search(html)
    m_site = _RE_SITE.search(html)
    name = (m_name.group(1).strip() if m_name else '')
    phone = (m_phone.group(1).strip() if m_phone else '')
    address = (m_addr.group(1).strip() if m_addr else '')
    site = (m_site.group(1).strip() if m_site else '')
    return {
        'name': name, 'phone': phone, 'address': address, 'site': site,
        'available': bool(name or phone or address or site),
    }


async def afetch(ctx, sem, url: str) -> dict:
    """Async: карточка 2ГИС по ссылке из КП. Ленивая подгрузка (те же 6
    прокруток, что twogis_check.afetch_2gis - без них JSON карточки в DOM
    ещё не подгружен)."""
    if not url:
        return {'url': url, 'available': False, 'name': '', 'phone': '',
                'address': '', 'site': '', 'error': 'ссылки нет'}
    import twogis_check
    _, fid = twogis_check.parse_firm(url)
    if not fid:
        return {'url': url, 'available': False, 'name': '', 'phone': '',
                'address': '', 'site': '', 'error': 'не распознан firm id'}
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(4000)
            for _ in range(6):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(700)
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
