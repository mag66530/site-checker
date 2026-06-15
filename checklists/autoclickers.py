"""
Страница «Автокликеры» — фоновый запуск кликеров GSC и Я.Вебмастера.

Запуск фоновый: кнопка стартует отдельный процесс и сразу освобождает
интерфейс. Параллельно можно уйти в чек-листы и работать там — кликер
крутится сам по себе (свой процесс + свой Chrome, ресурсы не конфликтуют).
Прогресс смотри кнопкой «Обновить лог».

Окружение:
  • Локально (streamlit run app.py) — работает.
  • Облако по ссылке — клики пока недоступны (нет браузера пользователя).
  • Свой сервер (в планах) — заработает так же (тот же фоновый запуск).
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable
LOG_FILE = ROOT / 'cache' / 'autoclick.log'
DONE_MARK = '✅ ВСЁ ГОТОВО'

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


def _launch_background(args: list[str], log_path: Path):
    """Запустить процесс в фоне, вывод — в файл. UI не блокируется."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    creationflags = 0
    if os.name == 'nt':
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    f = open(log_path, 'a', encoding='utf-8')
    proc = subprocess.Popen(
        [PY, *args], cwd=str(ROOT),
        stdout=f, stderr=subprocess.STDOUT, env=env,
        creationflags=creationflags,
    )
    return proc.pid


def _run_foreground(args: list[str], title: str):
    """Короткие задачи (открыть браузер) — со стримом вывода."""
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
        out.code('\n'.join(lines[-200:]), language='text')
    proc.wait()


# ── UI ──────────────────────────────────────────────────────────────

st.title('🖱 Автокликеры — GSC и Яндекс.Вебмастер')

st.warning(
    'Кликеры управляют браузером на ЭТОМ компьютере и требуют ручного входа в '
    'Google/Yandex. Работают, когда приложение запущено **локально** '
    '(`streamlit run app.py`). В облаке по ссылке клики пока недоступны. '
    'После переноса на свой сервер — заработают.'
)

if not _playwright_ok():
    st.error(
        'Playwright не установлен — кликеры не запустятся.\n\n'
        'Локально один раз:\n'
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
st.caption('Откроется Chrome. Войди в Google и Yandex аккаунты проекта. '
           'Окно не закрывай — кликеры к нему подключаются.')
if st.button('🌐 Открыть браузер для входа', use_container_width=True):
    _run_foreground(['open_browser.py'], 'Открываю Chrome…')

st.divider()

# ── Шаг 2: что прокликать ───────────────────────────────────────────
st.subheader('Шаг 2. Что прокликать')

do_gsc = st.checkbox('Прокликать ГСК', value=False)
do_wm = st.checkbox('Прокликать Вебмастер', value=False)

st.caption('Запуск фоновый — интерфейс сразу свободен. Можно уйти в чек-листы '
           'и работать параллельно, кликер крутится сам.')

if st.button('Запустить', use_container_width=True):
    if not do_gsc and not do_wm:
        st.info('Отметь хотя бы один пункт выше.')
    else:
        args = ['autoclick_run.py', '--project', pid]
        if do_gsc:
            args.append('--gsc')
        if do_wm:
            args.append('--wm')
        # очищаем лог и стартуем фоном
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE.write_text('', encoding='utf-8')
        except Exception:
            pass
        bg_pid = _launch_background(args, LOG_FILE)
        st.session_state['autoclick_started'] = datetime.now().strftime('%H:%M:%S')
        st.success(f'Запущено в фоне (PID {bg_pid}). Интерфейс свободен — '
                   f'можешь идти в чек-листы. Прогресс ниже по кнопке «Обновить лог».')

st.divider()

# ── Прогресс ────────────────────────────────────────────────────────
st.subheader('Прогресс')
if st.session_state.get('autoclick_started'):
    st.caption(f'Последний запуск: {st.session_state["autoclick_started"]}')

if st.button('🔄 Обновить лог', use_container_width=True):
    pass  # просто перерисовка

if LOG_FILE.exists():
    txt = LOG_FILE.read_text(encoding='utf-8', errors='ignore')
    if txt.strip():
        done = DONE_MARK in txt
        st.markdown('**Статус:** ' + ('✅ завершено' if done else '⏳ идёт…'))
        st.code('\n'.join(txt.splitlines()[-300:]), language='text')
    else:
        st.caption('Лог пуст — кликер ещё не запускали.')
else:
    st.caption('Лог появится после запуска.')
