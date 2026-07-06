"""
open_browser.py - открыть Chrome для автокликеров и сразу выйти.

Запускает реальный Chrome с отдельным профилем и debug-портом 9222,
дожидается открытия порта и завершается. Chrome остаётся открытым -
в нём нужно войти в Google/Yandex аккаунты проекта. Кликеры потом
подключаются к этому Chrome по CDP.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME_PROFILE_DIR = Path(__file__).parent / 'gsc_chrome_profile'
CDP_PORT = 9222


def find_chrome():
    for c in (
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
        / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
        / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', ''))
        / 'Google/Chrome/Application/chrome.exe',
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path('/usr/bin/google-chrome-stable'),
        Path('/usr/bin/google-chrome'),
    ):
        if c.exists():
            return c
    return None


def port_open(port):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=1)
        return True
    except Exception:
        return False


def main():
    chrome = find_chrome()
    if not chrome:
        print('Chrome не найден. Установи Google Chrome.')
        sys.exit(1)

    if port_open(CDP_PORT):
        print(f'Chrome уже открыт на порту {CDP_PORT}. Используем его.')
        return

    CHROME_PROFILE_DIR.mkdir(exist_ok=True)
    cmd = [
        str(chrome),
        f'--remote-debugging-port={CDP_PORT}',
        f'--user-data-dir={CHROME_PROFILE_DIR}',
        '--no-first-run', '--no-default-browser-check',
        '--disable-infobars', '--disable-session-crashed-bubble',
    ]
    print('Запускаю Chrome…')
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 20
    while time.time() < deadline:
        if port_open(CDP_PORT):
            print('Chrome открыт. Войди в Google и Yandex аккаунты проекта.')
            return
        time.sleep(0.5)
    print('Chrome не открыл порт за 20 сек. Проверь, не запущен ли обычный Chrome.')
    sys.exit(1)


if __name__ == '__main__':
    main()
