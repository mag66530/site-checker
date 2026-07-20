"""
Страница «Проверка целей» - прогон ВСЕХ целей Яндекс.Метрики проекта.

Эталон - каталог целей из Метрики (catalogs/goals-<проект>.json). Движок
(goals_run.py) открывает страницы сайта, выполняет безопасные действия (клики
по телефонам/почте/соцсетям/кнопкам форм - заявки НЕ отправляются) и слушает
Метрику. Отчёт: по каждой цели - Сработала / НЕ сработала / Прогоном форм /
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
    'smu': 'СМУ - Стальметурал',
    'imp': 'ИМП - Инметпром',
    'mpe': 'МПЭ - Мепэн',
}

# Внутри проекта - выбор страны/сайта; значение = код каталога goals-<pid>.json.
# У каждой страны свой счётчик Метрики и свой домен. Домены СМУ взяты из КП;
# ИМП-страны ждут домены (проверку по ним пока не запустить).
СТРАНЫ = {
    'smu': [
        ('smu',     'Россия · stalmetural.ru'),
        ('smu-uz',  'Узбекистан · stalmetural.uz'),
        ('smu-az',  'Азербайджан · smg.az'),
        ('smu-az2', 'Азербайджан, перевод · steelgroup.az'),
        ('smu-am',  'Армения · stalmetural.am'),
        ('smu-kg',  'Кыргызстан · stalmetural.kg'),
        ('smu-kz',  'Казахстан · stalmetural.kz'),
        ('smu-rb',  'Беларусь · stalmetural.by'),
    ],
    'imp': [
        ('imp',    'Россия · inmetprom.ru'),
        ('imp-uz', 'Узбекистан · inmetprom.uz'),
        ('imp-az', 'Азербайджан · inmetprom.az'),
        ('imp-kz', 'Казахстан · inmetprom.kz'),
        ('imp-kg', 'Кыргызстан · inmetprom.kg'),
        ('imp-rb', 'Беларусь · inmetprom.by'),
    ],
    'mpe': [
        ('mpe',    'Россия · mepen.ru'),
        ('mpe-uz', 'Узбекистан · mepen.uz'),
        ('mpe-kz', 'Казахстан · mepen.kz'),
        ('mpe-kg', 'Кыргызстан · mepen.kg'),
        ('mpe-rb', 'Беларусь · mepen.by'),
    ],
}

st.title('🎯 Проверка целей')
st.caption('Проверяем ВСЕ цели Яндекс.Метрики проекта: браузер выполняет действия '
           'на сайте (клики по телефонам, почте, соцсетям, кнопкам форм - заявки '
           'НЕ отправляются) и слушает, какие цели фиксирует Метрика. Цели '
           'отправки форм проверяются страницей «Проверка форм» и подтягиваются '
           'из её последнего отчёта.')

# Кнопка запуска - чёрная (как на «Чек-листе»), кнопка скачивания - зелёная.
st.markdown(
    """
    <style>
    div[data-testid="stButton"] > button[kind="primary"],
    div[data-testid="stButton"] > button[data-testid="stBaseButton-primary"],
    div[data-testid="stButton"] > button[data-testid="baseButton-primary"] {
        background: #1A1A1A !important; border: 1px solid #1A1A1A !important;
        color: #FFFFFF !important;
    }
    div[data-testid="stButton"] > button[kind="primary"] *,
    div[data-testid="stButton"] > button[data-testid="stBaseButton-primary"] *,
    div[data-testid="stButton"] > button[data-testid="baseButton-primary"] * {
        color: #FFFFFF !important;
    }
    div[data-testid="stButton"] > button[kind="primary"]:hover {
        background: #000000 !important; border-color: #000000 !important;
    }
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

_base = st.selectbox('Проект', list(PROJECTS), format_func=lambda k: PROJECTS[k],
                     index=None, placeholder='- выберите проект -')
if not _base:
    st.info('Выберите проект, чтобы запустить проверку целей.')
    st.stop()

_варианты = СТРАНЫ.get(_base, [(_base, PROJECTS[_base])])


def _load_cat(pid):
    f = ROOT / 'catalogs' / f'goals-{pid}.json'
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            return None
    return None


_cats = {p: _load_cat(p) for p, _ in _варианты}


def _домен_ок(pid):
    # Базовые проекты знают страницы (ACTIONS); странам нужен домен из каталога.
    if pid in ('smu', 'imp', 'mpe'):
        return True
    c = _cats.get(pid)
    return bool(c and (c.get('домен') or '').strip())


# ── Выбор сайтов/стран галочками (как на «Проверке форм») ────────────
st.subheader('Сайты / страны')
st.caption('По умолчанию отмечены все доступные. Проверка пройдёт по каждому '
           'отмеченному сайту, на каждый - свой отчёт.')


def _ck(pid):
    return f'gc_cb_{_base}_{pid}'


# При заходе на проект (и при СМЕНЕ проекта) по умолчанию отмечаем все сайты с
# доменом. В рамках одного проекта ручные снятия галочек сохраняются.
if st.session_state.get('gc_last_base') != _base:
    st.session_state['gc_last_base'] = _base
    for p, _ in _варианты:
        st.session_state[_ck(p)] = _домен_ок(p)

_доступные = [p for p, _ in _варианты if _домен_ок(p)]
_all_on = all(st.session_state.get(_ck(p), False) for p in _доступные)
_left, _right = st.columns([4.2, 1.3], vertical_alignment='top')
with _right:
    if st.button('Снять все' if _all_on else 'Выбрать все',
                 use_container_width=True, key=f'gc_toggle_{_base}'):
        for p in _доступные:
            st.session_state[_ck(p)] = not _all_on
        st.rerun()

_selected = []
with _left:
    for p, label in _варианты:
        _ok = _домен_ок(p)
        c = _cats.get(p)
        _n = len(c.get('цели', [])) if c else 0
        _lbl = f'{label}  ·  целей {_n}' + ('' if _ok else '  ·  домен уточняется')
        if st.checkbox(_lbl, key=_ck(p), disabled=not _ok) and _ok:
            _selected.append(p)
_n_sel = len(_selected)
if _n_sel:
    _lo_est, _hi_est = 3 + 5 * _n_sel, 5 + 10 * _n_sel
    _time_hint = (f' Ориентировочно ~{_lo_est}-{_hi_est} мин '
                  '(≈5-10 мин на сайт + сквозной заказ в начале).')
else:
    _time_hint = ''
st.caption(f'Выбрано сайтов: **{_n_sel} / {len(_доступные)}**.{_time_hint}')

# Рабочие файлы (лог/PID общего прогона) держим под базовым проектом.
WORK = ROOT / 'cache' / 'goals' / _base
LOG_FILE = WORK / 'run.log'
PID_FILE = WORK / 'run.pid'

st.divider()

# ── Формы внутри целей ───────────────────────────────────────────────
# Проверка целей САМА лишние формы не отправляет (не спамим реальными заявками):
# цели отправки форм зелёные по «reachGoal в коде». НО сквозной ЗАКАЗ (корзина →
# оформление) прогоняется автоматически прямо здесь - заказ-цели подтверждаются
# без отдельного запуска «Проверки форм».
st.caption('📨 Проверка целей сама прогоняет сквозной ЗАКАЗ (корзина → оформление) '
           'и подтверждает заказ-цели - отдельный запуск «Проверки форм» не нужен. '
           'Остальные формы не шлём: их цели зелёные по «reachGoal в коде».')


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

# Читаем лог и определяем «прогон завершён» ДО кнопок. Финал ловим по маркеру
# целей, а не по «ВСЁ ГОТОВО» форм. В облаке PID переиспользуется - тогда старый
# run.pid «оживает» чужим процессом и кнопка «Запустить» залипает выключенной.
# Поэтому: если в логе есть маркер завершения - прогон точно НЕ идёт, снимаем
# «живой» флаг и чистим устаревший PID (иначе только «Отменить» разблокирует).
log_txt = ''
if LOG_FILE.is_file():
    try:
        log_txt = LOG_FILE.read_text(encoding='utf-8')
    except Exception:
        log_txt = ''
_done = ('ПРОВЕРКА ЦЕЛЕЙ ЗАВЕРШЕНА' in log_txt) \
    or ('✗ Нет каталога' in log_txt) or ('✗ Не заданы' in log_txt)
if _done and _alive:
    _alive = False
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
_running = _alive and not _done


@st.cache_resource(show_spinner=False)
def _browser():
    import browser_setup
    return browser_setup.ensure_browser()


_bok, _bmsg = (True, '')
if not _alive:
    with st.spinner('Готовлю браузер (первый запуск в облаке - до минуты)…'):
        _bok, _bmsg = _browser()
    if not _bok:
        st.error(f'Браузер не готов: {_bmsg}. Проверка целей работает локально '
                 f'или на своём сервере; в облаке нужен playwright + packages.txt.')

# Прокси + проверка доступности сайта (над кнопкой запуска)
try:
    from site_access import render_proxy_access
    _dom = ''
    _vars = СТРАНЫ.get(_base, [])
    if _vars and '·' in _vars[0][1]:
        _dom = _vars[0][1].split('·')[-1].strip()
    render_proxy_access(f'goals_{_base}',
                        default_url=(f"https://{_dom}/" if _dom else ''),
                        pid=_base)
except Exception as _e_pa:
    st.caption(f'⚠ Блок прокси/доступа не загрузился: {_e_pa}')

c1, c2 = st.columns([3, 1])
with c1:
    if st.button('▶ Запустить проверку целей', use_container_width=True,
                 type='primary', disabled=_running or not _bok or not _selected):
        WORK.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text('', encoding='utf-8')
        # Удаляем ПРОШЛЫЙ сводный отчёт, чтобы, пока идёт новый прогон, не
        # показывался устаревший файл (частая путаница: выбрали 6 сайтов, а
        # виден отчёт от прошлого прогона на 2).
        try:
            (WORK / 'goals_report.xlsx').unlink(missing_ok=True)
        except Exception:
            pass
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        # Telegram: креды берём из секретов (те же, что у еженедельной проверки) и
        # кладём в окружение прогона - goals_run сам отправит сводный отчёт в чат.
        try:
            import tg_report
            env.update(tg_report.runner_env(_base))
        except Exception:
            pass
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        f = open(LOG_FILE, 'a', encoding='utf-8')
        # Формы гоняются САМИ внутри проверки целей (полный прогон по умолчанию) -
        # цели форм подтверждаются без отдельного запуска «Проверки форм» и без
        # ручных «подтягиваний».
        args = [PY, 'goals_run.py', '--projects', ','.join(_selected)]
        proc = subprocess.Popen(args, cwd=str(ROOT), stdout=f,
                                stderr=subprocess.STDOUT, env=env,
                                creationflags=flags)
        PID_FILE.write_text(str(proc.pid), encoding='utf-8')
        st.session_state['goals_started'] = datetime.now().strftime('%H:%M:%S')
        st.session_state['goals_started_ts'] = time.time()   # для секундомера прогона
        st.session_state['goals_selected'] = list(_selected)
        # Подпись прогона: проект + выбранные сайты. По ней прячем устаревший
        # прогресс, если пользователь сменил проект или набор галочек.
        st.session_state['goals_run_sig'] = f'{_base}|{",".join(sorted(_selected))}'
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
# log_txt / _done / _running уже вычислены выше (до кнопок).
# Прогресс И отчёт от СТАРОГО прогона (другой проект / другой набор галочек /
# прошлая сессия, в т.ч. после перезагрузки страницы) не показываем - только
# живой прогон или завершение ИМЕННО этого выбора в текущей сессии.
_cur_sig = f'{_base}|{",".join(sorted(_selected))}'
_own_run = (st.session_state.get('goals_run_sig') == _cur_sig)
if not _running and not _own_run:
    log_txt = ''

# Прогресс: движок пишет «СТРАНА i/N», «ПРОГРЕСС x/y», «цель: X», «ФОРМЫ: …».
import re as _re
_sm = _re.findall(r'СТРАНА\s+(\d+)\s*/\s*(\d+)', log_txt)
_pm = _re.findall(r'ПРОГРЕСС\s+(\d+)\s*/\s*(\d+)', log_txt)
_goals_hit = len(_re.findall(r'цель:\s', log_txt))
_forms_now = ('ФОРМЫ:' in log_txt) and ('СТРАНА' not in log_txt)
# Секундомер прогона (как на «Проверке форм»): сколько времени идёт/заняло.
# Старт берём из session_state (ставится при запуске); показываем только для
# СВОЕГО прогона - после перезагрузки страницы отметки нет, тогда «…».
_gts = st.session_state.get('goals_started_ts') if _own_run else None
if _running:
    _elapsed = int(time.time() - _gts) if _gts else None
    _run_mmss = f'{_elapsed // 60}:{_elapsed % 60:02d}' if _elapsed is not None else '…'
    if _forms_now:
        st.progress(0.05, text='Прогон форм (перед целями)…')
    elif _sm:
        _si, _sn = int(_sm[-1][0]), int(_sm[-1][1])
        _frac = (_si - 1) / max(_sn, 1)
        if _pm:
            _i, _n = int(_pm[-1][0]), int(_pm[-1][1])
            _frac += (_i / max(_n, 1)) / max(_sn, 1)
        st.progress(min(_frac, 0.99),
                    text=f'Сайт {_si} из {_sn} · целей поймано: {_goals_hit}')
    else:
        st.progress(0.02, text='Запуск браузера…')
    st.markdown(f'**Статус:** ⏳ идёт проверка… {_run_mmss} (страница обновляется сама)')
    # Реальная оценка зависит от числа сайтов и тяжести проекта: ~5-10 мин на
    # сайт (у МПЭ ~55 целей на страну = ближе к 10) + сквозной заказ в начале
    # (~3-5 мин, один раз). Число сайтов берём из лога (СТРАНА i/N - надёжнее
    # всего), иначе из набора этого прогона.
    _n_sites = None
    if _sm:
        _n_sites = int(_sm[-1][1])
    elif st.session_state.get('goals_selected'):
        _n_sites = len(st.session_state['goals_selected'])
    elif _selected:
        _n_sites = len(_selected)
    if _n_sites:
        _lo, _hi = 3 + 5 * _n_sites, 5 + 10 * _n_sites
        _est = (f'Ориентир для {_n_sites} сайт(ов): ~{_lo}-{_hi} мин '
                '(≈5-10 мин на сайт + сквозной заказ в начале).')
    else:
        _est = 'Ориентир: ≈5-10 мин на сайт + сквозной заказ в начале.'
    st.caption(_est + ' Можно уйти на другие вкладки - прогон не прервётся.')
elif 'ПРОВЕРКА ЦЕЛЕЙ ЗАВЕРШЕНА' in log_txt:
    st.progress(1.0, text=f'Готово · целей поймано: {_goals_hit}')
    # Итоговое время = последняя запись лога (финиш) минус старт прогона.
    _fin = LOG_FILE.stat().st_mtime if LOG_FILE.is_file() else None
    _spent = int(_fin - _gts) if (_gts and _fin and _fin > _gts) else None
    _spent_txt = f' · ⏱ заняло {_spent // 60}:{_spent % 60:02d}' if _spent else ''
    st.markdown(f'**Статус:** ✅ завершено{_spent_txt}')
elif log_txt:
    st.markdown('**Статус:** ⛔ остановлено / прервано')
else:
    st.markdown('**Статус:** ещё не запускалось')

if log_txt:
    tail = '\n'.join(log_txt.splitlines()[-25:])
    st.code(tail or ' ', language=None)

# ── Результаты: один сводный отчёт (лист «Сводка» + лист целей на сайт) ──
# Показываем ТОЛЬКО отчёт этого прогона (по подписи выбора). После перезагрузки
# страницы или ухода на другой проект старый файл на диске не показываем.
_REPORT = WORK / 'goals_report.xlsx'
if not _running and _own_run and _REPORT.is_file():
    st.subheader('Результаты (Excel)')
    st.caption('Один файл: лист «Сводка» (итоги по каждому сайту) и по отдельному '
               'листу целей на каждый проверенный сайт (РФ, УЗ, …) со статусом и '
               'пояснением по каждой цели.')
    st.download_button(
        f'⬇ Скачать сводный отчёт по целям ({PROJECTS[_base].split(" ")[0]})',
        data=_REPORT.read_bytes(),
        file_name=f'Цели-{_base}-{datetime.now().strftime("%d.%m.%Y")}.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        use_container_width=True,
    )

# Лог прогона (txt) - для разбора, если что-то не поймалось.
if not _running and log_txt.strip():
    st.download_button(
        '⬇ Скачать лог прогона (txt)',
        data=log_txt.encode('utf-8'),
        file_name=f'Цели-{_base}-log-{datetime.now().strftime("%d.%m.%Y")}.txt',
        mime='text/plain',
        use_container_width=True,
    )

if _running:
    time.sleep(3)
    st.rerun()
