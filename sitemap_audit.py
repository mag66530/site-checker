"""
sitemap_audit.py - аудит карты сайта (часть пункта 1.7, ТЗ 3.4).

ТЗ 3.4.2 - sitemap корректно настроен:
  • URL внутри абсолютные, https и своего хоста (http/чужой хост = баг);
  • у страниц есть lastmod / changefreq / priority - по протоколу поля
    опциональны, но ТЗ их требует: полное отсутствие = предупреждение.
ТЗ 3.4.3 - даты не генерируются динамически:
  • все lastmod одинаковые И свежие (сегодня/вчера) = подозрение;
  • снапшот дат хранится между прогонами: если с прошлого прогона
    «обновились» почти все даты - это динамическая генерация, а не
    реальные правки.

Доп. чек-лист «Sitemap.xml»:
  • лимиты на файл: >10 000 ссылок или >10 МБ = предупреждение (лимит
    допа), >50 000 или >50 МБ = баг (нарушение протокола sitemap);
  • структура: индекс-файл или одиночный; записей много (>10k), а
    индекса нет = предупреждение;
  • полнота: все категории/фильтры из CSV-выгрузки каталога должны быть
    в sitemap - отсутствие = баг (проверяется только при ПОЛНОМ обходе,
    без упора в лимиты MAX_SITEMAPS/MAX_ENTRIES);
  • HTML-карта сайта (/sitemap/): существует и не содержит ссылок на
    служебные страницы (корзина/ЛК/поиск/админка…).

Sitemap-индекс обходится рекурсивно (лимит файлов). Работает по тому же
адресу, что и загрузка товаров (sitemap_url проекта / robots).
"""
import json
import re
import time
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(__file__).parent / 'cache'
CACHE_DIR.mkdir(exist_ok=True)

_RE_INDEX = re.compile(r'<sitemapindex\b', re.I)
_RE_SM_LOC = re.compile(r'<loc>\s*(.*?)\s*</loc>', re.I | re.S)
_RE_URL_BLOCK = re.compile(r'<url>(.*?)</url>', re.I | re.S)
_RE_LASTMOD = re.compile(r'<lastmod>\s*(.*?)\s*</lastmod>', re.I | re.S)
_RE_CHANGEFREQ = re.compile(r'<changefreq>', re.I)
_RE_PRIORITY = re.compile(r'<priority>', re.I)

MAX_SITEMAPS = 10        # файлов индекса за прогон
MAX_ENTRIES = 20000      # записей <url> суммарно
SNAPSHOT_SAMPLE = 1000   # сколько пар url→lastmod хранить между прогонами


def _norm_host(h: str) -> str:
    h = (h or '').lower()
    return h[4:] if h.startswith('www.') else h


def _norm_path(p: str) -> str:
    """Нормализация пути для сверки каталог ↔ sitemap."""
    return '/' + (p or '').strip().strip('/').lower() + '/'


# Классификация дочерних sitemap по имени файла (доп. чек-лист, п.5):
# индекс должен дробить карту по типам страниц. Ключи ищем в имени loc.
_SM_TYPE_KEYS = (
    ('категории', ('categ', 'catalog', 'razdel', 'section', 'rubric')),
    ('фильтры',   ('filter', 'tag', 'teg', 'prop')),
    ('товары',    ('product', 'goods', 'tovar', 'item', 'element', 'offer')),
    ('услуги',    ('uslug', 'service', 'proizvodstvo', 'rabot')),
)


def _sitemap_type(loc: str) -> str:
    """Тип дочернего sitemap по имени файла; не опознан → 'прочее'.
    Слово «sitemap» вырезаем: оно содержит подстроку «item» и иначе
    ловило бы каждый файл в «товары»."""
    low = (loc or '').lower().replace('sitemap', '')
    for name, keys in _SM_TYPE_KEYS:
        if any(k in low for k in keys):
            return name
    return 'прочее'


async def audit_sitemap(root_url: str, host: str, *, proxy_url=None,
                        known_categories=None, known_filters=None,
                        known_services=None, log=None) -> dict:
    """Скачать sitemap (с обходом индекса) и проверить структуру записей.

    Возвращает {'files': n, 'total': n, 'bad_urls': [{'url','why'}, …],
                'with_lastmod': n, 'with_changefreq': n, 'with_priority': n,
                'lastmod_dates': {url: lastmod}, 'is_index': bool,
                'file_stats': [{'url','urls','bytes'}], 'truncated': bool,
                'index_children': [{'url','type'}], 'index_types': [str],
                'missing_catalog': {...}|None, 'error': str|None}."""
    import aiohttp
    from urllib.parse import urlsplit
    from sitemap import _sitemap_headers
    out = {'files': 0, 'total': 0, 'bad_urls': [],
           'with_lastmod': 0, 'with_changefreq': 0, 'with_priority': 0,
           'lastmod_dates': {}, 'is_index': False, 'file_stats': [],
           'truncated': False, 'index_children': [], 'index_types': [],
           'missing_catalog': None, 'error': None}
    my_host = _norm_host(host)
    sm_paths = set()          # нормализованные пути всех URL из sitemap
    seen, queue = set(), [root_url]
    try:
        async with aiohttp.ClientSession(headers=_sitemap_headers()) as session:
            while queue:
                if out['files'] >= MAX_SITEMAPS or out['total'] >= MAX_ENTRIES:
                    out['truncated'] = True
                    break
                u = queue.pop(0)
                if u in seen:
                    continue
                seen.add(u)
                try:
                    async with session.get(
                            u, timeout=aiohttp.ClientTimeout(total=30),
                            proxy=proxy_url) as r:
                        if r.status != 200:
                            if not out['files']:
                                out['error'] = f'sitemap отдаёт HTTP {r.status}'
                                return out
                            continue
                        data = await r.read()
                        xml = data.decode('utf-8', errors='replace')
                except Exception as e:
                    if not out['files']:
                        out['error'] = f'sitemap не скачался: {e}'
                        return out
                    continue
                out['files'] += 1
                if _RE_INDEX.search(xml):
                    if u == root_url:
                        out['is_index'] = True
                    _children = _RE_SM_LOC.findall(xml)
                    for _ch in _children:
                        _ch = (_ch or '').strip()
                        if _ch:
                            out['index_children'].append(
                                {'url': _ch, 'type': _sitemap_type(_ch)})
                    queue.extend(_children)
                    continue
                _file_urls = 0
                for block in _RE_URL_BLOCK.finditer(xml):
                    if out['total'] >= MAX_ENTRIES:
                        out['truncated'] = True
                        break
                    b = block.group(1)
                    m = _RE_SM_LOC.search(b)
                    loc = (m.group(1) if m else '').strip()
                    if not loc:
                        continue
                    out['total'] += 1
                    _file_urls += 1
                    try:
                        sm_paths.add(_norm_path(urlsplit(loc).path))
                    except Exception:
                        pass
                    # ТЗ 3.4.2: правильный URL - абсолютный, https, свой хост
                    sp = urlsplit(loc)
                    if not sp.scheme:
                        _why = 'не абсолютный URL'
                    elif sp.scheme != 'https':
                        _why = 'не https'
                    elif _norm_host(sp.netloc) != my_host:
                        _why = 'чужой хост'
                    else:
                        _why = None
                    if _why and len(out['bad_urls']) < 50:
                        out['bad_urls'].append({'url': loc, 'why': _why})
                    lm = _RE_LASTMOD.search(b)
                    if lm:
                        out['with_lastmod'] += 1
                        if len(out['lastmod_dates']) < SNAPSHOT_SAMPLE:
                            # только дата, без времени - для сравнения снапшотов
                            out['lastmod_dates'][loc] = lm.group(1).strip()[:10]
                    if _RE_CHANGEFREQ.search(b):
                        out['with_changefreq'] += 1
                    if _RE_PRIORITY.search(b):
                        out['with_priority'] += 1
                out['file_stats'].append(
                    {'url': u, 'urls': _file_urls, 'bytes': len(data)})
            if queue:
                out['truncated'] = True

        # Типы, на которые разбит индекс (п.5) - по именам дочерних файлов
        out['index_types'] = sorted(
            {c['type'] for c in out['index_children']})

        # ── Полнота: категории/фильтры/услуги из выгрузки есть в sitemap ──
        # Только при полном обходе: при упоре в лимиты «отсутствие» пути
        # ничего не значит - он мог быть в непрочитанной части.
        if ((known_categories or known_filters or known_services)
                and not out['truncated']):
            def _missing(paths):
                return [p for p in (paths or [])
                        if _norm_path(p) not in sm_paths]
            out['missing_catalog'] = {
                'categories': _missing(known_categories)[:50],
                'filters': _missing(known_filters)[:50],
                'services': _missing(known_services)[:50],
            }
    except Exception as e:
        out['error'] = str(e)
    return out


# ── Доп. чек-лист: HTML-карта сайта ──────────────────────────────────

# Служебные пути, которых не должно быть в HTML-карте (тот же смысл,
# что «мусор» в robots): корзина/ЛК/поиск/сравнение/заказ/админка.
_HTML_MAP_JUNK = ('/basket/', '/cart/', '/compare/', '/search/', '/auth/',
                  '/personal/', '/order/', '/checkout/', '/bitrix/', '/admin/')
_RE_HREF = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\']', re.I)


async def audit_html_sitemap(host: str, *, proxy_url=None) -> dict:
    """HTML-карта сайта (доп. чек-лист): существует по типовому адресу
    и не содержит ссылок на служебные страницы.

    Возвращает {'url': str|None, 'status': int|None,
                'junk_links': [{'url','label'}], 'error': str|None}."""
    import aiohttp
    from urllib.parse import urlsplit, urljoin
    from sitemap import _sitemap_headers
    out = {'url': None, 'status': None, 'junk_links': [], 'error': None}
    try:
        async with aiohttp.ClientSession(headers=_sitemap_headers()) as session:
            html = None
            for path in ('/sitemap/', '/sitemap.html'):
                u = f'https://{host}{path}'
                try:
                    async with session.get(
                            u, timeout=aiohttp.ClientTimeout(total=30),
                            allow_redirects=True, proxy=proxy_url) as r:
                        if r.status == 200:
                            out['url'], out['status'] = u, 200
                            html = (await r.read()).decode(
                                'utf-8', errors='replace')
                            break
                        if out['status'] is None:
                            out['url'], out['status'] = u, r.status
                except Exception as e:
                    if out['error'] is None:
                        out['error'] = str(e)
            if html:
                my_host = _norm_host(host)
                seen = set()
                for m in _RE_HREF.finditer(html):
                    link = urljoin(out['url'], m.group(1).strip())
                    sp = urlsplit(link)
                    if _norm_host(sp.netloc) != my_host:
                        continue
                    p = (sp.path or '/').lower()
                    if not p.endswith('/'):
                        p += '/'
                    for junk in _HTML_MAP_JUNK:
                        if p.startswith(junk) and link not in seen:
                            seen.add(link)
                            out['junk_links'].append(
                                {'url': link, 'label': junk})
                            break
                    if len(out['junk_links']) >= 20:
                        break
    except Exception as e:
        out['error'] = out['error'] or str(e)
    return out


# ── ТЗ 3.4.3: динамические даты (снапшот между прогонами) ────────────


def _snapshot_path(project_id: str) -> Path:
    return CACHE_DIR / f'sitemap_lastmod_{project_id}.json'


def analyze_lastmod(project_id: str, audit: dict) -> dict:
    """Сравнить lastmod с прошлым прогоном + эвристика «все даты свежие».

    Возвращает {'all_same_fresh': bool, 'changed_ratio': float|None,
                'prev_days_ago': int|None, 'warnings': [str]}."""
    from datetime import date, timedelta
    warnings = []
    dates = audit.get('lastmod_dates') or {}
    out = {'all_same_fresh': False, 'changed_ratio': None,
           'prev_days_ago': None, 'warnings': warnings}

    # Эвристика: ВСЕ lastmod одинаковые и это сегодня/вчера - похоже на
    # динамическую генерацию даты «на лету».
    if len(dates) >= 20:
        uniq = set(dates.values())
        if len(uniq) == 1:
            d = next(iter(uniq))
            fresh = {str(date.today()), str(date.today() - timedelta(days=1))}
            if d in fresh:
                out['all_same_fresh'] = True
                warnings.append('все lastmod в sitemap одинаковые и свежие '
                                '(сегодня/вчера) - похоже, даты генерируются '
                                'динамически, а не по реальным правкам')

    # Снапшот: сколько дат «обновилось» с прошлого прогона.
    p = _snapshot_path(project_id)
    prev = None
    try:
        if p.exists():
            prev = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        prev = None
    if prev and dates:
        prev_dates = prev.get('dates') or {}
        common = [u for u in dates if u in prev_dates]
        if len(common) >= 20:
            changed = sum(1 for u in common if dates[u] != prev_dates[u])
            ratio = changed / len(common)
            out['changed_ratio'] = round(ratio, 2)
            days_ago = max(0, int((time.time() - prev.get('ts', 0)) / 86400))
            out['prev_days_ago'] = days_ago
            if ratio > 0.9:
                warnings.append(
                    f'с прошлого прогона ({days_ago} дн. назад) «обновились» '
                    f'{int(ratio * 100)}% дат lastmod - похоже на динамическую '
                    f'генерацию дат, а не реальные правки страниц')
    # Сохраняем свежий снапшот (даже если сравнить было не с чем)
    if dates:
        try:
            p.write_text(json.dumps({'ts': time.time(), 'dates': dates},
                                    ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass
    return out
