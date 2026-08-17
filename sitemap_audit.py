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
import asyncio
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

# Сколько адресов ИЗ карты прозваниваем на код ответа и noindex. Все нельзя:
# в карте бывают сотни тысяч записей (у Метпромко - 290 406), это отдельный
# прогон на часы. Берём срез с РАВНЫМ шагом по всему списку, а не первые N:
# карта обычно отсортирована по разделам, и первые N - это один раздел.
URL_PROBE_SAMPLE = 200
URL_PROBE_CONCURRENCY = 8
# Потолок времени на весь прозвон. Нужен именно потолок, а не только таймаут
# одного запроса: 200 адресов по 20 с на восьми потоках, да ещё со второй
# попыткой - это до 16 минут внутри получасового прогона. Что не успели -
# помечаем как «не проверено», а не как находку.
URL_PROBE_BUDGET_S = 180
_PROBE_HEAD_BYTES = 200_000   # сколько читать для поиска meta robots в <head>

_RE_XML_DECL_ENC = re.compile(r'<\?xml[^>]*encoding\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_LOOKS_XML = re.compile(r'\s*(<\?xml|<urlset\b|<sitemapindex\b)', re.I)
_RE_LOOKS_HTML = re.compile(r'\s*(<!doctype html|<html\b)', re.I)


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


def analyze_sitemap_file(url: str, data: bytes, content_type: str = '') -> dict:
    """Формат и кодировка одного файла карты сайта (доп. чек-лист).

    Формат: карта - это .xml или .txt. Проверяем не расширение (оно может быть
    любым, если отдаётся через обработчик), а СОДЕРЖИМОЕ: XML-карта начинается
    с <?xml/<urlset/<sitemapindex, txt-карта - это список адресов по строке.
    HTML вместо карты - типовая поломка: сайт отдаёт страницу с кодом 200, и
    робот читает пустую карту.

    Кодировка: байты обязаны читаться как UTF-8; если в XML-декларации указана
    другая кодировка - это тоже находка (робот поверит декларации).

    → {'kind': 'xml'|'txt'|'html'|'непонятный', 'format_why': str|None,
       'encoding_why': str|None, 'declared_encoding': str|None, 'text': str}
    ЧИСТАЯ функция - есть юнит-тест.
    """
    data = data or b''
    bom = data.startswith(b'\xef\xbb\xbf')
    body = data[3:] if bom else data
    try:
        text = body.decode('utf-8')
        enc_why = None
    except UnicodeDecodeError:
        text = body.decode('utf-8', errors='replace')
        enc_why = 'файл не читается как UTF-8'

    m = _RE_XML_DECL_ENC.search(text[:400])
    declared = (m.group(1).strip().lower() if m else None)
    if enc_why is None and declared and declared not in ('utf-8', 'utf8'):
        enc_why = f'в XML-декларации заявлена кодировка {declared}, а не UTF-8'

    ct = (content_type or '').split(';')[0].strip().lower()
    if _RE_LOOKS_HTML.match(text):
        kind, fmt_why = 'html', 'вместо карты сайта отдаётся HTML-страница'
    elif _RE_LOOKS_XML.match(text):
        kind, fmt_why = 'xml', None
    elif _txt_sitemap_urls(text):
        # txt-карта: непустой список адресов, по одному в строке.
        kind, fmt_why = 'txt', None
    else:
        kind = 'непонятный'
        fmt_why = (f'содержимое не похоже ни на XML, ни на список адресов'
                   + (f' (Content-Type: {ct})' if ct else ''))
    return {'kind': kind, 'format_why': fmt_why, 'encoding_why': enc_why,
            'declared_encoding': declared, 'content_type': ct, 'text': text}


def _txt_sitemap_urls(text: str) -> list:
    """Адреса из txt-карты: по одному в строке, только http(s). Пустой список -
    значит это не txt-карта. ЧИСТАЯ функция - есть юнит-тест."""
    out = []
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s or s.startswith('#'):
            continue
        if not s.lower().startswith(('http://', 'https://')) or ' ' in s:
            return []                    # хоть одна «не строка-адрес» - не txt
        out.append(s)
    return out


def pick_probe_sample(urls: list, limit: int = URL_PROBE_SAMPLE) -> list:
    """Срез адресов для прозвона: равный шаг по всему списку, порядок сохранён.
    Меньше лимита - берём всё. ЧИСТАЯ функция - есть юнит-тест."""
    urls = [u for u in (urls or []) if u]
    if limit <= 0:
        return []
    if len(urls) <= limit:
        return list(urls)
    step = len(urls) / limit
    return [urls[int(i * step)] for i in range(limit)]


async def probe_sitemap_urls(urls: list, *, proxy_url=None,
                             limit: int = URL_PROBE_SAMPLE,
                             concurrency: int = URL_PROBE_CONCURRENCY,
                             budget_s: float = URL_PROBE_BUDGET_S,
                             log=None) -> dict:
    """Прозвон адресов ИЗ карты: код ответа и meta robots noindex.

    Зачем именно так:
      • редиректы НЕ ходим - 301 в карте сайта сам по себе находка, «куда
        привёл» не меняет того, что в карте лежит устаревший адрес;
      • берём GET, а не HEAD: заодно нужен <head> для meta robots, а часть
        сайтов на HEAD отвечает иначе, чем на обычный запрос;
      • noindex ищем и в meta, и в заголовке X-Robots-Tag - равноправные
        сигналы, второй в HTML не виден вовсе.

    Сеть отдельно от кодов ответа: таймаут или обрыв - это НЕ «страница
    отвечает не 200», это «проверить не удалось». Свалить их в одну кучу
    значит отправить клиенту в работу живые адреса, до которых не доехали мы
    сами. Поэтому на сетевой сбой даём вторую попытку, и только потом
    записываем адрес в unreachable - отдельным, мягким списком.

    → {'checked', 'sample_of', 'bad_status': [{'url','status'}],
       'noindex': [{'url','signal'}], 'unreachable': [{'url','why'}],
       'blocked': int, 'error': str|None}
    """
    import aiohttp
    from indexing_checker import (_find_meta_robots, _x_robots_noindex,
                                  is_blocked_status)
    from sitemap import _sitemap_headers

    sample = pick_probe_sample(urls, limit)
    out = {'checked': 0, 'sample_of': len(urls or []), 'bad_status': [],
           'noindex': [], 'unreachable': [], 'blocked': 0, 'skipped': 0,
           'error': None}
    if not sample:
        return out
    sem = asyncio.Semaphore(max(1, concurrency))
    дедлайн = time.monotonic() + max(0.0, budget_s)

    def _почему(e: Exception) -> str:
        """Текст сетевой ошибки. У таймаутов aiohttp str(e) пустой - тогда
        берём имя класса, иначе в отчёт уходит «нет ответа: »."""
        return (str(e) or '').strip() or type(e).__name__

    async def one(session, url):
        последняя = ''
        for попытка in (1, 2):          # вторая попытка - против случайных таймаутов
            if time.monotonic() >= дедлайн:
                # Бюджет вышел: честно «не проверено». Первая попытка уже
                # могла упасть - но раз времени нет, выводов не делаем.
                return {'url': url, 'status': None, 'skipped': True}
            async with sem:
                try:
                    async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=20),
                            allow_redirects=False, proxy=proxy_url) as r:
                        status = r.status
                        hdrs = {k.lower(): v for k, v in r.headers.items()}
                        body = b''
                        if status == 200:
                            body = await r.content.read(_PROBE_HEAD_BYTES)
                    break
                except Exception as e:      # noqa: BLE001
                    последняя = _почему(e)
            if попытка == 2:
                return {'url': url, 'status': None, 'error': последняя}
        html = body.decode('utf-8', errors='replace') if body else ''
        _, meta_noidx = _find_meta_robots(html) if html else (None, False)
        x_val, x_noidx = _x_robots_noindex(hdrs)
        return {'url': url, 'status': status, 'meta_noindex': meta_noidx,
                'x_noindex': x_noidx, 'x_val': x_val, 'error': None,
                'blocked': is_blocked_status(status)}

    try:
        async with aiohttp.ClientSession(
                headers=_sitemap_headers(url=sample[0])) as session:
            результаты = await asyncio.gather(
                *(one(session, u) for u in sample))
    except Exception as e:              # noqa: BLE001
        out['error'] = str(e)
        return out

    for r in результаты:
        if r.get('skipped'):
            out['skipped'] += 1
            continue
        out['checked'] += 1
        st = r.get('status')
        if st is None:
            # До адреса не доехали мы - в находки клиенту это не идёт.
            out['unreachable'].append({'url': r['url'],
                                       'why': r.get('error') or 'нет ответа'})
            continue
        if r.get('blocked'):
            # 403/429/503 - это про защиту сайта, а не про адрес в карте.
            out['blocked'] += 1
            continue
        if st != 200:
            out['bad_status'].append({'url': r['url'], 'status': st})
            continue
        if r.get('meta_noindex') or r.get('x_noindex'):
            out['noindex'].append({
                'url': r['url'],
                'signal': ('X-Robots-Tag: ' + (r.get('x_val') or 'noindex')
                           if r.get('x_noindex') else 'meta robots: noindex')})
    if log:
        log('info', f'Прозвон карты сайта: проверено {out["checked"]} из '
                    f'{out["sample_of"]}, не 200 - {len(out["bad_status"])}, '
                    f'с noindex - {len(out["noindex"])}, '
                    f'не доехали - {len(out["unreachable"])}'
                    + (f', не успели за {int(budget_s)} с - {out["skipped"]}'
                       if out['skipped'] else ''))
    return out


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
                        known_services=None, log=None,
                        probe_urls: int = URL_PROBE_SAMPLE) -> dict:
    """Скачать sitemap (с обходом индекса) и проверить структуру записей.

    probe_urls - сколько адресов ИЗ карты прозвонить на код ответа и noindex
    (0 - не звонить вовсе).

    Возвращает {'files': n, 'total': n, 'bad_urls': [{'url','why'}, …],
                'with_lastmod': n, 'with_changefreq': n, 'with_priority': n,
                'lastmod_dates': {url: lastmod}, 'is_index': bool,
                'file_stats': [{'url','urls','bytes'}], 'truncated': bool,
                'index_children': [{'url','type'}], 'index_types': [str],
                'missing_catalog': {...}|None,
                'format_issues': [{'url','why'}], 'encoding_issues': [...],
                'url_probe': {...}|None, 'error': str|None}."""
    import aiohttp
    from urllib.parse import urlsplit
    from sitemap import _sitemap_headers
    out = {'files': 0, 'total': 0, 'bad_urls': [],
           'with_lastmod': 0, 'with_changefreq': 0, 'with_priority': 0,
           'lastmod_dates': {}, 'is_index': False, 'file_stats': [],
           'truncated': False, 'index_children': [], 'index_types': [],
           'missing_catalog': None, 'format_issues': [], 'encoding_issues': [],
           'url_probe': None, 'error': None}
    my_host = _norm_host(host)
    sm_paths = set()          # нормализованные пути всех URL из sitemap
    all_locs = []             # абсолютные адреса из карты - для прозвона
    seen, queue = set(), [root_url]
    try:
        # url= - вход на закрытый сайт (там и sitemap за паролем).
        async with aiohttp.ClientSession(
                headers=_sitemap_headers(url=root_url)) as session:
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
                        _ct = r.headers.get('Content-Type', '')
                except Exception as e:
                    if not out['files']:
                        out['error'] = f'sitemap не скачался: {e}'
                        return out
                    continue
                # Формат и кодировка файла (доп. чек-лист). Текст берём отсюда:
                # он уже без BOM и с честным разбором UTF-8.
                _f = analyze_sitemap_file(u, data, _ct)
                xml = _f['text']
                if _f['format_why'] and len(out['format_issues']) < 20:
                    out['format_issues'].append({'url': u, 'why': _f['format_why']})
                if _f['encoding_why'] and len(out['encoding_issues']) < 20:
                    out['encoding_issues'].append({'url': u,
                                                   'why': _f['encoding_why']})
                out['files'] += 1
                if _f['kind'] == 'txt':
                    # txt-карта: просто список адресов, без lastmod/priority.
                    _txt_urls = _txt_sitemap_urls(xml)
                    for loc in _txt_urls:
                        if out['total'] >= MAX_ENTRIES:
                            out['truncated'] = True
                            break
                        out['total'] += 1
                        all_locs.append(loc)
                        try:
                            sm_paths.add(_norm_path(urlsplit(loc).path))
                        except Exception:
                            pass
                    out['file_stats'].append(
                        {'url': u, 'urls': len(_txt_urls), 'bytes': len(data)})
                    continue
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
                    all_locs.append(loc)
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

        # ── Адреса ИЗ карты: код ответа и noindex (доп. чек-лист) ──
        # Своим запросом, срезом по всей карте: см. URL_PROBE_SAMPLE.
        if probe_urls and all_locs:
            out['url_probe'] = await probe_sitemap_urls(
                all_locs, proxy_url=proxy_url, limit=probe_urls, log=log)
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

    Возвращает {'url': str|None, 'status': int|None, 'blocked': int|None,
                'junk_links': [{'url','label'}], 'error': str|None}.

    blocked - код вида 403/429/503, если сайт не пустил пробу. Тогда «карта не
    найдена» сказать нельзя: мы просто не смогли посмотреть."""
    import aiohttp
    from urllib.parse import urlsplit, urljoin
    from sitemap import _sitemap_headers
    from indexing_checker import is_blocked_status
    out = {'url': None, 'status': None, 'blocked': None,
           'junk_links': [], 'error': None}
    try:
        async with aiohttp.ClientSession(
                headers=_sitemap_headers(url=f'https://{host}/')) as session:
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
                        if out['blocked'] is None and is_blocked_status(r.status):
                            out['blocked'] = r.status
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
