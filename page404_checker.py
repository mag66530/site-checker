"""
page404_checker.py - страница 404 (пункт 1.18 чек-листа).

Запрашиваем ЗАВЕДОМО несуществующий URL и проверяем, как сайт отвечает:

  • корректный код ответа - ровно 404 (200 = «soft-404 шаблон», страница-
    заглушка мешает поисковикам выкидывать битые адреса из индекса - баг;
    редирект на главную - предупреждение);
  • дизайн совпадает с главной - косвенно, без рендера: на 404-странице
    те же шапка/подвал (<header>/<footer>) и есть общие CSS-файлы с
    главной (шаблон сайта, а не белый лист сервера);
  • уникальный заголовок и описание - <title> непустой и НЕ совпадает
    с главной, meta description присутствует;
  • полезность - есть ссылки на основные разделы (внутренние ссылки,
    в т.ч. на каталог) и форма заявки/консультации (<form> либо
    телефон tel:).

Шаблон 404 сквозной для всего сайта - проверяем главный домен и один
поддомен (не все города). 2 хоста × 2 запроса = дёшево, HTTP-only.
"""
from __future__ import annotations

import asyncio
import random
import re
import string
from urllib.parse import urlsplit

import aiohttp

_RE_TITLE = re.compile(r'<title\b[^>]*>(.*?)</title>', re.I | re.S)
_RE_DESC = re.compile(
    r'<meta\b[^>]*name\s*=\s*["\']description["\'][^>]*>', re.I)
_RE_CONTENT = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.I)
_RE_HEADER = re.compile(r'<header[\s>]', re.I)
_RE_FOOTER = re.compile(r'<footer[\s>]', re.I)
_RE_CSS = re.compile(
    r'<link\b[^>]*rel\s*=\s*["\'][^"\']*stylesheet[^"\']*["\'][^>]*>', re.I)
_RE_HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_A_HREF = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_FORM = re.compile(r'<form[\s>]', re.I)
_RE_TEL = re.compile(r'href\s*=\s*["\']tel:', re.I)

MIN_SECTION_LINKS = 3     # минимум внутренних ссылок, чтобы считать «есть разделы»


def _title(html: str) -> str:
    m = _RE_TITLE.search(html or '')
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''


def _description(html: str) -> str:
    for m in _RE_DESC.finditer(html or ''):
        cm = _RE_CONTENT.search(m.group(0))
        if cm:
            return cm.group(1).strip()
    return ''


def _css_files(html: str) -> set:
    """Имена CSS-файлов страницы (без query) - для сравнения шаблонов."""
    out = set()
    for m in _RE_CSS.finditer(html or ''):
        hm = _RE_HREF.search(m.group(0))
        if hm:
            out.add(hm.group(1).split('?')[0].rsplit('/', 1)[-1].lower())
    return out


def _internal_links(html: str, host: str) -> int:
    """Число РАЗНЫХ внутренних ссылок (свой хост или относительные)."""
    seen = set()
    for href in _RE_A_HREF.findall(html or ''):
        href = href.strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        sp = urlsplit(href)
        if sp.netloc and sp.netloc.lower().removeprefix('www.') != host:
            continue
        seen.add(sp.path or '/')
    return len(seen)


async def _fetch(session, url, proxy_url, allow_redirects=True):
    """(status, html, была_ли_переадресация). Ошибка сети → (None, '', False)."""
    to = aiohttp.ClientTimeout(total=25)
    try:
        async with session.get(url, timeout=to, proxy=proxy_url,
                               allow_redirects=allow_redirects) as r:
            html = await r.text(errors='replace')
            return r.status, html, bool(r.history)
    except Exception:
        return None, '', False


async def _check_host(session, main_url: str, city: str, proxy_url) -> dict:
    """Проверка 404-страницы одного хоста (главная того же хоста - эталон)."""
    sp = urlsplit(main_url)
    host = (sp.netloc or '').lower().removeprefix('www.')
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    probe_url = f'{sp.scheme}://{sp.netloc}/nesushchestvuyushchaya-{rand}/'

    out = {'city': city, 'host': sp.netloc, 'probe_url': probe_url,
           'status': None, 'redirected': False,
           'issues': [], 'warnings': [], 'error': None}
    issues, warnings = out['issues'], out['warnings']

    m_status, m_html, _ = await _fetch(session, main_url, proxy_url)
    if m_status is None or m_status >= 400 or not m_html:
        out['error'] = f'главная не открылась (HTTP {m_status}) - сравнить не с чем'
        return out

    status, html, redirected = await _fetch(session, probe_url, proxy_url)
    out['status'] = status
    out['redirected'] = redirected
    if status is None:
        out['error'] = 'несуществующий адрес не ответил (сеть/таймаут)'
        return out

    # 1. Код ответа
    if status == 404 or status == 410:
        pass                                   # корректно
    elif redirected:
        warnings.append(f'несуществующий адрес редиректит (итоговый код '
                        f'{status}) вместо ответа 404 - поисковики не '
                        f'выкинут битые URL из индекса')
    elif 200 <= status < 300:
        issues.append('несуществующий адрес отдаёт 200 вместо 404 '
                      '(soft-404 шаблон) - поисковики считают битые '
                      'адреса рабочими страницами')
    else:
        warnings.append(f'несуществующий адрес отдаёт {status} вместо 404')

    if not html:
        warnings.append('404-страница пустая (нет HTML) - нет шаблона с '
                        'навигацией')
        return out

    # 2. Дизайн совпадает с главной (косвенно: шапка/подвал + общие CSS)
    has_header = bool(_RE_HEADER.search(html)) == bool(_RE_HEADER.search(m_html))
    has_footer = bool(_RE_FOOTER.search(html)) == bool(_RE_FOOTER.search(m_html))
    css_common = _css_files(html) & _css_files(m_html)
    out['css_common'] = len(css_common)
    if not css_common and not (has_header and has_footer):
        warnings.append('дизайн 404-страницы не совпадает с главной '
                        '(нет общих CSS-файлов и шапки/подвала шаблона) - '
                        'похоже на серверную заглушку')

    # 3. Уникальный заголовок и описание
    t404, tmain = _title(html), _title(m_html)
    out['title'] = t404
    if not t404:
        warnings.append('у 404-страницы пустой <title>')
    elif t404 == tmain:
        warnings.append('заголовок 404-страницы совпадает с главной - '
                        'нужен свой («Страница не найдена» и т.п.)')
    if not _description(html):
        warnings.append('у 404-страницы нет meta description')

    # 4. Ссылки на разделы + форма заявки/консультации
    links = _internal_links(html, host)
    out['links'] = links
    if links < MIN_SECTION_LINKS:
        warnings.append('на 404-странице нет ссылок на основные разделы - '
                        'посетителю некуда идти')
    if not (_RE_FORM.search(html) or _RE_TEL.search(html)):
        warnings.append('на 404-странице нет формы заявки/консультации '
                        'и телефона')
    return out


async def check_404_pages(main_urls: list, proxy_url=None) -> dict:
    """Проверить 404-страницы по списку главных страниц (обычно главный
    домен + один поддомен). main_urls: [(city, url)].
    Возвращает {'available', 'hosts': [...]}"""
    if not main_urls:
        return {'available': False, 'hosts': []}
    from http_checker import make_browser_headers
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=make_browser_headers(),
                                     connector=connector) as session:
        hosts = await asyncio.gather(
            *[_check_host(session, u, c, proxy_url) for c, u in main_urls])
    return {'available': True, 'hosts': list(hosts)}
