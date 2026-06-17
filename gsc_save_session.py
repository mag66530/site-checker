"""
gsc_save_session.py
===================
Сохранить сессию Google через реальный Chrome.

Запуск:
    python gsc_save_session.py

Что делает:
    1. Запускает реальный Chrome с отдельной папкой профиля (gsc_chrome_profile/)
       – нет конфликтов с основным Chrome
    2. Ты логинишься в Google один раз
    3. Cookies сохраняются в gsc_session.json для gsc_reindex.py
    4. При повторных запусках Chrome уже авторизован (cookies в папке профиля)

Зависимости:
    pip install playwright
"""

import asyncio
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SESSION_FILE = Path('gsc_session.json')
# Отдельная папка для Chrome-профиля этого инструмента
CHROME_PROFILE_DIR = Path('gsc_chrome_profile').resolve()
CDP_PORT = 9222
CDP_URL = f'http://127.0.0.1:{CDP_PORT}'


def find_chrome() -> Path | None:
    candidates = [
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
        / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
        / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', ''))
        / 'Google/Chrome/Application/chrome.exe',
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path('/usr/bin/google-chrome-stable'),
        Path('/usr/bin/google-chrome'),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def kill_port_user():
    """Убить процесс занимающий CDP-порт (если есть)."""
    if sys.platform == 'win32':
        try:
            out = subprocess.check_output(
                f'netstat -ano | findstr :{CDP_PORT}',
                shell=True, text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    if pid.isdigit() and pid != '0':
                        subprocess.run(['taskkill', '/F', '/PID', pid],
                                       capture_output=True)
        except Exception:
            pass


def wait_for_port(port: int, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/json',
                                   timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('Playwright не установлен: pip install playwright')
        sys.exit(1)

    chrome_path = find_chrome()
    if not chrome_path:
        print('Chrome не найден. Установи Google Chrome.')
        sys.exit(1)

    print(f'Chrome: {chrome_path}')
    print(f'Профиль (отдельная папка): {CHROME_PROFILE_DIR}')
    print()

    # Убиваем что уже сидит на порту
    kill_port_user()
    time.sleep(1)

    # Создаём папку профиля если нет
    CHROME_PROFILE_DIR.mkdir(exist_ok=True)

    # Запускаем Chrome
    cmd = [
        str(chrome_path),
        f'--remote-debugging-port={CDP_PORT}',
        f'--user-data-dir={CHROME_PROFILE_DIR}',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-infobars',
        '--disable-session-crashed-bubble',
        '--disable-features=TranslateUI',
    ]
    print('Запускаю Chrome...')
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    print('Жду открытия порта...', end='', flush=True)
    ready = wait_for_port(CDP_PORT, timeout=20)
    if not ready:
        print(' не открылся.')
        print()
        print('Что попробовать:')
        print('  1. Убедись что Chrome не запущен (закрой все окна)')
        print('  2. Проверь что антивирус не блокирует порт 9222')
        print(f'  3. Попробуй вручную: "{chrome_path}" '
              f'--remote-debugging-port={CDP_PORT} '
              f'--user-data-dir="{CHROME_PROFILE_DIR}"')
        proc.terminate()
        sys.exit(1)
    print(' OK')
    print()

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)

        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        print('Открываю Google Search Console...')
        await page.goto('https://search.google.com/search-console/',
                        wait_until='domcontentloaded')
        await asyncio.sleep(3)

        if 'accounts.google.com' in page.url or 'signin' in page.url:
            print()
            print('=' * 55)
            print('  НУЖНО ВОЙТИ В GOOGLE АККАУНТ')
            print()
            print('  1. Войди в Google в открытом окне Chrome')
            print('  2. Дождись открытия Search Console')
            print('  3. Вернись сюда и нажми Enter')
            print('=' * 55)
        else:
            print()
            print('=' * 55)
            print('  УЖЕ АВТОРИЗОВАН!')
            print()
            print('  Убедись что выбран нужный аккаунт.')
            print('  Затем нажми Enter.')
            print('=' * 55)

        input()

        # Последняя проверка
        current = page.url
        if 'accounts.google.com' in current or 'signin' in current:
            print('Похоже вход не завершён. Войди и нажми Enter ещё раз.')
            input()

        await context.storage_state(path=str(SESSION_FILE))

        print()
        print(f'Сессия сохранена → {SESSION_FILE.resolve()}')
        print()
        print('Готово! Запусти проверку:')
        print('  python gsc_reindex.py --dry-run')

        await browser.close()  # отключиться от CDP, Chrome остаётся открытым

    # Не убиваем Chrome – пусть пользователь закроет сам
    print()
    print('Chrome можно закрыть вручную.')


if __name__ == '__main__':
    asyncio.run(main())
