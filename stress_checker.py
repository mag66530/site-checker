"""
stress_checker.py - «Нет ошибок сервера при парсинге / высокой нагрузке /
дублях категорий-фильтров-товаров» (пункт доп. чек-листа).

Три пробы, все сетевые (aiohttp), без браузера. Гоняются В КОНЦЕ прогона,
по галочке - нагрузку на прод по умолчанию не создаём:

  1) Парсинг - быстрый обход выборки страниц подряд, одной сессией, без
     пауз. Ловим 5xx, 429 (rate-limit) и «бан посреди обхода» (403/капча
     после серии успешных ответов: защита приняла нас за парсера).
  2) Высокая нагрузка - параллельный залп (по умолчанию 30 одновременных ×
     2 волны) на репрезентативные страницы. 5xx / обрыв = баг; рост медианы
     времени против базового (из прогона) более чем в 3 раза = деградация.
  3) Дубли категорий-фильтров-товаров - мутации URL (сдвоенный сегмент,
     двойной слэш, сдвоенный GET-параметр, глубокая пагинация, сдвоенный
     /filter/). Bitrix классически 500-ит на кривых вариациях URL. Ждём
     200/301/404 - это ок; 5xx = баг (сервер падает на мусорном адресе).

Предохранители: копятся 5xx/обрывы (порог) - проба немедленно
останавливается, не добиваем сервер. Бан на парсинге - нагрузку и дубли
НЕ гоняем (их результат уже недостоверен, помечаем «пропущено из-за бана»).

Основной прогон к этому моменту уже отработал и отчёт по страницам собран,
поэтому падение сервера или бан здесь = находка в отчёте, а не сбой прогона.
"""
import asyncio
import time
from statistics import median
from urllib.parse import urlsplit

import aiohttp

from http_checker import make_browser_headers


# Параллельных запросов в залпе нагрузки (настраивается из прогона).
LOAD_CONCURRENCY = 30
LOAD_WAVES = 2                  # волн залпа на каждую страницу
LOAD_DEGRADE_FACTOR = 3.0       # рост медианы времени против базового = деградация
LOAD_ERR_STOP_RATE = 0.30       # >30% ошибок в первой волне - стоп (не добиваем)
PARSE_SERVER_ERR_STOP = 5       # столько 5xx на парсинге - обрыв обхода
PARSE_OK_STREAK_FOR_BAN = 3     # 403/429 считаем баном только после серии успехов
PROBE_TIMEOUT_MS = 30000
MAX_PARSE_URLS = 50             # потолок обхода-парсинга

# Маркеры страницы-заглушки антибота (капча/челлендж) в теле ответа.
_BAN_MARKERS = (
    'captcha', 'cloudflare', 'attention required', 'ddos-guard', 'ddos guard',
    'access denied', 'are you a robot', 'проверка, что вы не робот',
    'подтвердите, что вы не робот', 'доступ ограничен', 'доступ запрещён',
)


def _looks_banned(code, body, ok_streak):
    """Похоже, защита закрыла доступ: капча в теле ЛИБО 403/429 после того,
    как мы уже успешно прошли несколько страниц (иначе одиночный 403 -
    просто закрытый раздел, а не бан обхода)."""
    if body and any(m in body for m in _BAN_MARKERS):
        return True
    if code in (403, 429) and ok_streak >= PARSE_OK_STREAK_FOR_BAN:
        return True
    return False


async def _probe(session, url, proxy_url, timeout_ms=PROBE_TIMEOUT_MS,
                 want_body=False):
    """Один запрос. Возвращает {code, error_kind, elapsed_ms, body}.
    body читаем куском (для детекта капчи) только когда want_body."""
    to = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    t0 = time.monotonic()
    try:
        async with session.get(url, timeout=to, allow_redirects=True,
                               proxy=proxy_url) as resp:
            body = ''
            if want_body:
                try:
                    raw = await resp.content.read(4096)
                    body = raw.decode('utf-8', 'replace').lower()
                except Exception:
                    body = ''
            else:
                await resp.release()
            return {'code': resp.status, 'error_kind': None,
                    'elapsed_ms': int((time.monotonic() - t0) * 1000),
                    'body': body}
    except asyncio.TimeoutError:
        return {'code': None, 'error_kind': 'timeout',
                'elapsed_ms': int((time.monotonic() - t0) * 1000), 'body': ''}
    except Exception:
        return {'code': None, 'error_kind': 'network',
                'elapsed_ms': int((time.monotonic() - t0) * 1000), 'body': ''}


async def probe_parsing(session, urls, proxy_url):
    """Быстрый последовательный обход выборки: 5xx, 429, бан посреди обхода."""
    urls = list(urls)[:MAX_PARSE_URLS]
    checked = 0
    server_errors = []      # [{url, code}]  - 5xx
    network_errors = []     # [{url}]        - таймаут/обрыв
    rate_limited = 0        # 429
    banned = None           # {url, code, after} - доступ закрыт после N успехов
    ok_streak = 0
    stopped = None
    for u in urls:
        r = await _probe(session, u, proxy_url, want_body=True)
        checked += 1
        code = r['code']
        if _looks_banned(code, r.get('body'), ok_streak):
            banned = {'url': u, 'code': code or 0, 'after': ok_streak}
            stopped = 'ban'
            break
        if code == 429:
            rate_limited += 1
        if code and 500 <= code < 600:
            server_errors.append({'url': u, 'code': code})
        elif r['error_kind']:
            network_errors.append({'url': u})
        elif code and 200 <= code < 400:
            ok_streak += 1
        if len(server_errors) >= PARSE_SERVER_ERR_STOP:
            stopped = 'server_errors'
            break
    return {
        'checked': checked, 'total': len(urls),
        'server_errors': server_errors, 'network_errors': network_errors,
        'rate_limited': rate_limited, 'banned': banned, 'stopped': stopped,
    }


async def probe_load(session, page_urls, baselines, proxy_url,
                     concurrency=LOAD_CONCURRENCY, waves=LOAD_WAVES):
    """Параллельный залп по репрезентативным страницам. 5xx/обрыв = баг,
    рост медианы времени против базового = деградация. Предохранитель:
    >30% ошибок в первой волне - стоп по этой странице."""
    pages = []
    for url in page_urls:
        elapsed, server_5xx, net_err, sent = [], 0, 0, 0
        stopped = False
        for wave in range(waves):
            rs = await asyncio.gather(
                *[_probe(session, url, proxy_url) for _ in range(concurrency)],
                return_exceptions=True)
            wave_err = 0
            for r in rs:
                sent += 1
                if isinstance(r, Exception):
                    net_err += 1
                    wave_err += 1
                    continue
                code = r['code']
                if code is None:
                    net_err += 1
                    wave_err += 1
                elif 500 <= code < 600:
                    server_5xx += 1
                    wave_err += 1
                else:
                    elapsed.append(r['elapsed_ms'])
            # Предохранитель: слишком много ошибок в первой волне - не добиваем.
            if wave == 0 and concurrency and wave_err / concurrency > LOAD_ERR_STOP_RATE:
                stopped = True
                break
        med = int(median(elapsed)) if elapsed else None
        base = baselines.get(url)
        degraded = bool(med and base and med > base * LOAD_DEGRADE_FACTOR)
        pages.append({
            'url': url, 'sent': sent, 'server_5xx': server_5xx,
            'network_errors': net_err, 'median_ms': med, 'baseline_ms': base,
            'degraded': degraded, 'stopped': stopped,
        })
    return {'concurrency': concurrency, 'waves': waves, 'pages': pages}


def _mutations(url, type_code):
    """Кривые вариации URL, на которых кривой роутинг отдаёт 500.
    Возвращает [(метка, url)]."""
    parts = urlsplit(url)
    base = f'{parts.scheme}://{parts.netloc}'
    path = parts.path or '/'
    segs = [s for s in path.split('/') if s]
    out = []
    if segs:
        # /catalog/truby/ -> /catalog/truby/truby/ (сдвоенный последний сегмент)
        out.append(('сдвоенный сегмент пути',
                    base + '/' + '/'.join(segs + [segs[-1]]) + '/'))
        # /catalog/truby/ -> /catalog//truby/ (двойной слэш в пути)
        out.append(('двойной слэш в пути',
                    base + '/' + '/'.join(segs[:-1] + ['', segs[-1]]) + '/'))
    # Глубокая пагинация и сдвоенный GET-параметр
    out.append(('глубокая пагинация (?PAGEN_1=99999)',
                base + path + '?PAGEN_1=99999'))
    out.append(('сдвоенный GET-параметр',
                base + path + '?sort=price&sort=price'))
    if '/filter/' in path:
        out.append(('сдвоенный сегмент /filter/',
                    base + path.replace('/filter/', '/filter/filter/', 1)))
    return out


async def probe_url_duplicates(session, samples, proxy_url):
    """Мутации URL по выборке (категория/фильтр/товар). 5xx = баг."""
    checked = 0
    server_errors = []      # [{kind, url, base, code}]
    for type_code, url in samples:
        for kind, mut in _mutations(url, type_code):
            r = await _probe(session, mut, proxy_url)
            checked += 1
            code = r['code']
            if code and 500 <= code < 600:
                server_errors.append({'kind': kind, 'url': mut,
                                      'base': url, 'code': code})
    return {'checked': checked, 'server_errors': server_errors,
            'samples': len(samples)}


async def run_stress_check(*, parse_urls, load_pages, dup_samples, baselines,
                           proxy_url=None, concurrency=LOAD_CONCURRENCY,
                           log=None):
    """Оркестратор трёх проб. Бан на парсинге - нагрузку и дубли пропускаем.
    parse_urls   - список URL для обхода;
    load_pages   - список репрезентативных страниц для залпа;
    dup_samples  - [(type_code, url)] для мутаций;
    baselines    - {url: elapsed_ms} базовое время из прогона."""
    def _log(m):
        if log:
            log(m)
    out = {'available': True, 'concurrency': concurrency}
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=make_browser_headers(),
                                     connector=connector) as session:
        _log(f'Нагрузка/парсинг: обход парсингом до {len(parse_urls)} страниц…')
        parsing = await probe_parsing(session, parse_urls, proxy_url)
        out['parsing'] = parsing
        _log(f'  парсинг: проверено {parsing["checked"]}, 5xx '
             f'{len(parsing["server_errors"])}, обрывов '
             f'{len(parsing["network_errors"])}, 429 {parsing["rate_limited"]}'
             + (', БАН - нагрузку/дубли пропускаю' if parsing['banned'] else ''))
        if parsing['banned']:
            out['load'] = {'skipped': 'ban'}
            out['duplicates'] = {'skipped': 'ban'}
            return out
        _log(f'  нагрузка: залп {concurrency}×{LOAD_WAVES} на '
             f'{len(load_pages)} страниц(ы)…')
        out['load'] = await probe_load(session, load_pages, baselines,
                                       proxy_url, concurrency)
        _log(f'  дубли URL: мутации по {len(dup_samples)} страницам…')
        out['duplicates'] = await probe_url_duplicates(session, dup_samples,
                                                       proxy_url)
    return out
