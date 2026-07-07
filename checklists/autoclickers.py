"""
Страница «Автокликеры» - фоновый запуск кликеров GSC и Я.Вебмастера.

Запуск фоновый: кнопка стартует отдельный процесс и сразу освобождает
интерфейс. Параллельно можно уйти в чек-листы и работать там - кликер
крутится сам по себе (свой процесс + свой Chrome, ресурсы не конфликтуют).
Прогресс смотри кнопкой «Обновить лог».

Окружение:
  • Локально (streamlit run app.py) - работает.
  • Облако по ссылке - клики пока недоступны (нет браузера пользователя).
  • Свой сервер (в планах) - заработает так же (тот же фоновый запуск).
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
PID_FILE = ROOT / 'cache' / 'autoclick.pid'
DONE_MARK = '✅ ВСЁ ГОТОВО'


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

PROJECTS = {
    'smu': {'name': 'СМУ - Стальметурал', 'google': 'stalmeturalru@gmail.com',
            'yandex': 'stalmetural19@yandex.ru', 'domain': 'stalmetural.ru'},
    'mpe': {'name': 'МПЭ - Mepen', 'google': 'mepen888@gmail.com',
            'yandex': 'mepen88@yandex.ru', 'domain': 'mepen.ru'},
    'imp': {'name': 'ИМП - Инметпром', 'google': 'inmetprom77@gmail.com',
            'yandex': 'inmetprom77@yandex.ru', 'domain': 'inmetprom.ru'},
}


def _playwright_ok() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _cdp_alive(host='127.0.0.1', port=9222, timeout=1.0) -> bool:
    """Есть ли локальный залогиненный Chrome (CDP 9222)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _session_secret():
    """base64-сессия для облачного режима из Streamlit Secrets (или None)."""
    try:
        from autoclick_browser import SESSION_SECRET_KEY
        if hasattr(st, 'secrets') and SESSION_SECRET_KEY in st.secrets:
            return str(st.secrets[SESSION_SECRET_KEY])
    except Exception:
        pass
    return None


def _launch_background(args: list[str], log_path: Path, extra_env: dict = None):
    """Запустить процесс в фоне, вывод - в файл. UI не блокируется."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    if extra_env:
        env.update(extra_env)
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


def _run_foreground(args: list[str], title: str):
    """Короткие задачи (открыть браузер) - со стримом вывода."""
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

st.title('🖱 Автокликеры - GSC и Яндекс.Вебмастер')

_cdp = _cdp_alive()
_cloud_session = _session_secret()
if _cdp:
    st.success('Режим: **локальный** - найден залогиненный Chrome (CDP 9222). '
               'Клики пойдут через него, как обычно.')
elif _cloud_session:
    st.info('Режим: **облачный** - локального Chrome нет, но в Secrets есть '
            'сессия (autoclick_session). Клики пойдут через headless-браузер '
            'с этой сессией. Если сессия протухла - кликер напишет в лог, '
            'тогда пере-экспортируй её локально (Шаг 1).')
else:
    st.warning(
        'Локального Chrome нет (CDP 9222) и сессии в Secrets нет '
        '(autoclick_session). Два пути:\n'
        '1. **Локально**: открой браузер для входа (Шаг 1) и запускай как раньше.\n'
        '2. **Облако**: локально экспортируй сессию (кнопка в Шаге 1) и положи '
        'строку в Streamlit Secrets ключом `autoclick_session` - после этого '
        'клики работают прямо из облака.'
    )

if not _playwright_ok():
    st.error(
        'Playwright не установлен - кликеры не запустятся.\n\n'
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
st.subheader('Шаг 1. Открыть браузер и войти (локально)')
st.caption('Откроется Chrome. Войди в Google и Yandex аккаунты проекта. '
           'Окно не закрывай - кликеры к нему подключаются.')
if st.button('🌐 Открыть браузер для входа', use_container_width=True):
    _run_foreground(['open_browser.py'], 'Открываю Chrome…')

st.caption('Для ОБЛАЧНЫХ кликов: когда вошёл в аккаунты - выгрузи сессию '
           'кнопкой ниже и положи строку в Streamlit Secrets ключом '
           '`autoclick_session`. Кнопка работает только локально '
           '(нужен Chrome на 9222).')
if st.button('💾 Экспорт сессии для облака', use_container_width=True,
             disabled=not _cdp):
    _run_foreground(['session_export.py'], 'Экспортирую сессию…')
    _b64_file = ROOT / 'cache' / 'autoclick_session.b64'
    if _b64_file.exists():
        st.caption('Скопируй строку ниже в Streamlit Secrets → '
                   '`autoclick_session = "<строка>"`:')
        st.code(_b64_file.read_text(encoding='utf-8'), language='text')

st.divider()

# ── Шаг 2: что прокликать ───────────────────────────────────────────
st.subheader('Шаг 2. Что прокликать')

do_gsc = st.checkbox('Прокликать ГСК', value=False)
do_wm = st.checkbox('Прокликать Вебмастер', value=False)

st.caption('Запуск фоновый - интерфейс сразу свободен. Можно уйти в чек-листы '
           'и работать параллельно, кликер крутится сам.')

_alive = _pid_alive(_read_pid())

_run_col, _cancel_col = st.columns([3, 1])
with _run_col:
    if st.button('Запустить', use_container_width=True, disabled=_alive):
        if not do_gsc and not do_wm:
            st.info('Отметь хотя бы один пункт выше.')
        elif not _cdp and not _cloud_session:
            st.error('Нет ни локального Chrome (9222), ни сессии в Secrets - '
                     'кликерам не через что работать. См. подсказку сверху.')
        else:
            args = ['autoclick_run.py', '--project', pid]
            if do_gsc:
                args.append('--gsc')
            if do_wm:
                args.append('--wm')
            # Режим: локальный Chrome в приоритете; нет его - облачный
            # headless с сессией из Secrets.
            extra_env = None
            if not _cdp and _cloud_session:
                try:
                    from autoclick_browser import (
                        session_file_from_secret, MODE_ENV, SESSION_FILE_ENV)
                    extra_env = {MODE_ENV: 'cloud',
                                 SESSION_FILE_ENV:
                                     session_file_from_secret(_cloud_session)}
                except Exception as e:
                    st.error(f'Сессия из Secrets не читается: {e}. '
                             f'Пере-экспортируй её локально.')
                    st.stop()
            try:
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOG_FILE.write_text('', encoding='utf-8')
            except Exception:
                pass
            bg_pid = _launch_background(args, LOG_FILE, extra_env)
            st.session_state['autoclick_started'] = datetime.now().strftime('%H:%M:%S')
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
if st.session_state.get('autoclick_started'):
    st.caption(f'Последний запуск: {st.session_state["autoclick_started"]}')

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
        st.caption('Лог пуст - кликер ещё не запускали.')
else:
    st.caption('Лог появится после запуска.')
