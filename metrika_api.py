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
from datetime import datetime
from typing import Optional, Callable

try:
    import requests
except ImportError:
    requests = None

from metrika_404 import Report404, Page404

API_URL = 'https://api-metrika.yandex.net/stat/v1/data'

# ID счётчиков Метрики по проектам
COUNTER_IDS = {
    'mpe': '99551890',
    'smu': '15630172',
    'imp': '94649678',
}

# Точный заголовок 404-страницы по проекту (подстрока, нижний регистр).
# Совпадает по самому надёжному куску, общему для всех вариантов проекта.
_404_TITLES = {
    'smu': 'страница не найдена | стальметурал',
    'mpe': 'страница не найдена',
    'imp': 'страница не найдена (ошибка 404)',
}
# Запасные маркеры, если для проекта не задан точный заголовок.
_404_MARKERS = [
    '404', 'страница не найдена', 'не найдена', 'not found',
    'page not found', 'нет такой страницы', 'ошибка 404',
]


def counter_id(project_id: str) -> Optional[str]:
    return COUNTER_IDS.get(project_id)


def _404_match_str(project_id: str) -> str:
    """Подстрока для фильтра API: точный заголовок проекта или базовый маркер."""
    return _404_TITLES.get(project_id) or 'страница не найдена'


def _is_404_title(title: str, project_id: str = None) -> bool:
    t = (title or '').lower()
    exact = _404_TITLES.get(project_id) if project_id else None
    if exact:
        return exact in t
    return any(m in t for m in _404_MARKERS)


def fetch_today_404(project_id: str, token: str,
                    proxy_url: Optional[str] = None,
                    log: Optional[Callable] = None,
                    counter: Optional[str] = None) -> Optional[Report404]:
    """Забрать 404-страницы за сегодня. Возвращает Report404 или None.
    counter — id счётчика (из секрета); если None — берём зашитый COUNTER_IDS."""
    def _log(msg):
        if log:
            log('info', msg)

    if requests is None:
        _log('⚠ Метрика-API: requests не установлен')
        return None
    cid = str(counter).strip() if counter else counter_id(project_id)
    if not cid:
        _log(f'⚠ Метрика-API: нет counter_id для {project_id}')
        return None
    if not token:
        _log(f'⚠ Метрика-API: токен не задан (metrika_oauth_{project_id})')
        return None

    # Фильтр по заголовку на стороне API: title содержит 404-заголовок проекта.
    flt = f"ym:pv:title=@'{_404_match_str(project_id)}'"
    params = {
        'ids': cid,
        'date1': 'today', 'date2': 'today',
        'metrics': 'ym:pv:pageviews',
        'dimensions': 'ym:pv:title,ym:pv:URL',
        'filters': flt,
        'accuracy': 'full',
        'limit': 1000,
        'sort': '-ym:pv:pageviews',
    }
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None

    try:
        r = requests.get(API_URL, params=params, headers=headers,
                         proxies=proxies, timeout=40)
    except Exception as e:
        _log(f'❌ Метрика-API: сеть — {e}')
        return None
    if r.status_code == 401:
        _log('❌ Метрика-API: токен невалиден/просрочен (401)')
        return None
    if r.status_code == 403:
        _log('❌ Метрика-API: нет доступа к счётчику (403) — проверь права токена')
        return None
    if r.status_code >= 400:
        _log(f'❌ Метрика-API: HTTP {r.status_code}: {r.text[:200]}')
        return None

    try:
        data = r.json().get('data', []) or []
    except Exception as e:
        _log(f'❌ Метрика-API: разбор ответа — {e}')
        return None

    pages = []
    total_views = 0
    for row in data:
        dims = row.get('dimensions', [])
        title = (dims[0].get('name') if len(dims) > 0 else '') or ''
        url = (dims[1].get('name') if len(dims) > 1 else '') or ''
        # Подстраховка: серверный фильтр мог пропустить — проверяем ещё раз
        if not _is_404_title(title, project_id):
            continue
        try:
            views = int(round(float(row.get('metrics', [0])[0])))
        except Exception:
            views = 0
        pages.append(Page404(page_title=title, page_url=url or None,
                             views=views, visitors=0))
        total_views += views

    today = datetime.now().strftime('%Y-%m-%d')
    _log(f'✓ Метрика-API: 404-страниц за сегодня {len(pages)} '
         f'(просмотров {total_views})')
    if not pages:
        return None
    return Report404(
        project_id=project_id, country_code='API', country_name='Сегодня (API)',
        report_date=today, received_at=datetime.now().isoformat(),
        pages=pages, total_views=total_views, total_pages=len(pages))


if __name__ == '__main__':
    # Самотест распознавания заголовков по проектам
    cases = [
        ('smu', 'Страница не найдена | Стальметурал', True),
        ('mpe', 'Страница не найдена', True),
        ('imp', 'Страница не найдена (Ошибка 404)', True),
        ('mpe', 'Каталог труб', False),
        ('smu', 'Деталь не найдена в каталоге', False),  # не 404
    ]
    for pid, title, want in cases:
        got = _is_404_title(title, pid)
        print('OK' if got == want else 'FAIL', pid, repr(title), '→', got)
    print('counters:', COUNTER_IDS)
