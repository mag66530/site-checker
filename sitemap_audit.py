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


async def audit_sitemap(root_url: str, host: str, *, proxy_url=None,
                        log=None) -> dict:
    """Скачать sitemap (с обходом индекса) и проверить структуру записей.

    Возвращает {'files': n, 'total': n, 'bad_urls': [{'url','why'}, …],
                'with_lastmod': n, 'with_changefreq': n, 'with_priority': n,
                'lastmod_dates': {url: lastmod}, 'error': str|None}."""
    import aiohttp
    from urllib.parse import urlsplit
    from sitemap import _sitemap_headers
    out = {'files': 0, 'total': 0, 'bad_urls': [],
           'with_lastmod': 0, 'with_changefreq': 0, 'with_priority': 0,
           'lastmod_dates': {}, 'error': None}
    my_host = _norm_host(host)
    seen, queue = set(), [root_url]
    try:
        async with aiohttp.ClientSession(headers=_sitemap_headers()) as session:
            while queue and out['files'] < MAX_SITEMAPS and out['total'] < MAX_ENTRIES:
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
                        xml = await r.text()
                except Exception as e:
                    if not out['files']:
                        out['error'] = f'sitemap не скачался: {e}'
                        return out
                    continue
                out['files'] += 1
                if _RE_INDEX.search(xml):
                    queue.extend(_RE_SM_LOC.findall(xml))
                    continue
                for block in _RE_URL_BLOCK.finditer(xml):
                    if out['total'] >= MAX_ENTRIES:
                        break
                    b = block.group(1)
                    m = _RE_SM_LOC.search(b)
                    loc = (m.group(1) if m else '').strip()
                    if not loc:
                        continue
                    out['total'] += 1
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
    except Exception as e:
        out['error'] = str(e)
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
