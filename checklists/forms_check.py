"""
Страница «Проверка форм» – фоновый прогон отправки форм на сайтах проекта.

Сделана по образцу страницы «Автокликеры»: кнопка стартует отдельный процесс
(forms_run.py) и сразу освобождает интерфейс. Движок открывает реальный Chrome
(Playwright, по умолчанию скрыто), заполняет формы и отправляет, результат
пишется в log_forms.xlsx.

Окружение:
  • Локально (streamlit run app.py) – работает.
  • Облако по ссылке – недоступно (нет браузера и движка на сервере).
  • Свой сервер (в планах) – заработает так же.
"""
import csv
import importlib.util
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable
LOG_FILE = ROOT / 'cache' / 'forms.log'
PID_FILE = ROOT / 'cache' / 'forms.pid'


def _load_cities(project: str):
    """Справочник городов проекта: список dict {country, city, url, mail}.
    Пусто, если файла нет. Первый город – основной сайт (Москва)."""
    f = ROOT / 'forms_tester' / 'projects' / project / 'cities.csv'
    if not f.exists():
        return []
    out = []
    try:
        with open(f, encoding='utf-8', newline='') as fh:
            for row in csv.DictReader(fh):
                city = (row.get('город') or '').strip()
                if city:
                    out.append({
                        'country': (row.get('страна') or 'Россия').strip(),
                        'city': city,
                        'url': (row.get('url') or '').strip(),
                        'mail': (row.get('почта') or '').strip(),
                    })
    except Exception:
        return []
    return out


_COUNTRY_FLAG = {
    'Россия': '🇷🇺', 'Казахстан': '🇰🇿', 'Беларусь': '🇧🇾', 'Кыргызстан': '🇰🇬',
    'Узбекистан': '🇺🇿', 'Азербайджан': '🇦🇿', 'Армения': '🇦🇲',
}

PROJECTS = {
    'smu': {'name': 'СМУ – Сталметурал', 'domain': 'stalmetural.ru'},
    'imp': {'name': 'ИМП – Инметпром', 'domain': 'inmetprom.ru'},
    'mpe': {'name': 'МПЭ – Мепэн', 'domain': 'mepen.ru'},
}

# Полный текст-подсказка (раньше был большим жёлтым блоком, теперь – в «❓»).
HELP_TEXT = (
    'Проверка открывает реальный браузер (Playwright) на ЭТОМ компьютере: '
    'заполняет формы на сайтах проекта и отправляет заявки. Работает, когда '
    'приложение запущено **локально** (`streamlit run app.py`). В облаке по '
    'ссылке недоступно. После переноса на свой сервер – заработает.'
)


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


def _deps_ready() -> tuple[bool, list[str]]:
    """Есть ли в этом окружении движок (его библиотеки + браузер). Возвращает
    (готово, список_чего_нет). На облаке по ссылке тут будет False."""
    missing = []
    for mod, label in (('bs4', 'beautifulsoup4'), ('requests', 'requests'),
                       ('openpyxl', 'openpyxl'), ('playwright', 'playwright')):
        if importlib.util.find_spec(mod) is None:
            missing.append(label)
    return (not missing), missing


def _launch_background(args: list[str], log_path: Path):
    """Запустить процесс в фоне, вывод – в файл. UI не блокируется."""
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


def _count_expected(project: str) -> int:
    """Сколько форм ожидается проверить (для шкалы прогресса). Best-effort:
    считаем включённые формы/модалки + шаги-формы в сценариях. Если не вышло – 0."""
    p = ROOT / 'forms_tester' / 'projects' / project / 'config.py'
    try:
        spec = importlib.util.spec_from_file_location(f'cfg_count_{project}', p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    except Exception:
        return 0

    def on(d) -> bool:
        v = d.get('включено', d.get('enabled', True))
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() not in ('false', '0', 'нет', 'off', '')

    total = 0
    try:
        for block in getattr(m, 'СТРАНИЦЫ_ДЛЯ_ПРОВЕРКИ', []) or []:
            if not on(block):
                continue
            for key in ('формы', 'модалки'):
                for item in block.get(key, []) or []:
                    if on(item):
                        total += 1
            for sc in block.get('сценарии', []) or []:
                if not on(sc):
                    continue
                for step in sc.get('шаги', []) or []:
                    if step.get('действие') in ('форма', 'модалка', 'проверить') and on(step):
                        total += 1
            for step in block.get('шаги', []) or []:  # legacy
                if step.get('действие') in ('форма', 'модалка', 'проверить') and on(step):
                    total += 1
    except Exception:
        return 0
    return total


def _rows_done(xlsx: Path):
    """Сколько форм уже записано в лог (строки минус шапка). None – не прочиталось."""
    if not xlsx.exists():
        return 0
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xlsx, read_only=True)
        n = (wb.active.max_row or 1) - 1
        wb.close()
        return max(n, 0)
    except Exception:
        return None


# ── Заголовок + подсказка «❓» ───────────────────────────────────────
# Кнопку скачивания отчёта подсвечиваем зелёным.
st.markdown(
    """
    <style>
    [data-testid="stDownloadButton"] button {
        background: #1E8E3E !important; border: 1px solid #1E8E3E !important;
    }
    [data-testid="stDownloadButton"] button * { color: #FFFFFF !important; }
    [data-testid="stDownloadButton"] button:hover {
        background: #176D30 !important; border-color: #176D30 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

_th, _qh = st.columns([0.88, 0.12], vertical_alignment='bottom')
with _th:
    st.title('📝 Проверка форм')
with _qh:
    with st.popover('❓', use_container_width=False):
        st.markdown(HELP_TEXT)

# ── Выбор проекта ────────────────────────────────────────────────────
pid_key = st.selectbox('Проект', list(PROJECTS.keys()),
                       format_func=lambda k: PROJECTS[k]['name'])
proj = PROJECTS[pid_key]
st.markdown(
    f"Будут проверены формы сайта **{proj['name']}** (`{proj['domain']}`): "
    'обратная связь, заявки, расчёты, оформление заказа и т.п. – по настройкам '
    'проекта.'
)

st.divider()

# ── Города / поддомены ───────────────────────────────────────────────
# Если у проекта есть справочник городов (cities.csv) – даём выбрать, какие
# поддомены проверять. Иначе – только основной сайт.
_cities = _load_cities(pid_key)
_chosen_cities = []          # список названий городов для прогона ([] = основной сайт)
if _cities:
    _main_city = _cities[0]['city']                  # Москва (основной)
    _all_names = [c['city'] for c in _cities]
    _others = _all_names[1:]
    # группировка по странам (с сохранением порядка)
    _groups = {}
    for c in _cities:
        _groups.setdefault(c['country'], []).append(c['city'])

    st.subheader('Города (поддомены)')
    _mode = st.radio(
        'Что проверяем',
        ['Только Москва (основной сайт)', 'Выбрать города', 'Случайные города'],
        horizontal=True, label_visibility='collapsed',
    )

    if _mode == 'Только Москва (основной сайт)':
        _chosen_cities = [_main_city]

    elif _mode == 'Случайные города':
        _n = st.number_input(f'Сколько случайных поддоменов (плюс {_main_city})',
                             min_value=1, max_value=len(_others),
                             value=min(3, len(_others)), step=1)
        _rnd = random.sample(_others, int(_n)) if _others else []
        _chosen_cities = [_main_city] + _rnd
        st.caption('Случайные выбираются заново при каждом запуске: ' + ', '.join(_chosen_cities))

    else:  # Выбрать города – СЕТКА ЧЕКБОКСОВ по странам
        def _ck(city):
            return f'fc_cb_{pid_key}_{city}'
        # один раз ставим дефолт: отмечена только Москва
        if not st.session_state.get(f'fc_init_{pid_key}'):
            for nm in _all_names:
                st.session_state[_ck(nm)] = (nm == _main_city)
            st.session_state[f'fc_init_{pid_key}'] = True

        _b1, _b2, _ = st.columns([1, 1, 4])
        if _b1.button('Выбрать все', use_container_width=True):
            for nm in _all_names:
                st.session_state[_ck(nm)] = True
            st.rerun()
        if _b2.button('Снять все', use_container_width=True):
            for nm in _all_names:
                st.session_state[_ck(nm)] = False
            st.rerun()

        _sel = []
        for _country, _names in _groups.items():
            st.markdown(f"**{_COUNTRY_FLAG.get(_country, '🏳')} {_country}**  ·  {len(_names)}")
            _cols = st.columns(6)
            for _i, _nm in enumerate(_names):
                if _cols[_i % 6].checkbox(_nm, key=_ck(_nm)):
                    _sel.append(_nm)
        _chosen_cities = _sel
        st.caption(f'Выбрано: **{len(_sel)} / {len(_all_names)}** городов.')

    if _mode != 'Выбрать города' and _chosen_cities:
        st.caption(f'Будет проверено городов: {len(_chosen_cities)}.')
    st.divider()

# ── Запуск ──────────────────────────────────────────────────────────
st.subheader('Запуск проверки')

clear_log = st.checkbox('Очищать лог Excel перед прогоном', value=True)
show_browser = st.checkbox('Показывать окно браузера', value=False)
st.caption('По умолчанию браузер работает скрыто (headless) – окно не '
           'показывается, отчёт всё равно формируется. Включи галочку выше, '
           'если хочешь видеть, как он заполняет формы.')

st.caption('Запуск фоновый – интерфейс сразу свободен. Можно уйти в чек-листы '
           'и работать параллельно, проверка крутится сама. Заявки '
           'отправляются по-настоящему (формы оформления заказа – без отправки).')

_alive = _pid_alive(_read_pid())

_run_col, _cancel_col = st.columns([3, 1])
with _run_col:
    if st.button('▶ Запустить проверку', use_container_width=True,
                 disabled=_alive):
        ready, _missing = _deps_ready()
        if not ready:
            # Движка нет в этом окружении (типично для облака по ссылке) –
            # не запускаем, показываем понятную инструкцию ниже. Заодно сбрасываем
            # прогресс и старый лог, чтобы не висел результат прошлого запуска.
            st.session_state['forms_dep_error'] = _missing
            st.session_state.pop('forms_started', None)
            st.session_state.pop('forms_started_ts', None)
            try:
                LOG_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            st.rerun()
        else:
            st.session_state.pop('forms_dep_error', None)
            args = ['forms_run.py', '--project', pid_key]
            if not clear_log:
                args.append('--no-clear-excel')
            if show_browser:
                args.append('--show-browser')
            if _chosen_cities:
                args += ['--cities', ','.join(_chosen_cities)]
            try:
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOG_FILE.write_text('', encoding='utf-8')
            except Exception:
                pass
            _launch_background(args, LOG_FILE)
            st.session_state['forms_started'] = datetime.now().strftime('%H:%M:%S')
            st.session_state['forms_started_ts'] = time.time()
            st.session_state['forms_project'] = pid_key
            st.session_state['forms_cities_n'] = max(1, len(_chosen_cities))
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

# ── Понятная ошибка: движок не установлен (показываем ТОЛЬКО после клика) ──
if not _alive and st.session_state.get('forms_dep_error'):
    st.error('Не получилось запустить проверку – в этом окружении нет браузера и нужных библиотек.')
    st.markdown(
        'Проверка форм работает **только локально** (на твоём компьютере) или на '
        'своём сервере с браузером – **в облачной версии по ссылке она недоступна**.\n\n'
        '**Чтобы запустить на своём компьютере:**\n'
        '1. Открой терминал в папке проекта и запусти приложение локально:\n'
        '   `streamlit run app.py`\n'
        '2. Один раз установи движок (там же, в терминале):\n'
        '   `pip install -r requirements-local.txt`\n'
        '3. И браузер для него:\n'
        '   `playwright install chromium`\n'
        '4. Обнови страницу и снова нажми «Запустить проверку».'
    )

st.divider()

# ── Прогресс ────────────────────────────────────────────────────────
# Всё в этой секции привязано к ВЫБРАННОМУ проекту (pid_key): при смене
# проекта статус/прогресс/лог/скачивание обновляются под него.
st.subheader('Прогресс')

_sel = pid_key
_run_proj = st.session_state.get('forms_project')      # какой проект реально гоняли
_this = (_run_proj == _sel)                            # выбранный == запущенный
xlsx = ROOT / 'cache' / 'forms' / _sel / 'log_forms.xlsx'

if _this and st.session_state.get('forms_started'):
    st.caption(f'Последний запуск: {st.session_state["forms_started"]}')

if _alive and _this:
    # Идёт проверка ВЫБРАННОГО проекта – живой прогресс
    _ts = st.session_state.get('forms_started_ts')
    _elapsed = int(time.time() - _ts) if _ts else None
    _mmss = f'{_elapsed // 60}:{_elapsed % 60:02d}' if _elapsed is not None else '…'
    _done = _rows_done(xlsx)
    _total = _count_expected(_sel) * st.session_state.get('forms_cities_n', 1)

    if _total and _done is not None:
        st.progress(min(_done / _total, 0.99), text=f'Проверено форм: {_done} из ~{_total}')
    else:
        st.progress(min(0.95, (_elapsed or 0) / 90.0), text='Идёт проверка…')

    st.caption(f'⏳ Идёт… {_mmss}. Обычно занимает от пары до нескольких минут '
               '(зависит от числа форм). Страница обновляется сама – можно уйти '
               'на другие вкладки, прогон не прервётся.')
    with st.expander('Подробный лог', expanded=True):
        _txt = LOG_FILE.read_text(encoding='utf-8', errors='ignore') if LOG_FILE.exists() else ''
        st.code('\n'.join(_txt.splitlines()[-300:]) or '…', language='text')
    time.sleep(2)
    st.rerun()

elif _alive and not _this:
    # Идёт проверка ДРУГОГО проекта – не путаем
    st.info(f'Сейчас идёт проверка проекта «{PROJECTS[_run_proj]["name"]}». '
            'Переключи выбор проекта на него, чтобы видеть прогресс.')
    time.sleep(2)
    st.rerun()

else:
    # Ничего не идёт. Лог/статус показываем только для прогона ВЫБРАННОГО проекта.
    if _this and st.session_state.get('forms_started') and \
            LOG_FILE.exists() and LOG_FILE.read_text(encoding='utf-8', errors='ignore').strip():
        st.markdown('**Статус:** ✅ завершено / остановлено')
        with st.expander('Подробный лог', expanded=False):
            st.code('\n'.join(LOG_FILE.read_text(encoding='utf-8', errors='ignore')
                              .splitlines()[-300:]), language='text')
    else:
        st.caption('Лог появится после запуска.')

    # ── Результат: Excel выбранного проекта (имя файла = Проект-Дата) ──
    if xlsx.exists():
        st.divider()
        st.subheader('Результаты (Excel)')
        _date = datetime.fromtimestamp(xlsx.stat().st_mtime).strftime('%d.%m.%Y')
        _fname = f'{_sel.capitalize()}-{_date}.xlsx'   # напр. Mpe-23.06.2026.xlsx
        st.caption(f'Лог проекта {PROJECTS[_sel]["name"]} '
                   '– дата, страница, форма, статус и комментарий с причиной (если не сработало).')
        st.download_button(
            f'⬇ Скачать {_fname}',
            data=xlsx.read_bytes(),
            file_name=_fname,
            mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            use_container_width=True,
        )
