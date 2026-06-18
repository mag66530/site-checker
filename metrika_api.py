"""
metrika_api.py — 404-страницы из Яндекс.Метрики за СЕГОДНЯ через Reporting API.

Почтовые отчёты (metrika_404.py) приходят с задержкой (за вчера). Этот модуль
тянет данные за сегодня напрямую из API:
  Отчёты → Содержание → «Заголовки страниц» = измерение ym:pv:title (+ ym:pv:URL).
404-страницы опознаём по заголовку (маркеры _404_MARKERS).

Авторизация: OAuth-токен Яндекса со scope `metrika:read`.
Секрет: metrika_oauth_<pid> (или общий metrika_oauth).
counter_id — ниже в COUNTER_IDS.
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

# У проекта несколько счётчиков (по доменам стран) — 404 собираем из ВСЕХ.
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
    'imp': ['94649678'],
}

# Серверный фильтр: заголовок содержит «найдена» (чистая подстрока, без
# спецсимволов) — сужает выборку. Точная проверка «не найдена» — на клиенте
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
            _log(f'⚠ discover_counters: сеть — {e}')
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
    if len(ov) > 1:                      # явный список из секрета — главный
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
    «не найдена» — ловит все варианты («... | Стальметурал», «(Ошибка 404)»)."""
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
        log(f'⚠ Метрика-API сч.{cid}: сеть — {e}')
        return [], 0
    if r.status_code >= 400:
        log(f'⚠ Метрика-API сч.{cid}: HTTP {r.status_code}: {r.text[:160]}')
        return [], 0
    try:
        data = (r.json() or {}).get('data', []) or []
    except Exception as e:
        log(f'⚠ Метрика-API сч.{cid}: разбор — {e}')
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
    По умолчанию последние 7 дней (трафик на 404 мал — за один день часто 0).
    date1/date2 — 'today' | 'yesterday' | 'NdaysAgo' | 'YYYY-MM-DD'."""
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


def list_counters(token, proxy_url=None):
    """Вывести все счётчики, доступные токену (Management API).
    Показывает id, имя, сайт и зеркала (поддомены) — для поиска нужного."""
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


def probe_counter(project_id, token, proxy_url=None, counter=None,
                  date='yesterday'):
    """Диагностика доступа: запрос БЕЗ фильтра (любые данные за день).
    Печатает total просмотров + топ заголовков. Если 0 — токен не видит счётчик
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
    total_pv = totals[0] if totals else '—'
    data = payload.get('data', []) or []
    print(f'Всего просмотров за день: {total_pv}; строк (заголовков): {len(data)}')

    # Заголовки с «найд»/«404» — печатаем через repr(), чтобы увидеть скрытые
    # символы (неразрывный пробел \xa0, другой дефис, хвостовые пробелы).
    hits = []
    for row in data:
        title = (row.get('dimensions', [{}])[0].get('name') or '')
        low = title.lower()
        if 'найд' in low or '404' in low:
            hits.append((row.get('metrics', ['—'])[0], title))
    print(f'\n=== Заголовки с «найд»/«404» ({len(hits)}) — repr показывает спецсимволы ===')
    for pv, title in hits[:30]:
        print(f'  {pv:>8}  {title!r}')
    if not hits:
        print('  (нет — за день 404 не было, либо заголовок без «найд»/«404»)')

    print('\n=== Топ-20 заголовков (repr — видно спецсимволы) ===')
    for row in data[:20]:
        title = (row.get('dimensions', [{}])[0].get('name') or '')
        pv = row.get('metrics', ['—'])[0]
        print(f'  {pv:>8}  {title!r}')
    if not data:
        print('→ 0 данных: токен НЕ видит этот счётчик, либо счётчик не тот, '
              'либо за этот день нет визитов.')


if __name__ == '__main__':
    # Прямой тест запроса к API:
    #   python metrika_api.py <pid> <token> [date] [counter] [proxy]
    #   date: today | yesterday | YYYY-MM-DD (по умолчанию today)
    # Без аргументов — печатает фильтры/счётчики (offline).
    import sys
    if len(sys.argv) < 3:
        print(f'Серверный фильтр: {_404_SERVER_FILTER}  + client-проверка «не найдена»')
        print('Счётчики по проектам:')
        for k in COUNTER_GROUPS:
            print(f'  {k}: {_counters_for(k)}')
        print('\nТест 404:  python metrika_api.py <pid> <token> [date1] [date2] [counter] [proxy]')
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
