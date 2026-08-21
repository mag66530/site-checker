"""
index_tech_pages.py - служебные (технические) страницы В ИНДЕКСЕ поисковика.

Пункт чек-листа: «технических страниц в индексе быть не должно». Речь про
служебные разделы движка - корзина, оформление заказа, поиск по сайту,
сравнение, избранное, личный кабинет, авторизация/регистрация, админка,
AJAX-обработчики и прочие каталоги вроде /local/ и /cgi-bin/. Такие адреса
не должны попадать в выдачу: они не отвечают ни на один запрос покупателя,
съедают краулинговый бюджет, а корзина/ЛК ещё и утекают в поиск с чужими
данными в заголовке.

Чем отличается от соседних проверок:
  • indexing_checker.check_paths_against_robots - смотрит, ЗАКРЫТЫ ли
    служебные пути в robots.txt (правило есть/нет). Здесь наоборот: факт -
    адрес РЕАЛЬНО в индексе, независимо от того, что написано в robots;
  • index_pages_checker / index_export_parser - берут ту же выборку из
    индекса, но проверяют только код ответа (404/410/soft-404).

Источник списка «в индексе» не свой: подмешиваемся к уже существующим -
выгрузка «Страницы в поиске» Яндекс.Вебмастера (index_export_parser),
выборка in-search/samples того же Вебмастера (index_pages_checker) и список
страниц из Google Search Console (index_gsc_api). Каждый источник кладёт
найденные служебные адреса в host['tech'], merge_index_404 их сливает.

Перед отчётом кандидаты перепроверяются живьём (reverify_tech_pages):
находка остаётся, только если адрес СЕЙЧАС отвечает 200 и на нём НЕТ
noindex. Страница уже удалена (404), уводит редиректом или закрыта
noindex'ом - поисковик просто помнит старое, клиенту предъявлять нечего.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import aiohttp

# ── Что считаем служебным ────────────────────────────────────────────
# Сегмент пути (ровно сегмент, не подстрока!) → человеческая метка.
# Сравнение по сегменту принципиально: подстрокой 'cart' поймалось бы
# /catalog/cartridge/, а 'personal' - /catalog/personalnye-podarki/.
_TECH_SEGMENTS = {
    # корзина
    'basket': 'корзина', 'cart': 'корзина', 'korzina': 'корзина',
    # оформление заказа
    'order': 'оформление заказа', 'checkout': 'оформление заказа',
    'oformlenie': 'оформление заказа', 'makeorder': 'оформление заказа',
    # поиск по сайту
    'search': 'поиск по сайту', 'poisk': 'поиск по сайту',
    # сравнение и отложенные
    'compare': 'сравнение товаров', 'sravnenie': 'сравнение товаров',
    'favorites': 'избранное', 'favorite': 'избранное',
    'wishlist': 'избранное', 'izbrannoe': 'избранное',
    'delayed': 'отложенные товары',
    # личный кабинет
    'personal': 'личный кабинет', 'cabinet': 'личный кабинет',
    'lk': 'личный кабинет', 'profile': 'личный кабинет',
    'account': 'личный кабинет',
    # вход/регистрация
    'auth': 'авторизация', 'login': 'авторизация', 'logout': 'авторизация',
    'register': 'регистрация', 'registration': 'регистрация',
    'forgot_password': 'восстановление пароля',
    'change_password': 'смена пароля',
    # админка
    'bitrix': 'админ. панель', 'admin': 'админ. панель',
    'administrator': 'админ. панель', 'wp-admin': 'админ. панель',
    # служебные каталоги и обработчики
    'ajax': 'AJAX-обработчик', 'api': 'служебный API',
    'local': 'служебный каталог /local/', 'cgi-bin': 'служебный каталог /cgi-bin/',
    'include': 'служебный каталог /include/', 'vendor': 'служебный каталог /vendor/',
    'node_modules': 'служебный каталог /node_modules/',
}

# Скриптовые обработчики: сегментом они не ловятся (имя файла с расширением),
# а в индексе встречаются - типовой след старой вёрстки Битрикса.
_TECH_FILES = {
    'auth.php': 'авторизация', 'login.php': 'авторизация',
    'logout.php': 'авторизация', 'register.php': 'регистрация',
    'cart.php': 'корзина', 'basket.php': 'корзина',
    'order.php': 'оформление заказа', 'search.php': 'поиск по сайту',
    'ajax.php': 'AJAX-обработчик', 'urlrewrite.php': 'служебный обработчик',
}


def classify_tech_url(url: str):
    """Служебный ли адрес. → {'label', 'segment'} или None.

    Смотрим ТОЛЬКО путь: параметрические дубли (?sort=, ?PAGEN_1=, utm) -
    это другая история (дубль обычной страницы, а не служебный раздел), их
    ловит проверка robots.txt. ЧИСТАЯ функция - есть юнит-тест."""
    path = (urlsplit((url or '').strip()).path or '/').lower()
    for seg in path.split('/'):
        if not seg:
            continue
        label = _TECH_SEGMENTS.get(seg)
        if label:
            return {'label': label, 'segment': seg}
        label = _TECH_FILES.get(seg)
        if label:
            return {'label': label, 'segment': seg}
    return None


# Потолок кандидатов на один хост: если в индекс уехал весь /personal/,
# отчёт не должен превратиться в простыню на тысячу строк - хватает примеров,
# чтобы задача была понятна (а перепроверять их все ещё и долго).
MAX_TECH_PER_HOST = 50


def tech_entry(url: str, source: str) -> dict:
    """Запись для host['tech'] - форма как у dead/errors (url/source/reason),
    чтобы отчёт читал их одинаково. None, если адрес не служебный."""
    hit = classify_tech_url(url)
    if not hit:
        return None
    return {'url': url, 'source': source, 'label': hit['label'],
            'reason': f'{hit["label"]} - служебная страница в индексе'}


def add_tech(host_bucket: dict, url: str, source: str) -> bool:
    """Добавить адрес в host['tech'], если он служебный и есть место.
    Возвращает True, если запись добавлена."""
    entry = tech_entry(url, source)
    if not entry:
        return False
    bucket = host_bucket.setdefault('tech', [])
    if len(bucket) >= MAX_TECH_PER_HOST:
        return False
    bucket.append(entry)
    return True


# ── Живая перепроверка кандидатов ────────────────────────────────────
# Данные поисковика - снимок: страницу могли удалить, увести редиректом или
# уже закрыть noindex'ом. Находка остаётся, только если адрес прямо сейчас
# отвечает 200 без noindex - тогда он и правда в индексе не просто так.

_TIMEOUT = 15
_CONCURRENCY = 10
# Потолок живых запросов за прогон: перепроверка идёт по боевому сайту, а
# служебных адресов в индексе бывает много.
MAX_TECH_REVERIFY = 150


def tech_verdict(status, noindex: bool) -> str:
    """'finding' - 200 без noindex (служебная страница правда открыта);
    'noindex' - 200, но закрыта noindex (поисковик выкинет её сам);
    'gone' - 404/410/редирект/недоступна (предъявлять нечего).
    ЧИСТАЯ функция - есть юнит-тест."""
    if status != 200:
        return 'gone'
    return 'noindex' if noindex else 'finding'


async def _check_one(session, url, proxy, sem):
    """(вердикт, код). GET без редиректов: 301 на служебном адресе значит,
    что страницы по нему уже нет."""
    from indexing_checker import _find_meta_robots, _x_robots_noindex
    to = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with sem:
        try:
            async with session.get(url, timeout=to, proxy=proxy,
                                   allow_redirects=False) as r:
                if r.status != 200:
                    return 'gone', r.status
                headers = {k.lower(): v for k, v in r.headers.items()}
                html = await r.text(errors='replace')
        except Exception:
            return 'gone', None
    _, noindex = _find_meta_robots(html)
    if not noindex:
        _, noindex = _x_robots_noindex(headers)
    return tech_verdict(200, noindex), 200


async def _check_all(urls, proxy):
    from http_checker import make_browser_headers
    sem = asyncio.Semaphore(_CONCURRENCY)
    conn = aiohttp.TCPConnector(limit=_CONCURRENCY, ttl_dns_cache=300)
    out = {}
    # url= первого адреса: сайт может быть закрыт паролем (см. closed-site).
    async with aiohttp.ClientSession(
            headers=make_browser_headers(url=(urls[0] if urls else '')),
            connector=conn) as s:
        tasks = [(u, asyncio.create_task(_check_one(s, u, proxy, sem)))
                 for u in urls]
        for u, t in tasks:
            out[u] = await t
    return out


def reverify_tech_pages(check: dict, proxy_url=None, log=None) -> dict:
    """Перепроверить служебные адреса из индекса живьём. Возвращает новый
    check, где в host['tech'] остались только страницы, которые СЕЙЧАС
    отвечают 200 без noindex."""
    def _log(m):
        if not log:
            return
        try:
            log('info', m)
        except TypeError:
            log(m)

    if not check or not check.get('hosts'):
        return check

    cand = []
    for h in check['hosts']:
        for e in h.get('tech') or []:
            if e.get('url'):
                cand.append(e['url'])
    cand = list(dict.fromkeys(cand))[:MAX_TECH_REVERIFY]
    if not cand:
        return check

    _log(f'Тех. страницы в индексе: перепроверяю вживую {len(cand)} адресов…')
    try:
        live = asyncio.run(_check_all(cand, proxy_url))
    except Exception as e:
        _log(f'⚠ перепроверка тех. страниц не удалась ({e}) - '
             f'оставляю список как есть')
        return check

    new_hosts, kept, dropped = [], 0, 0
    for h in check['hosts']:
        nh = dict(h)
        tech = []
        for e in h.get('tech') or []:
            verdict, st = live.get(e.get('url'), (None, None))
            if verdict is None:
                tech.append(e)          # не дошли до перепроверки (потолок)
                continue
            if verdict == 'finding':
                tech.append({**e, 'status': st})
                kept += 1
            else:
                dropped += 1
        nh['tech'] = tech
        new_hosts.append(nh)

    out = dict(check)
    out['hosts'] = new_hosts
    out['total_tech'] = sum(len(h.get('tech') or []) for h in new_hosts)
    out['tech_reverified'] = True
    _log(f'Тех. страницы в индексе: подтверждено {kept}, '
         f'убрано неподтверждённых {dropped}')
    return out
