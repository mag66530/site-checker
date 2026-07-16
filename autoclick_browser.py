"""
autoclick_browser.py - общий запуск браузера для автокликеров (ГСК/Вебмастер).

Два режима:
  • ЛОКАЛЬНЫЙ (по умолчанию): подключение к твоему залогиненному Chrome
    через CDP 9222 - как было всегда.
  • ОБЛАЧНЫЙ (env AUTOCLICK_MODE=cloud): headless Chromium от Playwright +
    сессия (cookies) из файла AUTOCLICK_SESSION_FILE. Сессия экспортируется
    ЛОКАЛЬНО скриптом session_export.py (из твоего залогиненного Chrome) и
    кладётся в Streamlit Secrets ключом autoclick_session (base64).

Облачный браузер маскируется под обычный Chrome (UA, webdriver=undefined,
русская локаль/таймзона) - Яндекс к этому терпим; Google строже, сессия
может слетать чаще (тогда пере-экспортировать).
"""
import base64
import json
import os
import tempfile

CDP_URL = 'http://127.0.0.1:9222'
MODE_ENV = 'AUTOCLICK_MODE'                 # 'cloud' | (пусто = локальный CDP)
SESSION_FILE_ENV = 'AUTOCLICK_SESSION_FILE'  # путь к storage_state.json
SESSION_SECRET_KEY = 'autoclick_session'     # имя секрета в Streamlit

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def is_cloud_mode() -> bool:
    return os.environ.get(MODE_ENV, '').strip().lower() == 'cloud'


def session_file_from_secret(b64: str) -> str:
    """base64-секрет → временный файл storage_state. Возвращает путь.
    Бросает исключение, если секрет не декодируется/не JSON."""
    data = base64.b64decode((b64 or '').strip())
    json.loads(data)                          # валидация формата
    f = tempfile.NamedTemporaryFile('wb', suffix='_autoclick_session.json',
                                    delete=False)
    f.write(data)
    f.close()
    return f.name


async def open_browser(p, log=None, engine='chromium'):
    """Открыть браузер по режиму. Возвращает (browser, page).

    p - активный async_playwright. engine - 'chromium' (по умолчанию, для всех
    проверок) или 'firefox' (легче по памяти - для отдельных проверок на
    бесплатном облаке, где Chromium падает по памяти; включается точечно
    вызывающим кодом). Ошибки бросаем наружу - вызывающий скрипт пишет их в
    свой лог."""
    def _log(msg):
        if log:
            log(msg)

    if is_cloud_mode():
        eng = 'firefox' if str(engine).lower() == 'firefox' else 'chromium'
        bt = p.firefox if eng == 'firefox' else p.chromium
        # НЕ browser_setup.ensure_browser: он открывает sync_playwright, что
        # внутри asyncio-цикла падает. Путь берём у уже открытого async-
        # playwright, доустанавливаем subprocess-ом при необходимости.
        _path = None
        try:
            _path = bt.executable_path
        except Exception:
            pass
        if not (_path and os.path.exists(_path)):
            import subprocess
            import sys
            _log(f'{eng} не найден - доустанавливаю (~1 мин)…')
            try:
                subprocess.run(
                    [sys.executable, '-m', 'playwright', 'install', eng],
                    check=True, capture_output=True, text=True, timeout=900)
            except Exception as e:
                detail = getattr(e, 'stderr', '') or str(e)
                raise RuntimeError(
                    f'браузер в облаке не готов: {str(detail)[:300]}')
        state = os.environ.get(SESSION_FILE_ENV, '')
        if not (state and os.path.exists(state)):
            raise RuntimeError(
                'нет файла сессии. Экспортируй сессию локально '
                '(кнопка на вкладке «Автокликеры» или session_export.py) '
                f'и положи в Streamlit Secrets: {SESSION_SECRET_KEY}')
        if eng == 'firefox':
            # Firefox легче по памяти (важно на бесплатном Streamlit).
            # Chromium-аргументы (--no-sandbox и т.п.) ему не нужны.
            browser = await p.firefox.launch(headless=True)
        else:
            browser = await p.chromium.launch(headless=True, args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox', '--disable-dev-shm-usage',
            ])
        # UA-маску ставим только Chromium; Firefox пусть шлёт свой честный UA
        # (Chrome-UA на движке Firefox выглядит подозрительно для анти-бота).
        _ctx = dict(storage_state=state, locale='ru-RU',
                    viewport={'width': 1440, 'height': 900},
                    timezone_id='Europe/Moscow')
        if eng == 'chromium':
            _ctx['user_agent'] = UA
        ctx = await browser.new_context(**_ctx)
        # navigator.webdriver=true выдаёт автоматизацию - прячем
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined})")
        page = await ctx.new_page()
        _log(f'Облачный браузер: headless {eng} + сессия из секрета')
        return browser, page

    # Локальный режим: твой залогиненный Chrome (CDP 9222).
    # ВАЖНО: подключение к 127.0.0.1 не должно ходить через внешний прокси -
    # если в окружении консоли остался HTTP(S)_PROXY (например, задавали
    # для git push), CDP-запрос уходил на прокси и падал с 407. Чистим
    # прокси-переменные процесса: кликер сам в сеть из Python не ходит
    # (только CDP к локальному Chrome; сам Chrome - со своими настройками).
    for _v in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
               'ALL_PROXY', 'all_proxy'):
        os.environ.pop(_v, None)
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
    browser = await p.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return browser, page
