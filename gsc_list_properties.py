"""
gsc_list_properties.py
======================
Разведка: вывести ВСЕ ресурсы (properties) аккаунта GSC.
Нужно чтобы понять структуру — отдельные ресурсы под каждый поддомен
или один ресурс-домен (sc-domain:...) на всё.

Перед запуском:
    1. python gsc_save_session.py  — откроет авторизованный Chrome (порт 9222)
    2. НЕ закрывай его.

Запуск:
    python gsc_list_properties.py
"""

import asyncio
import json
from urllib.parse import unquote

CDP_URL = 'http://127.0.0.1:9222'
GSC_HOME = 'https://search.google.com/search-console/'


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
            print('Не авторизован — перезапусти gsc_save_session.py и войди.')
            await browser.disconnect()
            return

        props = {}

        # Способ 1: все ссылки с resource_id в href
        for link in await page.query_selector_all('a[href*="resource_id="]'):
            href = await link.get_attribute('href') or ''
            if 'resource_id=' not in href:
                continue
            rid = unquote(href.split('resource_id=')[1].split('&')[0])
            txt = (await link.inner_text()).strip()
            props.setdefault(rid, txt or rid)

        # Способ 2: открыть переключатель ресурсов (dropdown слева сверху)
        if len(props) <= 1:
            print('Пробую открыть переключатель ресурсов...')
            for sel in ('text=Выбор ресурса', 'text=Search property',
                        '[aria-label*="ресурс"]', '[aria-label*="property"]'):
                try:
                    await page.click(sel, timeout=2500)
                    await page.wait_for_timeout(1500)
                    break
                except Exception:
                    continue
            for item in await page.query_selector_all('[role="option"], [role="menuitem"]'):
                txt = (await item.inner_text()).strip()
                if not txt:
                    continue
                rid = await item.get_attribute('data-value') or txt
                props.setdefault(rid, txt)

        print('\n=== РЕСУРСЫ GSC ===')
        if not props:
            print('Ничего не нашёл автоматически.')
            print('Открой переключатель ресурсов вручную в браузере и пришли скриншот.')
        else:
            for rid, name in props.items():
                kind = 'ДОМЕН (покрывает поддомены)' if rid.startswith('sc-domain:') \
                    else 'URL-префикс (только origin)'
                print(f'  • {name}')
                print(f'      resource_id: {rid}   [{kind}]')

        # Сохраняем в файл для следующего шага
        with open('gsc_properties.json', 'w', encoding='utf-8') as f:
            json.dump(props, f, ensure_ascii=False, indent=2)
        print('\nСохранено → gsc_properties.json')

        await browser.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
