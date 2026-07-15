"""
index_gsc_run.py - источник «Google» для проверки 404 в индексе.

Google Search Console сам помечает страницы по причинам («Не найдено (404)»,
«Ошибка сервера (5xx)» и т.д.) в отчёте «Индексирование → Страницы». Мы
браузером (та же сохранённая сессия, что у автокликеров/Яндекса) открываем
отчёт, проваливаемся в нужную причину и жмём «Экспортировать → CSV». Google
уже классифицировал страницы - код ответа проверять не надо.

Важные ограничения Google:
  • экспорт отдаёт максимум ~1000 адресов на причину (даже если их больше) -
    это ВЫБОРКА, не весь список;
  • у Google Domain-ресурс (sc-domain:<домен>) покрывает все поддомены разом,
    поэтому 404 приходят и по город-поддоменам (их Яндекс-часть и sitemap
    основного домена могут не видеть);
  • сессия Google слетает чаще, чем у Яндекса - тогда экспорт не пройдёт.

Ресурс и номер аккаунта берём из конфига проекта (gsc_resource / gsc_account),
иначе - sc-domain:<root_domain> и аккаунт 0.

Результат - в форме index_export_parser (dead/errors по хостам, source='Google')
в cache/index_gsc_<pid>.json → merge_index_404 сольёт с Яндексом и sitemap.

Запуск:
    python index_gsc_run.py --project smu
    python index_gsc_run.py --project smu --scout   # показать кнопки/ссылки
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
GSC_REPORT = ('https://search.google.com/u/{acct}/search-console/index'
              '?resource_id={res}&hl=ru')

# Причины GSC → (наш вердикт, текст строки в отчёте).
REASONS = [
    ('dead', 'Не найдено (404)'),
    ('server', 'Ошибка сервера (5xx)'),
]


def _log(msg: str):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _host_of(url: str) -> str:
    sp = urlsplit(url or '')
    h = (sp.netloc or '').lower()
    return h[4:] if h.startswith('www.') else h


def _gsc_target(project_id: str):
    """(resource_id, account) для GSC из конфига проекта."""
    from sources import load_project_config
    cfg = load_project_config(project_id) or {}
    res = cfg.get('gsc_resource')
    if not res:
        dom = cfg.get('root_domain') or ''
        res = f'sc-domain:{dom}' if dom else ''
    acct = str(cfg.get('gsc_account', '0'))
    return res, acct


def parse_gsc_export(data: bytes) -> list:
    """URL-ы из выгрузки GSC (drilldown). CSV или XLSX; URL в первой колонке,
    заголовок пропускаем."""
    urls = []
    if data[:2] == b'PK':                       # xlsx
        import warnings
        import openpyxl
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        # Лист с колонкой URL (у GSC обычно «Таблица»); если не нашли по
        # заголовку - берём любой, где первая колонка это http-адреса.
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if not rows:
                continue
            hdr0 = str((rows[0][0] if rows[0] else '') or '').strip().lower()
            body = rows[1:] if hdr0.startswith('url') else rows
            found = [str(r[0]).strip() for r in body
                     if r and str(r[0] or '').strip().startswith('http')]
            if found:
                urls.extend(found)
                break
    else:                                        # csv
        text = data.decode('utf-8-sig', errors='replace')
        for r in csv.reader(io.StringIO(text)):
            if r and str(r[0]).strip().startswith('http'):
                urls.append(str(r[0]).strip())
    return urls


async def _try_click(page, text: str, timeout: int = 5000) -> bool:
    try:
        await page.get_by_text(text, exact=False).first.click(timeout=timeout)
        return True
    except Exception:
        return False


async def _scout(page):
    _log('  scout: видимые кнопки/ссылки экспорта и причины:')
    for sel in ('button', 'a', '[role="button"]'):
        for el in await page.query_selector_all(sel):
            try:
                if not await el.is_visible():
                    continue
                t = ((await el.inner_text()) or '').strip().replace('\n', ' ')
                if t and any(k in t.lower() for k in
                             ('экспорт', 'скачать', 'csv', 'excel',
                              'не найдено', '404', 'ошибка сервера')):
                    _log(f'    [{sel}] "{t[:60]}"')
            except Exception:
                pass


async def _export_reason(page, res, acct, reason_text, scout) -> bytes | None:
    """Открыть отчёт «Страницы», провалиться в причину reason_text и скачать
    CSV. Возвращает байты файла или None."""
    url = GSC_REPORT.format(acct=acct, res=res)
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    except Exception as e:
        _log(f'  отчёт не открылся: {e}')
        return None
    await page.wait_for_timeout(6000)
    if 'accounts.google.com' in page.url or 'signin' in page.url:
        raise RuntimeError('НЕ АВТОРИЗОВАН в Google (сессия слетела/нет доступа)')

    # Скроллим - таблица причин подгружается ниже.
    for _ in range(6):
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(400)

    if scout:
        await _scout(page)
        return None

    # Проваливаемся в причину: кликаем строку с её текстом (ведёт на drilldown
    # с нужным item_key - надёжнее, чем угадывать ключ).
    if not await _try_click(page, reason_text, timeout=8000):
        _log(f'  строку «{reason_text}» не нашёл')
        await _scout(page)
        return None
    await page.wait_for_timeout(6000)

    # Экспорт: «Экспортировать» → «Скачать в формате CSV».
    try:
        async with page.expect_download(timeout=60000) as dl_info:
            await _try_click(page, 'Экспортировать', timeout=10000)
            await page.wait_for_timeout(800)
            if not await _try_click(page, 'CSV', timeout=5000):
                await _try_click(page, 'Excel', timeout=5000)
        download = await dl_info.value
        data = Path(await download.path()).read_bytes()
        _log(f'  «{reason_text}»: скачал {len(data)} байт')
        return data
    except Exception as e:
        _log(f'  ⚠ «{reason_text}»: экспорт не прошёл ({type(e).__name__}: {e})')
        await _scout(page)
        return None


async def _run(pid: str, scout: bool) -> dict:
    from autoclick_browser import open_browser
    from playwright.async_api import async_playwright

    res, acct = _gsc_target(pid)
    if not res:
        return {'available': False, 'source': 'gsc', 'hosts': [],
                'error': 'не задан GSC-ресурс (gsc_resource / root_domain)'}
    _log(f'GSC: ресурс {res}, аккаунт /u/{acct}/')

    by_host = {}
    async with async_playwright() as p:
        try:
            browser, page = await open_browser(p, _log)
        except Exception as e:
            return {'available': False, 'source': 'gsc', 'hosts': [],
                    'error': f'браузер/сессия недоступны: {e}'}
        try:
            for verdict, reason_text in REASONS:
                try:
                    data = await _export_reason(page, res, acct, reason_text, scout)
                except Exception as e:
                    if 'НЕ АВТОРИЗОВАН' in str(e):
                        return {'available': False, 'source': 'gsc', 'hosts': [],
                                'error': str(e)}
                    _log(f'  ⚠ {reason_text}: {e}')
                    data = None
                if not data:
                    continue
                for u in parse_gsc_export(data):
                    host = _host_of(u)
                    hb = by_host.setdefault(host, {
                        'host': host, 'dead': [], 'soft': [], 'errors': [],
                        'in_index_total': 0, 'checked': 0, 'ok': 0,
                        'redirects': 0})
                    entry = {'url': u, 'status': '404' if verdict == 'dead' else '5xx',
                             'source': 'Google', 'reason': f'GSC: {reason_text}'}
                    (hb['dead'] if verdict == 'dead' else hb['errors']).append(entry)
                    hb['checked'] += 1
                await page.wait_for_timeout(1500)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    if scout:
        return {'available': False, 'source': 'gsc', 'hosts': [],
                'error': 'scout-режим'}
    if not by_host:
        return {'available': False, 'source': 'gsc', 'hosts': [],
                'error': 'ни одной выгрузки GSC не получено (см. лог/кнопки)'}
    out = {'available': True, 'source': 'gsc', 'hosts': [], 'total_checked': 0,
           'total_dead': 0, 'total_soft': 0, 'error': None}
    for host, hb in sorted(by_host.items()):
        out['hosts'].append(hb)
        out['total_checked'] += hb['checked']
        out['total_dead'] += len(hb['dead'])
    return out


def main():
    ap = argparse.ArgumentParser(description='404 в индексе из Google Search Console')
    ap.add_argument('--project', required=True)
    ap.add_argument('--scout', action='store_true')
    a = ap.parse_args()
    res = asyncio.run(_run(a.project, a.scout))
    (ROOT / 'cache').mkdir(exist_ok=True)
    out = ROOT / 'cache' / f'index_gsc_{a.project}.json'
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding='utf-8')
    _log(f'Результат: available={res.get("available")}, '
         f'битых 404/410={res.get("total_dead", 0)} → {out.name}')
    if res.get('error'):
        _log(f'Заметка: {res["error"]}')


if __name__ == '__main__':
    main()
