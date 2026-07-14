"""
search_check.py - поиск по сайту находит не только товары (пункт чек-листа
«Поиск ищет не только товары, но и категории/теги»).

HTTP-only: берём реальную категорию прогона, читаем её название (H1),
делаем поисковый запрос /search/?q=<название> на том же хосте и смотрим,
есть ли в выдаче ссылка на САМУ эту категорию. Товары по такому запросу
найдутся почти всегда - показатель именно ссылка на категорию/раздел.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, quote

import aiohttp

_RE_H1 = re.compile(r'<h1\b[^>]*>(.*?)</h1>', re.I | re.S)
_RE_TAG = re.compile(r'<[^>]+>')

# Типовые пути поиска (Bitrix и общие).
_SEARCH_PATHS = ('/search/?q={q}', '/?q={q}', '/search/?query={q}')


async def _fetch(session, url, proxy_url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=25),
                               proxy=proxy_url, allow_redirects=True) as r:
            return r.status, await r.text(errors='replace')
    except Exception:
        return None, ''


async def _name_from_h1(session, url, proxy_url):
    """Название страницы из её H1 (хвост «в Городе» срезаем)."""
    st, html = await _fetch(session, url, proxy_url)
    if st != 200 or not html:
        return None
    m = _RE_H1.search(html)
    if not m:
        return None
    name = re.sub(r'\s+', ' ', _RE_TAG.sub(' ', m.group(1))).strip()
    name = re.sub(r'\s+в\s+[А-ЯЁ][\w-]+$', '', name).strip()
    return name if len(name) >= 3 else None


async def _probe_search(session, netloc_scheme, name, target_path, proxy_url):
    """Поиск сайта по name: (search_url|None, found: bool|None)."""
    for tpl in _SEARCH_PATHS:
        s_url = netloc_scheme + tpl.format(q=quote(name))
        st, html = await _fetch(session, s_url, proxy_url)
        if st != 200 or not html:
            continue
        found = (f'href="{target_path}' in html
                 or f"href='{target_path}" in html
                 or target_path.rstrip('/') + '"' in html)
        return s_url, found
    return None, None


async def check_search(category_url: str, filter_url: str = None,
                       proxy_url=None) -> dict:
    """Находит ли поиск САЙТА (та самая строка поиска: форма шлёт GET
    /search/?q=…) категорию и тег/фильтр по их названиям.

    Возвращает {'available', 'query', 'search_url', 'found_category',
                'tag_query', 'found_tag', 'error'}."""
    out = {'available': False, 'query': None, 'search_url': None,
           'status': None, 'found_category': None,
           'tag_query': None, 'found_tag': None, 'error': None}
    sp = urlsplit(category_url)
    base = f'{sp.scheme}://{sp.netloc}'
    cat_path = (sp.path or '/').rstrip('/') + '/'
    from http_checker import make_browser_headers
    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=make_browser_headers(),
                                     connector=connector) as session:
        name = await _name_from_h1(session, category_url, proxy_url)
        if not name:
            out['error'] = 'у категории нет H1 - нечего искать'
            return out
        out['query'] = name
        s_url, found = await _probe_search(session, base, name, cat_path,
                                           proxy_url)
        if s_url is None:
            out['error'] = 'страница поиска не найдена (/search/?q= и варианты)'
            return out
        out['available'] = True
        out['search_url'] = s_url
        out['status'] = 200
        out['found_category'] = found

        # Тег (страница-фильтр): ищем её название - есть ли ссылка на тег.
        if filter_url:
            fsp = urlsplit(filter_url)
            f_path = (fsp.path or '/').rstrip('/') + '/'
            f_name = await _name_from_h1(session, filter_url, proxy_url)
            if f_name and f_name.lower() != name.lower():
                out['tag_query'] = f_name
                _, out['found_tag'] = await _probe_search(
                    session, base, f_name, f_path, proxy_url)
        return out
