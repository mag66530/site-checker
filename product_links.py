"""
product_links.py — база товарных ссылок, собранных с листингов категорий.

Зачем: товары для проверки раньше брались только из sitemap.xml. Sitemap
может содержать удалённые или скрытые товары и не отражает того, что реально
видит покупатель. Здесь товары собираются с самих страниц категорий — с тех
же карточек, по которым ходит пользователь.

Как устроено (согласованная схема):
  1. Скрипт collect_products.py проходит ВСЕ категории проекта
     (главный домен, ТОЛЬКО первая страница листинга — пагинация не нужна).
  2. С каждой страницы собирает ссылки на карточки товаров.
  3. Результат сохраняется в репозитории:
        catalogs/{proj}-products.csv        — сами ссылки (path + категория)
        catalogs/{proj}-products-meta.json  — когда и как собрано
     На Streamlit Cloud месячный кэш на диске не живёт (контейнер
     пересоздаётся), поэтому база лежит в git и обновляется вручную
     раз в месяц: запустить скрипт → закоммитить → задеплоить.
  4. Приложение читает базу из репозитория, показывает количество товаров
     и дату сбора в интерфейсе. Через 30 дней база считается устаревшей —
     интерфейс подсвечивает, что пора пересобрать.

Распознавание ссылки на товар — та же логика, что в sitemap.py:
путь под /catalog/ с 3+ сегментами, не категория и не фильтр из каталога.
Так сборщик работает на всех проектах без привязки к вёрстке.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

PROJECT_ROOT = Path(__file__).parent
CATALOGS_DIR = PROJECT_ROOT / 'catalogs'

# Через сколько база считается устаревшей (тот самый «месячный кэш»)
STALE_AFTER_MS = 30 * 24 * 3600 * 1000

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

_HREF_RE = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\'#]+)["\']', re.IGNORECASE)


# ── Извлечение товарных ссылок из HTML листинга ─────────────────────


def extract_product_paths(
    html: str,
    page_url: str,
    known_category_paths: set[str],
    known_filter_paths: set[str],
) -> list[str]:
    """
    Достать из HTML страницы-листинга пути карточек товаров.

    Товар = ссылка на тот же хост, путь под /catalog/ с 3+ сегментами,
    которого нет среди известных категорий и фильтров (и без /filter/).
    """
    if not html:
        return []
    page_host = urlparse(page_url).hostname or ''

    seen: set[str] = set()
    out: list[str] = []
    for m in _HREF_RE.finditer(html):
        href = m.group(1).strip()
        if not href or href.startswith(('mailto:', 'tel:', 'javascript:')):
            continue
        absolute = urljoin(page_url, href)
        try:
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if parsed.hostname and page_host and parsed.hostname != page_host:
            continue

        path = parsed.path or ''
        if not path.startswith('/catalog/'):
            continue
        if '/filter/' in path:
            continue
        # Нормализуем к виду с завершающим слешем — как в каталогах
        norm = path if path.endswith('/') else path + '/'
        if norm in known_category_paths or norm in known_filter_paths:
            continue
        segments = [s for s in norm.strip('/').split('/') if s]
        if len(segments) < 3:
            continue
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


# ── Сбор по всем категориям (первая страница, без пагинации) ────────


async def collect_product_links(
    project: dict,
    category_paths: list[str],
    known_filter_paths: list[str],
    *,
    concurrency: int = 8,
    timeout_s: int = 30,
    max_attempts: int = 2,
    proxy_url: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    log: Optional[Callable] = None,
    progress: Optional[Callable] = None,   # progress(done, total)
) -> dict:
    """
    Пройти все категории проекта (главный домен, первая страница листинга)
    и собрать ссылки на товары.

    Возвращает:
      {
        'links': [{'url': path, 'category': cat_path}, ...],
        'categories_total': N, 'categories_ok': N, 'categories_failed': N,
        'failed_categories': [path, ...],
      }
    """
    if proxy_url is None:
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')

    base = project['main_url'].rstrip('/')
    known_cats = {p if p.endswith('/') else p + '/' for p in category_paths}
    known_filters = {p if p.endswith('/') else p + '/' for p in known_filter_paths or []}

    sem = asyncio.Semaphore(concurrency)
    seen_products: set[str] = set()
    links: list[dict] = []
    failed: list[str] = []
    done = 0
    total = len(category_paths)

    headers = {
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:

        async def fetch_category(cat_path: str):
            nonlocal done
            url = f'{base}{cat_path}'
            async with sem:
                html = None
                for attempt in range(max_attempts):
                    try:
                        async with session.get(url, timeout=timeout, proxy=proxy_url) as resp:
                            if resp.status == 200:
                                html = await resp.text(errors='replace')
                            break
                    except Exception:
                        if attempt == max_attempts - 1:
                            break
                        await asyncio.sleep(1.5)

                done += 1
                if progress:
                    try:
                        progress(done, total)
                    except Exception:
                        pass

                if html is None:
                    failed.append(cat_path)
                    if log:
                        log('warn', f'Не удалось загрузить {url}')
                    return

                paths = extract_product_paths(html, url, known_cats, known_filters)
                for p in paths:
                    if p not in seen_products:
                        seen_products.add(p)
                        links.append({'url': p, 'category': cat_path})

        await asyncio.gather(*(fetch_category(c) for c in category_paths))

    if log:
        log('info', f'Собрано {len(links)} товарных ссылок '
                    f'с {total - len(failed)} из {total} категорий')

    return {
        'links': links,
        'categories_total': total,
        'categories_ok': total - len(failed),
        'categories_failed': len(failed),
        'failed_categories': failed,
    }


# ── Хранение базы в репозитории ─────────────────────────────────────


def _csv_path(project_id: str) -> Path:
    return CATALOGS_DIR / f'{project_id}-products.csv'


def _meta_path(project_id: str) -> Path:
    return CATALOGS_DIR / f'{project_id}-products-meta.json'


def save_product_links(project_id: str, collected: dict) -> Path:
    """Сохранить собранные ссылки в catalogs/ (для коммита в репозиторий)."""
    CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
    csv_file = _csv_path(project_id)
    with open(csv_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['url', 'category'])
        writer.writeheader()
        for row in collected['links']:
            writer.writerow(row)

    meta = {
        'collected_at': int(time.time() * 1000),
        'products_count': len(collected['links']),
        'categories_total': collected['categories_total'],
        'categories_ok': collected['categories_ok'],
        'categories_failed': collected['categories_failed'],
    }
    with open(_meta_path(project_id), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return csv_file


def load_product_links(project_id: str) -> Optional[dict]:
    """
    Прочитать базу товаров проекта из репозитория.

    Возвращает {'pathnames': [...], 'collected_at_ms': int, 'is_stale': bool,
                'categories_total': int, 'categories_ok': int} или None,
    если база ещё не собиралась.
    """
    csv_file = _csv_path(project_id)
    if not csv_file.exists():
        return None
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            pathnames = [row['url'] for row in csv.DictReader(f) if row.get('url')]
    except Exception:
        return None

    meta = {}
    if _meta_path(project_id).exists():
        try:
            with open(_meta_path(project_id), 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            meta = {}

    collected_at = meta.get('collected_at', 0)
    age_ms = time.time() * 1000 - collected_at
    return {
        'pathnames': pathnames,
        'collected_at_ms': collected_at,
        'is_stale': (collected_at == 0) or (age_ms > STALE_AFTER_MS),
        'categories_total': meta.get('categories_total', 0),
        'categories_ok': meta.get('categories_ok', 0),
    }
