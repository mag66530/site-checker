"""
http_checker.py — асинхронная проверка URL'ов.

Точная копия логики Node.js версии:
  • Таймаут на одну попытку: 120 сек (2 минуты)
  • До 3 попыток при сетевых ошибках, таймаутах и 5xx
  • 4xx (включая 404) не ретраится — это устойчивый результат
  • Между попытками — пауза 2.5 сек
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
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

import aiohttp

from text_checker import find_text_issues, TextIssue
from content_checker import check_content, ContentResult


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
# Версия должна быть свежей — старые UA сами по себе подозрительны.
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
    
    Sec-Fetch-* заголовки появились в Chrome 76 (2019) — отсутствие их сразу
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

                    # Редирект — берём Location и идём дальше
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

                    # Финальный ответ — читаем тело
                    final_url = current_url
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
        'elapsed_ms': elapsed_ms,
    }


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
    proxy_url: Optional[str] = None,
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

    # Битые переменные — только для OK с body
    text_issues = []
    if is_ok and check_text and a['body_text']:
        try:
            text_issues = find_text_issues(a['body_text'], text_patterns)
        except Exception:
            text_issues = []

    # Структурная проверка контента — только для OK с body
    content = None
    if is_ok and check_structure and a['body_text']:
        try:
            content = check_content(a['body_text'], task.type_code)
        except Exception:
            content = None

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
    on_progress: Optional[Callable] = None,
    is_cancelled: Optional[Callable] = None,
    proxy_url: Optional[str] = None,
) -> list[CheckResult]:
    """
    Прогнать все задачи параллельно с ограничением concurrency.
    
    on_progress(result, done, total) — вызывается после каждой завершённой
    проверки (синхронно).
    
    is_cancelled() -> bool — если возвращает True, оставшиеся задачи
    помечаются как 'cancelled'.

    proxy_url — если задан (или есть env HTTP_PROXY), все запросы идут через прокси.
    """
    # Если прокси не задан явно — берём из переменной окружения
    if proxy_url is None:
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')

    sem = asyncio.Semaphore(concurrency)
    results = []
    done_count = 0
    total = len(tasks)

    headers = make_browser_headers(user_agent)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:

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
                    proxy_url=proxy_url,
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
