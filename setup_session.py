"""
setup_session.py — РАЗОВАЯ настройка входа для проверки 404 в индексе.

Открывает ВИДИМОЕ окно браузера, логинится в Google Search Console и в
Яндекс.Вебмастер, и сохраняет вход в файл session.json (storage_state:
cookies обоих сервисов). Дальше проверку (index_gsc_run / index404_run в
видимом режиме с AUTOCLICK_SESSION_FILE=session.json) может запускать ЛЮБОЙ
человек — окно откроется уже залогиненным, вводить ничего не надо.

Робот сам вводит e-mail из GSC_LOGIN_EMAIL и жмёт «Далее»; ПАРОЛЬ (и, если
Google/Яндекс попросят, подтверждение) вводит человек прямо в окне. Когда
вход виден — сессия сохраняется. Google-сессия живёт неделями; когда
протухнет — запусти этот файл ещё раз.

Запуск:  python setup_session.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
SESSION = ROOT / 'session.json'
EMAIL = os.environ.get('GSC_LOGIN_EMAIL', '').strip()

GSC_URL = 'https://search.google.com/u/0/search-console'
YA_URL = 'https://webmaster.yandex.ru/sites/'


def _log(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


async def _wait_until_left(page, bad_substrings, secs=300) -> bool:
    """Ждать (до secs), пока URL перестанет содержать эти подстроки (значит,
    ушли со страницы входа = вошли)."""
    for _ in range(int(secs / 3)):
        await page.wait_for_timeout(3000)
        try:
            u = page.url
        except Exception:
            u = ''
        if not any(b in u for b in bad_substrings):
            return True
    return False


async def _google_prefill_email(page):
    """Робот вводит e-mail (#identifierId) и жмёт «Далее». Остальное — человек."""
    if not EMAIL:
        _log('  GSC_LOGIN_EMAIL не задан — введи e-mail в окне сам.')
        return
    try:
        loc = page.locator(
            '#identifierId, input[name="identifier"], input[type="email"]').first
        await loc.wait_for(state='visible', timeout=8000)
        await loc.fill(EMAIL, timeout=6000)
        _log(f'  Ввёл e-mail {EMAIL} — жму «Далее». Теперь введи ПАРОЛЬ в окне.')
        for sel in ('#identifierNext button', '#identifierNext',
                    'button:has-text("Далее")', 'button:has-text("Next")'):
            try:
                await page.locator(sel).first.click(timeout=4000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(3000)
    except Exception as e:
        _log(f'  (авто-ввод e-mail не удался: {e} — введи руками в окне)')


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        launch = dict(headless=False, args=[
            '--disable-blink-features=AutomationControlled',
            '--start-maximized', '--no-first-run', '--no-default-browser-check'])
        browser = None
        for attempt in ({}, {'channel': 'chrome'}, {'channel': 'msedge'}):
            try:
                browser = await p.chromium.launch(**launch, **attempt)
                break
            except Exception:
                continue
        if browser is None:
            _log('Не удалось открыть браузер. Поставь его: '
                 'python -m playwright install chromium')
            sys.exit(1)
        ctx = await browser.new_context(
            locale='ru-RU', timezone_id='Europe/Moscow', no_viewport=True)
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await ctx.new_page()

        # ── Google Search Console ──
        _log('Открываю Google Search Console…')
        try:
            await page.goto(GSC_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            _log(f'  не открылся GSC: {e}')
        await page.wait_for_timeout(4000)
        if 'accounts.google.com' in page.url or 'signin' in page.url:
            await _google_prefill_email(page)
            _log('  Жду вход в Google (введи пароль/подтверждение в окне) — до 5 минут…')
            if await _wait_until_left(page, ['accounts.google.com', 'signin'], 300):
                _log('  ✓ Google: вход выполнен.')
            else:
                _log('  ⏰ Google: вход не завершён за 5 минут (сохраню что есть).')
        else:
            _log('  ✓ Google: уже залогинен.')

        # ── Яндекс.Вебмастер ──
        _log('Открываю Яндекс.Вебмастер…')
        try:
            await page.goto(YA_URL, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            _log(f'  не открылся Вебмастер: {e}')
        await page.wait_for_timeout(4000)
        if 'passport.yandex' in page.url:
            _log('  Войди в ЯНДЕКС в окне (логин/пароль) — жду до 5 минут…')
            if await _wait_until_left(page, ['passport.yandex'], 300):
                _log('  ✓ Яндекс: вход выполнен.')
            else:
                _log('  ⏰ Яндекс: вход не завершён за 5 минут (сохраню что есть).')
        else:
            _log('  ✓ Яндекс: уже залогинен.')

        # ── Сохраняем вход ──
        try:
            await ctx.storage_state(path=str(SESSION))
            _log(f'✅ СЕССИЯ СОХРАНЕНА: {SESSION.name}. Теперь проверку можно '
                 'запускать без входа (файл 2-PROVERKA).')
        except Exception as e:
            _log(f'⚠ не удалось сохранить сессию: {e}')
        await page.wait_for_timeout(1500)
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
