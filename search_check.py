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


async def check_search(category_url: str, proxy_url=None) -> dict:
    """Проверить, находит ли поиск сайта категорию по её названию.

    Возвращает {'available', 'query', 'search_url', 'status',
                'found_category', 'error'}."""
    out = {'available': False, 'query': None, 'search_url': None,
           'status': None, 'found_category': None, 'error': None}
    sp = urlsplit(category_url)
    cat_path = (sp.path or '/').rstrip('/') + '/'
    from http_checker import make_browser_headers
    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=make_browser_headers(),
                                     connector=connector) as session:
        st, html = await _fetch(session, category_url, proxy_url)
        if st != 200 or not html:
            out['error'] = f'категория не открылась (HTTP {st})'
            return out
        m = _RE_H1.search(html)
        if not m:
            out['error'] = 'у категории нет H1 - нечего искать'
            return out
        name = re.sub(r'\s+', ' ', _RE_TAG.sub(' ', m.group(1))).strip()
        # Срезаем хвост «в Городе» из шаблонных H1 - ищем по сути названия.
        name = re.sub(r'\s+в\s+[А-ЯЁ][\w-]+$', '', name).strip()
        if len(name) < 3:
            out['error'] = 'H1 категории слишком короткий'
            return out
        out['query'] = name

        for tpl in _SEARCH_PATHS:
            s_url = f'{sp.scheme}://{sp.netloc}' + tpl.format(q=quote(name))
            st, html = await _fetch(session, s_url, proxy_url)
            if st != 200 or not html:
                continue
            out['available'] = True
            out['search_url'] = s_url
            out['status'] = st
            # Ссылка на саму категорию в выдаче = поиск находит категории.
            out['found_category'] = (f'href="{cat_path}' in html
                                     or f"href='{cat_path}" in html
                                     or cat_path.rstrip('/') + '"' in html)
            return out
        out['error'] = 'страница поиска не найдена (/search/?q= и варианты)'
        return out
