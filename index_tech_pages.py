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
import re
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
    'order': 'оформление заказа', 'orders': 'оформление заказа',
    'checkout': 'оформление заказа', 'onepagecheckout': 'оформление заказа',
    'one-page-checkout': 'оформление заказа',
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


# Сегменты, по которым адрес служебный НЕ НАВЕРНЯКА: /order/ на большинстве
# сайтов - оформление заказа, но бывает и информационная «Как заказать».
# Прозвон по всем девяти боевым проектам (21.08.2026) показал только первое
# (СМУ /order → /basket/, ИМП /checkout/ → /cart/, МПЭ /personal/order/ →
# форма входа), однако на новом проекте может быть иначе. Поэтому мягкая
# метка: находка выписывается, только если ЖИВАЯ страница показывает разметку
# оформления (см. looks_like_checkout) - на информационной её нет.
_SOFT_SEGMENTS = {'order', 'orders'}


def _manual_tech_paths(project_id: str) -> set:
    """Пути тех. страниц проекта (заведены руками / найдены автопоиском) в
    сравнимом виде. Такие адреса служебными не считаем: проект сам их
    проверяет как обычные страницы, значит место в индексе им законное
    (например «Поиск по товару» /search/ у ИМП и SHOPMET)."""
    if not project_id:
        return set()
    try:
        import sources
        return {(p or '').rstrip('/').lower()
                for p in sources.get_tech_paths(project_id)}
    except Exception:
        return set()


def classify_tech_url(url: str, project_id: str = None):
    """Служебный ли адрес. → {'label', 'segment', 'soft'} или None.

    Смотрим ТОЛЬКО путь: параметрические дубли (?sort=, ?PAGEN_1=, utm) -
    это другая история (дубль обычной страницы, а не служебный раздел), их
    ловит проверка robots.txt.

    project_id - чтобы не обвинять страницы из СОБСТВЕННОГО списка тех.
    страниц проекта. ЧИСТАЯ функция - есть юнит-тест."""
    path = (urlsplit((url or '').strip()).path or '/').lower()
    if (path.rstrip('/') or '/') in _manual_tech_paths(project_id):
        return None
    for seg in path.split('/'):
        if not seg:
            continue
        label = _TECH_SEGMENTS.get(seg)
        if label:
            return {'label': label, 'segment': seg,
                    'soft': seg in _SOFT_SEGMENTS}
        label = _TECH_FILES.get(seg)
        if label:
            return {'label': label, 'segment': seg, 'soft': False}
    return None


# Потолок кандидатов на один хост: если в индекс уехал весь /personal/,
# отчёт не должен превратиться в простыню на тысячу строк - хватает примеров,
# чтобы задача была понятна (а перепроверять их все ещё и долго).
MAX_TECH_PER_HOST = 50


def tech_entry(url: str, source: str, project_id: str = None) -> dict:
    """Запись для host['tech'] - форма как у dead/errors (url/source/reason),
    чтобы отчёт читал их одинаково. None, если адрес не служебный.

    soft=True - метку надо подтвердить живой страницей (см. _SOFT_SEGMENTS)."""
    hit = classify_tech_url(url, project_id)
    if not hit:
        return None
    return {'url': url, 'source': source, 'label': hit['label'],
            'soft': hit['soft'],
            'reason': f'{hit["label"]} - служебная страница в индексе'}


def add_tech(host_bucket: dict, url: str, source: str,
             project_id: str = None) -> bool:
    """Добавить адрес в host['tech'], если он служебный и есть место.
    Возвращает True, если запись добавлена."""
    entry = tech_entry(url, source, project_id)
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


# Разметка страницы оформления заказа. Смотрим именно РАЗМЕТКУ, а не текст:
# фраза «оформить заказ» есть и на информационной «Как оформить заказ», а
# формы оформления с полями покупателя и итогом корзины - только на реальном
# чекауте. Маркеры собраны по нашим проектам: bx-soa - шаблон оформления
# Битрикса, ORDER_PROP_* - поля покупателя, остальное - типовые классы
# корзины/чекаута у Next.js-сайтов.
_RE_CHECKOUT = re.compile(
    r'bx-soa|ORDER_PROP_|name\s*=\s*["\']ORDER_|'
    r'(?:id|class)\s*=\s*["\'][^"\']*(?:checkout|basket|cart-total|order-form|'
    r'cart__total|order__form)', re.I)


def looks_like_checkout(html: str) -> bool:
    """Есть ли на странице разметка оформления заказа/корзины.

    Нужно для мягких меток (/order/): служебная это страница или
    информационная «Как заказать» - по адресу не понять, по разметке видно.
    ЧИСТАЯ функция - есть юнит-тест."""
    return bool(_RE_CHECKOUT.search((html or '')[:400_000]))


async def _check_one(session, url, proxy, sem):
    """(вердикт, код, признаки_чекаута). GET без редиректов: 301 на служебном
    адресе значит, что страницы по нему уже нет."""
    from indexing_checker import _find_meta_robots, _x_robots_noindex
    to = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with sem:
        try:
            async with session.get(url, timeout=to, proxy=proxy,
                                   allow_redirects=False) as r:
                if r.status != 200:
                    return 'gone', r.status, False
                headers = {k.lower(): v for k, v in r.headers.items()}
                html = await r.text(errors='replace')
        except Exception:
            return 'gone', None, False
    _, noindex = _find_meta_robots(html)
    if not noindex:
        _, noindex = _x_robots_noindex(headers)
    return tech_verdict(200, noindex), 200, looks_like_checkout(html)


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
            verdict, st, checkout = live.get(e.get('url'), (None, None, False))
            if verdict is None:
                tech.append(e)          # не дошли до перепроверки (потолок)
                continue
            if verdict != 'finding':
                dropped += 1
                continue
            # Мягкая метка (/order/) без разметки оформления - это
            # информационная страница «Как заказать», ей в индексе место.
            if e.get('soft') and not checkout:
                dropped += 1
                continue
            tech.append({**e, 'status': st})
            kept += 1
        nh['tech'] = tech
        new_hosts.append(nh)

    out = dict(check)
    out['hosts'] = new_hosts
    out['total_tech'] = sum(len(h.get('tech') or []) for h in new_hosts)
    out['tech_reverified'] = True
    _log(f'Тех. страницы в индексе: подтверждено {kept}, '
         f'убрано неподтверждённых {dropped}')
    return out
