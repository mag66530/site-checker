"""
index404_run.py - авто-скачивание выгрузки «Страницы в поиске» из
Яндекс.Вебмастера headless-браузером и разбор на 404.

Тот же механизм сессии, что у автокликеров (autoclick_browser.open_browser):
локальный залогиненный Chrome (CDP 9222) ЛИБО облачная сессия из секрета
autoclick_session. Логина с нуля НЕТ - у Яндекса капча, работаем только на
сохранённой сессии.

Для каждого хоста проекта заходит на
    webmaster.yandex.ru/site/https:<host>:443/indexing/searchable/
переключает на вкладку «Все страницы», жмёт «Скачать таблицу → CSV»,
парсит файл (index_export_parser) и пишет свод в cache/index404_<pid>.json
(структура как у check_index_404 - идёт в лист отчёта «404 в индексе»).

Запуск:
    python index404_run.py --project smu
    python index404_run.py --project smu --scout   # только показать кнопки
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from index_export_parser import analyze_exports

ROOT = Path(__file__).parent
SITES_URL = 'https://webmaster.yandex.ru/sites/'
SEARCHABLE_TMPL = 'https://webmaster.yandex.ru/site/{site_id}/indexing/searchable/'


def _log(msg: str):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _registrable(host: str) -> str:
    """Регистрируемый домен: spb.stalmetural.ru → stalmetural.ru. TLD у наших
    проектов однословные (.ru/.az/.by/.kg/.kz/.am/.uz) - берём 2 последних."""
    parts = (host or '').strip('.').split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def _project_domains(pid: str) -> set:
    """Регистрируемые домены проекта из catalogs/<pid>-subdomains.csv -
    чтобы из списка аккаунта Вебмастера оставить только сайты этого проекта."""
    doms = set()
    path = ROOT / 'catalogs' / f'{pid}-subdomains.csv'
    if not path.exists():
        return doms
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            u = (row.get('url') or '').strip()
            if u.startswith('http'):
                host = u.split('//', 1)[-1].strip('/').split('/')[0]
                if host:
                    doms.add(_registrable(host))
    return doms


def _hosts(pid: str) -> list:
    """Хосты проекта из catalogs/<pid>-subdomains.csv - ТОЛЬКО как fallback,
    если список сайтов из аккаунта Вебмастера получить не удалось."""
    path = ROOT / 'catalogs' / f'{pid}-subdomains.csv'
    if not path.exists():
        _log(f'Нет файла поддоменов: {path}')
        return []
    hosts = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            u = (row.get('url') or '').strip()
            if not u.startswith('http'):
                continue
            host = u.split('//', 1)[-1].strip('/').split('/')[0]
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def _host_from_site_id(site_id: str) -> str:
    """https:smg.az:443 → smg.az."""
    h = site_id
    for pre in ('https:', 'http:'):
        if h.startswith(pre):
            h = h[len(pre):]
    return h.rstrip(':').split(':')[0]


async def _collect_account_sites(page) -> list:
    """[(site_id, host)] реально существующих сайтов из аккаунта Вебмастера
    (страница /sites/). Это ground truth - именно эти сайты подтверждены в
    аккаунте, а не все поддомены из нашего каталога."""
    try:
        await page.goto(SITES_URL, wait_until='domcontentloaded', timeout=45000)
    except Exception as e:
        _log(f'  список сайтов аккаунта не открылся: {e}')
        return []
    await page.wait_for_timeout(4000)
    if 'passport.yandex' in page.url:
        # ВИДИМЫЙ режим: ждём, пока человек войдёт в Яндекс руками в окне.
        if os.environ.get('AUTOCLICK_MODE', '').strip().lower() == 'visible':
            _log('  ⚠ ВОЙДИ в Яндекс в открывшемся окне браузера '
                 '(webmaster.yandex.ru). Жду до 5 минут…')
            for _ in range(100):                 # 100 × 3с = 5 минут
                await page.wait_for_timeout(3000)
                if 'passport.yandex' not in page.url:
                    break
            else:
                raise RuntimeError('НЕ АВТОРИЗОВАН в Яндексе (за 5 мин вход не увидел)')
            _log('  ✓ Вижу вход — открываю список сайтов.')
            try:
                await page.goto(SITES_URL, wait_until='domcontentloaded',
                                timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
            if 'passport.yandex' in page.url:
                raise RuntimeError('НЕ АВТОРИЗОВАН в Яндексе (сессия слетела или не задана)')
        else:
            raise RuntimeError('НЕ АВТОРИЗОВАН в Яндексе (сессия слетела или не задана)')
    out, seen = [], set()
    for a in await page.query_selector_all('a[href*="/site/"]'):
        href = await a.get_attribute('href') or ''
        idx = href.find('/site/')
        if idx < 0:
            continue
        site_id = href[idx + len('/site/'):].split('/')[0]
        # id реального сайта - закодированный хост (есть точка/двоеточие);
        # навигационные ссылки вида /site/dashboard/ отсеиваем.
        if ('.' not in site_id and ':' not in site_id) or site_id in seen:
            continue
        seen.add(site_id)
        out.append((site_id, _host_from_site_id(site_id)))
    return out


async def _try_click(page, text: str, timeout: int = 5000) -> bool:
    try:
        await page.get_by_text(text, exact=False).first.click(timeout=timeout)
        return True
    except Exception:
        return False


async def _scout(page):
    """Показать видимые кнопки/ссылки со словами скачать/таблица/CSV/XLS -
    чтобы поймать реальный селектор при первом живом запуске."""
    _log('  scout: ищу кнопки скачивания…')
    for sel in ('button', 'a', '[role="button"]', 'span'):
        for el in await page.query_selector_all(sel):
            try:
                if not await el.is_visible():
                    continue
                t = ((await el.inner_text()) or '').strip().replace('\n', ' ')
                if t and any(k in t.lower() for k in
                             ('скач', 'таблиц', 'csv', 'xls', 'экспорт')):
                    _log(f'    [{sel}] "{t[:60]}"')
            except Exception:
                pass


async def _goto_backoff(page, url: str, tries: int = 5) -> bool:
    """Переход с защитой от 429 (как в webmaster_recheck)."""
    delay = 15
    for i in range(tries):
        try:
            resp = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        except Exception as e:
            _log(f'  goto упал ({e}) - пауза {delay}с')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 240)
            continue
        await page.wait_for_timeout(2500)
        status = resp.status if resp else 0
        try:
            head = (await page.inner_text('body'))[:300]
        except Exception:
            head = ''
        if status == 429 or 'Too many requests' in head or 'Слишком много' in head:
            _log(f'  ⚠ 429 - пауза {delay}с (попытка {i+1})')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 240)
            continue
        return True
    return False


async def _download_one(page, site_id: str, host: str, scout: bool) -> tuple:
    """(filename, bytes) | (None, None). Заходит на «Страницы в поиске»
    конкретного сайта аккаунта (по его site_id) и качает CSV.
    scout=True - только дамп кнопок."""
    url = SEARCHABLE_TMPL.format(site_id=site_id)
    if not await _goto_backoff(page, url):
        _log(f'  {host}: страница не открылась (429/сеть)')
        return None, None
    if 'passport.yandex' in page.url:
        raise RuntimeError('НЕ АВТОРИЗОВАН в Яндексе (сессия слетела или не задана)')
    await page.wait_for_timeout(1500)

    # Вкладка «Все страницы» - полный список в поиске (не только последние).
    await _try_click(page, 'Все страницы', timeout=4000)
    await page.wait_for_timeout(1500)

    if scout:
        await _scout(page)
        return None, None

    # Скачивание: «Скачать таблицу» открывает выбор формата → жмём CSV.
    # Оба клика внутри expect_download - какой из них реально запускает
    # скачивание, зависит от вёрстки Яндекса; ждём событие download.
    try:
        async with page.expect_download(timeout=45000) as dl_info:
            await _try_click(page, 'Скачать таблицу', timeout=8000)
            await page.wait_for_timeout(700)
            if not await _try_click(page, 'CSV', timeout=5000):
                await _try_click(page, 'XLS', timeout=5000)
        download = await dl_info.value
        tmp = await download.path()
        data = Path(tmp).read_bytes()
        fname = download.suggested_filename or f'{host}.csv'
        _log(f'  {host}: скачал {fname} ({len(data)} байт)')
        return fname, data
    except Exception as e:
        _log(f'  ⚠ {host}: скачать не удалось ({type(e).__name__}: {e})')
        await _scout(page)   # покажем кнопки, чтобы подстроить селектор
        return None, None


def _resolve_sites(account: list, pid: str) -> list:
    """Из списка сайтов аккаунта оставить сайты этого проекта, ПО ОДНОМУ на
    домен - корневой хост.

    Город-поддомены (abakan.stalmetural.ru, arhangelsk.stalmetural.ru…) - это
    отдельные сайты в аккаунте, но по сути региональные КЛОНЫ основного домена
    (тот же каталог, подставлен город). Их выгрузку отдельно не качаем: берём
    корень (host == регистрируемый домен). Если у домена корня в аккаунте нет -
    оставляем что есть, чтобы домен не выпал молча.

    Фильтр по регистрируемому домену проекта (spb.stalmetural.ru →
    stalmetural.ru) отсекает чужие сайты из общего аккаунта."""
    doms = _project_domains(pid)
    sites = [(sid, h) for sid, h in account if not doms or _registrable(h) in doms]
    if not sites:
        return account
    by_dom = {}
    for sid, h in sites:
        by_dom.setdefault(_registrable(h), []).append((sid, h))
    out = []
    for dom, group in by_dom.items():
        roots = [(sid, h) for sid, h in group if h == dom]
        out.extend(roots if roots else group)
    return out


async def _run(pid: str, max_hosts, scout: bool) -> dict:
    from autoclick_browser import open_browser
    from playwright.async_api import async_playwright

    files = []
    async with async_playwright() as p:
        try:
            browser, page = await open_browser(p, _log)
        except Exception as e:
            return {'available': False, 'source': 'yandex_export',
                    'error': f'браузер/сессия недоступны: {e}', 'hosts': []}
        try:
            # Ground truth - реальные сайты аккаунта Вебмастера, а НЕ все
            # поддомены из нашего каталога (город-поддомены обычно не отдельные
            # сайты, а страницы внутри основного домена).
            try:
                account = await _collect_account_sites(page)
            except Exception as e:
                if 'НЕ АВТОРИЗОВАН' in str(e):
                    return {'available': False, 'source': 'yandex_export',
                            'error': str(e), 'hosts': []}
                account = []
            sites = _resolve_sites(account, pid)
            _log(f'Сайтов в аккаунте: {len(account)}, из них по проекту '
                 f'{pid}: {len(sites)}')
            if not sites:
                # Fallback: список аккаунта не получили - берём хосты из каталога
                # (как раньше), site_id собираем стандартно https:<host>:443.
                _log('⚠ Список сайтов аккаунта пуст - fallback на каталог проекта')
                sites = [(f'https:{h}:443', h) for h in _hosts(pid)]
            if max_hosts:
                sites = sites[:max_hosts]
            if not sites:
                return {'available': False, 'source': 'yandex_export',
                        'error': 'нет сайтов для проверки', 'hosts': []}

            for site_id, host in sites:
                try:
                    fname, data = await _download_one(page, site_id, host, scout)
                    if data:
                        files.append((fname, data))
                except Exception as e:
                    _log(f'  ⚠ {host}: {e}')
                    if 'НЕ АВТОРИЗОВАН' in str(e):
                        return {'available': False, 'source': 'yandex_export',
                                'error': str(e), 'hosts': []}
                await page.wait_for_timeout(1200)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    if scout:
        return {'available': False, 'source': 'yandex_export',
                'error': 'scout-режим (только дамп кнопок)', 'hosts': []}
    if not files:
        return {'available': False, 'source': 'yandex_export',
                'error': 'ни одной выгрузки не скачано (см. лог/кнопки выше)',
                'hosts': []}
    return analyze_exports(files, log=lambda lvl, m: _log(m))


def main():
    ap = argparse.ArgumentParser(
        description='Авто-скачивание «Страницы в поиске» из Вебмастера → 404')
    ap.add_argument('--project', required=True)
    ap.add_argument('--max-hosts', type=int, default=None)
    ap.add_argument('--scout', action='store_true',
                    help='только показать кнопки скачивания, не качать')
    a = ap.parse_args()

    res = asyncio.run(_run(a.project, a.max_hosts, a.scout))
    (ROOT / 'cache').mkdir(exist_ok=True)
    out = ROOT / 'cache' / f'index404_{a.project}.json'
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding='utf-8')
    _log(f'Результат: available={res.get("available")}, '
         f'битых 404/410={res.get("total_dead", 0)} → {out.name}')
    if res.get('error'):
        _log(f'Заметка: {res["error"]}')


if __name__ == '__main__':
    main()
