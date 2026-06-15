"""
gsc_inspect_one.py
==================
Helper для отладки: открывает проверку ОДНОГО URL в уже запущенном Chrome
(через CDP, порт 9222 — повторный вход не нужен) и выводит все кнопки на
странице. Нужен чтобы поймать точный селектор кнопки «Запросить индексирование».

Перед запуском:
    1. Запусти gsc_save_session.py — он откроет Chrome с debug-портом 9222
       и оставит его открытым (авторизованным).
    2. НЕ закрывай этот Chrome.

Запуск:
    python gsc_inspect_one.py --resource "sc-domain:stalmetural.ru" --url "https://stalmetural.ru/broken-page"

    # Если знаешь resource_id из адресной строки GSC — подставь его.
    # Для домена-ресурса формат: sc-domain:example.com
    # Для URL-префикса: https://example.com/

Что делает:
    - Открывает инспекцию URL
    - Ждёт загрузки
    - Печатает ВСЕ кнопки (текст + role + ключевые атрибуты)
    - НИЧЕГО не кликает (только разведка)
"""

import argparse
import asyncio
from urllib.parse import quote

CDP_URL = 'http://127.0.0.1:9222'
INSPECT = 'https://search.google.com/search-console/inspect?resource_id={rid}&id={url}'


async def main(resource_id: str, url: str):
    from playwright.async_api import async_playwright

    inspect_url = INSPECT.format(
        rid=quote(resource_id, safe=''),
        url=quote(url, safe=''),
    )

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f'Не удалось подключиться к Chrome на {CDP_URL}: {e}')
            print('Сначала запусти gsc_save_session.py (он держит Chrome открытым).')
            return

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        print(f'Открываю инспекцию:\n  {url}\n')
        await page.goto(inspect_url, wait_until='domcontentloaded')
        await page.wait_for_timeout(6000)  # ждём прогрузки SPA

        print(f'Текущий URL вкладки: {page.url}\n')

        # Дамп всех кнопок
        print('=== КНОПКИ НА СТРАНИЦЕ ===')
        buttons = await page.query_selector_all(
            'button, [role="button"], a[role="button"]')
        if not buttons:
            print('Кнопок не найдено — возможно страница ещё грузится или нужен скролл.')
        for i, b in enumerate(buttons):
            try:
                txt = (await b.inner_text()).strip().replace('\n', ' ')
            except Exception:
                txt = ''
            if not txt:
                txt = (await b.get_attribute('aria-label')) or ''
            jsname = await b.get_attribute('jsname') or ''
            data_id = await b.get_attribute('data-id') or ''
            visible = await b.is_visible()
            if txt or jsname:
                print(f'[{i:2}] vis={visible} text="{txt[:60]}" '
                      f'jsname="{jsname}" data-id="{data_id}"')

        # Подсказка: ищем кнопку индексации по тексту
        print('\n=== ПОИСК КНОПКИ ИНДЕКСАЦИИ ===')
        for needle in ('Запросить индексирование', 'Запросить повторную индексацию',
                       'Request indexing', 'индексир'):
            loc = page.get_by_text(needle, exact=False)
            try:
                cnt = await loc.count()
            except Exception:
                cnt = 0
            print(f'  "{needle}": найдено {cnt}')

        print('\nГотово. Браузер оставляю открытым.')
        await browser.disconnect()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--resource', required=True,
                    help='resource_id: "sc-domain:example.com" или "https://example.com/"')
    ap.add_argument('--url', required=True, help='Полный URL страницы для проверки')
    args = ap.parse_args()
    asyncio.run(main(args.resource, args.url))
