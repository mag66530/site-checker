"""
session_export.py - экспорт сессии залогиненного Chrome для ОБЛАЧНЫХ
автокликеров.

Запускается ЛОКАЛЬНО, когда открыт залогиненный Chrome (кнопка «Открыть
браузер для входа» на вкладке «Автокликеры», вход в Яндекс и Google
АККАУНТОВ ЭТОГО ПРОЕКТА выполнен):

    python session_export.py --project smu

Что делает:
  1. Подключается к Chrome (CDP 9222) и забирает cookies Яндекса и Google.
  2. Пишет base64-строку в cache/autoclick_session_<проект>.b64.
  3. Печатает инструкцию: строку скопировать в Streamlit Secrets ключом
     autoclick_session_<проект> - после этого автокликеры работают в облаке.

У КАЖДОГО проекта свои аккаунты - экспортируй сессию отдельно для каждого:
войди в аккаунты проекта → экспорт → выйди → войди в аккаунты следующего →
экспорт. Секреты разные (autoclick_session_smu / _mpe / _imp) - не затирают
друг друга.

Сессию нужно пере-экспортировать, когда она протухнет (Яндекс - месяцы,
Google - может чаще): кликер в облаке напишет об этом в лог.
"""
import argparse
import base64
import json
import sys
from pathlib import Path

from autoclick_browser import CDP_URL, SESSION_SECRET_KEY

# Домены, чьи cookies нужны кликерам (Вебмастер + GSC). Остальное не тащим -
# секрет меньше, чужого в облако не уезжает.
_KEEP = ('yandex', 'google', 'gstatic', 'ya.ru')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', default='',
                    help='id проекта (smu/mpe/imp) - свой секрет на проект')
    a = ap.parse_args()
    _suffix = f'_{a.project}' if a.project else ''
    out_file = (Path(__file__).parent / 'cache'
                / f'autoclick_session{_suffix}.b64')
    secret_key = f'{SESSION_SECRET_KEY}{_suffix}'

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
    out_file.parent.mkdir(exist_ok=True)
    out_file.write_text(b64, encoding='utf-8')

    print(f'✓ Сессия выгружена: cookies Яндекса {ya}, Google {goog}')
    print(f'✓ Файл: {out_file}')
    print(f'✓ Размер секрета: {len(b64) // 1024 + 1} КБ')
    print()
    print('ДАЛЬШЕ (один раз, потом только при протухании):')
    print('  1. Открой настройки приложения на Streamlit Cloud → Secrets.')
    print(f'  2. Добавь строку:  {secret_key} = "<содержимое файла>"')
    print('  3. Сохрани - облачные автокликеры заработают.')
    if a.project:
        print(f'  (у каждого проекта свой секрет: {SESSION_SECRET_KEY}_smu / '
              f'_mpe / _imp)')


if __name__ == '__main__':
    main()
