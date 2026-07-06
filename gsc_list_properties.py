"""
gsc_list_properties.py
======================
Разведка: вывести ВСЕ ресурсы (properties) аккаунта GSC.
Нужно чтобы понять структуру - отдельные ресурсы под каждый поддомен
или один ресурс-домен (sc-domain:...) на всё.

Перед запуском:
    1. python gsc_save_session.py  - откроет авторизованный Chrome (порт 9222)
    2. НЕ закрывай его.

Запуск:
    python gsc_list_properties.py
"""

import asyncio
import json
import re
from urllib.parse import unquote

CDP_URL = 'http://127.0.0.1:9222'
GSC_HOME = 'https://search.google.com/search-console/'

# resource_id в GSC: либо "sc-domain:example.com", либо "https://host/".
# Из HTML переключателя выгребаем оба варианта.
_RE_DOMAIN = re.compile(r'sc-domain:[A-Za-z0-9.\-]+')
_RE_PREFIX = re.compile(r'https?:\\?/\\?/[A-Za-z0-9.\-]+\\?/')


def _extract_from_html(html: str) -> set:
    found = set()
    for m in _RE_DOMAIN.findall(html):
        found.add(m)
    for m in _RE_PREFIX.findall(html):
        # убираем экранирование \/ из JSON-блоков
        clean = m.replace('\\/', '/').replace('\\', '')
        # отсекаем служебные хосты Google
        host = clean.split('//', 1)[-1]
        if any(g in host for g in ('google.com', 'gstatic.com', 'googleapis.com',
                                   'googleusercontent.com', 'youtube.com',
                                   'schema.org', 'w3.org')):
            continue
        found.add(clean)
    return found


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f'Нет подключения к Chrome ({CDP_URL}): {e}')
            print('Сначала запусти gsc_save_session.py.')
            return

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        print('Открываю GSC...')
        await page.goto(GSC_HOME, wait_until='domcontentloaded')
        await page.wait_for_timeout(4000)

        if 'accounts.google.com' in page.url:
            print('Не авторизован - перезапусти gsc_save_session.py и войди.')
            await browser.disconnect()
            return

        found = set()

        # Способ 1: ссылки с resource_id в href
        for link in await page.query_selector_all('a[href*="resource_id="]'):
            href = await link.get_attribute('href') or ''
            if 'resource_id=' in href:
                found.add(unquote(href.split('resource_id=')[1].split('&')[0]))

        # Способ 2: открыть переключатель ресурсов и выгрести из его HTML.
        print('Открываю переключатель ресурсов...')
        opened = False
        for sel in ('text=Выбор ресурса', 'text=Search property',
                    '[aria-label*="ресурс"]', '[aria-label*="property"]',
                    'button[aria-haspopup="listbox"]',
                    '[jsname][role="combobox"]'):
            try:
                await page.click(sel, timeout=2500)
                opened = True
                await page.wait_for_timeout(1500)
                break
            except Exception:
                continue
        if not opened:
            print('  Не нашёл кнопку переключателя - выгребаю из общего HTML.')

        # Прокручиваем список (виртуализация подгружает строки при скролле)
        for _ in range(8):
            found |= _extract_from_html(await page.content())
            try:
                await page.mouse.wheel(0, 1200)
                await page.wait_for_timeout(400)
            except Exception:
                break

        # Финальный проход по всему HTML
        found |= _extract_from_html(await page.content())

        # Чистим: убираем дубли вида host и host/ , нормализуем
        props = {}
        for rid in found:
            kind = 'ДОМЕН (покрывает поддомены)' if rid.startswith('sc-domain:') \
                else 'URL-префикс (только origin)'
            props[rid] = kind

        print('\n=== РЕСУРСЫ GSC ===')
        if not props:
            print('Ничего не нашёл. Открой переключатель ресурсов вручную и пришли скриншот.')
        else:
            print(f'Найдено: {len(props)}')
            for rid, kind in sorted(props.items()):
                print(f'  • {rid}   [{kind}]')

        with open('gsc_properties.json', 'w', encoding='utf-8') as f:
            json.dump(list(props.keys()), f, ensure_ascii=False, indent=2)
        print('\nСохранено → gsc_properties.json')

        await browser.close()  # отключиться от CDP, Chrome остаётся открытым


if __name__ == '__main__':
    asyncio.run(main())
