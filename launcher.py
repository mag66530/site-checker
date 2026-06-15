"""
launcher.py — ЛОКАЛЬНЫЙ интерфейс для автокликеров GSC и Я.Вебмастера.

ВАЖНО: запускается ТОЛЬКО локально на ПК пользователя:
    streamlit run launcher.py

Почему локально: кликеры управляют браузером на этом компьютере (порт 9222)
и требуют ручного входа в Google/Yandex. Облачный Streamlit так не умеет.

Зависимости (один раз):
    pip install -r requirements-local.txt
    playwright install chromium
"""

import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent
PY = sys.executable

# Проект → какие аккаунты использовать (информационно — вход вручную)
PROJECTS = {
    'smu': {
        'name': 'СМУ — Сталметурал',
        'google': 'stalmeturalru@gmail.com',
        'yandex': 'stalmetural19@yandex.ru',
        'domain': 'stalmetural.ru',
    },
    'mpe': {
        'name': 'МПЭ — Mepen',
        'google': 'mepen888@gmail.com',
        'yandex': 'mepen88@yandex.ru',
        'domain': 'mepen.ru',
    },
    'inp': {
        'name': 'ИНП — Inmetprom',
        'google': 'inmetprom77@gmail.com',
        'yandex': 'inmetprom77@yandex.ru',
        'domain': 'inmetprom.ru',
    },
}


def run_stream(args: list[str], title: str):
    """Запустить скрипт и показывать вывод в реальном времени."""
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
    if proc.returncode == 0:
        st.success('Готово')
    else:
        st.warning(f'Завершено с кодом {proc.returncode}')


# ── UI ──────────────────────────────────────────────────────────────

st.set_page_config(page_title='Автокликеры GSC / Вебмастер', page_icon='🖱', layout='wide')
st.title('🖱 Автокликеры — GSC и Яндекс.Вебмастер')

st.info(
    'Это локальный инструмент. Он открывает браузер на ЭТОМ компьютере и требует '
    'ручного входа в Google/Yandex. Запускать только через `streamlit run launcher.py`.'
)

pid = st.selectbox(
    'Проект',
    list(PROJECTS.keys()),
    format_func=lambda k: PROJECTS[k]['name'],
)
proj = PROJECTS[pid]

st.markdown(
    f"Для проекта **{proj['name']}** войди в браузере в аккаунты:\n"
    f"- Google (GSC): `{proj['google']}`\n"
    f"- Yandex (Вебмастер): `{proj['yandex']}`"
)

st.divider()

# ── Шаг 1: браузер и вход ───────────────────────────────────────────
st.subheader('Шаг 1. Открыть браузер и войти')
st.caption('Откроется Chrome (порт 9222). Войди в нужные Google и Yandex аккаунты '
           'этого проекта. Окно не закрывай — кликеры подключаются к нему.')
if st.button('🌐 Открыть браузер для входа', use_container_width=True):
    run_stream(['gsc_save_session.py'], 'Запуск браузера…')

st.divider()

# ── Шаг 2: GSC ──────────────────────────────────────────────────────
st.subheader('Шаг 2. Google Search Console')
st.caption('Сначала обнови список ресурсов (по текущему Google-аккаунту), '
           'затем запусти проверку исправлений по причинам неиндексирования.')

c1, c2 = st.columns(2)
with c1:
    if st.button('🔄 Обновить список ресурсов GSC', use_container_width=True):
        run_stream(['gsc_list_properties.py'], 'Сбор ресурсов GSC…')
with c2:
    gsc_dry = st.checkbox('GSC: dry-run (не кликать)', value=True, key='gsc_dry')
    gsc_filter = st.text_input('Фильтр доменов (опц.)', value=proj['domain'],
                               key='gsc_filter',
                               help='Оставь домен проекта, чтобы обрабатывать только его ресурсы')

if st.button('▶ Запустить GSC: проверить исправления', use_container_width=True, type='primary'):
    args = ['gsc_validate_fixes.py']
    if gsc_dry:
        args.append('--dry-run')
    if gsc_filter.strip():
        args += ['--filter', gsc_filter.strip()]
    run_stream(args, 'GSC: проверка исправлений…')

st.divider()

# ── Шаг 3: Вебмастер ────────────────────────────────────────────────
st.subheader('Шаг 3. Яндекс.Вебмастер')
st.caption('Обходит все сайты текущего Yandex-аккаунта, жмёт «Проверить» по ошибкам.')

wm_dry = st.checkbox('Вебмастер: dry-run (не кликать)', value=True, key='wm_dry')
wm_limit = st.number_input('Лимит сайтов (0 = все)', min_value=0, value=0, step=1, key='wm_limit')

if st.button('▶ Запустить Вебмастер: проверить ошибки', use_container_width=True, type='primary'):
    args = ['webmaster_recheck.py']
    if wm_dry:
        args.append('--dry-run')
    if int(wm_limit) > 0:
        args += ['--limit', str(int(wm_limit))]
    run_stream(args, 'Вебмастер: проверка ошибок…')

st.divider()
st.caption('Логи: gsc_validate_log.json, webmaster_recheck_log.json — в папке проекта.')
