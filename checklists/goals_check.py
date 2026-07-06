"""
Страница «Проверка целей» – прогон ВСЕХ целей Яндекс.Метрики проекта.

Эталон – каталог целей из Метрики (catalogs/goals-<проект>.json). Движок
(goals_run.py) открывает страницы сайта, выполняет безопасные действия (клики
по телефонам/почте/соцсетям/кнопкам форм — заявки НЕ отправляются) и слушает
Метрику. Отчёт: по каждой цели — Сработала / НЕ сработала / Прогоном форм /
Авто / Вручную, с деталями (в т.ч. есть ли привязка reachGoal в коде сайта).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable

PROJECTS = {
    'smu': 'СМУ – Стальметурал',
    'imp': 'ИМП – Инметпром',
    'mpe': 'МПЭ – Мепэн',
}

st.title('🎯 Проверка целей')
st.caption('Проверяем ВСЕ цели Яндекс.Метрики проекта: браузер выполняет действия '
           'на сайте (клики по телефонам, почте, соцсетям, кнопкам форм — заявки '
           'НЕ отправляются) и слушает, какие цели фиксирует Метрика. Цели '
           'отправки форм проверяются страницей «Проверка форм» и подтягиваются '
           'из её последнего отчёта.')

pid = st.selectbox('Проект', list(PROJECTS), format_func=lambda k: PROJECTS[k])

CAT = ROOT / 'catalogs' / f'goals-{pid}.json'
WORK = ROOT / 'cache' / 'goals' / pid
LOG_FILE = WORK / 'run.log'
PID_FILE = WORK / 'run.pid'
REPORT = WORK / 'goals_report.xlsx'

if not CAT.is_file():
    st.warning(f'Каталог целей для проекта не загружен (catalogs/goals-{pid}.json). '
               'Пришлите выгрузку страницы «Конверсии» из Метрики.')
    st.stop()

каталог = json.loads(CAT.read_text(encoding='utf-8'))
st.markdown(f"**{каталог.get('проект','')}** · счётчик `{каталог.get('счётчик','')}` · "
            f"целей в каталоге: **{len(каталог.get('цели', []))}** "
            f"({каталог.get('источник','')})")


def _pid_alive(p):
    if not p:
        return False
    try:
        if os.name == 'nt':
            out = subprocess.run(['tasklist', '/FI', f'PID eq {p}'],
                                 capture_output=True, text=True)
            return str(p) in out.stdout
        os.kill(int(p), 0)
        return True
    except Exception:
        return False


def _read_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


_alive = _pid_alive(_read_pid())

c1, c2 = st.columns([3, 1])
with c1:
    if st.button('▶ Запустить проверку целей', use_container_width=True,
                 disabled=_alive):
        WORK.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text('', encoding='utf-8')
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        f = open(LOG_FILE, 'a', encoding='utf-8')
        proc = subprocess.Popen([PY, 'goals_run.py', '--project', pid],
                                cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                                env=env, creationflags=flags)
        PID_FILE.write_text(str(proc.pid), encoding='utf-8')
        st.session_state['goals_started'] = datetime.now().strftime('%H:%M:%S')
        st.rerun()
with c2:
    if st.button('⛔ Отменить', use_container_width=True, disabled=not _alive):
        try:
            p = _read_pid()
            if p:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(p)],
                                   capture_output=True)
                else:
                    os.kill(p, 9)
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        st.rerun()

st.subheader('Прогресс')
log_txt = ''
if LOG_FILE.is_file():
    try:
        log_txt = LOG_FILE.read_text(encoding='utf-8')
    except Exception:
        log_txt = ''
if _alive:
    st.markdown('**Статус:** ⏳ идёт проверка… (страница обновляется сама)')
elif 'ВСЁ ГОТОВО' in log_txt:
    st.markdown('**Статус:** ✅ завершено')
elif log_txt:
    st.markdown('**Статус:** ⛔ остановлено / прервано')
else:
    st.markdown('**Статус:** — ещё не запускалось')

if log_txt:
    tail = '\n'.join(log_txt.splitlines()[-25:])
    st.code(tail or ' ', language=None)

if REPORT.is_file() and not _alive:
    st.subheader('Результаты (Excel)')
    st.caption('Листы: «Сводка» (что открывали, что сработало) и «Цели Метрики» — '
               'по строке на каждую цель со статусом и пояснением.')
    st.download_button(
        '⬇ Скачать отчёт по целям',
        data=REPORT.read_bytes(),
        file_name=f'Цели-{PROJECTS[pid].split(" ")[0]}-{datetime.now().strftime("%d.%m.%Y")}.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )

if _alive:
    time.sleep(3)
    st.rerun()
