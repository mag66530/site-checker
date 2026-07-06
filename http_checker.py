"""
http_checker.py – асинхронная проверка URL'ов.

Точная копия логики Node.js версии:
  • Таймаут на одну попытку: 120 сек (2 минуты)
  • До 3 попыток при сетевых ошибках, таймаутах и 5xx
  • 4xx (включая 404) не ретраится – это устойчивый результат
  • Между попытками – пауза 2.5 сек
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
# Версия должна быть свежей – старые UA сами по себе подозрительны.
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
    
    Sec-Fetch-* заголовки появились в Chrome 76 (2019) – отсутствие их сразу
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

    # «Ссылки реально открываются» (404) – тяжёлая опц. проверка по каждой ссылке.
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
    # п.1.4.1 – верные переменные (чужой город/телефон/почта) | None – не проверяли
    region: Optional[dict] = None
    has_region_issues: bool = False
    # п.1.6 – СНГ-домен без РФ/СНГ/чужих стран | None – не проверяли / домен РФ
    cis: Optional[dict] = None
    has_cis_issues: bool = False

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

                    # Редирект – берём Location и идём дальше
                    if 300 <= resp.status < 400 and 'Location' in resp.headers:
                        from urllib.parse import urljoin
                        next_url = urljoin(current_url, resp.headers['Location'])
                        redirect_chain.append({
                            'from': current_url, 'to': next_url, 'code': resp.status,
                        })
                        current_url = next_url
                        final_url = next_url
                        if hop == MAX_REDIRECTS:
                            error_message = 'Превышен лимит редиректов'
                        continue

                    # Финальный ответ – читаем тело
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
# шаблона одинаковы на всех страницах домена – кэшируем по URL стиля.

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


async def _fetch_css_text(session, url, timeout_ms, proxy_url) -> str:
    # Пара попыток: один сбой/таймаут на стиль не должен «ослеплять» проверку
    # видимости для всего домена (цена/кнопка тогда ложно считаются видимыми).
    to = aiohttp.ClientTimeout(total=min(timeout_ms, 30000) / 1000)
    for attempt in range(2):
        try:
            async with session.get(url, timeout=to, allow_redirects=True,
                                   proxy=proxy_url) as r:
                if r.status != 200:
                    return ''          # 401/403/404 – повтор не поможет
                data = await r.read()
                if len(data) > _MAX_CSS_BYTES:
                    data = data[:_MAX_CSS_BYTES]
                return data.decode('utf-8', errors='replace')
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return ''
    return ''


async def _css_sel_for_url(session, url, timeout_ms, proxy_url, cache, locks, guard):
    """Разобранные скрывающие селекторы для одного CSS-URL (с кэшем)."""
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
        text = await _fetch_css_text(session, url, timeout_ms, proxy_url)
        sels = parse_hidden_selectors(text) if text else ()
        cache[url] = sels
        return sels


# ── «Ссылки реально открываются» (404) ──────────────────────────────


async def _link_status(session, url, timeout_ms, proxy_url):
    """Код ответа ссылки (после редиректов). HEAD дёшево; если сервер не любит
    HEAD (405/501/5xx) – перепроверяем GET. None – не удалось определить
    (таймаут/сеть): такое НЕ считаем битым (это не «нет страницы»)."""
    to = aiohttp.ClientTimeout(total=min(timeout_ms, 20000) / 1000)
    try:
        async with session.head(url, timeout=to, allow_redirects=True,
                                proxy=proxy_url) as r:
            # 2xx/3xx и даже 401/403 – ссылка ведёт на существующую страницу
            # (доступ/метод – не «битость»). 404/410 – явно битая.
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
                              timeout_ms=20000, limit=25):
    """Проверить, что ссылки в контенте реально открываются (не 404).

    Только ВНУТРЕННИЕ ссылки (тот же сайт): внешние часто блокируют ботов и
    дают ложные «битые». Битой считаем ТОЛЬКО явный 404/410 (страницы нет);
    таймаут/сеть/5xx/403 не считаем (это не «нет страницы» и оно флаки).
    Возвращает {'checked', 'broken':[{'url','code'}]} или None (нечего звонить)."""
    from content_checker import extract_content_links
    from urllib.parse import urljoin, urlparse
    if not html:
        return None

    def _host(h):
        h = (h or '').lower()
        return h[4:] if h.startswith('www.') else h

    base_host = _host(urlparse(base_url).netloc)
    todo, seen = [], set()
    for h in extract_content_links(html, limit=limit * 4):
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

    codes = await asyncio.gather(
        *[_link_status(session, u, timeout_ms, proxy_url) for u in todo],
        return_exceptions=True)
    broken = [{'url': u, 'code': code}
              for u, code in zip(todo, codes)
              if not isinstance(code, Exception) and code in (404, 410)]
    return {'checked': len(todo), 'broken': broken}


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
    region_ctx=None,            # RegionContext из region_checker.py
    proxy_url: Optional[str] = None,
    kp_map: Optional[dict] = None,
    get_css_hidden: Optional[Callable] = None,
    get_robots: Optional[Callable] = None,
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

    # Битые переменные – только для OK с body
    text_issues = []
    if is_ok and check_text and a['body_text']:
        try:
            text_issues = find_text_issues(a['body_text'], text_patterns)
        except Exception:
            text_issues = []

    # Структурная проверка контента – только для OK с body. Подтягиваем CSS,
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

    # Сверка контактов с КП – только на главной поддомена (шапка/подвал –
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
                a['body_text'], a.get('headers'), task.url, robots)
        except Exception:
            indexing = None

    # Метаданные (п.1.8): title/description/H1 + город + длины – из уже
    # скачанного HTML, без доп. запросов. Дубли считаются после батча.
    meta = None
    if check_meta and is_ok and a['body_text']:
        try:
            from meta_checker import extract_meta, check_meta as _check_meta
            meta = _check_meta(extract_meta(a['body_text']),
                               task.city, task.type_code)
        except Exception:
            meta = None

    # «Ссылки реально открываются» (404) – тяжёлая опц. проверка (запрос по
    # каждой ссылке). Делаем только если включено, страница открылась и это
    # тех. страница (их немного, они на главном домене – нагрузка ограничена).
    broken_links = None
    if check_links and is_ok and a['body_text'] and task.type_code == 'tech':
        try:
            broken_links = await check_content_links(
                session, a['body_text'], a['final_url'] or task.url,
                proxy_url=proxy_url, timeout_ms=timeout_ms)
        except Exception:
            broken_links = None

    # Региональные проверки (region_checker) – чистые regex по скачанному HTML.
    # п.1.4.1: чужой город в title/description/H1, телефон/почта другого города.
    region = None
    if check_region and is_ok and region_ctx is not None and a['body_text']:
        try:
            from region_checker import check_region_vars
            region = check_region_vars(a['body_text'], task.subdomain, region_ctx)
        except Exception:
            region = None
    # п.1.6: на СНГ-домене нет РФ / СНГ / чужих стран (сам вернёт None для РФ).
    cis = None
    if check_cis and is_ok and region_ctx is not None and a['body_text']:
        try:
            from region_checker import check_cis_mentions
            cis = check_cis_mentions(a['body_text'], task.subdomain, region_ctx)
        except Exception:
            cis = None

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
    region_ctx=None,            # RegionContext из region_checker.build_region_context
    on_progress: Optional[Callable] = None,
    is_cancelled: Optional[Callable] = None,
    proxy_url: Optional[str] = None,
    kp_map: Optional[dict] = None,
) -> list[CheckResult]:
    """
    Прогнать все задачи параллельно с ограничением concurrency.
    
    on_progress(result, done, total) – вызывается после каждой завершённой
    проверки (синхронно).
    
    is_cancelled() -> bool – если возвращает True, оставшиеся задачи
    помечаются как 'cancelled'.

    proxy_url – если задан (или есть env HTTP_PROXY), все запросы идут через прокси.
    """
    # Если прокси не задан явно – берём из переменной окружения
    if proxy_url is None:
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')

    sem = asyncio.Semaphore(concurrency)
    results = []
    done_count = 0
    total = len(tasks)

    headers = make_browser_headers(user_agent)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)

    # Кэш разобранных стилей (общий на весь батч – шаблонный CSS повторяется
    # на всех страницах домена, тянем каждый файл один раз).
    css_cache: dict = {}
    css_locks: dict = {}
    css_guard = asyncio.Lock()

    # Кэш robots.txt по хостам (для проверки индексации) – качаем каждый
    # поддомен один раз на батч.
    robots_cache: dict = {}
    robots_locks: dict = {}
    robots_guard = asyncio.Lock()

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:

        async def get_css_hidden(html, base_url):
            sels = []
            for u in _extract_stylesheet_links(html, base_url):
                sels.extend(await _css_sel_for_url(
                    session, u, timeout_ms, proxy_url, css_cache, css_locks, css_guard))
            return tuple(sels)

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
                    region_ctx=region_ctx,
                    proxy_url=proxy_url,
                    kp_map=kp_map,
                    get_css_hidden=get_css_hidden,
                    get_robots=get_robots if check_indexing else None,
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
