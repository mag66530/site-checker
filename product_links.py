"""
product_links.py – база товарных ссылок, собранных с листингов категорий.

Зачем: товары для проверки раньше брались только из sitemap.xml. Sitemap
может содержать удалённые или скрытые товары и не отражает того, что реально
видит покупатель. Здесь товары собираются с самих страниц категорий – с тех
же карточек, по которым ходит пользователь.

Как устроено (согласованная схема):
  1. Скрипт collect_products.py проходит ВСЕ категории проекта
     (главный домен, ТОЛЬКО первая страница листинга – пагинация не нужна).
  2. С каждой страницы собирает ссылки на карточки товаров.
  3. Результат сохраняется в репозитории:
        catalogs/{proj}-products.csv        – сами ссылки (path + категория)
        catalogs/{proj}-products-meta.json  – когда и как собрано
     На Streamlit Cloud месячный кэш на диске не живёт (контейнер
     пересоздаётся), поэтому база лежит в git и обновляется вручную
     раз в месяц: запустить скрипт → закоммитить → задеплоить.
  4. Приложение читает базу из репозитория, показывает количество товаров
     и дату сбора в интерфейсе. Через 30 дней база считается устаревшей –
     интерфейс подсвечивает, что пора пересобрать.

Распознавание ссылки на товар – та же логика, что в sitemap.py:
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

# Контейнеры карточек товара у наших проектов (СМУ / МПЭ / ИМП). Карточку
# узнаём по классу-контейнеру и берём ссылку ИЗ НЕЁ – так ловим и ИМП, где
# карточка ведёт на КОРНЕВОЙ адрес товара (/slug/), а не под /catalog/.
_CARD_SPLIT_RE = re.compile(
    r'\b(?:catalog-product-card-item|card-product|card-item|listing-card)\b'
)
# Ассеты/статика – не товар.
_ASSET_RE = re.compile(
    r'\.(?:svg|png|jpe?g|gif|webp|bmp|ico|css|js|woff2?|ttf|pdf)(?:\?|$)', re.I)
# Незарендеренные шаблонные переменные, попавшие в href (битый JS-шаблон).
_TEMPLATE_JUNK = ('${', '{{', '}}', '<%', '%7b', '%7d')


def _clean_href(href: str, page_url: str, page_host: str) -> str:
    """Нормализовать href карточки к пути или вернуть '' (не товар/мусор)."""
    href = (href or '').strip()
    if not href or href[0] == '#':
        return ''
    low = href.lower()
    if low.startswith(('mailto:', 'tel:', 'javascript:')):
        return ''
    if any(j in low for j in _TEMPLATE_JUNK):
        return ''                      # битый шаблон ${...}/{{...}} – не ссылка
    if _ASSET_RE.search(low):
        return ''
    try:
        parsed = urlparse(urljoin(page_url, href))
    except ValueError:
        return ''
    if parsed.hostname and page_host and parsed.hostname != page_host:
        return ''
    path = parsed.path or ''
    if not path or path == '/' or '/filter/' in path \
            or '/catalog/view/' in path or '/assets/' in path:
        return ''
    return path if path.endswith('/') else path + '/'


def _looks_like_product(path: str) -> bool:
    """Похоже на товар: под /catalog/ с 3+ сегментами (СМУ/МПЭ) ЛИБО корневой
    длинный slug с дефисами (ИМП: /list-otsinkovannyj-…-nlmk/), а не /about/."""
    segs = [s for s in path.strip('/').split('/') if s]
    if not segs:
        return False
    if path.startswith('/catalog/'):
        return len(segs) >= 3
    if len(segs) == 1:
        s = segs[0]
        return len(s) >= 12 and s.count('-') >= 2
    return False


def _is_facet_listing(path: str, known_paths: set[str]) -> bool:
    """URL-фильтр вида <категория>/<характеристика>/<значение>/ – это
    отфильтрованный листинг, а не карточка товара (так устроен ИМП). Узнаём по
    тому, что без последних 2 (или 4) сегментов остаётся известная категория/тег."""
    segs = [s for s in path.strip('/').split('/') if s]
    if len(segs) < 4:
        return False
    for cut in (2, 4):
        if len(segs) - cut < 1:
            break
        parent = '/' + '/'.join(segs[:-cut]) + '/'
        if parent in known_paths:
            return True
    return False


# ── Извлечение товарных ссылок из HTML листинга ─────────────────────


def extract_product_paths(
    html: str,
    page_url: str,
    known_category_paths: set[str],
    known_filter_paths: set[str],
) -> list[str]:
    """
    Достать из HTML страницы-листинга пути карточек товаров.

    Берём ссылку из каждой карточки товара (контейнер card-product / card-item /
    catalog-product-card-item / listing-card). Так корректно ловятся и товары
    ИМП, чей адрес – КОРНЕВОЙ slug (/list-otsinkovannyj-…/), а не путь под
    /catalog/. Известные категории/фильтры, /filter/, ассеты и битые шаблонные
    ссылки (${…}) отбрасываются.
    """
    if not html:
        return []
    page_host = urlparse(page_url).hostname or ''
    seen: set[str] = set()
    out: list[str] = []

    def consider(path: str) -> None:
        if (not path or path in seen
                or path in known_category_paths or path in known_filter_paths
                or not _looks_like_product(path)):
            return
        seen.add(path)
        out.append(path)

    chunks = _CARD_SPLIT_RE.split(html)
    found_cards = len(chunks) > 1
    for chunk in chunks[1:]:
        # первая «товарная» ссылка в карточке – это сам товар (а не иконка/счётчик)
        for m in _HREF_RE.finditer(chunk[:4000]):
            path = _clean_href(m.group(1), page_url, page_host)
            if path and _looks_like_product(path):
                consider(path)
                break

    # Фоллбэк для листингов без распознанных карточек – по ссылкам /catalog/,
    # отбрасывая URL-фильтры (категория + /характеристика/значение/).
    if not found_cards:
        known = known_category_paths | known_filter_paths
        for m in _HREF_RE.finditer(html):
            path = _clean_href(m.group(1), page_url, page_host)
            if (path and path.startswith('/catalog/')
                    and not _is_facet_listing(path, known)):
                consider(path)
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
    retry_failed: bool = True,
    proxy_url: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    log: Optional[Callable] = None,
    progress: Optional[Callable] = None,   # progress(done, total)
) -> dict:
    """
    Пройти все категории проекта (главный домен, первая страница листинга)
    и собрать ссылки на товары.

    Сначала основной проход с заданной параллельностью. Часть категорий под
    нагрузкой отдаёт таймаут/5xx – поэтому, если retry_failed=True, по упавшим
    делается второй, мягкий проход с низкой параллельностью: так возвращается
    почти всё, что отвалилось не из-за реального 404, а из-за нагрузки.

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

    seen_products: set[str] = set()
    links: list[dict] = []
    total = len(category_paths)
    done = 0

    headers = {
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)

    # Транзиентные коды – повторяем. 404/403/410 – устойчивый результат, не ретраим.
    _RETRY_CODES = {429, 500, 502, 503, 504}

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:

        async def fetch_category(cat_path, sem, timeout, out_failed, count_progress):
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
                            if resp.status not in _RETRY_CODES:
                                break       # 404/403 и т.п. – повторять бессмысленно
                    except Exception:
                        pass
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(1.5)

                if count_progress:
                    done += 1
                    if progress:
                        try:
                            progress(done, total)
                        except Exception:
                            pass

                if html is None:
                    out_failed.append(cat_path)
                    return
                for p in extract_product_paths(html, url, known_cats, known_filters):
                    if p not in seen_products:
                        seen_products.add(p)
                        links.append({'url': p, 'category': cat_path})

        # ── Проход 1: все категории, заданная параллельность ──
        sem1 = asyncio.Semaphore(concurrency)
        timeout1 = aiohttp.ClientTimeout(total=timeout_s)
        failed_1: list[str] = []
        await asyncio.gather(*(
            fetch_category(c, sem1, timeout1, failed_1, True) for c in category_paths
        ))

        # ── Проход 2: добиваем упавшие – мягко, низкая параллельность ──
        failed_final = failed_1
        if retry_failed and failed_1:
            if log:
                log('info', f'Повторный проход по {len(failed_1)} упавшим категориям '
                            f'(мягче, параллельность {max(2, concurrency // 3)})…')
            sem2 = asyncio.Semaphore(max(2, concurrency // 3))
            timeout2 = aiohttp.ClientTimeout(total=timeout_s + 30)
            failed_2: list[str] = []
            await asyncio.gather(*(
                fetch_category(c, sem2, timeout2, failed_2, False) for c in failed_1
            ))
            failed_final = failed_2

    if log:
        log('info', f'Собрано {len(links)} товарных ссылок '
                    f'с {total - len(failed_final)} из {total} категорий')

    return {
        'links': links,
        'categories_total': total,
        'categories_ok': total - len(failed_final),
        'categories_failed': len(failed_final),
        'failed_categories': failed_final,
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
