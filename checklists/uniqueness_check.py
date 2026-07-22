"""
Страница «Проверка уникальности» - уникальность контента страниц через text.ru.

Для выбранного проекта берёт НЕБОЛЬШУЮ выборку главного домена (главная, каталог,
N категорий, N товаров), достаёт основной текст каждой страницы и проверяет его в
text.ru: процент уникальности + с какими ЧУЖИМИ сайтами пересекается контент.
Свои домены/поддомены исключаются (exceptdomain), чтобы города-дубли не занулили
уникальность. Прогон фоновый (uniqueness_run.py), как «Скорость страниц».

Ключ text.ru (userkey) берётся из секретов (textru_key / textru_key_<pid>) и
передаётся фоновому процессу через переменную окружения TEXTRU_KEY - в git и в
аргументах командной строки ключ не светится.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY = sys.executable

OUT_ROOT = ROOT / 'cache' / 'uniqueness'
LOG_FILE = OUT_ROOT / 'run.log'
PID_FILE = OUT_ROOT / 'run.pid'

PROJECTS = {
    'smu': 'СМУ - Стальметурал', 'imp': 'ИМП - Инметпром',
    'mpe': 'МПЭ - Мепэн', 'avia': 'АПС - Авиапромсталь',
    'metpromko': 'МТТ - Метпромко',
}

C_GOOD, C_POOR, C_SOFT = '#1F9D2F', '#D03B3B', '#6B7280'


# ── Секреты / ключ ───────────────────────────────────────────────────
def _secret(key: str, default: str = '') -> str:
    try:
        if hasattr(st, 'secrets') and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default


def _textru_secret(pid: str) -> str:
    """Ключ text.ru из секретов/окружения (без введённого в поле)."""
    return (_secret(f'textru_key_{pid}') or _secret('textru_key')
            or os.environ.get('TEXTRU_KEY', '')).strip()


def _textru_key(pid: str) -> str:
    """Итоговый ключ: введённое в поле имеет приоритет, иначе - из секретов."""
    typed = (st.session_state.get(f'uniq_key_{pid}', '') or '').strip()
    return typed or _textru_secret(pid)


# ── Фоновый процесс ──────────────────────────────────────────────────
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


def _kill(pid):
    if not pid:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def _launch(args, extra_env=None):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v})
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    f = open(LOG_FILE, 'a', encoding='utf-8')
    proc = subprocess.Popen([PY, *args], cwd=str(ROOT), stdout=f,
                            stderr=subprocess.STDOUT, env=env, creationflags=flags)
    try:
        PID_FILE.write_text(str(proc.pid), encoding='utf-8')
    except Exception:
        pass
    return proc.pid


# ── Заголовок ────────────────────────────────────────────────────────
st.markdown(
    """<style>
    [data-testid="stDownloadButton"] button { background:#1E8E3E !important;
        border:1px solid #1E8E3E !important; }
    [data-testid="stDownloadButton"] button * { color:#FFF !important; }
    </style>""", unsafe_allow_html=True)

st.title('📄 Проверка уникальности контента')
st.caption('Проверяет через **text.ru**, насколько уникален текст страниц и с '
           'какими ЧУЖИМИ сайтами он пересекается. Берём небольшую выборку '
           'ГЛАВНОГО домена (города-поддомены — дубли, их не трогаем). Свои домены '
           'исключаются из сравнения. Каждая проверка тратит символы аккаунта text.ru.')

# Персист выбора проекта между вкладками.
_opts = list(PROJECTS.keys())
_saved = st.session_state.get('uniq_project_sel')
_idx = _opts.index(_saved) if _saved in _opts else None
pid = st.selectbox('Проект', _opts, format_func=lambda k: PROJECTS[k],
                   index=_idx, placeholder='- выберите проект -')
st.session_state['uniq_project_sel'] = pid
if not pid:
    st.info('Выберите проект, чтобы запустить проверку уникальности.')
    st.stop()

OUT_DIR = OUT_ROOT / pid

# ── Ключ text.ru (поле прямо в проверке, как токен Арсенкина; обязателен) ──
st.markdown('**🔑 Ключ text.ru (API-token)** — обязателен для запуска')
_has_secret = bool(_textru_secret(pid))
st.text_input(
    'Ключ text.ru', type='password', key=f'uniq_key_{pid}',
    label_visibility='collapsed',
    placeholder=('ключ уже задан в секретах проекта — можно оставить пусто'
                 if _has_secret
                 else 'вставь ключ text.ru (личный кабинет text.ru → раздел «API»)'),
    help='Личный userkey из личного кабинета text.ru → раздел «API».')
key = _textru_key(pid)
st.caption('Где взять: личный кабинет text.ru → раздел «API» (там ваш userkey). '
           'Ключ в git и в отчёты не попадает. Чтобы не вводить каждый раз — можно '
           'прописать его в секрет `textru_key` проекта.')
if not key:
    st.warning('⚠ Вставьте ключ text.ru — без него проверку не запустить.')

# ── Настройки выборки ────────────────────────────────────────────────
st.divider()
st.subheader('Что проверяем')
st.caption('Главная и каталог проверяются всегда. Ниже — сколько ещё категорий и '
           'товаров взять с главного домена. Больше страниц — точнее картина, но '
           'больше расход символов text.ru.')
_c1, _c2, _c3 = st.columns(3)
with _c1:
    n_cats = st.number_input('Категорий', 0, 30, 3, key=f'uniq_cats_{pid}',
                             help='Случайные категории каталога (главный домен).')
with _c2:
    n_prods = st.number_input('Товаров', 0, 30, 3, key=f'uniq_prods_{pid}',
                              help='Случайные карточки товаров (из базы листингов).')
with _c3:
    threshold = st.number_input('Порог уникальности, %', 50, 100, 95,
                                key=f'uniq_thr_{pid}',
                                help='Страницы ниже порога подсветятся красным и '
                                     'для них покажем сайты-источники пересечения.')
_n_pages = 2 + int(n_cats) + int(n_prods)
st.caption(f'Будет проверено примерно **{_n_pages}** страниц за прогон '
           '(главная + каталог + категории + товары). text.ru проверяет '
           'асинхронно — прогон занимает несколько минут.')

# ── Запуск ───────────────────────────────────────────────────────────
st.divider()
alive = _pid_alive(_read_pid())
c1, c2 = st.columns([3, 1])
with c1:
    if st.button('▶ Запустить проверку уникальности', use_container_width=True,
                 type='primary', disabled=alive or not key):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text('', encoding='utf-8')
        args = ['uniqueness_run.py', '--project', pid,
                '--categories', str(int(n_cats)), '--products', str(int(n_prods)),
                '--threshold', str(int(threshold))]
        _launch(args, extra_env={'TEXTRU_KEY': key})
        st.session_state['uniq_started'] = datetime.now().strftime('%H:%M:%S')
        st.rerun()
with c2:
    if st.button('⛔ Отменить', use_container_width=True, disabled=not alive):
        _kill(_read_pid())
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        st.rerun()

# ── Прогресс ─────────────────────────────────────────────────────────
st.divider()
st.subheader('Прогресс')
_log = LOG_FILE.read_text(encoding='utf-8', errors='ignore') if LOG_FILE.exists() else ''
_done = '✅ ГОТОВО' in _log or '✗ ОШИБКА' in _log
last_run = OUT_DIR / 'last_run.json'

if alive and not _done:
    import re as _re
    _sent = len(_re.findall(r'→ отправлено', _log))
    _got = len(_re.findall(r'✓ http', _log))
    if _sent:
        st.progress(min((_got + 1) / max(_sent, 1), 0.99),
                    text=f'Отправлено {_sent}, готово {_got}. text.ru считает…')
    else:
        st.progress(0.05, text='Готовлю выборку и скачиваю страницы…')
    with st.expander('Подробный лог', expanded=True):
        st.code('\n'.join(_log.splitlines()[-200:]) or '…', language='text')
    time.sleep(2)
    st.rerun()
else:
    if st.session_state.get('uniq_started'):
        st.caption(f'Последний запуск: {st.session_state["uniq_started"]}')
    if _log.strip():
        with st.expander('Подробный лог', expanded=False):
            st.code('\n'.join(_log.splitlines()[-200:]), language='text')

    # ── Результат ──
    if last_run.exists():
        try:
            data = json.loads(last_run.read_text(encoding='utf-8'))
        except Exception:
            data = None
        if data and data.get('project') == pid:
            st.divider()
            _s = data.get('summary', {})
            _thr = data.get('threshold', 95)
            st.markdown(f'#### Результат · {data.get("run_at", "")[:16].replace("T", " ")}')
            m1, m2, m3, m4 = st.columns(4)
            m1.metric('Проверено', f'{_s.get("checked", 0)}/{_s.get("total", 0)}')
            m2.metric('Средняя уникальность',
                      f'{_s.get("avg_unique")}%' if _s.get('avg_unique') is not None else '—')
            m3.metric(f'Ниже {_thr}%', _s.get('below', 0))
            m4.metric('Ошибок', _s.get('errors', 0))

            xlsx_name = data.get('xlsx_name', '')
            xlsx_path = OUT_DIR / xlsx_name if xlsx_name else None
            if xlsx_path and xlsx_path.exists():
                st.download_button(
                    f'⬇ Скачать отчёт «{xlsx_name}»', data=xlsx_path.read_bytes(),
                    file_name=xlsx_name, use_container_width=True,
                    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

            st.markdown('##### По страницам')
            for r in data.get('rows', []):
                _u = r.get('unique')
                _url = r.get('url', '')
                if r.get('error'):
                    st.markdown(
                        f'<span style="color:{C_SOFT}">— {_url}</span> · '
                        f'<span style="color:{C_SOFT};font-size:.85em">{r["error"]}</span>',
                        unsafe_allow_html=True)
                    continue
                _col = C_POOR if (_u is not None and _u < _thr) else C_GOOD
                _srcs = r.get('sources', [])
                st.markdown(
                    f'**<span style="color:{_col}">{_u:.1f}%</span>** · {_url}'
                    if _u is not None else f'? · {_url}', unsafe_allow_html=True)
                if _u is not None and _u < _thr and _srcs:
                    _lines = '\n'.join(
                        f'- {s["url"]}' + (f' — совпадение {s["plagiat"]:.1f}%'
                                           if s.get('plagiat') is not None else '')
                        for s in _srcs[:8])
                    st.markdown('Пересекается с:\n' + _lines)
        else:
            st.caption('Результат появится после запуска.')
    else:
        st.caption('Результат появится после запуска.')
