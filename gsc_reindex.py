"""
gsc_reindex.py
==============
Автоматический запрос индексирования в Google Search Console.

Поток (проверен на живом GSC):
    обзор ресурса → строка проверки URL (омнибокс) → Enter →
    ждём live-проверку → клик «Запросить индексирование» →
    ждём подтверждение/квоту → лог.

Подготовка:
    1. python gsc_save_session.py     # держит авторизованный Chrome на порту 9222
       (НЕ закрывай это окно Chrome)
    2. (необязательно) python gsc_list_properties.py  # соберёт gsc_properties.json

    Список URL: файл urls.txt, по одному URL в строке. Это битые/проблемные
    страницы (домен и поддомены) - например из отчёта чек-листа.

Запуск:
    python gsc_reindex.py                       # читает urls.txt
    python gsc_reindex.py --urls broken.txt
    python gsc_reindex.py --dry-run             # без клика, только проверка
    python gsc_reindex.py --limit 10            # не больше 10 URL за запуск
    python gsc_reindex.py --delay 8             # пауза между URL, сек

Важно про квоты Google:
    Ручной запрос индексирования лимитирован (~10-12 URL в сутки на ресурс).
    При превышении GSC отвечает «превышена квота» - скрипт это поймает и
    остановится по этому ресурсу.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

CDP_URL = 'http://127.0.0.1:9222'
LOG_FILE = Path('gsc_reindex_log.json')
PROPS_FILE = Path('gsc_properties.json')

OVERVIEW = ('https://search.google.com/search-console/performance/'
            'search-analytics?resource_id={rid}')

OMNIBOX = 'input[aria-label*="Проверка всех URL"]'
REINDEX_TEXT = 'Запросить индексирование'

# Тексты-исходы после клика
RE_SUCCESS = re.compile(
    r'(Индексирование запрошено|очередь на индексирован|Запрос на индексирован|'
    r'URL добавлен|Indexing requested)', re.I)
RE_QUOTA = re.compile(r'(превышен[ао].{0,20}квот|quota|Превышена дневная)', re.I)
RE_ALREADY = re.compile(r'(уже отправлен|already requested)', re.I)


def _log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    pfx = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ '}.get(level, '  ')
    print(f'[{ts}] {pfx}{msg}')


def _load_urls(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if s and (s.startswith('http://') or s.startswith('https://')):
            out.append(s)
    return out


def _origin(url: str) -> str:
    p = urlparse(url)
    return f'{p.scheme}://{p.netloc}/'


def _resolve_resource(url: str, valid: set) -> str | None:
    """Подобрать resource_id для URL.
    URL-префикс: origin (https://host/). Домен: sc-domain:host."""
    origin = _origin(url)
    if not valid:
        return origin  # нет списка - доверяем origin
    if origin in valid:
        return origin
    host = urlparse(url).netloc
    # ресурс-домен на корневой домен или сам хост
    parts = host.split('.')
    for i in range(len(parts) - 1):
        cand = 'sc-domain:' + '.'.join(parts[i:])
        if cand in valid:
            return cand
    if origin in valid:
        return origin
    return None


def _save_log(entries: list):
    LOG_FILE.write_text(
        json.dumps({'run_at': datetime.now().isoformat(), 'entries': entries},
                   ensure_ascii=False, indent=2),
        encoding='utf-8')


async def _open_resource(page, resource_id: str):
    await page.goto(OVERVIEW.format(rid=quote(resource_id, safe='')),
                    wait_until='domcontentloaded')
    await page.wait_for_timeout(3500)


async def reindex_url(page, url: str, dry_run: bool) -> dict:
    res = {'url': url, 'status': 'error', 'message': ''}
    try:
        # Вставляем URL в омнибокс
        omni = page.locator(OMNIBOX).first
        await omni.wait_for(state='attached', timeout=8000)
        await omni.click(timeout=8000)
        await omni.fill(url, timeout=8000)
        await page.keyboard.press('Enter')
        _log(f'проверяю: {url}')

        # Ждём кнопку «Запросить индексирование» (live-проверка ~30-60 сек)
        try:
            await page.wait_for_selector(f'text={REINDEX_TEXT}', timeout=70000)
        except Exception:
            body = (await page.inner_text('body'))
            if 'есть в индексе' in body or 'проиндексирована' in body:
                res['status'] = 'skipped'
                res['message'] = 'нет кнопки (проверь состояние страницы)'
            else:
                res['status'] = 'skipped'
                res['message'] = 'кнопка индексации не появилась'
            _log(f'  {res["message"]}', 'warn')
            return res

        if dry_run:
            res['status'] = 'dry_run'
            res['message'] = 'кнопка найдена, клик пропущен (--dry-run)'
            _log('  [DRY RUN] кнопка есть, не кликаю', 'ok')
            return res

        # Кликаем
        await page.get_by_text(REINDEX_TEXT, exact=False).first.click()
        _log('  клик «Запросить индексирование», жду результат…')

        # Ждём исход: успех / квота / уже отправлен (до 150 сек)
        deadline = asyncio.get_event_loop().time() + 150
        outcome = None
        while asyncio.get_event_loop().time() < deadline:
            await page.wait_for_timeout(3000)
            txt = await page.inner_text('body')
            if RE_QUOTA.search(txt):
                outcome = 'quota'
                break
            if RE_SUCCESS.search(txt) or RE_ALREADY.search(txt):
                outcome = 'ok'
                break

        if outcome == 'ok':
            res['status'] = 'ok'
            res['message'] = 'запрос отправлен'
            _log('  ✓ индексирование запрошено', 'ok')
        elif outcome == 'quota':
            res['status'] = 'quota'
            res['message'] = 'превышена квота на ресурсе'
            _log('  квота исчерпана - остановка по ресурсу', 'warn')
        else:
            res['status'] = 'warn'
            res['message'] = 'клик сделан, подтверждение не поймано'
            _log('  клик сделан, подтверждение не поймано', 'warn')

        # Закрываем диалог
        for close in ('ПОНЯТНО', 'OK', 'Готово', 'Закрыть'):
            try:
                b = page.get_by_role('button', name=close)
                if await b.is_visible(timeout=1500):
                    await b.click()
                    break
            except Exception:
                pass
        await page.keyboard.press('Escape')

    except Exception as e:
        res['status'] = 'error'
        res['message'] = str(e)
        _log(f'  ошибка: {e}', 'error')
    return res


async def run(urls_file: str, dry_run: bool, limit: int, delay: float):
    urls = _load_urls(Path(urls_file))
    if not urls:
        _log(f'Нет URL в {urls_file}. Заполни файл (по одному URL в строке).', 'error')
        return

    valid = set()
    if PROPS_FILE.exists():
        try:
            valid = set(json.loads(PROPS_FILE.read_text(encoding='utf-8')))
        except Exception:
            pass
    _log(f'URL к обработке: {len(urls)}; известных ресурсов: {len(valid) or "нет (доверяю origin)"}')

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('pip install playwright')
        sys.exit(1)

    # Группируем URL по ресурсу, чтобы не переоткрывать обзор зря
    by_res: dict[str, list[str]] = {}
    unresolved = []
    for u in urls:
        r = _resolve_resource(u, valid)
        if r is None:
            unresolved.append(u)
        else:
            by_res.setdefault(r, []).append(u)
    if unresolved:
        _log(f'Без ресурса в GSC (пропущу): {len(unresolved)}', 'warn')
        for u in unresolved[:10]:
            _log(f'    {u}')

    entries = []
    done = 0
    quota_blocked: set = set()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            _log(f'Нет подключения к Chrome ({CDP_URL}): {e}', 'error')
            _log('Сначала запусти gsc_save_session.py (держит Chrome открытым).')
            return

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        for resource_id, res_urls in by_res.items():
            if limit and done >= limit:
                break
            _log(f'\n── Ресурс: {resource_id}  ({len(res_urls)} URL) ──')
            await _open_resource(page, resource_id)

            for u in res_urls:
                if limit and done >= limit:
                    _log(f'Достигнут лимит {limit}', 'warn')
                    break
                if resource_id in quota_blocked:
                    break

                r = await reindex_url(page, u, dry_run)
                r['resource_id'] = resource_id
                entries.append(r)
                _save_log(entries)

                if r['status'] in ('ok', 'dry_run'):
                    done += 1
                if r['status'] == 'quota':
                    quota_blocked.add(resource_id)
                    _log(f'Ресурс {resource_id} пропущен до завтра (квота)', 'warn')
                    break

                await asyncio.sleep(delay)

        await browser.close()

    ok = sum(1 for e in entries if e['status'] == 'ok')
    dr = sum(1 for e in entries if e['status'] == 'dry_run')
    sk = sum(1 for e in entries if e['status'] == 'skipped')
    q = sum(1 for e in entries if e['status'] == 'quota')
    er = sum(1 for e in entries if e['status'] == 'error')
    _log(f'\n══ Готово: запрошено {ok}, dry-run {dr}, пропущено {sk}, '
         f'квота {q}, ошибок {er} ══')
    _log(f'Лог → {LOG_FILE.resolve()}')


def parse_args():
    ap = argparse.ArgumentParser(description='Авто-запрос индексирования в GSC')
    ap.add_argument('--urls', default='urls.txt', help='файл со списком URL')
    ap.add_argument('--dry-run', action='store_true', help='без клика, только проверка')
    ap.add_argument('--limit', type=int, default=0, help='максимум URL за запуск')
    ap.add_argument('--delay', type=float, default=6, help='пауза между URL, сек')
    return ap.parse_args()


if __name__ == '__main__':
    a = parse_args()
    asyncio.run(run(a.urls, a.dry_run, a.limit, a.delay))
