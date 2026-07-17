"""
Страница «Вход в Google» - вход в Search Console по скриншотам (для облака).

Зачем: числа «Количество страниц в ГСК» (проиндексировано / просканировано-не-
индексировано / сумма) есть только в интерфейсе Search Console, а автоматический
вход бота Google блокирует. Здесь человек проходит вход РУКАМИ, но через
скриншоты: тул на облаке держит браузер и шлёт картинку страницы входа, ты
вводишь то, что просит Google (логин / пароль / код), тул печатает это в браузер.
Google пускает - вход проходит живой человек. На выходе сохраняется сессия, и
дальше проверка «Количество страниц в ГСК» снимается сама, без ручного ввода.

Движок - gsc_login_helper.py (отдельный фоновый процесс). Общение через файлы в
cache/gsc_login/<pid>/ (screen.png, status.json, input.txt, action.txt,
session.b64, stop.flag). Готовую сессию runner подхватывает автоматически, пока
жив контейнер; чтобы пережить перезапуск - строку можно положить в Streamlit
Secrets ключом autoclick_session_<pid>.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable

PROJECTS = {
    'smu': 'СМУ - Стальметурал',
    'imp': 'ИМП - Инметпром',
    'mpe': 'МПЭ - Mepen',
}


def _dir(pid: str) -> Path:
    d = ROOT / 'cache' / 'gsc_login' / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file(pid: str) -> Path:
    return _dir(pid) / 'helper.pid'


def _read_helper_pid(pid: str):
    try:
        return int(_pid_file(pid).read_text().strip())
    except Exception:
        return None


def _pid_alive(proc_pid) -> bool:
    if not proc_pid:
        return False
    if os.name == 'nt':
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {proc_pid}'],
                                 capture_output=True, text=True).stdout
            return str(proc_pid) in out
        except Exception:
            return False
    try:
        os.waitpid(proc_pid, os.WNOHANG)      # прибрать зомби
    except Exception:
        pass
    try:
        os.kill(proc_pid, 0)
        return True
    except Exception:
        return False


def _status(pid: str) -> dict:
    try:
        return json.loads((_dir(pid) / 'status.json').read_text(encoding='utf-8'))
    except Exception:
        return {}


def _screen_bytes(pid: str):
    f = _dir(pid) / 'screen.png'
    try:
        if f.exists():
            return f.read_bytes()
    except Exception:
        pass
    return None


def _session_b64(pid: str):
    f = _dir(pid) / 'session.b64'
    try:
        if f.exists():
            s = f.read_text(encoding='utf-8').strip()
            return s or None
    except Exception:
        pass
    return None


def _write_input(pid: str, text: str):
    try:
        (_dir(pid) / 'input.txt').write_text(text, encoding='utf-8')
    except Exception:
        pass


def _write_action(pid: str, action: str):
    try:
        (_dir(pid) / 'action.txt').write_text(action, encoding='utf-8')
    except Exception:
        pass


def _stop(pid: str):
    try:
        (_dir(pid) / 'stop.flag').write_text('1', encoding='utf-8')
    except Exception:
        pass
    p = _read_helper_pid(pid)
    if p and os.name != 'nt':
        import signal
        try:
            os.kill(p, signal.SIGTERM)
        except Exception:
            pass


def _reset(pid: str):
    """Очистить сигналы/результат для нового входа."""
    for f in ('input.txt', 'action.txt', 'session.b64', 'stop.flag',
              'status.json', 'screen.png', 'helper.pid', 'helper.log'):
        try:
            (_dir(pid) / f).unlink(missing_ok=True)
        except Exception:
            pass


def _launch(pid: str) -> int:
    _reset(pid)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    creationflags = 0
    if os.name == 'nt':
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    logf = open(_dir(pid) / 'helper.log', 'a', encoding='utf-8')
    proc = subprocess.Popen(
        [PY, 'gsc_login_helper.py', '--project', pid], cwd=str(ROOT),
        stdout=logf, stderr=subprocess.STDOUT, env=env,
        creationflags=creationflags)
    try:
        _pid_file(pid).write_text(str(proc.pid), encoding='utf-8')
    except Exception:
        pass
    return proc.pid


_PHASE_RU = {
    'start': '⏳ Запускаю браузер…',
    'login': '🔐 Вход в Google - нужен твой ввод',
    'done': '✅ Вход выполнен, сессия сохранена',
    'stopped': '⛔ Остановлено',
    'error': '⚠ Ошибка',
}


# ── UI ──────────────────────────────────────────────────────────────
st.title('🔐 Вход в Google (по скриншотам)')

st.caption(
    'Нужен один раз, чтобы снять «Количество страниц в ГСК» без ручного ввода. '
    'Тул откроет вход в Search Console на облаке и будет присылать скриншоты - '
    'вводишь, что просит Google (почту, пароль, код). После входа сессия '
    'сохраняется, и проверка считает страницы сама.')

pid = st.selectbox('Проект', list(PROJECTS.keys()),
                   format_func=lambda k: PROJECTS[k])

_helper_running = _pid_alive(_read_helper_pid(pid))
_ready = _session_b64(pid)
_stt = _status(pid)
_phase = _stt.get('phase', '')

st.divider()

# ── Готовая сессия ──────────────────────────────────────────────────
if _ready and _phase != 'login':
    st.success('Вход выполнен - сессия для этого проекта сохранена. '
               'Теперь запусти «Количество страниц в ГСК» в чек-листе: '
               'проверка возьмёт эту сессию сама, вводить ничего не нужно.')
    with st.expander('Сохранить сессию надолго (переживёт перезапуск облака)'):
        st.caption(
            'Сессия живёт, пока работает контейнер Streamlit. Чтобы не входить '
            'заново после «засыпания» приложения, скопируй строку ниже в '
            f'Settings → Secrets ключом `autoclick_session_{pid}`:')
        st.code(f'autoclick_session_{pid} = "{_ready}"', language='toml')
    if st.button('🔁 Войти заново', use_container_width=True):
        _reset(pid)
        _launch(pid)
        st.rerun()
    st.stop()

# ── Активный вход ───────────────────────────────────────────────────
if _helper_running or _phase in ('start', 'login', 'error'):
    _top = st.container()
    with _top:
        _c1, _c2 = st.columns([3, 1])
        with _c1:
            st.markdown(f'**Статус:** {_PHASE_RU.get(_phase, _phase or "…")}')
        with _c2:
            if st.button('⛔ Остановить', use_container_width=True):
                _stop(pid)
                st.rerun()

    prompt = _stt.get('prompt') or ''
    if prompt and _helper_running:
        st.info(prompt)

    if not _helper_running:
        # Процесс входа завершился (ошибка/таймаут), а сессии нет.
        if _phase == 'error':
            st.error(_stt.get('msg') or 'Не удалось войти. Попробуй заново.')
        else:
            st.warning('Процесс входа завершился, а сессия не сохранилась. '
                       'Скорее всего вышло время (15 мин) - начни заново.')
        if st.button('🔁 Войти заново', type='primary',
                     use_container_width=True):
            _reset(pid)
            _launch(pid)
            st.rerun()
        _log = _dir(pid) / 'helper.log'
        if _log.exists():
            with st.expander('Диагностика (лог)'):
                try:
                    st.code('\n'.join(_log.read_text(
                        encoding='utf-8', errors='ignore').splitlines()[-60:]),
                        language='text')
                except Exception:
                    pass
        st.stop()

    # Ввод (на основной странице - не дёргается автообновлением скрина).
    with st.form('gsc_login_input', clear_on_submit=True):
        val = st.text_input(
            'Что вводим', placeholder='почта / пароль / код - что просит Google',
            label_visibility='collapsed')
        _f1, _f2, _f3, _f4 = st.columns(4)
        with _f1:
            _send = st.form_submit_button('↵ Отправить + Далее',
                                          use_container_width=True)
        with _f2:
            _enter = st.form_submit_button('↵ Только Далее',
                                           use_container_width=True)
        with _f3:
            _refresh = st.form_submit_button('🔄 Обновить экран',
                                             use_container_width=True)
        with _f4:
            _back = st.form_submit_button('◀ Назад', use_container_width=True)
    if _send and val.strip():
        _write_input(pid, val)
        st.rerun()
    elif _enter:
        _write_action(pid, 'enter')
        st.rerun()
    elif _refresh:
        _write_action(pid, 'refresh')
        st.rerun()
    elif _back:
        _write_action(pid, 'back')
        st.rerun()

    st.caption('Скрин обновляется сам каждые 2 сек. Печатай спокойно - поле '
               'ввода не сбрасывается. «Далее» в Google жми кнопкой «↵ Далее».')

    # Живой скриншот + статус - в авто-обновляемом фрагменте (не трогает ввод).
    @st.fragment(run_every=2)
    def _live():
        stt = _status(pid)
        ph = stt.get('phase', '')
        # Дошли до конечного состояния - обновим всю страницу и остановим опрос.
        if ph in ('done', 'stopped') or _session_b64(pid):
            st.rerun(scope='app')
        img = _screen_bytes(pid)
        if img:
            st.image(img, caption='Экран браузера на облаке',
                     use_container_width=True)
        else:
            st.caption('Жду первый скриншот…')
    _live()
    st.stop()

# ── Простаивает - предложить старт ──────────────────────────────────
_is_cloud_env = (os.name != 'nt' and not os.environ.get('DISPLAY'))
if not _is_cloud_env:
    st.info('Похоже, это локальный запуск. Скриншот-вход нужен в основном для '
            'облака (site-checker.streamlit.app). Локально проще войти в свой '
            'браузер на вкладке «Автокликеры».')

st.markdown(
    'Как это работает:\n'
    '1. Жмёшь «Начать вход» - на облаке открывается страница входа Google.\n'
    '2. Приходит скриншот. Вводишь то, что видно на нём (почту → «↵ Далее» → '
    'пароль → «↵ Далее» → при запросе код).\n'
    '3. Как войдём в Search Console - сессия сохранится, страница это покажет.\n'
    '4. Дальше запускаешь «Количество страниц в ГСК» в чек-листе - всё само.')

if st.button('▶ Начать вход', type='primary', use_container_width=True):
    _launch(pid)
    st.rerun()
