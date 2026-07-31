# -*- coding: utf-8 -*-
"""
twogis_map_check.py - контакты организации с публичной карточки 2ГИС
(2gis.ru/<city>/firm/<id>), сверка с КП. Та же страница, что уже дёргает
twogis_check.py ради рейтинга, - берём оттуда же ещё имя/телефон/адрес/сайт.

Раньше телефон/адрес/сайт разбирались регуляркой по внедрённому в страницу
JSON (initialState вида "url":"...","text":"..."). Это оказалось ненадёжно:
на части карточек (напр. Севастополь у СМУ) сайт в HTML открытым текстом
такой строкой больше не встречается - ссылка на сайт теперь идёт через
аналитический редирект (link.2gis.ru/…/<base64-пейлоад>), сам домен виден
только как ТЕКСТ ссылки, а не в JSON. Разбираем то же, что видит пользователь -
панель контактов карточки (div._8sgdp4, её user явно показал в HTML):
  • имя      - og:title вида «Отзывы о <Имя>, <рубрика>, …» (мета-тег head,
    не зависит от JS-рендера ленты, не меняли);
  • телефон  - ссылка <a href="tel:+7…"> внутри панели контактов;
  • адрес    - ссылка <a href="/<город>/geo/<id>">улица, дом</a> + соседняя
    строка «район, город» рядом (класс _1p8iqzw) - берём вместе, но
    address_match() у нас ищет по словам улицы + номеру дома, так что
    хватило бы и одной ссылки;
  • сайт     - внешняя ссылка через link.2gis.ru (тем же путём идут и
    соцсети - VK/OK/WhatsApp/Telegram/YouTube), но ТОЛЬКО у сайта текст
    ссылки выглядит как голый домен («stalmetural.ru»), у соцсетей текст -
    название сети («WhatsApp», «ВКонтакте» и т.п.) - фильтруем по этому.

Классы вида «_8sgdp4»/«_1p8iqzw» - автосгенерированные (CSS-in-JS), могут
смениться при переверстке 2ГИС - тогда extract() просто не найдёт панель и
тихо вернёт available=False (штатный исход, как и раньше).

Как и у Яндекса: не удалось прочитать - available=False, штатный исход.
"""
import re

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 всегда есть в этом проекте
    BeautifulSoup = None

_RE_TITLE = re.compile(r'og:title"\s+content="Отзывы о ([^,"]{1,80}),')
# Слаг города перед "/geo/" не фиксируем строгим набором символов (был баг:
# «rostov-na-donu» проходил, «n_novgorod» с подчёркиванием - нет) - ищем сам
# паттерн "/geo/<id>" где угодно в пути, слаг города вообще не разбираем.
_RE_GEO_HREF = re.compile(r'/geo/\d+')
_RE_DOMAIN_TEXT = re.compile(r'^[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+$', re.IGNORECASE)


def _contacts_panel(soup):
    """Панель контактов карточки (адрес/режим работы/телефон/сайт/соцсети).
    Не нашли по классу - падаем на всю страницу (хуже точность, но не падаем)."""
    return soup.find('div', class_='_8sgdp4') or soup


def _find_phone(panel) -> str:
    a = panel.find('a', href=lambda h: bool(h) and h.startswith('tel:'))
    if not a:
        return ''
    return a['href'].split(':', 1)[1].strip()


def _find_address(panel) -> str:
    geo_a = None
    for a in panel.find_all('a', href=True):
        if _RE_GEO_HREF.search(a['href']):
            geo_a = a
            break
    if not geo_a:
        return ''
    street = geo_a.get_text(strip=True)
    # Соседняя строка «район, город» - ищем ближайшего предка, у которого она
    # есть СРЕДИ потомков (глубина вложенности отличается между карточками).
    city_line = ''
    node = geo_a
    for _ in range(5):
        node = node.parent
        if node is None or node is panel:
            break
        sib = node.find('div', class_='_1p8iqzw')
        if sib:
            city_line = sib.get_text(strip=True)
            break
    return f'{street}, {city_line}' if city_line else street


def _find_site(panel) -> str:
    for a in panel.find_all('a', href=True):
        if 'link.2gis.ru' not in a['href']:
            continue
        # Некоторые карточки пишут текст ссылки со слэшем на конце
        # («surgut.stalmetural.ru/») - без rstrip regex не совпадал, и сайт
        # ошибочно считался отсутствующим на карточке (хотя он есть).
        txt = a.get_text(strip=True).rstrip('/')
        if txt and _RE_DOMAIN_TEXT.match(txt):
            return txt.lower()
    return ''


def extract(html: str) -> dict:
    """HTML карточки (после подгрузки ленты - см. afetch) → {name, phone,
    address, site, available}."""
    if BeautifulSoup is None:  # pragma: no cover
        return {'name': '', 'phone': '', 'address': '', 'site': '', 'available': False}
    soup = BeautifulSoup(html, 'html.parser')
    panel = _contacts_panel(soup)

    m_name = _RE_TITLE.search(html)
    name = m_name.group(1).strip() if m_name else ''
    phone = _find_phone(panel)
    address = _find_address(panel)
    site = _find_site(panel)
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
