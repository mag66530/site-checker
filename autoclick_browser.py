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


# Поля cookie, которые принимает Playwright в storage_state. Chrome в свежих
# версиях кладёт в экспорт ещё partitionKey и _crHasCrossSiteAncestor - на них
# запуск падает с «Protocol error (Storage.setCookies): Invalid cookie fields»,
# и вся сессия оказывается непригодной. Лишнее отбрасываем.
_COOKIE_FIELDS = ('name', 'value', 'domain', 'path', 'expires',
                  'httpOnly', 'secure', 'sameSite')
_SAME_SITE = ('Strict', 'Lax', 'None')


def sanitize_state(state: dict) -> dict:
    """storage_state → storage_state, пригодный для Playwright."""
    cookies = []
    for c in (state or {}).get('cookies') or []:
        чистая = {k: c[k] for k in _COOKIE_FIELDS if k in c}
        if not чистая.get('name') or 'value' not in чистая:
            continue
        # sameSite строго из трёх значений: Chrome пишет и 'unspecified'/None.
        if чистая.get('sameSite') not in _SAME_SITE:
            чистая['sameSite'] = 'Lax'
        # expires - число; сессионная cookie помечается -1.
        try:
            чистая['expires'] = float(чистая.get('expires', -1))
        except (TypeError, ValueError):
            чистая['expires'] = -1
        cookies.append(чистая)
    return {'cookies': cookies, 'origins': (state or {}).get('origins') or []}


def session_file_from_secret(b64: str) -> str:
    """base64-секрет → временный файл storage_state. Возвращает путь.
    Бросает исключение, если секрет не декодируется/не JSON."""
    data = base64.b64decode((b64 or '').strip())
    state = sanitize_state(json.loads(data))
    f = tempfile.NamedTemporaryFile('w', suffix='_autoclick_session.json',
                                    delete=False, encoding='utf-8')
    json.dump(state, f, ensure_ascii=False)
    f.close()
    return f.name


async def open_browser(p, log=None):
    """Открыть браузер по режиму. Возвращает (browser, page).

    p - активный async_playwright. Ошибки бросаем наружу - вызывающий
    скрипт пишет их в свой лог."""
    def _log(msg):
        if log:
            log(msg)

    if is_cloud_mode():
        # НЕ browser_setup.ensure_browser: он открывает sync_playwright, что
        # внутри asyncio-цикла падает. Путь Chromium берём у уже открытого
        # async-playwright, доустанавливаем subprocess-ом при необходимости.
        _path = None
        try:
            _path = p.chromium.executable_path
        except Exception:
            pass
        if not (_path and os.path.exists(_path)):
            import subprocess
            import sys
            _log('Chromium не найден - доустанавливаю (~1 мин)…')
            try:
                subprocess.run(
                    [sys.executable, '-m', 'playwright', 'install', 'chromium'],
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
                'и вставь строку в «Настройки проекта» → «Сессия браузера»')
        # Файл мог быть записан не нами (старый экспорт, ручная правка) -
        # приводим к формату Playwright, иначе new_context падает на первом же
        # неизвестном поле cookie и сессия целиком считается негодной.
        try:
            with open(state, encoding='utf-8') as _f:
                _чистый = sanitize_state(json.load(_f))
            with open(state, 'w', encoding='utf-8') as _f:
                json.dump(_чистый, _f, ensure_ascii=False)
        except Exception as _e:
            raise RuntimeError(f'сессия не читается: {_e}') from _e
        browser = await p.chromium.launch(headless=True, args=[
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox', '--disable-dev-shm-usage',
        ])
        ctx = await browser.new_context(
            storage_state=state, user_agent=UA, locale='ru-RU',
            viewport={'width': 1440, 'height': 900},
            timezone_id='Europe/Moscow',
        )
        # navigator.webdriver=true выдаёт автоматизацию - прячем
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined})")
        page = await ctx.new_page()
        _log('Облачный браузер: headless Chromium + сессия из секрета')
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
