"""
open_browser.py - открыть браузер для автокликеров и сразу выйти.

Запускает Chromium-браузер с ОТДЕЛЬНЫМ профилем и debug-портом 9222,
дожидается открытия порта и завершается. Браузер остаётся открытым -
в нём нужно войти в Google/Yandex аккаунты проекта. Кликеры потом
подключаются к нему по CDP.

Какой браузер берём (по порядку):
  1. браузер ПО УМОЛЧАНИЮ из реестра Windows - «в каком сидишь, тот и
     откроем» (только Chromium-семейство: Chrome/Edge/Яндекс/Brave/Opera);
  2. любой найденный Chromium-браузер по известным путям;
  3. Chromium от Playwright (ставится INSTALL-скриптом - есть всегда).
Firefox не подходит - у него нет Chrome DevTools Protocol.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME_PROFILE_DIR = Path(__file__).parent / 'gsc_chrome_profile'
CDP_PORT = 9222

# Известные Chromium-браузеры (все понимают --remote-debugging-port)
_PF = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
_PF86 = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
_LOCAL = Path(os.environ.get('LOCALAPPDATA', 'C:/'))
_CANDIDATES = [
    _PF / 'Google/Chrome/Application/chrome.exe',
    _PF86 / 'Google/Chrome/Application/chrome.exe',
    _LOCAL / 'Google/Chrome/Application/chrome.exe',
    _LOCAL / 'Yandex/YandexBrowser/Application/browser.exe',
    _PF / 'Yandex/YandexBrowser/Application/browser.exe',
    _PF86 / 'Microsoft/Edge/Application/msedge.exe',
    _PF / 'Microsoft/Edge/Application/msedge.exe',
    _PF / 'BraveSoftware/Brave-Browser/Application/brave.exe',
    _LOCAL / 'BraveSoftware/Brave-Browser/Application/brave.exe',
    Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
    Path('/usr/bin/google-chrome-stable'),
    Path('/usr/bin/google-chrome'),
    Path('/usr/bin/chromium-browser'),
]
# Имена exe Chromium-семейства - для проверки браузера по умолчанию
_CHROMIUM_EXES = ('chrome.exe', 'browser.exe', 'msedge.exe', 'brave.exe',
                  'opera.exe', 'vivaldi.exe', 'chromium.exe')


def _default_browser_exe():
    """Путь к exe браузера по умолчанию (Windows, из реестра) - «в каком
    сидишь». Возвращает Path или None (не Windows / не Chromium / не нашли)."""
    if os.name != 'nt':
        return None
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\Shell\Associations'
                r'\UrlAssociations\http\UserChoice') as k:
            prog_id = winreg.QueryValueEx(k, 'ProgId')[0]
        with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT,
                rf'{prog_id}\shell\open\command') as k:
            cmd = winreg.QueryValueEx(k, None)[0]
        # Команда вида: "C:\...\chrome.exe" --single-argument %1
        exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split()[0]
        p = Path(exe)
        if p.exists() and p.name.lower() in _CHROMIUM_EXES:
            return p
    except Exception:
        pass
    return None


def _playwright_chromium():
    """Chromium, установленный Playwright'ом (фоллбэк - есть всегда)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and os.path.exists(path):
                return Path(path)
    except Exception:
        pass
    return None


def find_chrome():
    default = _default_browser_exe()
    if default:
        print(f'Браузер по умолчанию: {default.name}')
        return default
    for c in _CANDIDATES:
        if c.exists():
            return c
    pw = _playwright_chromium()
    if pw:
        print('Системный браузер не найден - использую Chromium от Playwright.')
        return pw
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
        print('Chromium-браузер не найден (Chrome/Edge/Яндекс/Brave) и '
              'Playwright-Chromium не установлен. Запусти '
              '«INSTALL (run once).bat» или установи Google Chrome.')
        sys.exit(1)
    print(f'Использую: {chrome}')

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
