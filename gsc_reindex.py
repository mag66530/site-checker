"""
gsc_reindex.py
==============
Шаг 2: Автоматически запросить повторную индексацию для страниц с ошибками.

Запуск:
    python gsc_reindex.py                    # все свойства
    python gsc_reindex.py --property example.com  # только одно
    python gsc_reindex.py --dry-run          # проверка без кликов
    python gsc_reindex.py --limit 20         # не более 20 URL за запуск
    python gsc_reindex.py --headless         # без видимого браузера

Требования:
    - gsc_session.json (сгенерировать через gsc_save_session.py)
    - pip install playwright
    - playwright install chromium

Логика:
    1. Загружает сохранённую сессию Google
    2. Открывает GSC, собирает список всех свойств
    3. Для каждого свойства: переходит в Индексирование → Страницы → Ошибки
    4. Собирает список URL с ошибками
    5. Для каждого URL открывает инструмент проверки и нажимает
       "Запросить повторную индексацию"
    6. Сохраняет результат в gsc_reindex_log.json

Ограничения Google:
    - URL Inspection: ~10 запросов в день на свойство через API
      (через UI лимит мягче, но при злоупотреблении — капча)
    - Между запросами пауза 5 секунд (настраивается через DELAY_BETWEEN_URLS_SEC)
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

SESSION_FILE = Path('gsc_session.json')
LOG_FILE = Path('gsc_reindex_log.json')

GSC_HOME = 'https://search.google.com/search-console/'
GSC_COVERAGE = 'https://search.google.com/search-console/index?resource_id={resource_id}'
GSC_INSPECT = 'https://search.google.com/search-console/inspect?resource_id={resource_id}&id={url}'

# Пауза между запросами (секунды)
DELAY_BETWEEN_URLS_SEC = 6
DELAY_BETWEEN_PROPERTIES_SEC = 3

# Таймаут ожидания элементов (мс)
ELEMENT_TIMEOUT = 15_000


# ── Утилиты ────────────────────────────────────────────────────────


def _find_chrome():
    import os
    candidates = [
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
        / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
        / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', ''))
        / 'Google/Chrome/Application/chrome.exe',
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path('/usr/bin/google-chrome-stable'),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _log(msg: str, level: str = 'info'):
    ts = datetime.now().strftime('%H:%M:%S')
    prefix = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ '}.get(level, '  ')
    print(f'[{ts}] {prefix}{msg}')


def _save_log(log: list):
    LOG_FILE.write_text(
        json.dumps({'run_at': datetime.now().isoformat(), 'entries': log},
                   ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


# ── Получение свойств GSC ──────────────────────────────────────────


async def get_properties(page) -> list[dict]:
    """
    Открывает главную GSC и собирает все свойства.
    Возвращает [{'name': ..., 'resource_id': ...}, ...]
    """
    _log('Открываю GSC...')
    await page.goto(GSC_HOME, wait_until='domcontentloaded')
    await page.wait_for_timeout(3000)

    # Проверяем редирект на логин
    if 'accounts.google.com' in page.url:
        _log('Сессия истекла — запусти gsc_save_session.py заново', 'error')
        sys.exit(1)

    properties = []

    # GSC показывает свойства в dropdown или на главной странице
    # Пробуем несколько способов найти список свойств

    # Способ 1: кнопка выбора свойства (dropdown в шапке)
    try:
        # Открываем dropdown свойств
        selector_btn = 'button[data-id="sc-search-console-nav-back-button"], ' \
                       '[data-sc-data-prop-selector], ' \
                       'div[role="button"]:has-text("Search property")'
        # Ищем элемент с текстом о переходе к свойствам
        prop_links = await page.query_selector_all('a[href*="resource_id="]')
        for link in prop_links:
            href = await link.get_attribute('href')
            if not href:
                continue
            # resource_id закодирован в URL
            if 'resource_id=' in href:
                rid = href.split('resource_id=')[1].split('&')[0]
                from urllib.parse import unquote
                rid_decoded = unquote(rid)
                name = await link.inner_text()
                name = name.strip()
                if rid_decoded and rid_decoded not in [p['resource_id'] for p in properties]:
                    properties.append({'name': name or rid_decoded, 'resource_id': rid_decoded})
    except Exception as e:
        _log(f'Способ 1 не сработал: {e}', 'warn')

    # Способ 2: на главной странице GSC список свойств — карточки
    if not properties:
        try:
            await page.wait_for_selector('[data-property-url], .SC-property-item, '
                                         'div[jsname] a[href*="resource_id"]',
                                         timeout=5000)
            cards = await page.query_selector_all('a[href*="resource_id="]')
            seen = set()
            for c in cards:
                href = await c.get_attribute('href')
                if not href or 'resource_id=' not in href:
                    continue
                from urllib.parse import unquote
                rid = unquote(href.split('resource_id=')[1].split('&')[0])
                if rid in seen:
                    continue
                seen.add(rid)
                txt = (await c.inner_text()).strip()
                properties.append({'name': txt or rid, 'resource_id': rid})
        except Exception as e:
            _log(f'Способ 2 не сработал: {e}', 'warn')

    # Способ 3: открываем property selector через меню
    if not properties:
        try:
            _log('Пробую открыть dropdown свойств...', 'warn')
            # Ищем кнопку текущего свойства в шапке
            await page.click('text=Search property', timeout=4000)
            await page.wait_for_timeout(1000)
            prop_items = await page.query_selector_all('[role="option"], [role="menuitem"]')
            for item in prop_items:
                txt = (await item.inner_text()).strip()
                if txt:
                    # Пытаемся получить resource_id из data-атрибута
                    rid = await item.get_attribute('data-value') or ''
                    if not rid:
                        rid = txt  # использовать имя как id
                    properties.append({'name': txt, 'resource_id': rid})
        except Exception as e:
            _log(f'Способ 3 не сработал: {e}', 'warn')

    if not properties:
        _log('Не удалось автоматически найти свойства GSC.', 'error')
        _log('Возможно GSC обновил интерфейс. Открываю браузер — скопируй resource_id вручную.')

    _log(f'Найдено свойств: {len(properties)}')
    for p in properties:
        _log(f'  • {p["name"]}  [{p["resource_id"]}]')
    return properties


# ── Получение ошибочных URL для свойства ──────────────────────────


async def get_error_urls(page, resource_id: str) -> list[str]:
    """
    Переходит в отчёт Индексирование→Страницы, фильтрует ошибки.
    Возвращает список URL для повторной индексации.
    """
    coverage_url = GSC_COVERAGE.format(resource_id=quote(resource_id, safe=''))
    _log(f'Открываю отчёт по ресурсу: {resource_id}')
    await page.goto(coverage_url, wait_until='domcontentloaded')
    await page.wait_for_timeout(4000)

    error_urls: list[str] = []

    try:
        # Ищем вкладку/фильтр "Ошибки" (Error / Ошибка)
        # GSC показывает несколько статусов: Ошибка / Предупреждение / Исключено / Действующие
        error_tab = None
        for text in ['Ошибка', 'Error', 'Errors']:
            try:
                error_tab = await page.wait_for_selector(
                    f'text="{text}"', timeout=3000)
                if error_tab:
                    break
            except Exception:
                pass

        if error_tab:
            await error_tab.click()
            await page.wait_for_timeout(2000)

        # Собираем URL из таблицы ошибок
        # Таблица содержит строки с данными — ищем ссылки или текст URL
        rows = await page.query_selector_all(
            'table tr[data-row], '
            '.SC-coverage-table tr, '
            '[role="row"]:not([role="columnheader"])'
        )

        for row in rows:
            # Пробуем найти URL в тексте ячейки
            cells = await row.query_selector_all('td, [role="cell"]')
            for cell in cells:
                txt = (await cell.inner_text()).strip()
                if txt.startswith('http://') or txt.startswith('https://'):
                    if txt not in error_urls:
                        error_urls.append(txt)
                    break

        # Если таблица пустая — проверяем через количество ошибок
        if not error_urls:
            # Попробуем найти ссылки внутри таблицы
            links = await page.query_selector_all(
                'a[href*="inspect"], a[href*="/search-console/"]')
            for link in links:
                href = await link.get_attribute('href') or ''
                txt = (await link.inner_text()).strip()
                if txt.startswith('http://') or txt.startswith('https://'):
                    if txt not in error_urls:
                        error_urls.append(txt)

    except Exception as e:
        _log(f'Ошибка при получении URL для {resource_id}: {e}', 'warn')

    _log(f'Найдено URL с ошибками: {len(error_urls)}')
    return error_urls


# ── Запрос повторной индексации для одного URL ─────────────────────


async def request_reindex(page, resource_id: str, url: str, dry_run: bool) -> dict:
    """
    Открывает URL Inspection для url, нажимает "Запросить повторную индексацию".
    Возвращает {'url': ..., 'status': 'ok'|'error'|'skipped', 'message': ...}
    """
    result = {'url': url, 'resource_id': resource_id, 'status': 'error', 'message': ''}

    try:
        inspect_url = GSC_INSPECT.format(
            resource_id=quote(resource_id, safe=''),
            url=quote(url, safe=''),
        )
        _log(f'  Проверка: {url}')
        await page.goto(inspect_url, wait_until='domcontentloaded')
        await page.wait_for_timeout(4000)

        # Ждём загрузки инструмента проверки
        # Страница содержит статус индексации и кнопку повторного запроса
        try:
            await page.wait_for_selector(
                'text="Запросить повторную индексацию", '
                'text="Request indexing", '
                '[data-sc-request-indexing]',
                timeout=ELEMENT_TIMEOUT,
            )
        except Exception:
            # Кнопки может не быть (URL уже проиндексирован или недоступен)
            page_text = await page.inner_text('body')
            if 'URL находится в Google' in page_text or 'URL is on Google' in page_text:
                result['status'] = 'skipped'
                result['message'] = 'Уже в индексе'
                _log(f'    Уже в индексе — пропускаем', 'ok')
                return result
            result['status'] = 'skipped'
            result['message'] = 'Кнопка повторной индексации не найдена'
            _log(f'    Кнопка не найдена (возможно URL недоступен)', 'warn')
            return result

        # Находим и кликаем кнопку
        btn = None
        for btn_text in ['Запросить повторную индексацию', 'Request indexing']:
            try:
                btn = page.get_by_text(btn_text, exact=False).first
                if await btn.is_visible():
                    break
                btn = None
            except Exception:
                btn = None

        if btn is None:
            result['status'] = 'skipped'
            result['message'] = 'Кнопка не видна'
            return result

        if dry_run:
            result['status'] = 'dry_run'
            result['message'] = 'dry-run — клик пропущен'
            _log(f'    [DRY RUN] Нашёл кнопку, кликать не стал', 'ok')
            return result

        await btn.click()
        _log(f'    Клик по кнопке...')

        # Ждём результата — Google показывает диалог или сообщение об успехе
        success = False
        for wait_text in [
            'Запрос принят',
            'Indexing requested',
            'done',
            'успешно',
        ]:
            try:
                await page.wait_for_selector(
                    f'text="{wait_text}"', timeout=12_000)
                success = True
                break
            except Exception:
                pass

        # Также ищем по общим признакам успешного диалога
        if not success:
            try:
                # Ищем любой диалог/модал после клика
                await page.wait_for_selector(
                    '[role="dialog"], .VfPpkd-xl07Ob-XxIAqe',
                    timeout=8000,
                )
                dialog_text = ''
                dialog = await page.query_selector('[role="dialog"]')
                if dialog:
                    dialog_text = await dialog.inner_text()
                if dialog_text and ('ошибк' not in dialog_text.lower() and
                                    'error' not in dialog_text.lower()):
                    success = True
                    # Закрываем диалог если есть кнопка "OK" / "Готово"
                    for close_text in ['OK', 'Готово', 'Done', 'Закрыть']:
                        try:
                            close_btn = page.get_by_role('button', name=close_text)
                            if await close_btn.is_visible(timeout=2000):
                                await close_btn.click()
                                break
                        except Exception:
                            pass
            except Exception:
                pass

        if success:
            result['status'] = 'ok'
            result['message'] = 'Запрос отправлен'
            _log(f'    ✓ Запрос принят', 'ok')
        else:
            result['status'] = 'warn'
            result['message'] = 'Клик выполнен, но подтверждение не получено'
            _log(f'    Клик выполнен — подтверждение не найдено', 'warn')

    except Exception as e:
        result['status'] = 'error'
        result['message'] = str(e)
        _log(f'    Ошибка: {e}', 'error')

    return result


# ── Основной цикл ──────────────────────────────────────────────────


async def run(
    property_filter: Optional[str] = None,
    dry_run: bool = False,
    limit: int = 0,
    headless: bool = False,
):
    if not SESSION_FILE.exists():
        _log(f'{SESSION_FILE} не найден — запусти gsc_save_session.py', 'error')
        sys.exit(1)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('pip install playwright && playwright install chromium')
        sys.exit(1)

    _log(f'Запуск{"  [DRY RUN]" if dry_run else ""}')
    all_results = []
    total_requested = 0

    async with async_playwright() as p:
        chrome_path = _find_chrome()
        browser = await p.chromium.launch(
            headless=headless,
            executable_path=str(chrome_path) if chrome_path else None,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-first-run',
                '--disable-infobars',
            ],
            ignore_default_args=['--enable-automation'],
        )
        context = await browser.new_context(
            storage_state=str(SESSION_FILE),
            locale='ru-RU',
            timezone_id='Europe/Moscow',
            viewport={'width': 1440, 'height': 900},
        )
        page = await context.new_page()

        # Собираем свойства
        properties = await get_properties(page)

        if property_filter:
            properties = [
                p for p in properties
                if property_filter.lower() in p['resource_id'].lower()
                or property_filter.lower() in p['name'].lower()
            ]
            _log(f'После фильтра --property: {len(properties)} свойств')

        if not properties:
            _log('Нет свойств для обработки', 'warn')
            await browser.close()
            return

        for prop in properties:
            rid = prop['resource_id']
            _log(f'\n{"─" * 50}')
            _log(f'Свойство: {prop["name"]}')

            # Получаем URL с ошибками
            error_urls = await get_error_urls(page, rid)
            if not error_urls:
                _log('Ошибок нет — пропускаем', 'ok')
                await asyncio.sleep(DELAY_BETWEEN_PROPERTIES_SEC)
                continue

            # Обрабатываем URL
            for url in error_urls:
                if limit and total_requested >= limit:
                    _log(f'Достигнут лимит {limit} URL — остановка')
                    break

                result = await request_reindex(page, rid, url, dry_run)
                all_results.append(result)

                if result['status'] in ('ok', 'dry_run'):
                    total_requested += 1

                _save_log(all_results)
                await asyncio.sleep(DELAY_BETWEEN_URLS_SEC)

            await asyncio.sleep(DELAY_BETWEEN_PROPERTIES_SEC)

        await browser.close()

    _log(f'\n{"═" * 50}')
    ok = sum(1 for r in all_results if r['status'] == 'ok')
    skip = sum(1 for r in all_results if r['status'] == 'skipped')
    err = sum(1 for r in all_results if r['status'] == 'error')
    _log(f'Готово: запрошено {ok}, пропущено {skip}, ошибок {err}')
    _log(f'Лог сохранён → {LOG_FILE.resolve()}')


# ── CLI ────────────────────────────────────────────────────────────


def parse_args():
    p = argparse.ArgumentParser(
        description='Автозапрос повторной индексации в Google Search Console')
    p.add_argument('--property', metavar='DOMAIN',
                   help='Фильтр по имени свойства (например: example.com)')
    p.add_argument('--dry-run', action='store_true',
                   help='Проверка без реальных кликов')
    p.add_argument('--limit', type=int, default=0,
                   help='Максимум URL за один запуск (0 = без лимита)')
    p.add_argument('--headless', action='store_true',
                   help='Без видимого окна браузера')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()
    asyncio.run(run(
        property_filter=args.property,
        dry_run=args.dry_run,
        limit=args.limit,
        headless=args.headless,
    ))
