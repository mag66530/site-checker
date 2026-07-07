"""
browser_setup.py - подготовка браузера (Chromium для Playwright) в облаке.

На Streamlit Cloud нет заранее установленного браузера: библиотека playwright
ставится из requirements.txt, а сам Chromium нужно доустановить в рантайме
(`playwright install chromium`). Системные библиотеки Chromium ставит Streamlit
Cloud по packages.txt. Локально (где браузер уже стоит) функция просто
подтверждает готовность и ничего не качает.

Результат кэшируется на процесс - установка идёт максимум один раз за запуск
контейнера (первый прогон дольше на ~1 минуту).
"""
from __future__ import annotations

import functools
import os
import subprocess
import sys


def _браузер_на_месте() -> bool:
    """Есть ли уже установленный Chromium у playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return bool(path and os.path.exists(path))
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def ensure_browser() -> tuple[bool, str]:
    """Гарантирует наличие Chromium. Возвращает (готово, сообщение).
    Кэшируется: реальная установка выполняется один раз за жизнь процесса."""
    try:
        import playwright  # noqa: F401
    except Exception:
        return False, ('нет библиотеки playwright (добавьте в requirements.txt '
                       'и перезапустите приложение)')

    if _браузер_на_месте():
        return True, 'браузер готов'

    # Ставим Chromium (без системных зависимостей - их даёт packages.txt).
    try:
        subprocess.run(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            check=True, capture_output=True, text=True, timeout=900,
        )
    except Exception as e:  # noqa: BLE001
        detail = getattr(e, 'stderr', '') or str(e)
        return False, f'не удалось установить Chromium: {str(detail)[:300]}'

    if _браузер_на_месте():
        return True, 'браузер установлен'
    return False, 'Chromium установлен, но не запускается (проверьте packages.txt)'
