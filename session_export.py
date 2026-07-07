"""
session_export.py - экспорт сессии залогиненного Chrome для ОБЛАЧНЫХ
автокликеров.

Запускается ЛОКАЛЬНО, когда открыт залогиненный Chrome (кнопка «Открыть
браузер для входа» на вкладке «Автокликеры», вход в Яндекс и Google выполнен):

    python session_export.py

Что делает:
  1. Подключается к Chrome (CDP 9222) и забирает cookies Яндекса и Google.
  2. Пишет base64-строку в cache/autoclick_session.b64.
  3. Печатает инструкцию: строку скопировать в Streamlit Secrets ключом
     autoclick_session - после этого автокликеры работают в облаке.

Сессию нужно пере-экспортировать, когда она протухнет (Яндекс - месяцы,
Google - может чаще): кликер в облаке напишет об этом в лог.
"""
import base64
import json
import sys
from pathlib import Path

from autoclick_browser import CDP_URL, SESSION_SECRET_KEY

OUT_FILE = Path(__file__).parent / 'cache' / 'autoclick_session.b64'

# Домены, чьи cookies нужны кликерам (Вебмастер + GSC). Остальное не тащим -
# секрет меньше, чужого в облако не уезжает.
_KEEP = ('yandex', 'google', 'gstatic', 'ya.ru')


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('pip install playwright')
        sys.exit(1)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f'Нет подключения к Chrome ({CDP_URL}): {e}')
            print('Сначала открой браузер для входа (вкладка «Автокликеры» → '
                  '«Открыть браузер для входа») и войди в Яндекс и Google.')
            sys.exit(1)
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            print('В Chrome нет открытого контекста - открой хотя бы одну вкладку.')
            sys.exit(1)
        state = ctx.storage_state()

    cookies = [c for c in state.get('cookies', [])
               if any(k in (c.get('domain') or '').lower() for k in _KEEP)]
    ya = sum(1 for c in cookies if 'yandex' in (c.get('domain') or '').lower()
             or 'ya.ru' in (c.get('domain') or '').lower())
    goog = len(cookies) - ya
    if not cookies:
        print('Cookies Яндекса/Google не найдены - ты вошёл в аккаунты?')
        sys.exit(1)

    payload = json.dumps({'cookies': cookies, 'origins': []},
                         ensure_ascii=False).encode('utf-8')
    b64 = base64.b64encode(payload).decode('ascii')
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(b64, encoding='utf-8')

    print(f'✓ Сессия выгружена: cookies Яндекса {ya}, Google {goog}')
    print(f'✓ Файл: {OUT_FILE}')
    print(f'✓ Размер секрета: {len(b64) // 1024 + 1} КБ')
    print()
    print('ДАЛЬШЕ (один раз, потом только при протухании):')
    print('  1. Открой настройки приложения на Streamlit Cloud → Secrets.')
    print(f'  2. Добавь строку:  {SESSION_SECRET_KEY} = "<содержимое файла>"')
    print('  3. Сохрани - облачные автокликеры заработают.')


if __name__ == '__main__':
    main()
