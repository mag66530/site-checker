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

import asyncio
import subprocess
import sys


async def _браузер_на_месте_async() -> bool:
    """Реальный запуск+закрытие браузера - не просто проверка наличия файла
    по executable_path. Причина: этот путь указывает на «обычный» Chromium,
    а headless-запуск (везде в проекте - headless=True, без channel) у
    новых Playwright по умолчанию использует ОТДЕЛЬНЫЙ бинарник
    chromium-headless-shell - executable_path его не видит, из-за чего
    проверка врала «браузер готов», пока реальный запуск падал с
    «Executable doesn't exist» (карты в «Проверке КП» ни разу не звали
    ensure_browser() - на облаке ничего не доставилось вовсе)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        await browser.close()
        return True


last_error: str = ''    # реальный текст последней ошибки - см. ensure_browser()


def _run_async(coro):
    """Запустить корутину, гарантируя ProactorEventLoop на Windows.

    Настоящая причина «браузер не готов» на Windows: Streamlit-сервер (через
    Tornado) переключает asyncio на SelectorEventLoop ДЛЯ ВСЕГО ПРОЦЕССА -
    а SelectorEventLoop на Windows НЕ ПОДДЕРЖИВАЕТ подпроцессы вообще
    (`NotImplementedError` в `_make_subprocess_transport`). Playwright же
    обязательно запускает свой драйвер подпроцессом - без Proactor не
    получится ни в одном месте, где Playwright вызывается ПРЯМО из
    Streamlit-скрипта (а не из отдельного `python ...` процесса - там
    Tornado не участвует, там всё уже работало и без этого обхода)."""
    if sys.platform == 'win32':
        loop = asyncio.ProactorEventLoop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)


def _браузер_на_месте() -> bool:
    """Есть ли уже установленный Chromium у playwright."""
    global last_error
    try:
        ok = _run_async(_браузер_на_месте_async())
        if ok:
            last_error = ''
        return ok
    except Exception as e:  # noqa: BLE001
        # Раньше здесь тихо глотали исключение - пользователь видел только
        # общую заглушку «проверьте packages.txt» без единого намёка на то,
        # что РЕАЛЬНО сломалось. Сохраняем текст - ensure_browser() покажет
        # его в сообщении вместо гадания вслепую.
        last_error = f'{type(e).__name__}: {e}'
        return False


_УСПЕХ: tuple[bool, str] | None = None    # кэшируем ТОЛЬКО успех - см. ниже


def ensure_browser() -> tuple[bool, str]:
    """Гарантирует наличие Chromium. Возвращает (готово, сообщение).

    Кэшируем ТОЛЬКО успешный результат (реальная установка тогда выполняется
    один раз за жизнь процесса). Неудачу НЕ кэшируем - иначе один транзиентный
    сбой (например, при самом первом запуске контейнера) залипал бы навсегда:
    следующая проверка снова видела бы "браузер не готов", даже если Chromium
    к этому моменту прекрасно работает (обёртки-вызовы этой функции в
    forms_check.py/goals_check.py ЕЩЁ и сами кэшируют через st.cache_resource -
    два слоя кэша усугубляли проблему одинаково)."""
    global _УСПЕХ
    if _УСПЕХ is not None:
        return _УСПЕХ

    try:
        import playwright  # noqa: F401
    except Exception:
        return False, ('нет библиотеки playwright (добавьте в requirements.txt '
                       'и перезапустите приложение)')

    if _браузер_на_месте():
        _УСПЕХ = (True, 'браузер готов')
        return _УСПЕХ

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
        _УСПЕХ = (True, 'браузер установлен')
        return _УСПЕХ
    _detail = f' - {last_error}' if last_error else ''
    return False, f'Chromium установлен, но не запускается{_detail} (проверьте packages.txt)'
