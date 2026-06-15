"""
gsc_indexing_report.py
======================
Разведка отчёта «Индексирование → Страницы» одного ресурса.
Выводит таблицу причин (Причина + статус Проверки) и кнопки/ссылки —
чтобы поймать селекторы для авто-«Проверить исправление».

Перед запуском:
    python gsc_save_session.py   # авторизованный Chrome на порту 9222

Запуск:
    python gsc_indexing_report.py --resource "https://vladimir.mepen.ru/"

    # после захода в строку причины (вручную или передав --open-first)
    python gsc_indexing_report.py --resource "https://vladimir.mepen.ru/" --open-first
"""

import argparse
import asyncio
from urllib.parse import quote

CDP_URL = 'http://127.0.0.1:9222'
# Отчёт «Страницы» (индексирование)
INDEX_REPORT = 'https://search.google.com/search-console/index?resource_id={rid}'


async def dump_rows(page):
    print('\n=== СТРОКИ ТАБЛИЦ (role=row / tr) ===')
    rows = await page.query_selector_all('[role="row"], tr')
    if not rows:
        print('  Строк не найдено.')
    for i, r in enumerate(rows):
        try:
            txt = (await r.inner_text()).strip().replace('\n', ' | ')
            vis = await r.is_visible()
            if txt:
                print(f'[{i:2}] vis={vis} {txt[:140]}')
        except Exception:
            pass


async def dump_buttons(page):
    print('\n=== КНОПКИ ===')
    for i, b in enumerate(await page.query_selector_all(
            'button, [role="button"], a[role="button"]')):
        try:
            txt = (await b.inner_text()).strip().replace('\n', ' ')
            if not txt:
                txt = await b.get_attribute('aria-label') or ''
            if txt and await b.is_visible():
                print(f'[{i:2}] "{txt[:80]}"')
        except Exception:
            pass


async def dump_links(page):
    print('\n=== ССЫЛКИ С ТЕКСТОМ (первые 40 видимых) ===')
    n = 0
    for a in await page.query_selector_all('a, [role="link"], [jsaction]'):
        try:
            txt = (await a.inner_text()).strip().replace('\n', ' ')
            if txt and await a.is_visible() and len(txt) > 3:
                print(f'  "{txt[:90]}"')
                n += 1
                if n >= 40:
                    break
        except Exception:
            pass


async def main(resource_id: str, open_first: bool):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f'Нет подключения к Chrome ({CDP_URL}): {e}')
            print('Сначала запусти gsc_save_session.py.')
            return

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        url = INDEX_REPORT.format(rid=quote(resource_id, safe=''))
        print(f'Открываю отчёт «Страницы»:\n  {resource_id}\n  {url}\n')
        await page.goto(url, wait_until='domcontentloaded')
        await page.wait_for_timeout(6000)
        print(f'URL вкладки: {page.url}')

        body = (await page.inner_text('body'))[:200]
        if 'не найден' in body.lower():
            print('⚠ Ресурс не открылся — проверь resource_id.')

        # Скроллим до конца — второй блок «Проблемы с представлением страниц»
        # подгружается ниже по странице.
        for _ in range(8):
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(1500)

        # Заголовки разделов отчёта
        print('\n=== ЗАГОЛОВКИ РАЗДЕЛОВ ===')
        for el in await page.query_selector_all('h1, h2, h3, [role="heading"]'):
            try:
                t = (await el.inner_text()).strip().replace('\n', ' ')
                if t and any(k in t.lower() for k in
                             ('индексир', 'представлен', 'поиск', 'причин', 'проблем')):
                    print(f'  • {t[:90]}')
            except Exception:
                pass

        # Все строки tr[data-rowid] на странице (оба блока)
        print('\n=== ВСЕ tr[data-rowid] (оба блока) ===')
        for tr in await page.query_selector_all('tr[data-rowid]'):
            try:
                rid = await tr.get_attribute('data-rowid')
                t = (await tr.inner_text()).strip().replace('\n', ' ')
                print(f'  rowid={rid}: {t[:100]}')
            except Exception:
                pass

        await dump_buttons(page)

        # Пробуем кликнуть первую причину и показать детальную страницу
        if open_first:
            print('\n=== ПРОБУЮ ОТКРЫТЬ ПЕРВУЮ ПРИЧИНУ ===')
            opened = False
            # Строки причин — <tr data-rowid="N"> с jsaction.
            rows = await page.query_selector_all('tr[data-rowid]')
            print(f'  Найдено строк tr[data-rowid]: {len(rows)}')
            for tr in rows:
                try:
                    rid = await tr.get_attribute('data-rowid')
                    txt = (await tr.inner_text()).strip().replace('\n', ' ')
                    print(f'    rowid={rid}: {txt[:80]}')
                except Exception:
                    pass
            # Кликаем первую строку
            try:
                first = page.locator('tr[data-rowid]').first
                await first.click(timeout=6000)
                opened = True
                print('  Кликнул первую причину (tr[data-rowid])')
            except Exception as e:
                print(f'  Клик не удался: {type(e).__name__}: {e}')
            if opened:
                await page.wait_for_timeout(6000)
                print(f'  URL после клика: {page.url}')
                await dump_buttons(page)
                # Подсветим кандидата на кнопку проверки
                print('\n  Поиск кнопки проверки исправления:')
                for needle in ('Проверить исправление', 'ПРОВЕРИТЬ ИСПРАВЛЕНИЕ',
                               'Подтвердить исправление', 'Validate', 'Начать проверку'):
                    try:
                        cnt = await page.get_by_text(needle, exact=False).count()
                    except Exception:
                        cnt = 0
                    print(f'    "{needle}": {cnt}')
            else:
                print('  Не нашёл кликабельную причину.')

        print('\nГотово. Браузер оставляю открытым.')
        await browser.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--resource', required=True)
    ap.add_argument('--open-first', action='store_true',
                    help='кликнуть первую причину и показать кнопки детальной страницы')
    args = ap.parse_args()
    asyncio.run(main(args.resource, args.open_first))
