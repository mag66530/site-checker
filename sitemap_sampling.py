"""
sitemap_sampling.py - случайная выборка страниц из карт сайта для чек-листа.

Идея: sitemap-индекс обычно разбит на файлы по видам страниц (products-1,
products-2, category, filters-1…). Проверять ВСЕ файлы каждый прогон дорого,
а то же самое видно на выборке. Поэтому: на каждый «вид» карты - один
случайный файл, из него - случайный пул ссылок. За несколько прогонов так
покрывается весь ассортимент видов, а не одна и та же горстка страниц.

«Вид» карты - имя файла без цифр и разделителей (-, _):
sitemap-products-1.xml и sitemap-products-2.xml → один вид «sitemapproducts».
Без словаря ключевых слов - работает для любого сайта и любого языка. Карта
без цифрового продолжения - тоже свой вид (берётся сама по себе).
"""
import random
import re
from typing import Optional
from urllib.parse import urlsplit

from sitemap import _sitemap_headers, _parse_urls, _fetch_one

_RE_INDEX = re.compile(r'<sitemapindex\b', re.I)
_RE_NONALPHA = re.compile(r'[-_\d]+')

MAX_INDEX_FILES = 200  # защита от вырожденных случаев (индекс индексов)


def kind_of(sitemap_url: str) -> str:
    """«Вид» карты по имени файла: без цифр/разделителей -_. Без словаря
    ключевых слов - работает для любого сайта. ЧИСТАЯ функция."""
    name = urlsplit(sitemap_url).path.rsplit('/', 1)[-1]
    name = name.rsplit('.', 1)[0]  # без расширения
    return _RE_NONALPHA.sub('', name).lower()


def group_by_kind(sitemap_urls: list[str]) -> dict[str, list[str]]:
    """Сгруппировать листовые карты по «виду». Порядок вставки сохраняется
    (важно для стабильного вывода в UI). ЧИСТАЯ функция."""
    groups: dict[str, list[str]] = {}
    for u in sitemap_urls:
        groups.setdefault(kind_of(u), []).append(u)
    return groups


def pick_one_per_kind(groups: dict[str, list[str]], excluded: set,
                      rng: Optional[random.Random] = None) -> list[str]:
    """По одному случайному файлу на «вид», кроме исключённых конкретных
    файлов (если у вида не осталось кандидатов - вид пропускается).
    ЧИСТАЯ функция."""
    rng = rng or random
    chosen = []
    for urls in groups.values():
        candidates = [u for u in urls if u not in excluded]
        if candidates:
            chosen.append(rng.choice(candidates))
    return chosen


# ── Сеть: находим листовые карты и качаем выбранные ─────────────────────


async def discover_child_sitemaps(host: str, *, proxy_url=None, log=None) -> list[str]:
    """Найти листовые карты сайта: Sitemap: из robots.txt → разворачиваем
    индекс(ы) рекурсивно до файлов, которые сами уже не <sitemapindex>.
    Карта без вложенного индекса - лист сама по себе."""
    import aiohttp
    from indexing_checker import fetch_robots

    async with aiohttp.ClientSession(headers=_sitemap_headers()) as session:
        info = await fetch_robots(session, host, proxy_url=proxy_url)
        if not info.sitemaps:
            if log:
                log('warn', f'{host}: в robots.txt нет строк Sitemap:')
            return []

        leaves: list[str] = []
        seen: set = set()
        queue = list(info.sitemaps)
        processed = 0
        while queue and processed < MAX_INDEX_FILES:
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            try:
                xml = await _fetch_one(session, u, proxy_url=proxy_url)
            except Exception as e:
                if log:
                    log('warn', f'Не удалось загрузить {u}: {e}')
                continue
            processed += 1
            if _RE_INDEX.search(xml):
                queue.extend(_parse_urls(xml))
            else:
                leaves.append(u)
        return leaves


async def sample_urls_from_sitemaps(sitemap_urls: list[str], urls_per_map: int,
                                    *, proxy_url=None,
                                    rng: Optional[random.Random] = None,
                                    log=None) -> list[str]:
    """Скачать выбранные листовые карты и взять случайный пул ссылок с
    каждой (до urls_per_map с файла)."""
    import aiohttp
    rng = rng or random
    out: list[str] = []
    async with aiohttp.ClientSession(headers=_sitemap_headers()) as session:
        for u in sitemap_urls:
            try:
                xml = await _fetch_one(session, u, proxy_url=proxy_url)
            except Exception as e:
                if log:
                    log('warn', f'Не удалось загрузить {u}: {e}')
                continue
            urls = [x for x in _parse_urls(xml) if x.startswith('http')]
            if len(urls) > urls_per_map:
                urls = rng.sample(urls, urls_per_map)
            out.extend(urls)
    return out


async def pick_sample_urls(groups: dict[str, list[str]], excluded: set,
                           urls_per_map: int, *, proxy_url=None,
                           rng: Optional[random.Random] = None,
                           log=None) -> list[str]:
    """Полный проход: 1 файл на вид → пул ссылок с каждого выбранного файла."""
    chosen = pick_one_per_kind(groups, excluded, rng=rng)
    if log:
        log('info', f'Карты сайта: выбрано {len(chosen)} файлов из {len(groups)} видов')
    return await sample_urls_from_sitemaps(
        chosen, urls_per_map, proxy_url=proxy_url, rng=rng, log=log)
