"""
w3c_perf.py - валидация W3C и скорость ресурсов (пункт 1.16).

Три проверки, все по ВЫБОРКЕ страниц (главная + категория + товар) - у W3C
жёсткие лимиты, по всем страницам гонять нельзя:
  • HTML валиден - W3C Nu Html Checker (validator.w3.org/nu, GET ?doc=url -
    Nu сам качает страницу; POST крупного тела ловит 502);
  • CSS валиден - W3C CSS Validator (jigsaw.w3.org/css-validator, по URL);
  • время загрузки основных ресурсов - качаем HTML + свои/внешние CSS/JS/
    шрифты/картинки и замеряем время по типам (грубый серверный прокси
    реального рендера, но показывает тяжёлые ресурсы).

Внешние сервисы + скачивание ресурсов = медленно, поэтому за отдельной
галочкой (по запросу).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlsplit

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# Для W3C - ПРОСТОЙ UA: полный браузерный триггерит Cloudflare-челлендж
# (HTTP 403 «Just a moment…») на validator.w3.org. Простой проходит стабильно.
_W3C_UA = "checklist-validator/1.0 (+site-checker)"

_NU_URL = 'https://validator.w3.org/nu/'           # GET ?doc=<url>&out=json
_CSS_URL = 'https://jigsaw.w3.org/css-validator/validator'

# коды, при которых стоит повторить (транзиентные)
_RETRY_CODES = (429, 500, 502, 503, 504)


def _w3c_msg(prefix, status):
    """Понятное сообщение по HTTP-коду ответа W3C."""
    if status == 403:
        return f'{prefix} заблокировал (HTTP 403 - Cloudflare, повторить позже)'
    if status == 429:
        return f'{prefix} лимит запросов (HTTP 429, повторить позже)'
    if status and status >= 500:
        return f'{prefix} сервер недоступен (HTTP {status}, повторить позже)'
    return f'{prefix} не вернул JSON (HTTP {status})'

_RE_LINK = re.compile(r'<link\b[^>]*>', re.I)
_RE_SCRIPT = re.compile(r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_IMG = re.compile(r'<img\b[^>]*?(?:data-src|src)\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)

_FONT_EXT = ('.woff2', '.woff', '.ttf', '.otf', '.eot')
_IMG_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.svg', '.bmp')

# лимиты, чтобы прогон не растянулся
CAP = {'css': 12, 'js': 12, 'font': 8, 'img': 12}


def _get(url, proxy=None, timeout=25):
    """(ms, bytes, status). Скачивает тело - для честного времени передачи."""
    import requests
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers={'User-Agent': _UA}, timeout=timeout,
                         proxies=proxies, allow_redirects=True)
        data = r.content
        return int((time.monotonic() - t0) * 1000), len(data), r.status_code
    except Exception:
        return int((time.monotonic() - t0) * 1000), 0, None


def validate_html(url, proxy=None) -> dict:
    """W3C Nu по URL страницы: {errors, warnings, samples[], error(str|None)}.

    Валидируем через GET ?doc=<url> - W3C сам качает страницу. POST крупного
    HTML-тела (>150КБ) стабильно ловит 502 от Nu-бэкенда; ?doc= работает с
    первой попытки. До 3 попыток с паузой на транзиентных кодах."""
    import requests
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    status = None
    for attempt in range(3):
        try:
            r = requests.get(_NU_URL, params={'doc': url, 'out': 'json'},
                             headers={'User-Agent': _W3C_UA},
                             timeout=60, proxies=proxies)
            status = r.status_code
            try:
                j = r.json()
            except Exception:
                if status in _RETRY_CODES and attempt < 2:
                    time.sleep(4 + attempt * 3)
                    continue
                return {'errors': None, 'warnings': None, 'samples': [],
                        'error': _w3c_msg('W3C Nu', status)}
            msgs = j.get('messages', []) or []
            errs = [m for m in msgs if m.get('type') == 'error']
            warns = [m for m in msgs if m.get('type') != 'error']
            return {'errors': len(errs), 'warnings': len(warns),
                    'samples': [m.get('message', '')[:120] for m in errs[:5]],
                    'error': None}
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(4 + attempt * 3)
                continue
            return {'errors': None, 'warnings': None, 'samples': [],
                    'error': str(e)}
    return {'errors': None, 'warnings': None, 'samples': [],
            'error': _w3c_msg('W3C Nu', status)}


def validate_css(url, proxy=None) -> dict:
    """W3C CSS Validator по URL страницы: {errors, warnings, error}."""
    import requests
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    status = None
    for attempt in range(3):
        try:
            r = requests.get(_CSS_URL,
                             params={'profile': 'css3', 'output': 'json',
                                     'uri': url},
                             headers={'User-Agent': _W3C_UA}, timeout=60,
                             proxies=proxies)
            status = r.status_code
            try:
                j = r.json()
            except Exception:
                if status in _RETRY_CODES and attempt < 2:
                    time.sleep(4 + attempt * 3)
                    continue
                return {'errors': None, 'warnings': None,
                        'error': _w3c_msg('W3C CSS', status)}
            res = (j.get('cssvalidation', {}) or {}).get('result', {}) or {}
            return {'errors': res.get('errorcount'),
                    'warnings': res.get('warningcount'), 'error': None}
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(4 + attempt * 3)
                continue
            return {'errors': None, 'warnings': None, 'error': str(e)}
    return {'errors': None, 'warnings': None, 'error': _w3c_msg('W3C CSS', status)}


def _kind(url: str) -> str:
    p = urlsplit(url.split('?')[0]).path.lower()
    if p.endswith('.css'):
        return 'css'
    if p.endswith('.js'):
        return 'js'
    if p.endswith(_FONT_EXT):
        return 'font'
    if p.endswith(_IMG_EXT):
        return 'img'
    return ''


def resource_timings(url, html, proxy=None) -> dict:
    """Время загрузки ресурсов страницы по типам. Возвращает
    {html_ms, by_type:{css/js/font/img:{ms,count,kb}}, slowest, total_ms}."""
    # HTML уже есть - но замерим отдельным запросом для чистого времени.
    html_ms, html_kb = 0, 0
    m0, b0, _ = _get(url, proxy)
    html_ms, html_kb = m0, b0 // 1024

    # Собираем ресурсы из HTML.
    res = {'css': [], 'js': [], 'font': [], 'img': []}
    for tag in _RE_LINK.findall(html or ''):
        hm = _RE_HREF.search(tag)
        if not hm:
            continue
        u = urljoin(url, hm.group(1).strip())
        low = tag.lower()
        if 'stylesheet' in low or _kind(u) == 'css':
            res['css'].append(u)
        elif 'as="font"' in low.replace(' ', '') or _kind(u) == 'font':
            res['font'].append(u)
    for src in _RE_SCRIPT.findall(html or ''):
        res['js'].append(urljoin(url, src.strip()))
    for src in _RE_IMG.findall(html or ''):
        if not src.strip().startswith('data:'):
            res['img'].append(urljoin(url, src.strip()))

    by_type, slowest = {}, {'url': '', 'ms': 0, 'kind': ''}
    for k, urls in res.items():
        uniq = list(dict.fromkeys(urls))[:CAP[k]]
        tot_ms, tot_kb = 0, 0
        for u in uniq:
            ms, b, st = _get(u, proxy, timeout=20)
            tot_ms += ms
            tot_kb += b // 1024
            if ms > slowest['ms']:
                slowest = {'url': u, 'ms': ms, 'kind': k}
        by_type[k] = {'ms': tot_ms, 'count': len(uniq), 'kb': tot_kb}
    total = html_ms + sum(v['ms'] for v in by_type.values())
    return {'html_ms': html_ms, 'html_kb': html_kb, 'by_type': by_type,
            'slowest': slowest, 'total_ms': total}


def check_page(url, proxy=None) -> dict:
    """Одна страница: HTML-валидность + CSS-валидность + время ресурсов."""
    out = {'url': url, 'html': None, 'css': None, 'timings': None,
           'error': None}
    ms, _, st = _get(url, proxy)
    if st is None or st >= 400:
        out['error'] = f'страница не открылась (HTTP {st})'
        return out
    # тело для валидации HTML
    import requests
    try:
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        html = requests.get(url, headers={'User-Agent': _UA}, timeout=25,
                            proxies=proxies).text
    except Exception:
        html = ''
    out['html'] = validate_html(url, proxy)   # Nu сам качает URL (?doc=)
    time.sleep(2)                    # щадим лимиты W3C между запросами
    out['css'] = validate_css(url, proxy)
    out['timings'] = resource_timings(url, html, proxy)
    return out


def check_pages(urls, proxy=None, log=None) -> dict:
    """Выборка страниц. Возвращает {available, pages:[...], note}."""
    def _log(m):
        if log:
            log(m)
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return {'available': False, 'pages': [], 'note': 'нет страниц'}
    pages = []
    for i, u in enumerate(urls, 1):
        if i > 1:
            time.sleep(3)            # пауза между страницами - лимиты W3C
        _log(f'  [{i}/{len(urls)}] W3C+скорость: {u}')
        try:
            pages.append(check_page(u, proxy))
        except Exception as e:  # noqa: BLE001
            pages.append({'url': u, 'html': None, 'css': None,
                          'timings': None, 'error': str(e)})
    return {'available': True, 'pages': pages, 'note': None}
