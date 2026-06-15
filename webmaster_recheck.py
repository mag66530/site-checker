"""
webmaster_recheck.py
====================
Автокликер «Проверить» по ошибкам диагностики в Яндекс.Вебмастере.

Алгоритм:
    1. webmaster.yandex.ru/sites/ → список всех сайтов
    2. для каждого сайта: Сводка → блок «Диагностика. Проблемы сайта» →
       клик «N ошибок» (a/div.DiagnosticProblem) → страница checklist
    3. для каждой ошибки со статусом НЕ «Проверяем сайт на ошибку»:
       раскрыть аккордеон → кнопка «Проверьте» (a.link_theme_normal) → клик
    4. пропустить если «Проверяем» или кнопки нет → следующий сайт

Подготовка (один Chrome на всё):
    1. python gsc_save_session.py        # держит Chrome на порту 9222
    2. в этом Chrome открой webmaster.yandex.ru и войди в Яндекс (один раз)

Запуск:
    python webmaster_recheck.py --dry-run            # разведка: не кликает, всё логирует
    python webmaster_recheck.py --site "https://webmaster.yandex.ru/site/https:vladimir.mepen.ru:443/"
    python webmaster_recheck.py                       # все сайты, боевой
    python webmaster_recheck.py --limit 1             # только первый сайт
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

CDP_URL = 'http://127.0.0.1:9222'
SITES_URL = 'https://webmaster.yandex.ru/sites/'
LOG_FILE = Path('webmaster_recheck_log.json')

# Селекторы (реальные классы Я.Вебмастера, из DevTools)
SEL_PROBLEM = '.DiagnosisChecklistProblem'
SEL_STATUS_INPROGRESS = '.DiagnosisChecklistProblemTitle-Status_status_IN_PROGRESS'
SEL_CHEVRON = '.DiagnosisChecklistProblem-Chevron'
SEL_LINKS = '.DiagnosisChecklistProblemLandingLinksContainer a.link_theme_normal'
TXT_CHECKING = 'Проверяем сайт на ошибку'
TXT_CHECK_BTN = ('Проверьте', 'Проверить')


def _log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    pfx = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ '}.get(level, '  ')
    print(f'[{ts}] {pfx}{msg}')


def _save_log(entries):
    LOG_FILE.write_text(
        json.dumps({'run_at': datetime.now().isoformat(), 'entries': entries},
                   ensure_ascii=False, indent=2), encoding='utf-8')


async def _collect_sites(page) -> list[str]:
    await page.goto(SITES_URL, wait_until='domcontentloaded')
    await page.wait_for_timeout(4000)
    if 'passport.yandex' in page.url:
        _log('Не авторизован в Яндексе. Войди в webmaster.yandex.ru '
             'в открытом Chrome и повтори.', 'error')
        return []
    sites = []
    for a in await page.query_selector_all('a[href*="/site/"]'):
        href = await a.get_attribute('href') or ''
        if '/site/' not in href:
            continue
        idx = href.find('/site/')
        tail = href[idx + len('/site/'):]
        site_id = tail.split('/')[0]
        # Реальный id сайта — это закодированный хост (есть точка/двоеточие):
        # https:vladimir.mepen.ru:443. Навигационные ссылки вида /site/dashboard/
        # такого не содержат — отсеиваем.
        if '.' not in site_id and ':' not in site_id:
            continue
        root = f'https://webmaster.yandex.ru/site/{site_id}/'
        if root not in sites:
            sites.append(root)
    return sites


async def _open_checklist(page, site_root: str) -> bool:
    """Открыть страницу ошибок диагностики сайта."""
    # Идём на сводку, ищем ссылку «N ошибок» в блоке диагностики
    await page.goto(site_root, wait_until='domcontentloaded')
    await page.wait_for_timeout(3500)

    # Прямая попытка: ссылка-проблема диагностики
    prob = page.locator(f'a{SEL_PROBLEM}, {SEL_PROBLEM} a, a:has-text("ошиб")').first
    try:
        if await prob.count() > 0:
            await prob.click(timeout=5000)
            await page.wait_for_timeout(3500)
            return True
    except Exception:
        pass

    # Fallback: прямой переход на checklist
    for path in ('optimization/checklist/', 'diagnostics/', 'health/'):
        try:
            await page.goto(site_root + path, wait_until='domcontentloaded')
            await page.wait_for_timeout(3000)
            if '/checklist' in page.url or 'diagnostic' in page.url or 'health' in page.url:
                return True
        except Exception:
            continue
    return True  # всё равно попробуем разобрать что есть


async def _process_problems(page, dry_run: bool) -> dict:
    stat = {'problems': 0, 'checking': 0, 'no_button': 0, 'clicked': 0, 'errors': 0}

    problems = await page.query_selector_all(SEL_PROBLEM)
    stat['problems'] = len(problems)
    _log(f'  URL страницы: {page.url}')
    _log(f'  блоков .DiagnosticProblem: {len(problems)}')

    # Если блоков нет — разведка: показываем классы с «Diagnostic»/«Problem»
    # и ссылки/элементы со словом «ошиб», чтобы поймать реальные селекторы.
    if not problems:
        _log('  блоков нет — дамп кандидатов:')
        try:
            classes = await page.eval_on_selector_all(
                '*',
                """els => {
                    const s = new Set();
                    for (const e of els) {
                        const c = (e.className && e.className.baseVal !== undefined)
                            ? e.className.baseVal : e.className;
                        if (typeof c === 'string') {
                            for (const cl of c.split(/\\s+/)) {
                                if (/Diagnostic|Problem|checklist|Health/i.test(cl)) s.add(cl);
                            }
                        }
                    }
                    return [...s].slice(0, 40);
                }""")
            _log(f'    классы с Diagnostic/Problem: {classes}')
        except Exception as e:
            _log(f'    дамп классов не удался: {e}')
        for el in (await page.query_selector_all('a, button'))[:60]:
            try:
                t = (await el.inner_text()).strip().replace('\n', ' ')
                if 'ошиб' in t.lower() and await el.is_visible():
                    _log(f'    ссылка/кнопка с «ошиб»: "{t[:60]}"')
            except Exception:
                pass

    for i, prob in enumerate(problems):
        try:
            txt = (await prob.inner_text()).strip().replace('\n', ' ')
            _log(f'  [{i}] {txt[:80]}')

            # уже проверяется? (по классу статуса или по тексту)
            in_progress = await prob.query_selector(SEL_STATUS_INPROGRESS)
            if in_progress or TXT_CHECKING in txt:
                stat['checking'] += 1
                _log('      статус «Проверяем» — пропуск', 'warn')
                continue

            # раскрыть аккордеон кликом по шеврону, чтобы показались ссылки
            chevron = await prob.query_selector(SEL_CHEVRON)
            if chevron:
                try:
                    await chevron.click(timeout=3000)
                    await page.wait_for_timeout(900)
                except Exception:
                    pass

            # ищем ссылку-кнопку «Проверьте» в контейнере landing-ссылок
            btn = None
            for a in await prob.query_selector_all(SEL_LINKS):
                bt = (await a.inner_text()).strip()
                if any(k.lower() in bt.lower() for k in TXT_CHECK_BTN):
                    btn = a
                    break
            if btn is None:
                stat['no_button'] += 1
                _log('      кнопки «Проверьте» нет', 'warn')
                continue

            if dry_run:
                bt = (await btn.inner_text()).strip()
                _log(f'      [DRY RUN] кнопка есть: «{bt[:30]}», не кликаю', 'ok')
                stat['clicked'] += 1
                continue

            await btn.click()
            await page.wait_for_timeout(1500)
            stat['clicked'] += 1
            _log('      ✓ клик «Проверьте»', 'ok')

        except Exception as e:
            stat['errors'] += 1
            _log(f'      ошибка: {e}', 'error')

    return stat


async def run(single_site: str | None, dry_run: bool, limit: int):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('pip install playwright')
        sys.exit(1)

    entries = []
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            _log(f'Нет подключения к Chrome ({CDP_URL}): {e}', 'error')
            _log('Сначала запусти gsc_save_session.py.')
            return

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if single_site:
            sites = [single_site]
        else:
            sites = await _collect_sites(page)
            _log(f'Сайтов в Вебмастере: {len(sites)}')
        if not sites:
            await browser.close()
            return

        if limit:
            sites = sites[:limit]

        for site in sites:
            _log(f'\n── Сайт: {site} ──')
            try:
                await _open_checklist(page, site)
                stat = await _process_problems(page, dry_run)
                entries.append({'site': site, **stat})
            except Exception as e:
                _log(f'  сайт упал: {e}', 'error')
                entries.append({'site': site, 'error': str(e)})
            _save_log(entries)

        await browser.close()

    tot_click = sum(e.get('clicked', 0) for e in entries)
    tot_check = sum(e.get('checking', 0) for e in entries)
    tot_nob = sum(e.get('no_button', 0) for e in entries)
    _log(f'\n══ Готово: кликов {tot_click}, уже проверяются {tot_check}, '
         f'без кнопки {tot_nob} ══')
    _log(f'Лог → {LOG_FILE.resolve()}')


def parse_args():
    ap = argparse.ArgumentParser(description='Автокликер «Проверить» в Я.Вебмастере')
    ap.add_argument('--site', default=None, help='один сайт (URL корня /site/<id>/)')
    ap.add_argument('--dry-run', action='store_true', help='не кликать, только лог')
    ap.add_argument('--limit', type=int, default=0, help='максимум сайтов')
    return ap.parse_args()


if __name__ == '__main__':
    a = parse_args()
    _log('Старт' + ('  [DRY RUN]' if a.dry_run else ''))
    asyncio.run(run(a.site, a.dry_run, a.limit))
