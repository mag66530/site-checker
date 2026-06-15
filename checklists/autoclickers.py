"""
Страница «Автокликеры» — запуск кликеров GSC и Я.Вебмастера прямо из приложения.

ВАЖНО про окружение:
  • Локально (streamlit run app.py на ПК) — работает: открывает браузер на этом
    компьютере, человек логинится, кликеры подключаются и кликают.
  • Облако (Streamlit Cloud по ссылке) — пока НЕ работает: у сервера нет браузера
    пользователя и нельзя пройти ручной вход Google/Yandex.
  • Свой сервер (в планах) — заработает так же, как локально, если на сервере
    установлен Playwright/Chromium и однажды выполнен вход в нужные аккаунты.

Код запуска один и тот же (subprocess + Playwright), поэтому при переносе на
сервер ничего менять не нужно — только окружение.
"""
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable

PROJECTS = {
    'smu': {'name': 'СМУ — Сталметурал', 'google': 'stalmeturalru@gmail.com',
            'yandex': 'stalmetural19@yandex.ru', 'domain': 'stalmetural.ru'},
    'mpe': {'name': 'МПЭ — Mepen', 'google': 'mepen888@gmail.com',
            'yandex': 'mepen88@yandex.ru', 'domain': 'mepen.ru'},
    'imp': {'name': 'ИМП — Инметпром', 'google': 'inmetprom77@gmail.com',
            'yandex': 'inmetprom77@yandex.ru', 'domain': 'inmetprom.ru'},
}


def _playwright_ok() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def run_stream(args: list[str], title: str):
    """Запустить скрипт и стримить вывод в реальном времени."""
    st.markdown(f'**{title}**')
    out = st.empty()
    lines: list[str] = []
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    try:
        proc = subprocess.Popen(
            [PY, *args], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', env=env,
        )
    except Exception as e:
        st.error(f'Не удалось запустить: {e}')
        return
    for line in proc.stdout:
        lines.append(line.rstrip())
        out.code('\n'.join(lines[-300:]), language='text')
    proc.wait()
    (st.success if proc.returncode == 0 else st.warning)(
        'Готово' if proc.returncode == 0 else f'Код выхода {proc.returncode}')


# ── UI ──────────────────────────────────────────────────────────────

st.title('🖱 Автокликеры — GSC и Яндекс.Вебмастер')

st.warning(
    'Кликеры управляют браузером на ЭТОМ компьютере и требуют ручного входа в '
    'Google/Yandex. Поэтому они работают, когда приложение запущено **локально** '
    '(`streamlit run app.py`). В облаке по ссылке клики пока недоступны '
    '(сервер не управляет твоим браузером). После переноса на свой сервер — заработают.'
)

if not _playwright_ok():
    st.error(
        'Playwright не установлен в этом окружении — кликеры не запустятся.\n\n'
        'Локально выполни один раз:\n'
        '```\npip install -r requirements-local.txt\nplaywright install chromium\n```'
    )

pid = st.selectbox('Проект', list(PROJECTS.keys()),
                   format_func=lambda k: PROJECTS[k]['name'])
proj = PROJECTS[pid]
st.markdown(
    f"Войди в браузере в аккаунты проекта **{proj['name']}**:\n"
    f"- Google (GSC): `{proj['google']}`\n"
    f"- Yandex (Вебмастер): `{proj['yandex']}`"
)

st.divider()

# ── Шаг 1: вход ─────────────────────────────────────────────────────
st.subheader('Шаг 1. Открыть браузер и войти')
st.caption('Откроется Chrome (порт 9222). Войди в Google и Yandex аккаунты проекта. '
           'Окно не закрывай — кликеры к нему подключаются.')
if st.button('🌐 Открыть браузер для входа', use_container_width=True):
    run_stream(['gsc_save_session.py'], 'Запуск браузера…')

st.divider()

# ── Шаг 2: что прокликать ───────────────────────────────────────────
st.subheader('Шаг 2. Что прокликать')

do_gsc = st.checkbox('✅ Прокликать ошибки ГСК (проверить исправления)', value=False)
do_wm = st.checkbox('✅ Прокликать ошибки Вебмастера', value=False)
dry = st.checkbox('Сначала проверка без кликов (dry-run)', value=True,
                  help='Покажет что нашёл и где есть кнопки, но не нажмёт. '
                       'Убедился — сними галку и запусти боевой.')

st.caption('ГСК работает по доменам/поддоменам проекта из списка — собирать ничего не надо.')

if st.button('▶ Запустить выбранное', type='primary', use_container_width=True):
    if not do_gsc and not do_wm:
        st.info('Отметь хотя бы один пункт выше.')
    else:
        if do_gsc:
            args = ['gsc_validate_fixes.py', '--project', pid]
            if dry:
                args.append('--dry-run')
            run_stream(args, 'ГСК: проверка исправлений…')
        if do_wm:
            args = ['webmaster_recheck.py']
            if dry:
                args.append('--dry-run')
            run_stream(args, 'Вебмастер: проверка ошибок…')

st.divider()
st.caption('Логи: gsc_validate_log.json, webmaster_recheck_log.json — в папке проекта.')
