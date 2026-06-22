"""
Страница «Проверка форм» — фоновый прогон отправки форм на сайтах проекта.

Сделана по образцу страницы «Автокликеры»: кнопка стартует отдельный процесс
(forms_run.py) и сразу освобождает интерфейс. Движок открывает реальный Chrome
(Playwright), заполняет формы и отправляет, результат пишется в log_forms.xlsx.

Окружение:
  • Локально (streamlit run app.py) — работает.
  • Облако по ссылке — недоступно (нет браузера на сервере).
  • Свой сервер (в планах) — заработает так же.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable
LOG_FILE = ROOT / 'cache' / 'forms.log'
PID_FILE = ROOT / 'cache' / 'forms.pid'

PROJECTS = {
    'smu': {'name': 'СМУ – Сталметурал', 'domain': 'stalmetural.ru'},
    'imp': {'name': 'ИМП – Инметпром', 'domain': 'inmetprom.ru'},
    'mpe': {'name': 'МПЭ – Мепэн', 'domain': 'mepen.ru'},
}


def _read_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == 'nt':
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                 capture_output=True, text=True).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill_tree(pid):
    if not pid:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                       capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


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
    try:
        PID_FILE.write_text(str(proc.pid), encoding='utf-8')
    except Exception:
        pass
    return proc.pid


# ── UI ──────────────────────────────────────────────────────────────

st.title('📝 Проверка форм')

st.warning(
    'Проверка открывает реальный браузер (Playwright) на ЭТОМ компьютере: '
    'заполняет формы на сайтах проекта и отправляет заявки. Работает, когда '
    'приложение запущено **локально** (`streamlit run app.py`). В облаке по '
    'ссылке недоступно. После переноса на свой сервер — заработает.'
)

if not _playwright_ok():
    st.error(
        'Playwright не установлен — проверка не запустится.\n\n'
        'Локально один раз:\n'
        '```\npip install -r requirements-local.txt\nplaywright install chromium\n```'
    )

pid_key = st.selectbox('Проект', list(PROJECTS.keys()),
                       format_func=lambda k: PROJECTS[k]['name'])
proj = PROJECTS[pid_key]
st.markdown(
    f"Будут проверены формы сайта **{proj['name']}** (`{proj['domain']}`): "
    'обратная связь, заявки, расчёты, оформление заказа и т.п. — по настройкам '
    'проекта.'
)

st.divider()

# ── Запуск ──────────────────────────────────────────────────────────
st.subheader('Запуск проверки')

clear_log = st.checkbox('Очищать лог Excel перед прогоном', value=True)

st.caption('Запуск фоновый — интерфейс сразу свободен. Можно уйти в чек-листы '
           'и работать параллельно, проверка крутится сама. Заявки '
           'отправляются по-настоящему (формы оформления заказа — без отправки).')

_alive = _pid_alive(_read_pid())

_run_col, _cancel_col = st.columns([3, 1])
with _run_col:
    if st.button('▶ Запустить проверку', use_container_width=True,
                 disabled=_alive):
        args = ['forms_run.py', '--project', pid_key]
        if not clear_log:
            args.append('--no-clear-excel')
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE.write_text('', encoding='utf-8')
        except Exception:
            pass
        _launch_background(args, LOG_FILE)
        st.session_state['forms_started'] = datetime.now().strftime('%H:%M:%S')
        st.session_state['forms_project'] = pid_key
        st.rerun()
with _cancel_col:
    if st.button('⛔ Отменить', use_container_width=True, disabled=not _alive):
        _kill_tree(_read_pid())
        try:
            PID_FILE.unlink(missing_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as _f:
                _f.write('\n⛔ ОТМЕНЕНО пользователем\n')
        except Exception:
            pass
        st.rerun()

st.divider()

# ── Прогресс ────────────────────────────────────────────────────────
st.subheader('Прогресс')
if st.session_state.get('forms_started'):
    st.caption(f'Последний запуск: {st.session_state["forms_started"]}')

st.button('🔄 Обновить лог', use_container_width=True)  # просто перерисовка

if _alive:
    st.markdown('**Статус:** ⏳ идёт…')
elif LOG_FILE.exists() and LOG_FILE.read_text(encoding='utf-8', errors='ignore').strip():
    st.markdown('**Статус:** ✅ завершено / остановлено')

if LOG_FILE.exists():
    txt = LOG_FILE.read_text(encoding='utf-8', errors='ignore')
    if txt.strip():
        st.code('\n'.join(txt.splitlines()[-300:]), language='text')
    else:
        st.caption('Лог пуст — проверку ещё не запускали.')
else:
    st.caption('Лог появится после запуска.')

# ── Результат: Excel ────────────────────────────────────────────────
_proj_for_xlsx = st.session_state.get('forms_project', pid_key)
xlsx = ROOT / 'cache' / 'forms' / _proj_for_xlsx / 'log_forms.xlsx'
if xlsx.exists():
    st.divider()
    st.subheader('Результаты (Excel)')
    st.caption(f'Лог проекта {PROJECTS[_proj_for_xlsx]["name"]} '
               '— колонки: дата, страница, форма, статус, код ответа и т.д.')
    st.download_button(
        '⬇ Скачать log_forms.xlsx',
        data=xlsx.read_bytes(),
        file_name=f'log_forms_{_proj_for_xlsx}.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        use_container_width=True,
    )
