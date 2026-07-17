"""
metrika_api.py - 404-страницы из Яндекс.Метрики за СЕГОДНЯ через Reporting API.

Почтовые отчёты (metrika_404.py) приходят с задержкой (за вчера). Этот модуль
тянет данные за сегодня напрямую из API:
  Отчёты → Содержание → «Заголовки страниц» = измерение ym:pv:title (+ ym:pv:URL).
404-страницы опознаём по заголовку (маркеры _404_MARKERS).

Авторизация: OAuth-токен Яндекса со scope `metrika:read`.
Секрет: metrika_oauth_<pid> (или общий metrika_oauth).
counter_id - ниже в COUNTER_IDS.
"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

try:
    import requests
except ImportError:
    requests = None

from metrika_404 import Report404, Page404

API_URL = 'https://api-metrika.yandex.net/stat/v1/data'
COUNTERS_URL = 'https://api-metrika.yandex.net/management/v1/counters'

# Основной счётчик проекта (для совместимости / fallback).
COUNTER_IDS = {
    'mpe': '99551890',
    'smu': '15630172',
    'imp': '94649678',
}

# Авто-дискавери счётчиков для проектов с сотнями счётчиков (по городам).
# Берём из Management API все счётчики, где site содержит include и НЕ содержит
# ни одного exclude (Яндекс.Карты, pulscen и т.п.). Результат кешируется.
COUNTER_AUTO = {
    'mpe': {'include': 'mepen', 'exclude': ['yandex', 'pulscen', 'maps', 'карты']},
}

# У проекта несколько счётчиков (по доменам стран) - 404 собираем из ВСЕХ.
COUNTER_GROUPS = {
    'smu': [
        '15630172',  # stalmetural.ru
        '92479924',  # stalmetural.kz
        '92597022',  # stalmetural.kg
        '92480064',  # stalmetural.am
        '92628866',  # stalmetural.by
        '92479352',  # stalmetural.uz
        '92907275',  # steemet.uz
        '92479314',  # smg.az
    ],
    'mpe': ['99551890'],
    'imp': [
        '94649678',   # inmetprom.ru
        '98964781',   # inmetprom.kz
        '98965804',   # inmetprom.kg
        '98964717',   # inmetprom.by
        '98966236',   # inmetprom.uz
        '109924919',  # inmetprom.az
    ],
}

# Серверный фильтр: заголовок содержит «найдена» (чистая подстрока, без
# спецсимволов) - сужает выборку. Точная проверка «не найдена» - на клиенте
# (устойчиво к вариациям заголовка по доменам и скрытым символам \xa0).
_404_SERVER_FILTER = "ym:pv:title=@'найдена'"


def counter_id(project_id: str) -> Optional[str]:
    return COUNTER_IDS.get(project_id)


def _parse_counters(override) -> list:
    """override (секрет metrika_counter_<pid>) → список id. Принимает строку
    '15630172, 92479924 …', список/кортеж, или одиночный id."""
    if not override:
        return []
    if isinstance(override, (list, tuple)):
        items = [str(x) for x in override]
    else:
        import re as _re
        items = _re.split(r'[\s,;]+', str(override))
    return [s.strip() for s in items if s and s.strip()]


_COUNTER_CACHE_DIR = Path(__file__).parent / 'cache' / 'metrika-counters'


def _counter_cache_path(project_id):
    return _COUNTER_CACHE_DIR / f'{project_id}.json'


def _load_counter_cache(project_id, ttl_hours=24):
    p = _counter_cache_path(project_id)
    if not p.exists():
        return None
    try:
        import json as _json
        d = _json.loads(p.read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(d.get('saved_at'))
        if datetime.now() - ts > timedelta(hours=ttl_hours):
            return None
        return d.get('ids') or None
    except Exception:
        return None


def _save_counter_cache(project_id, ids):
    import json as _json
    p = _counter_cache_path(project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({'saved_at': datetime.now().isoformat(), 'ids': ids},
                             ensure_ascii=False), encoding='utf-8')


def discover_counters(token, include, exclude, proxy_url=None, log=None):
    """Из Management API собрать id счётчиков, у кого site содержит include и
    не содержит ни один exclude (по site и name). С пагинацией."""
    def _log(m):
        if log:
            log('info', m)
    if requests is None:
        return []
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    inc = include.lower()
    exc = [e.lower() for e in (exclude or [])]
    ids, offset, per = [], 1, 1000
    while True:
        try:
            r = requests.get(COUNTERS_URL, headers=headers, proxies=proxies,
                             params={'per_page': per, 'offset': offset}, timeout=40)
        except Exception as e:
            _log(f'⚠ discover_counters: сеть - {e}')
            break
        if r.status_code >= 400:
            _log(f'⚠ discover_counters: HTTP {r.status_code}: {r.text[:160]}')
            break
        chunk = (r.json() or {}).get('counters', []) or []
        for c in chunk:
            site = (c.get('site') or '').lower()
            name = (c.get('name') or '').lower()
            if inc in site and not any(x in site or x in name for x in exc):
                ids.append(str(c.get('id')))
        if len(chunk) < per:
            break
        offset += per
    return ids


def _counters_for(project_id, override=None, token=None, proxy_url=None, log=None):
    """Список счётчиков проекта. Приоритет:
    1) список из секрета (если задано >1 счётчика);
    2) авто-дискавери (COUNTER_AUTO) с кешем 24ч;
    3) группа COUNTER_GROUPS;
    4) одиночный override / зашитый COUNTER_IDS."""
    ov = _parse_counters(override)
    if len(ov) > 1:                      # явный список из секрета - главный
        return ov
    auto = COUNTER_AUTO.get(project_id)
    if auto and token:
        cached = _load_counter_cache(project_id)
        if cached:
            if log:
                log('info', f'Метрика-API: счётчиков из кеша {len(cached)}')
            return cached
        found = discover_counters(token, auto['include'], auto.get('exclude'),
                                  proxy_url, log)
        if found:
            _save_counter_cache(project_id, found)
            if log:
                log('info', f'Метрика-API: дискавери счётчиков {len(found)} '
                            f'(site~{auto["include"]})')
            return found
    if project_id in COUNTER_GROUPS:
        return COUNTER_GROUPS[project_id]
    if ov:
        return ov
    cid = counter_id(project_id)
    return [cid] if cid else []


def _is_404_title(title) -> bool:
    """404 по заголовку: нормализуем (nbsp→пробел, нижний регистр) и ищем
    «не найдена» - ловит все варианты («... | Стальметурал», «(Ошибка 404)»)."""
    t = (title or '').replace('\xa0', ' ').lower()
    return 'не найдена' in t


def _query_counter_404(cid, token, proxy_url, date1, date2, log):
    """404-страницы одного счётчика за период. Возвращает (pages, total_views)."""
    params = {
        'ids': cid,
        'date1': date1, 'date2': date2,
        'metrics': 'ym:pv:pageviews',
        'dimensions': 'ym:pv:title,ym:pv:URL',
        'filters': _404_SERVER_FILTER,
        'accuracy': 'full',
        'limit': 5000,
        'sort': '-ym:pv:pageviews',
    }
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    try:
        r = requests.get(API_URL, params=params, headers=headers,
                         proxies=proxies, timeout=40)
    except Exception as e:
        log(f'⚠ Метрика-API сч.{cid}: сеть - {e}')
        return [], 0
    if r.status_code >= 400:
        log(f'⚠ Метрика-API сч.{cid}: HTTP {r.status_code}: {r.text[:160]}')
        return [], 0
    try:
        data = (r.json() or {}).get('data', []) or []
    except Exception as e:
        log(f'⚠ Метрика-API сч.{cid}: разбор - {e}')
        return [], 0

    pages, tv = [], 0
    for row in data:
        dims = row.get('dimensions', [])
        title = (dims[0].get('name') if len(dims) > 0 else '') or ''
        url = (dims[1].get('name') if len(dims) > 1 else '') or ''
        if not _is_404_title(title):       # точная проверка на клиенте
            continue
        try:
            views = int(round(float(row.get('metrics', [0])[0])))
        except Exception:
            views = 0
        pages.append(Page404(page_title=title, page_url=url or None,
                             views=views, visitors=0))
        tv += views
    return pages, tv


def fetch_today_404(project_id: str, token: str,
                    proxy_url: Optional[str] = None,
                    log: Optional[Callable] = None,
                    counter: Optional[str] = None,
                    date1: str = '7daysAgo', date2: str = 'today'
                    ) -> Optional[Report404]:
    """404-страницы за ПЕРИОД по ВСЕМ счётчикам проекта (домены стран).
    По умолчанию последние 7 дней (трафик на 404 мал - за один день часто 0).
    date1/date2 - 'today' | 'yesterday' | 'NdaysAgo' | 'YYYY-MM-DD'."""
    def _log(msg):
        if log:
            log('info', msg)

    if requests is None:
        _log('⚠ Метрика-API: requests не установлен')
        return None
    if not token:
        _log(f'⚠ Метрика-API: токен не задан (metrika_oauth_{project_id})')
        return None
    counters = _counters_for(project_id, counter, token=token,
                             proxy_url=proxy_url, log=log)
    if not counters:
        _log(f'⚠ Метрика-API: нет счётчиков для {project_id}')
        return None

    _log(f'Метрика-API: {len(counters)} счётчик(ов), период {date1}…{date2}, '
         f'фильтр {_404_SERVER_FILTER}')
    all_pages, total_views = [], 0
    for cid in counters:
        pg, tv = _query_counter_404(cid, token, proxy_url, date1, date2, _log)
        all_pages.extend(pg)
        total_views += tv
        _log(f'  счётчик {cid}: 404-адресов {len(pg)} (просмотров {tv})')

    rep_date = datetime.now().strftime('%Y-%m-%d')
    _log(f'✓ Метрика-API: 404-адресов за {date1}…{date2} всего {len(all_pages)} '
         f'(просмотров {total_views})')
    if not all_pages:
        _log('Метрика-API: 0 строк по всем счётчикам. Проверь токен/права и '
             'что за период были 404.')
        return None
    return Report404(
        project_id=project_id, country_code='API',
        country_name=f'За период {date1}…{date2} (API)',
        report_date=rep_date, received_at=datetime.now().isoformat(),
        pages=all_pages, total_views=total_views, total_pages=len(all_pages))


def _last_day_of_month(y, m):
    import calendar
    return calendar.monthrange(y, m)[1]


def _clamp_date(y, m, d):
    """date(y, m, d) с усечением дня до последнего дня месяца (31 мар → 28/29
    фев для прошлого месяца; 29 фев → 28 фев для невисокосного года)."""
    from datetime import date
    return date(y, m, min(d, _last_day_of_month(y, m)))


def _traffic_periods(today=None):
    """Календарные периоды «к дате» для сравнения трафика:
      день   - сегодня            vs вчера;
      месяц  - 1-е..сегодня       vs 1-е..та же дата прошлого месяца;
      год    - 1 янв..сегодня     vs 1 янв..та же дата прошлого года.
    → [(label, (cur1, cur2), (prev1, prev2)), ...] (все - date)."""
    from datetime import date, timedelta
    today = today or date.today()
    yest = today - timedelta(days=1)
    day = ('день', (today, today), (yest, yest))

    cur_m_start = today.replace(day=1)
    prev_m_last = cur_m_start - timedelta(days=1)
    prev_m_start = prev_m_last.replace(day=1)
    prev_m_end = _clamp_date(prev_m_last.year, prev_m_last.month, today.day)
    month = ('месяц', (cur_m_start, today), (prev_m_start, prev_m_end))

    cur_y_start = today.replace(month=1, day=1)
    prev_y_start = cur_y_start.replace(year=today.year - 1)
    prev_y_end = _clamp_date(today.year - 1, today.month, today.day)
    year = ('год', (cur_y_start, today), (prev_y_start, prev_y_end))
    return [day, month, year]


# Порядок и подписи колонок «тип страницы» (по URL приземления).
PAGE_TYPE_ORDER = ['main', 'category', 'service', 'product', 'filter', 'tag',
                   'info', 'tech']
PAGE_TYPE_LABELS = {
    'main': 'Главная', 'category': 'Категория', 'service': 'Услуга',
    'product': 'Товар', 'filter': 'Фильтр', 'tag': 'Тег',
    'info': 'Информационная', 'tech': 'Техническая',
}
# Счётчиков в одном запросе. У mpe ~166 счётчиков - крупные чанки режут
# число запросов (быстрее), но слишком крупный чанк × длинный период Метрика
# не считает («Query is too complicated»). 50 - компромисс с выборкой ниже.
_CHUNK = 50
# Выборка (sampling): считать по доле визитов и экстраполировать. Без неё запрос
# «166 счётчиков × год × разбивка по URL» отдаёт HTTP 400 «Query is too
# complicated» или уходит в таймаут. Для ДИНАМИКИ трафика выборки достаточно.
_ACCURACY = '0.5'
_HTTP_TIMEOUT = 60   # год по сотне счётчиков не влезал в 45с


def _load_pagetypes(project_id):
    """Правила классификации URL по типам. 5 типов ловятся Bitrix-путями
    (main/category/product/filter/tech), 3 (service/tag/info) - паттернами из
    catalogs/pagetypes-<pid>.json (подстроки пути). tech по умолчанию из
    sources.TECH_PAGE_PATHS проекта; конфиг может дополнить/переопределить."""
    cfg = {'service': [], 'tag': [], 'info': [], 'tech': []}
    try:
        import sources
        cfg['tech'] = list(sources.TECH_PAGE_PATHS.get(project_id, []))
    except Exception:
        pass
    f = Path(__file__).parent / 'catalogs' / f'pagetypes-{project_id}.json'
    if f.exists():
        try:
            import json as _json
            d = _json.loads(f.read_text(encoding='utf-8'))
            for k in ('service', 'tag', 'info'):
                if d.get(k):
                    cfg[k] = list(d[k])
            if d.get('tech'):
                cfg['tech'] = cfg['tech'] + list(d['tech'])
        except Exception:
            pass
    return cfg


def _classify_path(path, cfg):
    """URL-путь → тип страницы (см. PAGE_TYPE_ORDER) или None (прочее)."""
    p = (path or '/').split('?')[0].split('#')[0]
    if not p.startswith('/'):
        p = '/' + p
    if not p.endswith('/'):
        p += '/'
    low = p.lower()
    # Конфиг-паттерны раньше общих правил (услуга/тег/инфо/тех - явные разделы).
    for t in ('service', 'tag', 'info', 'tech'):
        if any(frag.lower() in low for frag in cfg.get(t, [])):
            return t
    if '/filter/' in low:
        return 'filter'
    if p == '/':
        return 'main'
    if low.startswith('/catalog/'):
        segs = [s for s in p.split('/') if s]
        if len(segs) <= 1:
            return None                  # корень /catalog/ - не считаем
        return 'category' if len(segs) == 2 else 'product'
    return None


def _metrika_json(ids_csv, token, proxy_url, d1, d2, extra, log):
    """GET к stat/v1/data (с выборкой _ACCURACY). Ретрай 1 раз на таймаут/сеть.
    Возвращает payload (dict) или None при ошибке."""
    params = {'ids': ids_csv, 'date1': d1, 'date2': d2, 'accuracy': _ACCURACY}
    params.update(extra)
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    for attempt in (1, 2):
        try:
            r = requests.get(API_URL, params=params, headers=headers,
                             proxies=proxies, timeout=_HTTP_TIMEOUT)
        except Exception as e:
            if attempt == 1:
                continue
            log(f'⚠ Метрика-трафик {d1}…{d2}: сеть - {e}')
            return None
        if r.status_code >= 400:
            log(f'⚠ Метрика-трафик {d1}…{d2}: HTTP {r.status_code}: '
                f'{r.text[:140]}')
            return None
        try:
            return r.json() or {}
        except Exception as e:
            log(f'⚠ Метрика-трафик: разбор - {e}')
            return None


def _agg_dim(counters, token, proxy_url, d1, d2, metric, dim, log,
             limit=100000):
    """Разбивка metric по dim, просуммированная по всем счётчикам (чанки).
    → {ключ(id или name, lower): значение}. Ключ - id измерения если есть."""
    out = {}
    for i in range(0, len(counters), _CHUNK):
        ids = ','.join(counters[i:i + _CHUNK])
        payload = _metrika_json(ids, token, proxy_url, d1, d2,
                                {'metrics': metric, 'dimensions': dim,
                                 'limit': limit, 'sort': '-' + metric}, log)
        if not payload:
            continue
        for row in payload.get('data', []) or []:
            dims = row.get('dimensions') or [{}]
            key = (dims[0].get('id') or dims[0].get('name') or '')
            key = str(key).strip().lower()
            try:
                val = float((row.get('metrics') or [0])[0])
            except (TypeError, ValueError):
                val = 0.0
            out[key] = out.get(key, 0.0) + val
    return out


def _row_stats(counters, token, proxy_url, d1, d2, cfg, log):
    """Все показатели одной строки (один период) по всем счётчикам.
    Rate-метрики (отказы/глубина/время) усредняем ВЗВЕШЕННО по визитам
    (суммировать проценты по чанкам нельзя). → dict или None."""
    metrics = ('ym:s:visits,ym:s:sumGoalReachesAny,ym:s:bounceRate,'
               'ym:s:pageDepth,ym:s:avgVisitDurationSeconds')
    visits = leads = 0.0
    w_bounce = w_depth = w_dur = 0.0    # взвешенные суммы (× визиты чанка)
    ok = False
    for i in range(0, len(counters), _CHUNK):
        ids = ','.join(counters[i:i + _CHUNK])
        payload = _metrika_json(ids, token, proxy_url, d1, d2,
                                {'metrics': metrics}, log)
        if not payload:
            continue
        ok = True
        t = payload.get('totals') or []
        g = lambda idx: float(t[idx]) if len(t) > idx and t[idx] is not None else 0.0
        cv = g(0)
        visits += cv
        leads += g(1)
        w_bounce += g(2) * cv
        w_depth += g(3) * cv
        w_dur += g(4) * cv
    if not ok:
        return None
    v = visits or 1.0
    src = _agg_dim(counters, token, proxy_url, d1, d2, 'ym:s:visits',
                   'ym:s:lastTrafficSource', log)
    org = _agg_dim(counters, token, proxy_url, d1, d2, 'ym:s:visits',
                   'ym:s:lastSearchEngine', log)
    adv = _agg_dim(counters, token, proxy_url, d1, d2, 'ym:s:visits',
                   'ym:s:lastAdvEngine', log)
    direct = src.get('direct', 0.0)
    yandex = (sum(x for k, x in org.items() if 'yandex' in k)
              + sum(x for k, x in adv.items() if k.startswith('ya')))
    google = (sum(x for k, x in org.items() if 'google' in k)
              + sum(x for k, x in adv.items() if 'google' in k))
    paths = _agg_dim(counters, token, proxy_url, d1, d2, 'ym:s:visits',
                     'ym:s:startURLPath', log)
    pages = {t: 0.0 for t in PAGE_TYPE_ORDER}
    for path, val in paths.items():
        tp = _classify_path(path, cfg)
        if tp:
            pages[tp] += val
    return {
        'visits': int(round(visits)),
        'direct': int(round(direct)),
        'yandex': int(round(yandex)),
        'google': int(round(google)),
        'leads': int(round(leads)),
        'conv': round(leads / v * 100, 2),
        'bounce': round(w_bounce / v, 1),
        'depth': round(w_depth / v, 2),
        'duration': int(round(w_dur / v)),
        'pages': {t: int(round(pages[t])) for t in PAGE_TYPE_ORDER},
    }


def fetch_traffic_comparison(project_id, token, proxy_url=None, counter=None,
                             log=None) -> Optional[dict]:
    """Динамика трафика по ВСЕМ счётчикам проекта: день/месяц/год, каждый в
    двух строках (текущий период и прошлый - вчера / прошлый месяц / прошлый
    год). По каждой строке: визиты (итого), прямые/Яндекс/Google, лиды,
    конверсия, отказы, глубина, время, разбивка по типам страниц. → dict для
    листа «Динамика трафика» или None."""
    def _log(m):
        if log:
            log('info', m)

    if requests is None or not token:
        _log('⚠ Метрика-трафик: нет requests или токена')
        return None
    counters = _counters_for(project_id, counter, token=token,
                             proxy_url=proxy_url, log=log)
    if not counters:
        _log(f'⚠ Метрика-трафик: нет счётчиков для {project_id}')
        return None
    cfg = _load_pagetypes(project_id)
    _log(f'Метрика-трафик: {len(counters)} счётчик(ов), день/месяц/год × 2')

    rows = []
    for label, (c1, c2), (p1, p2) in _traffic_periods():
        for kind, (a, b) in (('текущий', (c1, c2)), ('прошлый', (p1, p2))):
            st = _row_stats(counters, token, proxy_url, a.isoformat(),
                            b.isoformat(), cfg, _log)
            if st is None:
                continue
            rows.append({'year': a.year, 'period': label.capitalize(),
                         'kind': kind, 'd1': a.isoformat(), 'd2': b.isoformat(),
                         **st})
            _log(f'  {label}/{kind}: визиты {st["visits"]}, лиды {st["leads"]}')
    if not rows:
        return None
    return {'available': True, 'counters': len(counters), 'rows': rows}


def list_counters(token, proxy_url=None):
    """Вывести все счётчики, доступные токену (Management API).
    Показывает id, имя, сайт и зеркала (поддомены) - для поиска нужного."""
    if requests is None:
        print('requests не установлен'); return
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    params = {'per_page': 1000}
    try:
        r = requests.get(COUNTERS_URL, params=params, headers=headers,
                         proxies=proxies, timeout=40)
    except Exception as e:
        print('Сеть:', e); return
    print('HTTP', r.status_code)
    if r.status_code >= 400:
        print('Ответ:', r.text[:400]); return
    counters = (r.json() or {}).get('counters', []) or []
    print(f'Счётчиков доступно: {len(counters)}\n')
    for c in counters:
        site2 = c.get('site2') or {}
        mirrors = site2.get('mirrors2') or []
        print(f"id={c.get('id')}  «{c.get('name')}»  site={c.get('site')}")
        if mirrors:
            print(f"    зеркала ({len(mirrors)}): {', '.join(mirrors[:8])}"
                  + (' …' if len(mirrors) > 8 else ''))
    print('\n→ Нужен счётчик, у которого в зеркалах поддомены '
          '(voronezh.stalmetural.ru и т.п.).')


def counter_info(token, counter, proxy_url=None):
    """Детали одного счётчика: site, зеркала (mirrors2) и фильтры."""
    if requests is None:
        print('requests не установлен'); return
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    url = f'https://api-metrika.yandex.net/management/v1/counter/{counter}'
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=40)
    except Exception as e:
        print('Сеть:', e); return
    print('HTTP', r.status_code)
    if r.status_code >= 400:
        print('Ответ:', r.text[:400]); return
    c = (r.json() or {}).get('counter', {}) or {}
    site2 = c.get('site2') or {}
    mirrors = site2.get('mirrors2') or []
    print(f"id={c.get('id')}  «{c.get('name')}»  site={c.get('site')}")
    print(f"\nЗеркала mirrors2 ({len(mirrors)}):")
    for m in mirrors:
        print(f'  {m}')
    if not mirrors:
        print('  (пусто)')
    flt = c.get('filters') or []
    print(f"\nФильтры счётчика ({len(flt)}):")
    import json as _json
    for f in flt:
        print('  ' + _json.dumps(f, ensure_ascii=False))
    if not flt:
        print('  (пусто)')


# ── Цель на 404 в Метрике (регулярный мониторинг: не сборка почтового отчёта
# из письма/выгрузки, а прямой запрос к живой конфигурации счётчика при
# каждом прогоне) ────────────────────────────────────────────────────────
GOALS_URL_TMPL = 'https://api-metrika.yandex.net/management/v1/counter/{counter}/goals'


def counter_goals(cid, token, proxy_url=None) -> Optional[list]:
    """Список целей счётчика (Management API). None - не удалось узнать
    (сеть/токен/доступ) - НЕ путать с [] (счётчик реально ответил и целей
    у него просто нет)."""
    if requests is None or not token or not cid:
        return None
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    url = GOALS_URL_TMPL.format(counter=cid)
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=40)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    return (r.json() or {}).get('goals') or []


def _goal_looks_like_404(goal: dict) -> bool:
    """Похожа ли цель на отслеживание 404-ошибок - по названию ИЛИ по любому
    ТЕКСТОВОМУ значению внутри описания цели (JS-идентификатор события лежит
    в разных полях в зависимости от типа цели - ищем по всей структуре, а не
    по одному конкретному ключу, чтобы не зависеть от точной схемы). Поле
    `id` (и подобные служебные числа) из поиска исключаем нарочно - это
    большое произвольное число, которое может случайно содержать «404» и
    дать ложное «цель есть» там, где её на самом деле нет. В реальных
    счётчиках СМУ/ИМП такая цель уже настроена: название «404», JS-
    идентификатор «404error» - оба матчатся уже по названию. ЧИСТАЯ функция
    (юнит-тест без сети)."""
    if '404' in str(goal.get('name') or ''):
        return True
    import json as _json
    relevant = {k: v for k, v in (goal or {}).items()
                if k not in ('id', 'counter_id')}
    try:
        blob = _json.dumps(relevant, ensure_ascii=False)
    except Exception:
        blob = str(relevant)
    return '404' in blob


def has_404_goal(project_id, token, proxy_url=None, counter=None, log=None) -> dict:
    """Есть ли хотя бы у одного счётчика проекта цель на отслеживание 404 -
    живым запросом к Метрике (не по ранее выгруженному каталогу целей,
    который мог устареть). Переиспользует _counters_for - те же счётчики
    (в т.ч. по странам), что и «404 из Метрики».

    Возвращает {'есть': bool, 'счётчики': {cid: {'есть': bool|None,
    'название': str|None}}} - «есть»=None у счётчика значит «не удалось
    узнать» (не то же самое, что «нет цели»)."""
    def _log(msg):
        if log:
            log('info', msg)

    result = {'есть': False, 'счётчики': {}}
    if requests is None or not token:
        _log('⚠ Метрика-API (цель 404): токен не задан или requests не установлен')
        return result
    counters = _counters_for(project_id, counter, token=token,
                             proxy_url=proxy_url, log=log)
    if not counters:
        _log(f'⚠ Метрика-API (цель 404): нет счётчиков для {project_id}')
        return result
    for cid in counters:
        goals = counter_goals(cid, token, proxy_url)
        if goals is None:
            result['счётчики'][cid] = {'есть': None, 'название': None}
            continue
        found = next((g for g in goals if _goal_looks_like_404(g)), None)
        result['счётчики'][cid] = {
            'есть': bool(found),
            'название': found.get('name') if found else None,
        }
        if found:
            result['есть'] = True
    return result


def probe_counter(project_id, token, proxy_url=None, counter=None,
                  date='yesterday'):
    """Диагностика доступа: запрос БЕЗ фильтра (любые данные за день).
    Печатает total просмотров + топ заголовков. Если 0 - токен не видит счётчик
    или счётчик не тот. Используется из CLI: python metrika_api.py check ..."""
    cid = str(counter).strip() if counter else counter_id(project_id)
    print(f'Проверка: проект={project_id} счётчик={cid} дата={date}')
    if requests is None:
        print('requests не установлен'); return
    params = {
        'ids': cid, 'date1': date, 'date2': date,
        'metrics': 'ym:pv:pageviews',
        'dimensions': 'ym:pv:title',
        'accuracy': 'full', 'limit': 2000, 'sort': '-ym:pv:pageviews',
    }
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    try:
        r = requests.get(API_URL, params=params, headers=headers,
                         proxies=proxies, timeout=40)
    except Exception as e:
        print('Сеть:', e); return
    print('HTTP', r.status_code)
    if r.status_code >= 400:
        print('Ответ:', r.text[:400]); return
    payload = r.json()
    totals = payload.get('totals') or []
    total_pv = totals[0] if totals else '-'
    data = payload.get('data', []) or []
    print(f'Всего просмотров за день: {total_pv}; строк (заголовков): {len(data)}')

    # Заголовки с «найд»/«404» - печатаем через repr(), чтобы увидеть скрытые
    # символы (неразрывный пробел \xa0, другой дефис, хвостовые пробелы).
    hits = []
    for row in data:
        title = (row.get('dimensions', [{}])[0].get('name') or '')
        low = title.lower()
        if 'найд' in low or '404' in low:
            hits.append((row.get('metrics', ['-'])[0], title))
    print(f'\n=== Заголовки с «найд»/«404» ({len(hits)}) - repr показывает спецсимволы ===')
    for pv, title in hits[:30]:
        print(f'  {pv:>8}  {title!r}')
    if not hits:
        print('  (нет - за день 404 не было, либо заголовок без «найд»/«404»)')

    print('\n=== Топ-20 заголовков (repr - видно спецсимволы) ===')
    for row in data[:20]:
        title = (row.get('dimensions', [{}])[0].get('name') or '')
        pv = row.get('metrics', ['-'])[0]
        print(f'  {pv:>8}  {title!r}')
    if not data:
        print('→ 0 данных: токен НЕ видит этот счётчик, либо счётчик не тот, '
              'либо за этот день нет визитов.')


if __name__ == '__main__':
    # Прямой тест запроса к API:
    #   python metrika_api.py <pid> <token> [date] [counter] [proxy]
    #   date: today | yesterday | YYYY-MM-DD (по умолчанию today)
    # Без аргументов - печатает фильтры/счётчики (offline).
    import sys
    if len(sys.argv) < 3:
        print(f'Серверный фильтр: {_404_SERVER_FILTER}  + client-проверка «не найдена»')
        print('Счётчики по проектам:')
        for k in COUNTER_GROUPS:
            print(f'  {k}: {_counters_for(k)}')
        print('\nТест 404:  python metrika_api.py <pid> <token> [date1] [date2] [counter] [proxy]')
        print('Трафик:    python metrika_api.py traffic <pid> <token> [counter] [proxy]')
        print('Проверка:  python metrika_api.py check <pid> <token> [counter] [date] [proxy]')
        print('Счётчики:  python metrika_api.py list_counters <token> [proxy]')
        print('Детали:    python metrika_api.py counter_info <token> <counter> [proxy]')
        print('Дискавери: python metrika_api.py discover <pid> <token> [proxy]')
        sys.exit(0)

    # Список всех счётчиков токена
    if sys.argv[1] == 'list_counters':
        _tok = sys.argv[2]
        _prx = sys.argv[3] if len(sys.argv) > 3 else None
        list_counters(_tok, _prx)
        sys.exit(0)

    # Авто-дискавери счётчиков проекта (по site)
    if sys.argv[1] == 'discover':
        _pid = sys.argv[2]
        _tok = sys.argv[3]
        _prx = sys.argv[4] if len(sys.argv) > 4 else None
        auto = COUNTER_AUTO.get(_pid)
        if not auto:
            print(f'Для {_pid} нет правила COUNTER_AUTO'); sys.exit(0)
        ids = discover_counters(_tok, auto['include'], auto.get('exclude'),
                                _prx, lambda lvl, m: print(m))
        print(f'\nНайдено счётчиков (site~{auto["include"]}, '
              f'кроме {auto.get("exclude")}): {len(ids)}')
        print(', '.join(ids))
        sys.exit(0)

    # Детали одного счётчика (зеркала + фильтры)
    if sys.argv[1] == 'counter_info':
        _tok = sys.argv[2]
        _cnt = sys.argv[3]
        _prx = sys.argv[4] if len(sys.argv) > 4 else None
        counter_info(_tok, _cnt, _prx)
        sys.exit(0)

    # Сравнение трафика день/месяц/год
    if sys.argv[1] == 'traffic':
        _pid = sys.argv[2]
        _tok = sys.argv[3]
        _cnt = sys.argv[4] if len(sys.argv) > 4 else None
        _prx = sys.argv[5] if len(sys.argv) > 5 else None
        res = fetch_traffic_comparison(_pid, _tok, _prx, counter=_cnt,
                                       log=lambda lvl, m: print(m))
        if not res:
            print('\nпусто'); sys.exit(0)
        print(f'\nсчётчиков {res["counters"]}, строк {len(res["rows"])}')
        for r in res['rows']:
            print(f'  {r["year"]} {r["period"]:6} {r["kind"]:8} '
                  f'визиты={r["visits"]:>7} прямые={r["direct"]:>6} '
                  f'Я={r["yandex"]:>6} G={r["google"]:>6} лиды={r["leads"]:>4} '
                  f'конв={r["conv"]}% отказы={r["bounce"]}% гл={r["depth"]} '
                  f't={r["duration"]}s | {r["pages"]}')
        sys.exit(0)

    # Диагностика доступа без фильтра
    if sys.argv[1] == 'check':
        _pid = sys.argv[2]
        _tok = sys.argv[3]
        _cnt = sys.argv[4] if len(sys.argv) > 4 else None
        _date = sys.argv[5] if len(sys.argv) > 5 else 'yesterday'
        _prx = sys.argv[6] if len(sys.argv) > 6 else None
        probe_counter(_pid, _tok, _prx, counter=_cnt, date=_date)
        sys.exit(0)

    _pid = sys.argv[1]
    _tok = sys.argv[2]
    _date1 = sys.argv[3] if len(sys.argv) > 3 else '7daysAgo'
    _date2 = sys.argv[4] if len(sys.argv) > 4 else 'today'
    _cnt = sys.argv[5] if len(sys.argv) > 5 else None
    _prx = sys.argv[6] if len(sys.argv) > 6 else None
    rep = fetch_today_404(_pid, _tok, _prx, lambda lvl, m: print(m),
                          counter=_cnt, date1=_date1, date2=_date2)
    if rep:
        print(f'\nИТОГО за {_date1}…{_date2}: {rep.total_pages} адресов, '
              f'{rep.total_views} просмотров')
        for p in rep.pages[:30]:
            print(f'  {p.views:>5}  {p.page_url}')
    else:
        print('\nИТОГО: пусто (см. сообщения выше)')
