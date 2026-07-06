"""
sitemap.py - загрузка и парсинг sitemap.xml для определения товарных URL.

Точная копия логики Node.js версии:
  • Sitemap может быть индексом (<sitemapindex>) или обычным (<urlset>)
  • Обходим рекурсивно, собираем все URL
  • «Товар» = путь под /catalog/ с 3+ сегментами, который НЕ совпадает
    с известными категориями и фильтрами из xlsx-каталога
  • Кеш на 24 часа в cache/{project_id}-sitemap.json
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import urlparse

import aiohttp


PROJECT_ROOT = Path(__file__).parent
CACHE_DIR = PROJECT_ROOT / 'cache'
CACHE_TTL_MS = 24 * 3600 * 1000  # 24 часа

URL_RE = re.compile(r'<loc>\s*([^<]+?)\s*</loc>')
SITEMAP_INDEX_RE = re.compile(r'<sitemapindex\b', re.IGNORECASE)

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)


def _sitemap_headers(user_agent: str = DEFAULT_USER_AGENT) -> dict:
    """Заголовки для запроса XML-карты сайта (Accept: xml, остальное как у браузера)."""
    return {
        'User-Agent': user_agent,
        'Accept': 'application/xml,text/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Sec-Ch-Ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Connection': 'keep-alive',
    }


# ── Сбор URL'ов из sitemap ──────────────────────────────────────────


async def _fetch_one(session, url: str, timeout_s: int = 30, proxy_url: Optional[str] = None) -> str:
    """Скачать содержимое одного sitemap-файла."""
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with session.get(url, timeout=timeout, proxy=proxy_url) as resp:
        return await resp.text()


def _parse_urls(xml: str) -> list[str]:
    """Извлечь все <loc>...</loc>."""
    return [m.group(1).strip() for m in URL_RE.finditer(xml)]


async def collect_all_urls(
    root_sitemap_url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    max_sitemaps: int = 50,
    log: Optional[Callable] = None,
    proxy_url: Optional[str] = None,
) -> list[str]:
    """Рекурсивно обойти sitemap-индекс и собрать все URL."""
    # Если прокси не задан явно - берём из env
    if proxy_url is None:
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')

    seen_sitemaps = set()
    collected = []
    queue = [root_sitemap_url]
    processed = 0

    headers = _sitemap_headers(user_agent)
    async with aiohttp.ClientSession(headers=headers) as session:
        while queue and processed < max_sitemaps:
            next_url = queue.pop(0)
            if next_url in seen_sitemaps:
                continue
            seen_sitemaps.add(next_url)

            try:
                xml = await _fetch_one(session, next_url, proxy_url=proxy_url)
            except Exception as e:
                if log:
                    log('warn', f'Не удалось загрузить {next_url}: {e}')
                continue

            processed += 1
            if log:
                log('info', f'Обработан sitemap: {next_url}')

            if SITEMAP_INDEX_RE.search(xml):
                # Это индекс - добавляем под-sitemap'ы в очередь
                for sub in _parse_urls(xml):
                    queue.append(sub)
            else:
                # Обычный sitemap - собираем URL'ы
                collected.extend(_parse_urls(xml))

    if queue and log:
        log('warn', f'Достигнут лимит обхода {max_sitemaps} sitemap-ов. Часть пропущена.')

    return collected


def _to_pathnames(urls: list[str]) -> list[str]:
    """URL → pathname, с дедупом."""
    out = []
    seen = set()
    for u in urls:
        try:
            p = urlparse(u).path
        except ValueError:
            continue
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── Кеш ─────────────────────────────────────────────────────────────


def _cache_path(project_id: str) -> Path:
    return CACHE_DIR / f'{project_id}-sitemap.json'


def _load_cached(project_id: str) -> Optional[dict]:
    """Прочитать кеш если он свежий."""
    p = _cache_path(project_id)
    if not p.exists():
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        age_ms = (time.time() * 1000) - cache.get('fetched_at', 0)
        if age_ms > CACHE_TTL_MS:
            return None
        return cache
    except Exception:
        return None


def _save_cache(project_id: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(project_id), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def invalidate_sitemap_cache(project_id: str) -> None:
    """Стереть кеш sitemap'а конкретного проекта."""
    p = _cache_path(project_id)
    if p.exists():
        p.unlink()


def get_cached_products_info(project_id: str) -> Optional[dict]:
    """
    Прочитать инфу о товарах из кеша sitemap (даже если кеш не свежий).
    Используется в UI чтобы показать «Товаров: ~N (по sitemap от DATE)».
    
    Возвращает {'count': int, 'fetched_at_ms': int, 'is_fresh': bool}
    или None если кеша вообще нет.
    """
    p = _cache_path(project_id)
    if not p.exists():
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        fetched_at = cache.get('fetched_at', 0)
        age_ms = (time.time() * 1000) - fetched_at
        return {
            'count': len(cache.get('pathnames', [])),
            'fetched_at_ms': fetched_at,
            'is_fresh': age_ms <= CACHE_TTL_MS,
        }
    except Exception:
        return None


# ── Главная функция: товарные pathname'ы ────────────────────────────


async def load_product_pathnames(
    project: dict,
    known_category_paths: list[str],
    known_filter_paths: list[str],
    *,
    force_reload: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
    log: Optional[Callable] = None,
    proxy_url: Optional[str] = None,
) -> dict:
    """
    Получить список товарных pathname'ов для проекта.
    Возвращает: {'pathnames': [...], 'fetched_at': ms, 'warning': str or None}.
    """
    if not force_reload:
        cached = _load_cached(project['id'])
        if cached and 'pathnames' in cached:
            return cached

    sitemap_url = project.get('sitemap_url')
    if not sitemap_url:
        return {
            'pathnames': [], 'fetched_at': int(time.time() * 1000),
            'warning': 'sitemap_url не указан в конфиге',
        }

    if log:
        log('info', f'Загружаю sitemap из {sitemap_url}…')

    try:
        all_urls = await collect_all_urls(sitemap_url, user_agent=user_agent, log=log, proxy_url=proxy_url)
    except Exception as e:
        return {
            'pathnames': [], 'fetched_at': int(time.time() * 1000),
            'warning': f'Не удалось загрузить sitemap: {e}',
        }

    all_paths = _to_pathnames(all_urls)
    known_cats = set(known_category_paths or [])
    known_filters = set(known_filter_paths or [])

    products = []
    for p in all_paths:
        # Исключаем известные категории и фильтры
        if p in known_cats or p in known_filters:
            continue
        if p in ('/', '/catalog/'):
            continue
        # /filter/ в пути - явный фильтр
        if '/filter/' in p:
            continue
        if not p.startswith('/catalog/'):
            continue

        # Минимум 3 сегмента: /catalog/<категория>/<товар>/
        segments = [s for s in p.rstrip('/').split('/') if s]
        if len(segments) < 3:
            continue

        products.append(p)

    data = {
        'pathnames': products,
        'fetched_at': int(time.time() * 1000),
        'total_urls_in_sitemap': len(all_paths),
    }
    _save_cache(project['id'], data)

    if log:
        log('info', f'Sitemap: всего {len(all_paths)} URL, отфильтровано {len(products)} товаров')

    return data
