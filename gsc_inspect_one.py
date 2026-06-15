"""
gsc_inspect_one.py
==================
Отладочный helper. Открывает ОБЗОР ресурса GSC (не сломанный deep-link),
по желанию вставляет URL в строку проверки и жмёт Enter, затем выводит
все поля ввода и кнопки — чтобы поймать селекторы омнибокса и кнопки
«Запросить индексирование».

Перед запуском:
    python gsc_save_session.py   # держит авторизованный Chrome на порту 9222

Запуск:
    # Только разведка страницы ресурса (поля + кнопки):
    python gsc_inspect_one.py --resource "https://vladimir.mepen.ru/"

    # + попытка проверить конкретный URL через омнибокс:
    python gsc_inspect_one.py --resource "https://vladimir.mepen.ru/" --url "https://vladimir.mepen.ru/"
"""

import argparse
import asyncio
from urllib.parse import quote

CDP_URL = 'http://127.0.0.1:9222'
# Рабочая страница ресурса — отчёт «Эффективность» (стабильный маршрут).
OVERVIEW = 'https://search.google.com/search-console/performance/search-analytics?resource_id={rid}'


async def dump_inputs(page):
    print('\n=== ПОЛЯ ВВОДА (input / textarea / combobox) ===')
    inputs = await page.query_selector_all(
        'input, textarea, [role="combobox"], [contenteditable="true"]')
    if not inputs:
        print('  Полей не найдено.')
    for i, el in enumerate(inputs):
        try:
            al = await el.get_attribute('aria-label') or ''
            ph = await el.get_attribute('placeholder') or ''
            nm = await el.get_attribute('name') or ''
            tp = await el.get_attribute('type') or ''
            vis = await el.is_visible()
            print(f'[{i:2}] vis={vis} type="{tp}" aria-label="{al[:50]}" '
                  f'placeholder="{ph[:40]}" name="{nm}"')
        except Exception:
            pass


async def dump_buttons(page):
    print('\n=== КНОПКИ ===')
    buttons = await page.query_selector_all('button, [role="button"], a[role="button"]')
    if not buttons:
        print('  Кнопок не найдено.')
    for i, b in enumerate(buttons):
        try:
            txt = (await b.inner_text()).strip().replace('\n', ' ')
            if not txt:
                txt = await b.get_attribute('aria-label') or ''
            vis = await b.is_visible()
            if txt:
                print(f'[{i:2}] vis={vis} "{txt[:70]}"')
        except Exception:
            pass


async def main(resource_id: str, url: str | None):
    from playwright.async_api import async_playwright

    overview_url = OVERVIEW.format(rid=quote(resource_id, safe=''))

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f'Нет подключения к Chrome ({CDP_URL}): {e}')
            print('Сначала запусти gsc_save_session.py.')
            return

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        print(f'Открываю обзор ресурса:\n  {resource_id}\n')
        await page.goto(overview_url, wait_until='domcontentloaded')
        await page.wait_for_timeout(6000)
        print(f'URL вкладки: {page.url}')

        body = (await page.inner_text('body'))[:300]
        if 'не найден' in body.lower() or 'not found' in body.lower():
            print('\n⚠ Похоже ресурс не открылся (404/не найден).')
            print('Проверь точный resource_id из gsc_properties.json.')

        await dump_inputs(page)
        await dump_buttons(page)

        # Попытка проверить URL через омнибокс
        if url:
            print(f'\n=== ПРОБУЮ ПРОВЕРИТЬ URL ЧЕРЕЗ ОМНИБОКС ===\n  {url}')
            filled = False
            # Строка проверки URL: input с aria-label "Проверка всех URL на ресурсе…"
            for sel in (
                'input[aria-label*="Проверка всех URL"]',
                'input[aria-label*="Проверка"]',
                'input[aria-label*="Inspect"]',
            ):
                try:
                    omni = page.locator(sel).first
                    await omni.wait_for(state='attached', timeout=4000)
                    await omni.click(timeout=6000)
                    await omni.fill(url, timeout=6000)
                    await page.keyboard.press('Enter')
                    filled = True
                    print(f'  Ввёл URL в поле: {sel}')
                    break
                except Exception as e:
                    print(f'  {sel} — не подошёл ({type(e).__name__})')
                    continue
            if not filled:
                print('  Не нашёл строку проверки — смотри список полей выше.')
            else:
                print('  Жду результат проверки (до 45 сек)...')
                # Ждём появления кнопки индексации или текста статуса
                try:
                    await page.wait_for_selector(
                        'text=Запросить индексирование', timeout=45000)
                    print('  ✓ Кнопка «Запросить индексирование» появилась')
                except Exception:
                    print('  Кнопка не появилась за 45 сек — дамп ниже покажет что есть')
                await dump_buttons(page)

        print('\nГотово. Браузер оставляю открытым.')
        await browser.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--resource', required=True,
                    help='resource_id: "https://host/" или "sc-domain:example.com"')
    ap.add_argument('--url', default=None, help='URL для проверки через омнибокс (опц.)')
    args = ap.parse_args()
    asyncio.run(main(args.resource, args.url))
