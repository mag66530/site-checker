"""
home_dupes_checker.py - проверка дублей главной страницы.

Главная должна открываться по ОДНОМУ адресу. Если та же главная доступна по
разным адресам с кодом 200 (www / без www, http / https, со слэшем / без,
/index.php, /index.html, двойной слэш, мусорный ?параметр) - для поисковика это
дубли одной страницы. Аналог coolakov.ru и be1.ru/dubli-stranic, но точнее: те
просто смотрят «200 или нет», а мы ещё проверяем редирект и тег canonical:

  • вариант делает 301/302 на главную            → склеено, ок;
  • отдаёт 200, но <link rel=canonical> → главная → ок (поисковик склеит сам);
  • отдаёт 200 и canonical пустой / на себя        → ДУБЛЬ (реальная проблема);
  • 404/410                                        → адреса нет, дубля нет.

Только HTTP (aiohttp), без браузера - надёжно работает и на облаке.

CLI:
    python home_dupes_checker.py --project smu
    python home_dupes_checker.py --domain stalmetural.ru
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

# Вердикты для каждого варианта адреса.
V_MAIN = 'main'          # это сама каноническая главная (200)
V_REDIRECT = 'redirect'  # 3xx на главную (или в сторону) - склеено
V_CANONICAL = 'canonical'  # 200, но canonical → главная - поисковик склеит
V_DUPLICATE = 'duplicate'  # 200 без корректного canonical - реальный дубль
V_ABSENT = 'absent'      # 404/410 - такого адреса нет
V_ERROR = 'error'        # таймаут / ошибка сети / прочий код


def _canonical_from_html(html: str, base: str):
    """URL из <link rel="canonical"> (абсолютный) или None."""
    if not html:
        return None
    m = re.search(r'<link\b[^>]*\brel=["\']?canonical["\']?[^>]*>', html, re.I)
    if not m:
        return None
    href = re.search(r'\bhref=["\']([^"\']+)["\']', m.group(0), re.I)
    if not href:
        return None
    try:
        return urljoin(base, href.group(1).strip())
    except Exception:
        return None


def _split_home(home: str):
    sp = urlsplit(home)
    return sp.scheme.lower(), sp.netloc.lower()


def _norm_path(path: str) -> str:
    """Свернуть путь к «корневому» виду: /index.php|html|htm → /, // → /,
    убрать хвостовой слэш. Для сравнения «это та же главная»."""
    path = re.sub(r'/index\.(php|html?|htm)$', '/', path or '/', flags=re.I)
    path = re.sub(r'/{2,}', '/', path)
    if path != '/':
        path = path.rstrip('/') or '/'
    return path


def _same_home_norm(url, home) -> bool:
    """Тот же адрес главной с точностью до нормализации (для цели редиректа и
    для canonical): та же схема+хост, путь сворачивается к «/»."""
    if not url:
        return False
    try:
        sp = urlsplit(url)
    except Exception:
        return False
    hs, hh = _split_home(home)
    return (sp.scheme.lower() == hs and sp.netloc.lower() == hh
            and _norm_path(sp.path) == '/')


def _is_main_url(url, home) -> bool:
    """Ровно каноническая главная (строго): та же схема+хост, путь «» или «/»,
    без query. Только такой 200 - это «главная», всё остальное с кодом 200 -
    кандидат в дубли."""
    sp = urlsplit(url)
    hs, hh = _split_home(home)
    return (sp.scheme.lower() == hs and sp.netloc.lower() == hh
            and sp.path in ('', '/') and not sp.query)


def home_variants(home: str) -> list:
    """Варианты адресов главной для проверки (как в coolakov / be1)."""
    sp = urlsplit(home)
    host = sp.netloc.lower()
    bare = host[4:] if host.startswith('www.') else host
    www = 'www.' + bare
    out = []
    for scheme in ('https', 'http'):
        for h in (bare, www):
            b = f'{scheme}://{h}'
            out += [b + '/', b + '/index.php', b + '/index.html',
                    b + '//', b + '/?dubli=1']
    seen, res = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            res.append(v)
    return res


def _classify(url, status, location, canonical, home, final=None):
    """Вердикт по варианту адреса. Возвращает (verdict, человекочитаемо)."""
    if isinstance(status, str):                       # 'timeout' / 'error:...'
        return V_ERROR, ('таймаут' if status == 'timeout' else 'ошибка сети')
    if status in (404, 410):
        return V_ABSENT, f'{status} - адреса нет'
    if 300 <= status < 400:
        target = final or (urljoin(url, location) if location else None)
        if _same_home_norm(target, home):
            return V_REDIRECT, f'{status} → главная'
        return V_REDIRECT, f'{status} → {target or location or "?"}'
    if status == 200:
        if _is_main_url(url, home):
            return V_MAIN, '200 - это главная'
        # canonical засчитываем только если он указывает на ТОЧНУЮ чистую
        # главную (не на себя /index.php и не на другой хост/www) - иначе
        # поисковик считает этот адрес отдельной страницей = дубль.
        if canonical and _is_main_url(canonical, home):
            return V_CANONICAL, '200, canonical → главная (склейка)'
        return V_DUPLICATE, '200, без корректного canonical'
    return V_ERROR, f'код {status}'


async def _probe(session, url, proxy):
    """Первый ответ без следования редиректам: (status, Location, canonical)."""
    try:
        async with session.get(
                url, allow_redirects=False, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15)) as r:
            status = r.status
            loc = r.headers.get('Location')
            canonical = None
            if status == 200:
                try:
                    body = await r.text(errors='ignore')
                except Exception:
                    body = ''
                canonical = _canonical_from_html(body[:300000], str(r.url))
            return status, loc, canonical
    except asyncio.TimeoutError:
        return 'timeout', None, None
    except Exception:
        return 'error', None, None


async def _final_url(session, url, proxy):
    """Конечный адрес после всех редиректов (для 3xx) или None."""
    try:
        async with session.get(
                url, allow_redirects=True, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=20)) as r:
            return str(r.url)
    except Exception:
        return None


async def _resolve_home(session, root_domain, proxy) -> str:
    """Определить каноническую главную: куда в итоге ведёт https://<домен>/
    (учтёт выбор www/не-www и http→https)."""
    for scheme in ('https', 'http'):
        final = await _final_url(session, f'{scheme}://{root_domain}/', proxy)
        if final:
            sp = urlsplit(final)
            return urlunsplit((sp.scheme, sp.netloc, sp.path or '/', '', ''))
    return f'https://{root_domain}/'


async def check_home_dupes(root_domain: str, proxy_url=None, log=None) -> dict:
    """Проверить дубли главной. Возвращает dict для отчёта."""
    def _log(m):
        if log:
            log(m)

    if not root_domain:
        return {'available': False, 'error': 'не задан домен (root_domain)'}
    root_domain = root_domain.strip().lower()
    if root_domain.startswith('www.'):
        root_domain = root_domain[4:]

    connector = aiohttp.TCPConnector(limit=8, ssl=False, ttl_dns_cache=300)
    try:
        from http_checker import make_browser_headers
        headers = make_browser_headers()
    except Exception:
        headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        async with aiohttp.ClientSession(connector=connector,
                                         headers=headers) as session:
            home = await _resolve_home(session, root_domain, proxy_url)
            _log(f'Главная определена как {home}')
            variants = home_variants(home)

            async def _one(url):
                status, loc, canonical = await _probe(session, url, proxy_url)
                final = None
                if isinstance(status, int) and 300 <= status < 400:
                    final = await _final_url(session, url, proxy_url)
                verdict, note = _classify(url, status, loc, canonical, home, final)
                return {'url': url, 'status': status,
                        'redirect': final or loc, 'canonical': canonical,
                        'verdict': verdict, 'note': note}

            rows = await asyncio.gather(*[_one(v) for v in variants])
    except Exception as e:  # noqa: BLE001
        return {'available': False, 'error': f'сеть недоступна: {e}'}

    dupes = sum(1 for r in rows if r['verdict'] == V_DUPLICATE)
    _log(f'Проверено вариантов: {len(rows)}, реальных дублей: {dupes}')
    return {'available': True, 'home': home, 'variants': rows,
            'dupes': dupes, 'checked': len(rows)}


def run_check(root_domain: str, proxy_url=None, log=None) -> dict:
    """Синхронная обёртка для runner_30min (проверка быстрая, без браузера)."""
    return asyncio.run(check_home_dupes(root_domain, proxy_url=proxy_url, log=log))


def main():
    ap = argparse.ArgumentParser(description='Проверка дублей главной страницы')
    ap.add_argument('--project')
    ap.add_argument('--domain')
    a = ap.parse_args()
    dom = a.domain
    if not dom and a.project:
        try:
            from sources import load_project_config
            dom = (load_project_config(a.project) or {}).get('root_domain')
        except Exception:
            pass
    if not dom:
        print('Укажи --domain <домен> или --project <id>')
        raise SystemExit(2)
    res = run_check(dom, log=lambda m: print(f'[home-dupes] {m}'))
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
