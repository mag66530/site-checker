"""
http_checker.py - асинхронная проверка URL'ов.

Точная копия логики Node.js версии:
  • Таймаут на одну попытку: 120 сек (2 минуты)
  • До 3 попыток при сетевых ошибках, таймаутах и 5xx
  • 4xx (включая 404) не ретраится - это устойчивый результат
  • Между попытками - пауза 2.5 сек
  • После успешной OK-проверки опционально ищет битые переменные

Поддержка HTTP-прокси:
  Если задана переменная окружения HTTP_PROXY (или передан proxy_url),
  все запросы идут через неё. Без прокси приложение работает локально как раньше.

Оценка скорости (Google Core Web Vitals):
  < 2.5с  → fast      (ОК)
  2.5-4с  → normal    (ОК)
  4-8с    → slow      (Медленно)
  > 8с    → very_slow (Долгий ответ сервера)
"""
import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp

from text_checker import find_text_issues, TextIssue
from content_checker import check_content, ContentResult, parse_hidden_selectors


# ── Константы ────────────────────────────────────────────────────────


class STATUS:
    OK = 'ok'
    REDIRECT = 'redirect'
    REDIRECT_LOOP = 'redirect_loop'   # цикл или бесконечная цепочка = ошибка
    NOT_FOUND = 'not_found'
    CLIENT_ERROR = 'client_error'
    SERVER_ERROR = 'server_error'
    TIMEOUT = 'timeout'
    NETWORK = 'network_error'
    CANCELLED = 'cancelled'


class SPEED:
    FAST = 'fast'
    NORMAL = 'normal'
    SLOW = 'slow'
    VERY_SLOW = 'very_slow'


# User-Agent от свежего Chrome 131 (актуальный на 2026 год).
# Версия должна быть свежей - старые UA сами по себе подозрительны.
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)


def make_browser_headers(user_agent: str = DEFAULT_USER_AGENT) -> dict:
    """
    Реалистичный набор HTTP-заголовков, имитирующий настоящий Chrome.
    
    Многие anti-bot защиты (включая Cloudflare и SiteSecure) детектируют ботов
    по отсутствию или нестандартному порядку этих заголовков. Реальный Chrome
    шлёт их именно в таком составе и порядке.
    
    Sec-Fetch-* заголовки появились в Chrome 76 (2019) - отсутствие их сразу
    выдаёт «голый» HTTP-клиент.
    """
    return {
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Sec-Ch-Ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        'Connection': 'keep-alive',
    }


def rate_speed(elapsed_ms: Optional[int]) -> Optional[str]:
    """Оценить скорость по времени ответа."""
    if elapsed_ms is None:
        return None
    if elapsed_ms < 2500:
        return SPEED.FAST
    if elapsed_ms < 4000:
        return SPEED.NORMAL
    if elapsed_ms < 8000:
        return SPEED.SLOW
    return SPEED.VERY_SLOW


def classify(http_code: Optional[int], error_kind: Optional[str]) -> str:
    """Классифицировать результат запроса."""
    if error_kind == 'timeout':
        return STATUS.TIMEOUT
    if error_kind == 'redirect_loop':
        return STATUS.REDIRECT_LOOP
    if error_kind and not http_code:
        return STATUS.NETWORK
    if not http_code:
        return STATUS.NETWORK
    if 200 <= http_code < 300:
        return STATUS.OK
    if 300 <= http_code < 400:
        return STATUS.REDIRECT
    if http_code == 404:
        return STATUS.NOT_FOUND
    if 400 <= http_code < 500:
        return STATUS.CLIENT_ERROR
    if http_code >= 500:
        return STATUS.SERVER_ERROR
    return STATUS.NETWORK


def should_retry(status: str) -> bool:
    """Ретраить только то что может стать ОК со второй попытки."""
    return status in (STATUS.TIMEOUT, STATUS.NETWORK, STATUS.SERVER_ERROR)


# ── Результаты ──────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Результат проверки одного URL."""
    # Контекст задачи
    url: str
    city: str
    subdomain: str
    type_code: str
    type_label: str

    # Результат запроса
    http_code: Optional[int] = None
    status: str = STATUS.NETWORK
    is_ok: bool = False
    is_warning: bool = False
    is_error: bool = True

    # Метрики
    elapsed_ms: int = 0
    body_size: int = 0
    speed_rating: Optional[str] = None
    attempts: int = 1

    # Редиректы
    final_url: Optional[str] = None
    redirect_chain: list[dict] = field(default_factory=list)

    # Ошибки (если были)
    error_kind: Optional[str] = None        # 'timeout' | 'network' | None
    error_message: Optional[str] = None

    # Поиск битых переменных
    text_issues: list[TextIssue] = field(default_factory=list)
    has_text_issues: bool = False

    # Структурная проверка контента (блоки страницы)
    content: Optional["ContentResult"] = None
    content_bugs: int = 0
    has_content_bugs: bool = False

    # Сверка контактов с КП (только для главных страниц поддоменов)
    kp_result: Optional[dict] = None

    # Сверка адресов всех городов на странице «Контакты» с КП (только /contacts/)
    contacts_addr: Optional[dict] = None

    # Сверка телефона в контенте страницы с КП (например /kak-sdelat-pokupku/)
    page_phone: Optional[dict] = None

    # «Ссылки реально открываются» (404) - тяжёлая опц. проверка по каждой ссылке.
    # {'checked': int, 'broken': [{'url': str, 'code': int}]} | None (не проверяли)
    broken_links: Optional[dict] = None

    # Индексация (п.1.7): robots.txt / meta robots / X-Robots-Tag / canonical.
    # dict из indexing_checker.analyze_page_indexing | None (не проверяли)
    indexing: Optional[dict] = None
    has_indexing_issues: bool = False

    # Метаданные (п.1.8): title / description / H1 + город + длины.
    # dict из meta_checker.check_meta | None (не проверяли)
    meta: Optional[dict] = None
    has_meta_issues: bool = False

    # Региональные проверки (region_checker):
    # п.1.4.1 - верные переменные (чужой город/телефон/почта) | None - не проверяли
    region: Optional[dict] = None
    has_region_issues: bool = False
    # п.1.6 - СНГ-домен без РФ/СНГ/чужих стран | None - не проверяли / домен РФ
    cis: Optional[dict] = None
    has_cis_issues: bool = False

    # п.1.3.1 + 1.3.2 (часть пункта 1.8) - единственность title/description/H1,
    # дубли H2, заголовки h2-h6 вне текста | None - не проверяли
    meta_unique: Optional[dict] = None
    has_meta_unique_issues: bool = False

    # п.1.11 (ТЗ 2.1/2.1.1) - вёрстка и адаптивность: viewport, битые CSS,
    # @media | None - не проверяли
    layout: Optional[dict] = None
    has_layout_issues: bool = False

    # п.1.12 (ТЗ 3.5) - микроразметка Schema.org и OpenGraph
    # | None - не проверяли / нерелевантный тип страницы
    markup: Optional[dict] = None
    has_markup_issues: bool = False

    # Доп. чек-лист «1.8 заголовки безопасности»: HSTS/CSP/X-Frame и т.п.
    # dict из security_checker.check_security_headers | None - не проверяли
    security: Optional[dict] = None
    has_security_issues: bool = False

    # п.1.15 изображения: alt / webp-avif / вес. dict из image_checker | None
    images: Optional[dict] = None
    has_image_issues: bool = False

    checked_at: Optional[str] = None


# ── Одна попытка ────────────────────────────────────────────────────


async def _attempt_once(
    session: aiohttp.ClientSession,
    url: str,
    timeout_ms: int,
    proxy_url: Optional[str] = None,
) -> dict:
    """
    Одна попытка обращения к URL. Сама обрабатывает редиректы вручную,
    чтобы собрать redirect_chain.
    """
    started = time.monotonic()
    redirect_chain = []
    current_url = url
    http_code = None
    error_kind = None
    error_message = None
    body_text = None
    body_size = 0
    final_url = url
    resp_headers = None
    MAX_REDIRECTS = 10

    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)

    visited = {url}
    try:
        for hop in range(MAX_REDIRECTS + 1):
            try:
                async with session.get(
                    current_url,
                    timeout=timeout,
                    allow_redirects=False,
                    proxy=proxy_url,
                ) as resp:
                    http_code = resp.status

                    # Редирект - берём Location и идём дальше
                    if 300 <= resp.status < 400 and 'Location' in resp.headers:
                        from urllib.parse import urljoin
                        next_url = urljoin(current_url, resp.headers['Location'])
                        redirect_chain.append({
                            'from': current_url, 'to': next_url, 'code': resp.status,
                        })
                        current_url = next_url
                        final_url = next_url
                        # Циклический редирект: адрес уже был в цепочке
                        if next_url in visited:
                            error_kind = 'redirect_loop'
                            error_message = (f'Циклический редирект: цепочка '
                                             f'возвращается на {next_url}')
                            break
                        visited.add(next_url)
                        if hop == MAX_REDIRECTS:
                            error_kind = 'redirect_loop'
                            error_message = ('Превышен лимит редиректов '
                                             f'({MAX_REDIRECTS}) - цепочка '
                                             'не заканчивается')
                        continue

                    # Финальный ответ - читаем тело
                    final_url = current_url
                    # Заголовки финального ответа (для X-Robots-Tag и т.п.);
                    # ключи в lower, повторяющиеся склеиваем через запятую.
                    try:
                        resp_headers = {
                            k.lower(): ', '.join(resp.headers.getall(k))
                            for k in set(resp.headers)
                        }
                    except Exception:
                        resp_headers = None
                    try:
                        body_bytes = await resp.read()
                        body_size = len(body_bytes)
                        # Пытаемся декодировать как текст (для text-checker)
                        try:
                            body_text = body_bytes.decode(resp.charset or 'utf-8', errors='replace')
                        except Exception:
                            body_text = None
                    except Exception:
                        body_text = None
                    break
            except asyncio.TimeoutError:
                error_kind = 'timeout'
                error_message = 'Таймаут запроса'
                break
            except aiohttp.ClientError as e:
                error_kind = 'network'
                error_message = str(e)
                break
    except Exception as e:
        error_kind = 'network'
        error_message = str(e)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        'http_code': http_code,
        'error_kind': error_kind,
        'error_message': error_message,
        'final_url': final_url,
        'redirect_chain': redirect_chain,
        'body_size': body_size,
        'body_text': body_text,
        'headers': resp_headers,
        'elapsed_ms': elapsed_ms,
    }


# ── Подгрузка CSS (чтобы видеть, что спрятано стилями) ───────────────
# Цена/кнопка могут быть в HTML, но скрыты правилом display:none из CSS-файла.
# Чтобы это поймать, тянем подключённые на странице стили (свой хост),
# разбираем «скрывающие» селекторы и передаём их в check_content. Стили
# шаблона одинаковы на всех страницах домена - кэшируем по URL стиля.

_MAX_CSS_BYTES = 3_000_000


def _extract_stylesheet_links(html: str, base_url: str) -> list[str]:
    """Абсолютные URL подключённых стилей того же хоста (без дублей)."""
    host = urlsplit(base_url).netloc
    out, seen = [], set()
    for tag in re.findall(r'<link\b[^>]*>', html, re.I):
        if 'stylesheet' not in tag.lower():
            continue
        m = re.search(r'href\s*=\s*["\']([^"\']+)', tag, re.I)
        if not m:
            continue
        href = m.group(1).strip()
        if not href or href.startswith('data:'):
            continue
        absu = urljoin(base_url, href).split('#')[0]
        sp = urlsplit(absu)
        if sp.scheme not in ('http', 'https') or sp.netloc != host:
            continue
        if absu not in seen:
            seen.add(absu)
            out.append(absu)
    return out[:12]


_RE_IMG_ANY = re.compile(
    r'<img\b[^>]*?(?:data-src|src)\s*=\s*["\']([^"\']+)["\']', re.I)


def _extract_img_srcs(html: str, base_url: str, limit: int = 15) -> list[str]:
    """Абсолютные URL картинок ТОГО ЖЕ хоста (для проверки веса, п.1.15)."""
    host = urlsplit(base_url).netloc
    out, seen = [], set()
    for src in _RE_IMG_ANY.findall(html or ''):
        src = src.strip()
        if not src or src.startswith('data:'):
            continue
        absu = urljoin(base_url, src).split('#')[0]
        sp = urlsplit(absu)
        if sp.scheme not in ('http', 'https') or sp.netloc != host:
            continue
        if absu not in seen:
            seen.add(absu)
            out.append(absu)
            if len(out) >= limit:
                break
    return out


async def _img_size(session, url, timeout_ms, proxy_url, cache):
    """Размер картинки по Content-Length (HEAD; при 405/нет длины - GET-стрим).
    {'url','bytes'|None}. Кэш на батч."""
    if url in cache:
        return cache[url]
    to = aiohttp.ClientTimeout(total=min(timeout_ms, 15000) / 1000)
    size = None
    try:
        async with session.head(url, timeout=to, allow_redirects=True,
                                proxy=proxy_url) as r:
            cl = r.headers.get('Content-Length')
            if cl and cl.isdigit():
                size = int(cl)
    except Exception:
        pass
    if size is None:
        try:
            async with session.get(url, timeout=to, allow_redirects=True,
                                   proxy=proxy_url) as r:
                cl = r.headers.get('Content-Length')
                if cl and cl.isdigit():
                    size = int(cl)
                else:
                    size = len(await r.read())
        except Exception:
            size = None
    info = {'url': url, 'bytes': size}
    cache[url] = info
    return info


async def _fetch_css_text(session, url, timeout_ms, proxy_url):
    """(HTTP-статус | None, текст CSS). Пара попыток: один сбой/таймаут на
    стиль не должен «ослеплять» проверку видимости для всего домена
    (цена/кнопка тогда ложно считаются видимыми)."""
    to = aiohttp.ClientTimeout(total=min(timeout_ms, 30000) / 1000)
    for attempt in range(2):
        try:
            async with session.get(url, timeout=to, allow_redirects=True,
                                   proxy=proxy_url) as r:
                if r.status != 200:
                    return r.status, ''    # 401/403/404 - повтор не поможет
                data = await r.read()
                if len(data) > _MAX_CSS_BYTES:
                    data = data[:_MAX_CSS_BYTES]
                return 200, data.decode('utf-8', errors='replace')
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return None, ''                # сеть/таймаут - статус неизвестен
    return None, ''


_RE_CSS_MEDIA_WIDTH = re.compile(r'@media[^{]*\b(?:max|min)-width', re.I)


async def _css_info_for_url(session, url, timeout_ms, proxy_url, cache, locks, guard):
    """Инфо по одному CSS-URL (с кэшем на батч): скрывающие селекторы +
    HTTP-статус + признак @media-запросов (для проверки вёрстки, п.1.11)."""
    if url in cache:
        return cache[url]
    async with guard:
        lock = locks.get(url)
        if lock is None:
            lock = asyncio.Lock()
            locks[url] = lock
    async with lock:
        if url in cache:
            return cache[url]
        status, text = await _fetch_css_text(session, url, timeout_ms, proxy_url)
        # @font-face без font-display: swap/optional/fallback - браузер
        # прячет текст до загрузки шрифта, макет дёргается (CLS, п.1.11).
        _faces = re.findall(r'@font-face\s*\{[^}]*\}', text or '', re.I)
        _noswap = sum(1 for f in _faces
                      if not re.search(r'font-display\s*:\s*'
                                       r'(?:swap|optional|fallback)', f, re.I))
        info = {
            'url': url,
            'status': status,
            'has_media': bool(text and _RE_CSS_MEDIA_WIDTH.search(text)),
            'minified': _looks_minified(text),
            'fontface': len(_faces),
            'fontface_noswap': _noswap,
            # Состояния интерактивных элементов (п. чек-листа hover/focus/
            # active) - наличие псевдоклассов в CSS.
            'has_hover': bool(text and ':hover' in text),
            'has_focus': bool(text and ':focus' in text),
            'has_active': bool(text and ':active' in text),
            'selectors': parse_hidden_selectors(text) if text else (),
        }
        cache[url] = info
        return info


def _looks_minified(text) -> Optional[bool]:
    """Минифицирован ли CSS/JS: мало переносов строк, длинные строки. None,
    если контент не получен (не судим)."""
    if not text:
        return None
    n = text.count('\n')
    return n <= 3 or (len(text) / (n + 1)) > 200


# ── «Ссылки реально открываются» (404) ──────────────────────────────


async def _link_status(session, url, timeout_ms, proxy_url):
    """Код ответа ссылки (после редиректов). HEAD дёшево; если сервер не любит
    HEAD (405/501/5xx) - перепроверяем GET. None - не удалось определить
    (таймаут/сеть): такое НЕ считаем битым (это не «нет страницы»)."""
    to = aiohttp.ClientTimeout(total=min(timeout_ms, 20000) / 1000)
    try:
        async with session.head(url, timeout=to, allow_redirects=True,
                                proxy=proxy_url) as r:
            # 2xx/3xx и даже 401/403 - ссылка ведёт на существующую страницу
            # (доступ/метод - не «битость»). 404/410 - явно битая.
            if r.status < 405 or r.status in (410,):
                return r.status
    except Exception:
        pass
    try:
        async with session.get(url, timeout=to, allow_redirects=True,
                               proxy=proxy_url) as r:
            return r.status
    except Exception:
        return None


async def check_content_links(session, html, base_url, *, proxy_url=None,
                              timeout_ms=20000, limit=120,
                              link_cache: dict = None,
                              budget: list = None):
    """Проверить, что ссылки СТРАНИЦЫ реально открываются (не 404).
    Чек-лист «нет битых ссылок на странице»: ВСЯ страница (текст + блоки +
    шапка/подвал/листинг), не только контентная зона.

    Только ВНУТРЕННИЕ ссылки (тот же сайт): внешние часто блокируют ботов и
    дают ложные «битые». Битой считаем ТОЛЬКО явный 404/410 (страницы нет);
    таймаут/сеть/5xx/403 не считаем (это не «нет страницы» и оно флаки).

    link_cache - общий кеш кодов на весь прогон (шапка/подвал/меню одинаковы
    на всех страницах - каждую уникальную ссылку звоним ОДИН раз за прогон).
    budget - [остаток] общий лимит новых прозвонов на прогон.
    Возвращает {'checked', 'broken':[{'url','code'}]} или None (нечего звонить)."""
    from content_checker import extract_content_links
    from urllib.parse import urljoin, urlparse
    if not html:
        return None
    link_cache = link_cache if link_cache is not None else {}

    def _host(h):
        h = (h or '').lower()
        return h[4:] if h.startswith('www.') else h

    base_host = _host(urlparse(base_url).netloc)
    todo, seen = [], set()
    for h in extract_content_links(html, limit=limit * 4, include_chrome=True):
        absu = urljoin(base_url, h)
        pu = urlparse(absu)
        if pu.scheme not in ('http', 'https') or _host(pu.netloc) != base_host:
            continue                       # только http(s) и только свой сайт
        key = absu.split('#')[0]
        if key in seen:
            continue
        seen.add(key)
        todo.append(key)
        if len(todo) >= limit:
            break
    if not todo:
        return None

    # Звоним только НОВЫЕ ссылки (нет в кеше прогона), в пределах бюджета.
    new = [u for u in todo if u not in link_cache]
    if budget is not None:
        new = new[:max(budget[0], 0)]
        budget[0] -= len(new)
    if new:
        codes = await asyncio.gather(
            *[_link_status(session, u, timeout_ms, proxy_url) for u in new],
            return_exceptions=True)
        for u, code in zip(new, codes):
            link_cache[u] = None if isinstance(code, Exception) else code
    checked = [u for u in todo if u in link_cache]
    broken = [{'url': u, 'code': link_cache[u]}
              for u in checked if link_cache[u] in (404, 410)]
    return {'checked': len(checked), 'broken': broken}


# ── Проверка с ретраями ─────────────────────────────────────────────


async def check_one(
    session: aiohttp.ClientSession,
    task,                       # CheckTask из sources.py
    *,
    timeout_ms: int = 120000,
    max_attempts: int = 3,
    retry_delay_ms: int = 2500,
    check_text: bool = True,
    text_patterns: str | None = None,
    check_structure: bool = True,
    check_links: bool = False,
    check_indexing: bool = False,
    check_meta: bool = False,
    check_region: bool = False,
    check_cis: bool = False,
    check_layout: bool = False,
    check_markup: bool = False,
    check_security: bool = False,
    check_images: bool = False,
    region_ctx=None,            # RegionContext из region_checker.py
    proxy_url: Optional[str] = None,
    kp_map: Optional[dict] = None,
    get_css_hidden: Optional[Callable] = None,
    get_robots: Optional[Callable] = None,
    get_css_infos: Optional[Callable] = None,
    get_image_infos: Optional[Callable] = None,
    links_cache: Optional[dict] = None,   # общий кеш прозвона ссылок (прогон)
    links_budget: Optional[list] = None,  # [остаток] лимит новых прозвонов
) -> CheckResult:
    """Проверить один URL с возможными повторами."""
    last = None
    attempts = 0
    for i in range(max_attempts):
        attempts += 1
        attempt = await _attempt_once(session, task.url, timeout_ms, proxy_url=proxy_url)
        status = classify(attempt['http_code'], attempt['error_kind'])
        last = {**attempt, 'status': status}

        if not should_retry(status):
            break
        if i < max_attempts - 1:
            await asyncio.sleep(retry_delay_ms / 1000)

    a = last
    status = a['status']
    is_ok = (status == STATUS.OK)

    # Битые переменные - только для OK с body
    text_issues = []
    if is_ok and check_text and a['body_text']:
        try:
            text_issues = find_text_issues(a['body_text'], text_patterns)
        except Exception:
            text_issues = []

    # Структурная проверка контента - только для OK с body. Подтягиваем CSS,
    # чтобы цена/кнопка, скрытые стилями (display:none), считались невидимыми.
    content = None
    if is_ok and check_structure and a['body_text']:
        css_hidden = ()
        if get_css_hidden is not None:
            try:
                css_hidden = await get_css_hidden(
                    a['body_text'], a['final_url'] or task.url)
            except Exception:
                css_hidden = ()
        try:
            content = check_content(a['body_text'], task.type_code,
                                    css_hidden=css_hidden, url=task.url)
        except Exception:
            content = None

    # Сверка контактов с КП - только на главной поддомена (шапка/подвал -
    # сквозные, контакты привязаны к городу/домену).
    kp_result = None
    if is_ok and kp_map and task.type_code == 'main' and a['body_text']:
        try:
            from kp import check_against_kp
            kp_res = check_against_kp(a['body_text'], task.subdomain, kp_map)
            if kp_res.matched_kp:
                kp_result = {
                    'domain': kp_res.domain, 'city': kp_res.city,
                    'issues': kp_res.issues, 'has_issues': kp_res.has_issues,
                }
        except Exception:
            kp_result = None

    # Сверка адресов ВСЕХ городов на странице «Контакты» с КП.
    contacts_addr = None
    if (is_ok and kp_map and task.type_code == 'tech' and a['body_text']
            and '/contact' in task.url.lower()):
        try:
            from kp import check_contacts_addresses
            contacts_addr = check_contacts_addresses(a['body_text'], kp_map)
        except Exception:
            contacts_addr = None

    # Сверка телефона в контенте страницы с КП (например /kak-sdelat-pokupku/).
    page_phone = None
    if (is_ok and kp_map and task.type_code == 'tech' and a['body_text']
            and 'kak-sdelat-pokupku' in task.url.lower()):
        try:
            from kp import check_page_phone
            page_phone = check_page_phone(a['body_text'], task.subdomain, kp_map)
        except Exception:
            page_phone = None

    # Индексация (п.1.7): robots.txt (эталон) + meta robots + X-Robots-Tag +
    # canonical. robots.txt хоста берём через get_robots (кэш на весь батч).
    indexing = None
    if check_indexing and is_ok:
        try:
            from indexing_checker import analyze_page_indexing
            robots = None
            if get_robots is not None:
                host = urlsplit(task.url).netloc
                robots = await get_robots(host)
            indexing = analyze_page_indexing(
                a['body_text'], a.get('headers'), task.url, robots,
                page_type=task.type_code)
        except Exception:
            indexing = None

    # Метаданные (п.1.8): title/description/H1 + город + длины - из уже
    # скачанного HTML, без доп. запросов. Дубли считаются после батча.
    meta = None
    if check_meta and is_ok and a['body_text']:
        try:
            from meta_checker import extract_meta, check_meta as _check_meta
            meta = _check_meta(extract_meta(a['body_text']),
                               task.city, task.type_code)
        except Exception:
            meta = None

    # «Ссылки реально открываются» (404) - тяжёлая опц. проверка. Чек-лист:
    # ВСЕ ссылки ВСЕХ страниц (не только тех.) - нагрузку держит общий кеш
    # прогона (сквозные шапка/подвал/меню звонятся один раз) + бюджет.
    broken_links = None
    if check_links and is_ok and a['body_text']:
        try:
            broken_links = await check_content_links(
                session, a['body_text'], a['final_url'] or task.url,
                proxy_url=proxy_url, timeout_ms=timeout_ms,
                link_cache=links_cache, budget=links_budget)
        except Exception:
            broken_links = None

    # Региональные проверки (region_checker) - чистые regex по скачанному HTML.
    # п.1.4.1: чужой город в title/description/H1, телефон/почта другого города.
    region = None
    if check_region and is_ok and region_ctx is not None and a['body_text']:
        try:
            from region_checker import check_region_vars
            region = check_region_vars(a['body_text'], task.subdomain, region_ctx)
        except Exception:
            region = None
        # Технический регион (гео-сигналы) - только с главной поддомена:
        # meta geo.* и Schema addressLocality сквозные для всего поддомена.
        if task.type_code == 'main':
            try:
                from region_checker import check_geo_region
                _city = region_ctx.host_city.get(task.subdomain, '') or task.city
                _geo = check_geo_region(a['body_text'], _city)
                if region is None:
                    region = {'город': _city, 'issues': []}
                region['geo'] = _geo
            except Exception:
                pass
    # п.1.6: на СНГ-домене нет РФ / СНГ / чужих стран (сам вернёт None для РФ).
    cis = None
    if check_cis and is_ok and region_ctx is not None and a['body_text']:
        try:
            from region_checker import check_cis_mentions
            cis = check_cis_mentions(a['body_text'], task.subdomain, region_ctx)
        except Exception:
            cis = None
    # п.1.11 (ТЗ 2.1/2.1.1): вёрстка и адаптивность - viewport, битые CSS,
    # @media. Статусы CSS берём из кэша батча (стили уже качаются для
    # проверки видимости цены/кнопок - лишних запросов нет).
    layout = None
    if check_layout and is_ok and a['body_text']:
        try:
            from layout_checker import check_layout as _check_layout
            _css_infos = None
            if get_css_infos is not None:
                _css_infos = await get_css_infos(
                    a['body_text'], a['final_url'] or task.url)
            layout = _check_layout(a['body_text'], _css_infos,
                                   base_url=a['final_url'] or task.url)
        except Exception:
            layout = None
        # ТЗ 2.2/2.3: переходы из меню шапки (тех. страницы + каталог).
        # Меню сквозное - прозваниваем ссылки один раз на поддомен, с его
        # главной. Битой считаем ТОЛЬКО явный 404/410 (как в check_content_links).
        if layout is not None and task.type_code == 'main':
            try:
                from layout_checker import extract_menu_links
                _menu = extract_menu_links(
                    a['body_text'], a['final_url'] or task.url)
                if _menu:
                    _menu_sem = asyncio.Semaphore(8)

                    async def _probe_menu(u):
                        async with _menu_sem:
                            return await _link_status(
                                session, u, min(timeout_ms, 20000), proxy_url)

                    _codes = await asyncio.gather(
                        *[_probe_menu(u) for u in _menu], return_exceptions=True)
                    _broken = [{'url': u, 'code': c}
                               for u, c in zip(_menu, _codes)
                               if not isinstance(c, Exception) and c in (404, 410)]
                    layout['menu'] = {'checked': len(_menu), 'broken': _broken}
                    if _broken:
                        layout['issues'].append(
                            'битые ссылки в меню шапки (404) - переходы по '
                            'тех. страницам/каталогу не работают')
            except Exception:
                pass
        # Favicon: установлен и реально грузится. Сквозной - проверяем один
        # раз на поддомен, с его главной. Битым считаем только явный 404/410
        # (сеть/таймаут - не приговор, как в меню).
        if layout is not None and task.type_code == 'main':
            try:
                from layout_checker import extract_favicon
                _fav_url, _fav_tag = extract_favicon(
                    a['body_text'], a['final_url'] or task.url)
                _fav_status = None
                if _fav_url:
                    _fav_status = await _link_status(
                        session, _fav_url, min(timeout_ms, 20000), proxy_url)
                layout['favicon'] = {'url': _fav_url, 'tag': _fav_tag,
                                     'status': _fav_status}
                if _fav_url and _fav_status in (404, 410):
                    layout['issues'].append(
                        'favicon не грузится (битая ссылка в link rel="icon")'
                        if _fav_tag else
                        'favicon не установлен (нет <link rel="icon">, '
                        'и /favicon.ico отдаёт 404)')
            except Exception:
                pass

    # п.1.12 (ТЗ 3.5): микроразметка Schema.org + OpenGraph. Только основные
    # типы страниц (+ контакты); сам чекер вернёт None для нерелевантных.
    markup = None
    if check_markup and is_ok and a['body_text']:
        try:
            from schema_checker import check_markup as _check_markup
            markup = _check_markup(a['body_text'], task.type_code, task.url)
        except Exception:
            markup = None

    # п.1.3.1 + 1.3.2 (та же галочка 1.8): единственность title/description/H1,
    # дубли H2 и «текстовость» заголовков (h2-h6 не в шапке/подвале/меню).
    meta_unique = None
    if check_meta and is_ok and a['body_text']:
        try:
            from meta_checker import check_tags
            meta_unique = check_tags(a['body_text'], task.url, task.type_code)
        except Exception:
            meta_unique = None

    # Доп. чек-лист «1.8»: заголовки безопасности финального ответа.
    # Заголовки есть у любого ответа (не только 200), но проверяем по OK -
    # редиректы/ошибки отдают свой набор, не репрезентативно.
    security = None
    if check_security and is_ok and a.get('headers'):
        try:
            from security_checker import check_security_headers
            security = check_security_headers(a['headers'],
                                              a['final_url'] or task.url)
        except Exception:
            security = None

    # п.1.15: изображения - alt, современные форматы, вес (HEAD своих картинок).
    images = None
    if check_images and is_ok and a['body_text']:
        try:
            from image_checker import check_images as _check_images
            _base = a['final_url'] or task.url
            _img_infos = None
            if get_image_infos is not None:
                _img_infos = await get_image_infos(a['body_text'], _base)
            images = _check_images(a['body_text'], _base, _img_infos)
        except Exception:
            images = None

    return CheckResult(
        url=task.url,
        city=task.city,
        subdomain=task.subdomain,
        type_code=task.type_code,
        type_label=task.type_label,
        http_code=a['http_code'],
        status=status,
        is_ok=is_ok,
        is_warning=(status == STATUS.REDIRECT),
        is_error=(status not in (STATUS.OK, STATUS.REDIRECT)),
        elapsed_ms=a['elapsed_ms'],
        body_size=a['body_size'],
        speed_rating=rate_speed(a['elapsed_ms']) if is_ok else None,
        attempts=attempts,
        final_url=a['final_url'] if a['final_url'] != task.url else None,
        redirect_chain=a['redirect_chain'],
        error_kind=a['error_kind'],
        error_message=a['error_message'],
        text_issues=text_issues,
        has_text_issues=len(text_issues) > 0,
        content=content,
        content_bugs=content.bug_count if content else 0,
        has_content_bugs=bool(content and content.has_bugs),
        kp_result=kp_result,
        contacts_addr=contacts_addr,
        page_phone=page_phone,
        broken_links=broken_links,
        indexing=indexing,
        has_indexing_issues=bool(indexing and indexing.get('issues')),
        meta=meta,
        has_meta_issues=bool(meta and meta.get('issues')),
        region=region,
        has_region_issues=bool(region and region.get('issues')),
        cis=cis,
        has_cis_issues=bool(cis and cis.get('issues')),
        meta_unique=meta_unique,
        has_meta_unique_issues=bool(meta_unique and meta_unique.get('issues')),
        layout=layout,
        has_layout_issues=bool(layout and layout.get('issues')),
        markup=markup,
        has_markup_issues=bool(markup and markup.get('issues')),
        security=security,
        has_security_issues=bool(security and security.get('issues')),
        images=images,
        has_image_issues=bool(images and images.get('issues')),
        checked_at=None,
    )


# ── Параллельный батч с прогрессом ───────────────────────────────────


async def run_batch(
    tasks: list,
    *,
    concurrency: int = 6,
    timeout_ms: int = 120000,
    max_attempts: int = 3,
    retry_delay_ms: int = 2500,
    user_agent: str = DEFAULT_USER_AGENT,
    check_text: bool = True,
    text_patterns: str | None = None,
    check_structure: bool = True,
    check_links: bool = False,
    check_indexing: bool = False,
    check_meta: bool = False,
    check_region: bool = False,
    check_cis: bool = False,
    check_layout: bool = False,
    check_markup: bool = False,
    check_security: bool = False,
    check_images: bool = False,
    region_ctx=None,            # RegionContext из region_checker.build_region_context
    on_progress: Optional[Callable] = None,
    is_cancelled: Optional[Callable] = None,
    proxy_url: Optional[str] = None,
    kp_map: Optional[dict] = None,
) -> list[CheckResult]:
    """
    Прогнать все задачи параллельно с ограничением concurrency.
    
    on_progress(result, done, total) - вызывается после каждой завершённой
    проверки (синхронно).
    
    is_cancelled() -> bool - если возвращает True, оставшиеся задачи
    помечаются как 'cancelled'.

    proxy_url - если задан (или есть env HTTP_PROXY), все запросы идут через прокси.
    """
    # Если прокси не задан явно - берём из переменной окружения
    if proxy_url is None:
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')

    sem = asyncio.Semaphore(concurrency)
    results = []
    done_count = 0
    total = len(tasks)

    headers = make_browser_headers(user_agent)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)

    # Кэш разобранных стилей (общий на весь батч - шаблонный CSS повторяется
    # на всех страницах домена, тянем каждый файл один раз).
    css_cache: dict = {}
    css_locks: dict = {}
    css_guard = asyncio.Lock()
    img_cache: dict = {}                 # размеры картинок (HEAD), п.1.15

    # Кэш robots.txt по хостам (для проверки индексации) - качаем каждый
    # поддомен один раз на батч.
    robots_cache: dict = {}
    robots_locks: dict = {}
    robots_guard = asyncio.Lock()

    # Кеш прозвона ссылок на весь прогон (проверка «нет битых ссылок»):
    # сквозные ссылки шапки/подвала/меню одинаковы на всех страницах -
    # каждую уникальную звоним один раз. Бюджет - общий лимит новых
    # прозвонов на прогон, чтобы прогон не разползался по времени.
    links_cache: dict = {}
    links_budget = [2500]

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:

        async def get_css_hidden(html, base_url):
            sels = []
            for u in _extract_stylesheet_links(html, base_url):
                info = await _css_info_for_url(
                    session, u, timeout_ms, proxy_url, css_cache, css_locks, css_guard)
                sels.extend(info['selectors'])
            return tuple(sels)

        async def get_css_infos(html, base_url):
            """[{'url','status','has_media'}] по подключённым CSS страницы
            (для проверки вёрстки, п.1.11). Тот же кэш - без лишних запросов."""
            out = []
            for u in _extract_stylesheet_links(html, base_url):
                out.append(await _css_info_for_url(
                    session, u, timeout_ms, proxy_url, css_cache, css_locks, css_guard))
            return out

        async def get_image_infos(html, base_url):
            """[{'url','bytes'}] по своим картинкам страницы (для веса, п.1.15).
            HEAD, кэш на батч (шаблонные картинки повторяются)."""
            out = []
            for u in _extract_img_srcs(html, base_url):
                out.append(await _img_size(
                    session, u, timeout_ms, proxy_url, img_cache))
            return out

        async def get_robots(host):
            if host in robots_cache:
                return robots_cache[host]
            async with robots_guard:
                lock = robots_locks.get(host)
                if lock is None:
                    lock = asyncio.Lock()
                    robots_locks[host] = lock
            async with lock:
                if host in robots_cache:
                    return robots_cache[host]
                from indexing_checker import fetch_robots
                info = await fetch_robots(session, host, proxy_url=proxy_url)
                robots_cache[host] = info
                return info

        async def worker(task):
            nonlocal done_count
            async with sem:
                if is_cancelled and is_cancelled():
                    return CheckResult(
                        url=task.url, city=task.city, subdomain=task.subdomain,
                        type_code=task.type_code, type_label=task.type_label,
                        status=STATUS.CANCELLED, is_ok=False, is_error=False,
                    )
                result = await check_one(
                    session, task,
                    timeout_ms=timeout_ms,
                    max_attempts=max_attempts,
                    retry_delay_ms=retry_delay_ms,
                    check_text=check_text,
                    text_patterns=text_patterns,
                    check_structure=check_structure,
                    check_links=check_links,
                    check_indexing=check_indexing,
                    check_meta=check_meta,
                    check_region=check_region,
                    check_cis=check_cis,
                    check_layout=check_layout,
                    check_markup=check_markup,
                    check_security=check_security,
                    check_images=check_images,
                    region_ctx=region_ctx,
                    proxy_url=proxy_url,
                    kp_map=kp_map,
                    get_css_hidden=get_css_hidden,
                    get_robots=get_robots if check_indexing else None,
                    get_css_infos=get_css_infos if check_layout else None,
                    get_image_infos=get_image_infos if check_images else None,
                    links_cache=links_cache, links_budget=links_budget,
                )
                done_count += 1
                if on_progress:
                    try:
                        on_progress(result, done_count, total)
                    except Exception:
                        pass
                return result

        results = await asyncio.gather(*(worker(t) for t in tasks))

    return results
