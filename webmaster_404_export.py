"""
webmaster_404_export.py
========================
«Проверка страниц в индексе на 404-ошибку»: скачивает в Яндекс.Вебмастере
CSV «Страницы в поиске → Последние изменения» по каждому сайту проекта и
перепроверяет вживую те URL, которые Яндекс НЕДАВНО убрал из поиска из-за
ошибки. Находка - только если страница ДЕЙСТВИТЕЛЬНО до сих пор не отвечает
нормально (сверка с реальностью нашим собственным запросом, а не просто то,
что помнит Яндекс - см. _ERROR_STATUSES и recheck_verdict()).

Разбор CSV подтверждён на реальной выгрузке mepen.ru (30126 строк): среди
статусов Яндекса ('SEARCHABLE', 'LOW_DEMAND', 'HTTP_ERROR', 'DUPLICATE',
'UNKNOWN_URL', 'CLEAN_PARAMS', 'ROBOTS_TXT_ERROR', 'NOT_CANONICAL',
'REDIRECT_NOTSEARCHABLE') похожи на «страница пропала из-за ошибки» ровно
два - HTTP_ERROR (500/504) и UNKNOWN_URL (страница вообще не ответила) - и
ОБА встречаются исключительно с event=DELETE (страницу только что убрали из
поиска, не «её никогда не было»). Остальные статусы - другие темы
(ROBOTS_TXT_ERROR уже покрыт отдельным пунктом 1.7, REDIRECT_NOTSEARCHABLE -
это редирект, не 404, и т.д.) - не считаем находками здесь.

Подготовка (тот же Chrome, что и для webmaster_recheck.py/gsc_*.py):
    1. python gsc_save_session.py        # держит Chrome на порту 9222
    2. в этом Chrome войди в webmaster.yandex.ru (один раз)

Запуск:
    python webmaster_404_export.py --dry-run --project mpe   # разведка: до скачивания
    python webmaster_404_export.py --project mpe --limit 1   # один сайт проекта, боевой
    python webmaster_404_export.py --site "https://webmaster.yandex.ru/site/https:mepen.ru:443/"

ВАЖНО: кнопка «Скачать таблицу» рисуется JS-ом (не статичный href) - если
селектор по тексту не совпадёт, скрипт САМ допечатает в лог видимые кнопки/
ссылки страницы (тот же приём разведки, что и в webmaster_recheck.py) -
пришли этот кусок лога, чтобы поправить селектор одним заходом.
"""
import argparse
import asyncio
import csv
import io
import json
import random
import sys
from datetime import datetime
from pathlib import Path

CDP_URL = 'http://127.0.0.1:9222'
LOG_FILE = Path('webmaster_404_log.json')
DOWNLOAD_DIR = Path('cache/webmaster_404')

# Статусы Яндекса, похожие на «страница пропала из-за ошибки» - см. докстринг
# модуля про то, почему именно эти два и почему остальные - не сюда.
_ERROR_STATUSES = ('HTTP_ERROR', 'UNKNOWN_URL')

TXT_DOWNLOAD_BTN = 'Скачать таблицу'
TXT_CSV = 'CSV'


def _log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    pfx = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ '}.get(level, '  ')
    print(f'[{ts}] {pfx}{msg}')


def _save_log(entries):
    LOG_FILE.write_text(
        json.dumps({'run_at': datetime.now().isoformat(), 'entries': entries},
                   ensure_ascii=False, indent=2), encoding='utf-8')


def _sites_from_project(pid: str) -> list:
    """Сайты Вебмастера из списка поддоменов проекта (тот же приём, что и в
    webmaster_recheck.py/gsc_validate_fixes.py). Site-id Вебмастера =
    https:<host>:443 → корень /site/<id>/."""
    csv_path = Path(__file__).parent / 'catalogs' / f'{pid}-subdomains.csv'
    if not csv_path.exists():
        _log(f'Нет файла поддоменов: {csv_path}', 'error')
        return []
    sites = []
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            u = (row.get('url') or '').strip()
            if not u.startswith('http'):
                continue
            host = u.split('//', 1)[-1].strip('/').split('/')[0]
            site_id = f'https:{host}:443'
            root = f'https://webmaster.yandex.ru/site/{site_id}/'
            if root not in sites:
                sites.append(root)
    return sites


# ── Чистые функции (разбор CSV, вердикт по живому статусу) ──────────────
# Юнит-тестируются без сети/браузера - см. tests/test_webmaster_404_export.py.

def parse_indexing_csv(raw: str) -> list:
    """Разбирает CSV «Страницы в поиске → Последние изменения» (реальные
    колонки Яндекса: updateDate,url,httpCode,status,target,lastAccess,
    title,event) и возвращает только строки со статусом из
    _ERROR_STATUSES - кандидатов на «страница сломалась». ЧИСТАЯ функция."""
    out = []
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        if (row.get('status') or '').strip() in _ERROR_STATUSES:
            out.append({
                'url': (row.get('url') or '').strip(),
                'status': row.get('status'),
                'yandex_http_code': row.get('httpCode'),
                'last_access': row.get('lastAccess'),
                'title': row.get('title'),
            })
    return out


def dedup_by_url(candidates: list) -> list:
    """Один URL может встретиться несколько раз в истории за 3 месяца -
    оставляем одну запись на URL (Яндекс отдаёт от новых событий к старым,
    поэтому первая встреченная запись - самая свежая, её и оставляем).
    ЧИСТАЯ функция."""
    seen = {}
    for c in candidates:
        seen.setdefault(c['url'], c)
    return list(seen.values())


def recheck_verdict(status_code) -> str:
    """Вердикт по РЕАЛЬНОМУ текущему статусу страницы (наш собственный
    запрос сейчас, а не то, что помнит Яндекс за прошлый визит). ЧИСТАЯ
    функция. «подтверждено: …» - строки, с которых начинается настоящая
    находка (см. run() - фильтр confirmed)."""
    if status_code is None:
        return 'не удалось проверить'
    if status_code == 200:
        return 'уже не проблема (200)'
    if status_code in (404, 410):
        return 'подтверждено: страница не существует'
    if 500 <= status_code < 600:
        return 'подтверждено: ошибка сервера'
    return f'подтверждено: код {status_code}'


def recheck_candidates(candidates: list, proxy_url=None, log=None) -> list:
    """Синхронный live-прозвон кандидатов - тот же простой requests, что уже
    используется в metrika_api.py/webmaster_api.py (не тянем сюда отдельный
    aiohttp ради десятков URL за сайт)."""
    import requests
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    out = []
    for c in candidates:
        status = None
        try:
            r = requests.head(c['url'], timeout=15, allow_redirects=False, proxies=proxies)
            status = r.status_code
            if status in (405, 501):     # HEAD не поддержан - пробуем GET
                r = requests.get(c['url'], timeout=15, allow_redirects=False, proxies=proxies)
                status = r.status_code
        except Exception as e:
            if log:
                log(f'    {c["url"]}: сеть - {e}', 'warn')
        out.append({**c, 'live_status': status, 'verdict': recheck_verdict(status)})
    return out


# ── Браузерная часть (Playwright) - вслепую написана по докстрингу выше,
# первый прогон почти наверняка потребует одной правки селектора ──────────

async def _goto_backoff(page, url: str, tries: int = 6) -> bool:
    """Переход с защитой от 429 (тот же приём, что в webmaster_recheck.py)."""
    delay = 20
    for i in range(tries):
        try:
            resp = await page.goto(url, wait_until='domcontentloaded')
        except Exception as e:
            _log(f'goto упал ({e}) - пауза {delay}с', 'warn')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        await page.wait_for_timeout(2000)
        status = resp.status if resp else 0
        head = (await page.inner_text('body'))[:300]
        if status == 429 or 'Too many requests' in head or 'Слишком много запросов' in head:
            _log(f'⚠ 429 - пауза {delay}с (попытка {i+1})', 'warn')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        return True
    _log('429 не прошёл после ретраев - пропускаю сайт', 'error')
    return False


async def _select_site_and_open_searchable(page, site_root: str) -> bool:
    """Открыть «Страницы в поиске» конкретного сайта. Основной путь - URL с
    site_id в пути (как у /optimization/checklist/ в webmaster_recheck.py);
    если Вебмастер всё равно показал общий экран без выбранного сайта -
    запасной путь через выпадающий список «Выбрать сайт»."""
    target = site_root + 'indexing/searchable/'
    if not await _goto_backoff(page, target):
        return False
    await page.wait_for_timeout(2000)
    if '/site/https' not in page.url and '/site/http' not in page.url:
        host = ''
        if 'https:' in site_root:
            host = site_root.split('/site/https:', 1)[-1].split(':443')[0]
        _log(f'  сайт не выбрался по прямому URL (сейчас: {page.url}) - '
             f'пробую выпадающий список (хост «{host}»)', 'warn')
        try:
            await page.get_by_text('Выбрать сайт', exact=False).click(timeout=5000)
            await page.wait_for_timeout(800)
            await page.get_by_text(host, exact=False).first.click(timeout=5000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            _log(f'  не удалось выбрать сайт через список: {e}', 'error')
            return False
    return True


async def _dump_visible_controls(page, limit=40):
    """Разведка при сбое: какие кнопки/ссылки реально видны на странице -
    чтобы починить селектор по логу одним заходом, не гадая вслепую."""
    try:
        texts = []
        for el in (await page.query_selector_all('button, a, [role=button]'))[:120]:
            if not await el.is_visible():
                continue
            t = (await el.inner_text()).strip().replace('\n', ' ')
            if t:
                texts.append(t[:40])
        _log(f'  видимые кнопки/ссылки на странице: {texts[:limit]}', 'warn')
    except Exception as e:
        _log(f'  дамп кнопок не удался: {e}', 'warn')


async def _download_searchable_csv(page, dest_dir: Path):
    """Жмёт «Скачать таблицу» → CSV, дожидается файла. Возвращает Path к
    скачанному файлу или None."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with page.expect_download(timeout=30000) as dl_info:
            await page.get_by_text(TXT_DOWNLOAD_BTN, exact=False).click(timeout=8000)
            await page.wait_for_timeout(500)
            await page.get_by_text(TXT_CSV, exact=True).click(timeout=8000)
        download = await dl_info.value
        dest = dest_dir / download.suggested_filename
        await download.save_as(str(dest))
        return dest
    except Exception as e:
        _log(f'  скачать CSV не удалось: {e}', 'error')
        await _dump_visible_controls(page)
        return None


async def run(single_site, dry_run, limit, project, proxy_url=None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('pip install playwright')
        sys.exit(1)

    entries = []
    async with async_playwright() as p:
        try:
            from autoclick_browser import open_browser
            browser, page = await open_browser(p, lambda m: _log(m, 'info'))
        except Exception as e:
            _log(f'Браузер не открылся: {e}', 'error')
            _log('Локально: запусти gsc_save_session.py (Chrome на 9222), '
                 'в нём войди в webmaster.yandex.ru.')
            return

        if single_site:
            sites = [single_site]
        elif project:
            sites = _sites_from_project(project)
            _log(f'Сайтов из списка проекта {project}: {len(sites)}')
        else:
            _log('Укажи --project или --site', 'error')
            await browser.close()
            return
        if not sites:
            await browser.close()
            return
        if limit:
            sites = sites[:limit]

        for si, site in enumerate(sites):
            _log(f'\n── Сайт: {site} ──')
            entry = {'site': site}
            try:
                if not await _select_site_and_open_searchable(page, site):
                    entry['error'] = 'не удалось открыть «Страницы в поиске»'
                    await _dump_visible_controls(page)
                    entries.append(entry)
                    continue

                if dry_run:
                    _log('  [DRY RUN] страница открылась, дальше не иду '
                         '(скачивание/проверка пропущены)', 'ok')
                    entry['dry_run'] = True
                    entries.append(entry)
                    continue

                sub_dir = DOWNLOAD_DIR / site.rstrip('/').split('/')[-1]
                csv_path = await _download_searchable_csv(page, sub_dir)
                if not csv_path:
                    entry['error'] = 'не удалось скачать CSV'
                    entries.append(entry)
                    continue

                raw = csv_path.read_text(encoding='utf-8')
                candidates = dedup_by_url(parse_indexing_csv(raw))
                _log(f'  кандидатов (Яндекс убрал из-за ошибки): {len(candidates)}')

                checked = recheck_candidates(candidates, proxy_url, log=_log)
                confirmed = [c for c in checked if c['verdict'].startswith('подтверждено')]
                entry.update({
                    'checked_file': str(csv_path),
                    'candidates': len(candidates),
                    'confirmed': len(confirmed),
                    'confirmed_urls': confirmed,
                })
                _log(f'  подтверждено вживую: {len(confirmed)} из {len(candidates)}', 'ok')
                for c in confirmed[:15]:
                    _log(f'    {c["url"]} - {c["verdict"]}', 'warn')
            except Exception as e:
                entry['error'] = str(e)
                _log(f'  сайт упал: {e}', 'error')
            entries.append(entry)
            _save_log(entries)
            if si < len(sites) - 1:
                await asyncio.sleep(4 + random.random() * 4)

        await browser.close()

    tot_conf = sum(e.get('confirmed', 0) for e in entries)
    tot_cand = sum(e.get('candidates', 0) for e in entries)
    _log(f'\n══ Готово: подтверждено {tot_conf} из {tot_cand} кандидатов ══')
    _log(f'Лог → {LOG_FILE.resolve()}')


def parse_args():
    ap = argparse.ArgumentParser(
        description='404 по данным Яндекс.Вебмастера (Страницы в поиске)')
    ap.add_argument('--project', default=None,
                    help='проект: smu|mpe|imp|avia|metpromko - сайты из '
                         'catalogs/<pid>-subdomains.csv')
    ap.add_argument('--site', default=None, help='один сайт (URL корня /site/<id>/)')
    ap.add_argument('--dry-run', action='store_true',
                    help='только открыть страницу, не скачивать и не звонить URL')
    ap.add_argument('--limit', type=int, default=0, help='максимум сайтов')
    ap.add_argument('--proxy', default=None, help='прокси для live-прозвона (если нужен)')
    return ap.parse_args()


if __name__ == '__main__':
    a = parse_args()
    _log('Старт' + ('  [DRY RUN]' if a.dry_run else ''))
    asyncio.run(run(a.site, a.dry_run, a.limit, a.project, a.proxy))
