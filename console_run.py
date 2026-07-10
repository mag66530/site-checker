"""
console_run.py - проверка «в консоли браузера нет ошибок JavaScript»
(пункт 1.14). Отдельный процесс с браузером (Playwright): 30-мин прогон
ходит по HTTP без браузера, а ошибки JS - рантайм, статикой не видны.

Открывает КАЖДУЮ переданную страницу (те, что выбрал пользователь: главная,
каталог, категории, фильтры, товары, тех.страницы) в headless Chromium,
слушает console.error и необработанные исключения (pageerror), записывает
их по каждой странице.

Запуск (URL-ы приходят файлом-списком от runner_30min):
    python console_run.py --project smu --urls-file cache/console_urls_smu.json

Локально - обычный headless; в облаке (env CCR_AGENT_PROXY_ENABLED) трафик
через сетевой стек драйвера (route.fetch), как в filters_run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / 'cache'
CACHE.mkdir(exist_ok=True)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAX_PAGES = 120          # верхняя граница, чтобы прогон не растянулся
WAIT_MS = 2500           # ждать после загрузки (асинхронные ошибки)

# Шумные сторонние ошибки, которые НЕ вина сайта (аналитика/виджеты/CORS
# сторонних доменов) - не считаем ошибкой сайта.
_IGNORE = (
    'mc.yandex', 'metrika', 'google-analytics', 'googletagmanager',
    'gtag', 'facebook', 'vk.com', 'jivosite', 'jivo', 'bitrix24',
    'recaptcha', 'gstatic', 'doubleclick', 'adservice',
    'err_blocked_by_client', 'net::err_', 'favicon',
    # браузерные политики/уведомления, не баги сайта:
    'permissions policy', 'permissions-policy', 'client hints',
    'high-entropy', 'deprecat', 'was preloaded using link preload',
    'is deprecated', 'quirks mode',
    'requeststorageaccess', 'storage access', 'permission denied',
    'third-party cookie', 'samesite',
)


def _noise(text: str) -> bool:
    t = (text or '').lower()
    return any(m in t for m in _IGNORE)


def run(pid: str, urls: list, log) -> dict:
    from playwright.sync_api import sync_playwright
    _via_driver = bool(os.environ.get('CCR_AGENT_PROXY_ENABLED'))
    urls = [u for u in dict.fromkeys(urls) if u][:MAX_PAGES]
    out = {'available': True, 'checked': 0, 'pages': [], 'note': None}
    with sync_playwright() as pw:
        b = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = b.new_context(
            locale='ru-RU', viewport={'width': 1440, 'height': 900},
            ignore_https_errors=_via_driver, user_agent=_UA,
            extra_http_headers={'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        if _via_driver:
            def _route(route, request):
                try:
                    route.fulfill(response=ctx.request.fetch(request))
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass
            ctx.route('**/*', _route)
        page = ctx.new_page()
        ctx.on('page', lambda p: p != page and p.close())

        errs: list = []
        page.on('console', lambda m: (
            errs.append((m.text or '').strip()[:200])
            if getattr(m, 'type', '') == 'error' else None))
        page.on('pageerror', lambda e: errs.append(('PageError: ' + str(e))[:200]))

        for i, url in enumerate(urls, 1):
            errs.clear()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=40000)
                page.wait_for_timeout(WAIT_MS)
            except Exception as e:  # noqa: BLE001
                log(f'  [{i}/{len(urls)}] ⚠ не открылась: {url} ({e})')
                out['pages'].append({'url': url, 'errors': [],
                                     'note': 'страница не открылась'})
                out['checked'] += 1
                continue
            # уникальные ошибки сайта (шум аналитики/виджетов отсеиваем)
            uniq = list(dict.fromkeys(x for x in errs if x and not _noise(x)))
            out['checked'] += 1
            out['pages'].append({'url': url, 'errors': uniq[:15]})
            if uniq:
                log(f'  [{i}/{len(urls)}] ❌ {len(uniq)} ошибок JS: {url}')
            else:
                log(f'  [{i}/{len(urls)}] ✅ чисто: {url}')
        try:
            ctx.close(); b.close()
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--urls-file', required=True)
    a = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    try:
        urls = json.loads(Path(a.urls_file).read_text(encoding='utf-8-sig')) or []
    except Exception as e:  # noqa: BLE001
        urls = []
        log(f'⚠ список URL не прочитан: {e}')

    out_path = CACHE / f'console_{a.project}.json'
    if not urls:
        log('Проверка консоли: список страниц пуст - пропуск.')
        out_path.write_text(json.dumps(
            {'available': False, 'checked': 0, 'pages': [],
             'note': 'нет страниц для проверки'}, ensure_ascii=False),
            encoding='utf-8')
        return

    log(f'Проверка консоли: страниц {len(urls)}, запускаю браузер…')
    try:
        res = run(a.project, urls, log)
    except Exception as e:  # noqa: BLE001
        log(f'⚠ Проверка консоли: {e}')
        res = {'available': True, 'checked': 0, 'pages': [],
               'note': f'браузер не запустился: {e}'}
    out_path.write_text(json.dumps(res, ensure_ascii=False), encoding='utf-8')
    _bad = sum(1 for p in res['pages'] if p.get('errors'))
    log(f'✓ Проверка консоли: страниц {res["checked"]}, с ошибками JS {_bad}')


if __name__ == '__main__':
    main()
