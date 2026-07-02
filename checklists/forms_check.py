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

from urllib.parse import urlparse

import streamlit as st

ROOT = Path(__file__).parent.parent
PY = sys.executable
LOG_FILE = ROOT / 'cache' / 'forms.log'
PID_FILE = ROOT / 'cache' / 'forms.pid'


def _load_cities(project: str):
    """Справочник городов проекта: список dict {country, city, url, mail}.
    Пусто, если файла нет. Первый город – основной сайт (Москва)."""
    project = CITIES_FROM.get(project, project)
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
    'Киргизия': '🇰🇬', 'Узбекистан': '🇺🇿', 'Азербайджан': '🇦🇿', 'Армения': '🇦🇲',
}


def _host(url: str) -> str:
    return urlparse((url or '').strip()).netloc


def _main_domains(cities):
    """Основной домен каждой страны: строка справочника с самым «коротким» хостом
    (без поддомена-города: mepen.kz, а не aktau.mepen.kz). Порядок стран – как в csv."""
    best = {}
    for c in cities:
        h = _host(c['url'])
        depth = h.count('.')
        cur = best.get(c['country'])
        if cur is None or depth < cur[0]:
            best[c['country']] = (depth, c)
    out, seen = [], set()
    for c in cities:
        if c['country'] not in seen:
            seen.add(c['country'])
            out.append(best[c['country']][1])
    return out

PROJECTS = {
    'smu': {'name': 'СМУ – Стальметурал', 'domain': 'stalmetural.ru'},
    'imp': {'name': 'ИМП – Инметпром', 'domain': 'inmetprom.ru'},
    'mpe': {'name': 'МПЭ – Мепэн', 'domain': 'mepen.ru'},
    # Быстрая проверка ТОЛЬКО оформления заказа через корзину (Мепэн).
    'mpe_cart': {'name': 'МПЭ – Корзина', 'domain': 'mepen.ru'},
}

# Проекты-варианты берут справочник городов у «родителя» (свой config.py,
# общий cities.csv). Держим в синхроне с CITIES_FROM в forms_run.py.
CITIES_FROM = {
    'mpe_cart': 'mpe',
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


def _flag_on(v) -> bool:
    """Значение флага «включено» → bool (терпимо к строкам «нет»/«off»/…)."""
    if isinstance(v, bool):
        return v
    if v is None:
        return True
    return str(v).strip().lower() not in ('false', '0', 'нет', 'off', '')


def _list_forms(project: str):
    """Список форм проекта В ПОРЯДКЕ ПРОГОНА: [{'page','name'}, ...].
    Имена = ровно те «названия», что движок пишет в отчёт (сценарии/формы/модалки).
    Учитываем только ВКЛЮЧЁННЫЕ страницы/формы, чтобы не показывать то, что не гоняется."""
    p = ROOT / 'forms_tester' / 'projects' / project / 'config.py'
    try:
        spec = importlib.util.spec_from_file_location(f'cfg_forms_{project}', p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    except Exception:
        return []

    out, seen = [], set()

    def add(page, name):
        name = (name or '').strip()
        if name and name not in seen:
            seen.add(name)
            out.append({'page': page, 'name': name})

    try:
        for block in getattr(m, 'СТРАНИЦЫ_ДЛЯ_ПРОВЕРКИ', []) or []:
            if not _flag_on(block.get('включено', True)):
                continue
            page = str(block.get('тип') or '').strip()
            # Сценарии (клик → форма). Имя = «название» сценария.
            if _flag_on(block.get('сценарий_включен', True)):
                сцены = block.get('сценарии') or []
                for sc in сцены:
                    if _flag_on(sc.get('включено', True)):
                        add(page, sc.get('название') or block.get('название_сценария') or page)
                # legacy: шаги прямо в блоке (без «сценарии»)
                if not сцены and (block.get('шаги')):
                    add(page, block.get('название_сценария') or page)
            # Отдельные формы на странице
            if _flag_on(block.get('формы_включены', True)):
                for f in block.get('формы') or []:
                    if _flag_on(f.get('включено', True)):
                        add(page, f.get('название'))
            # Модалки (кнопка открывает окно)
            if _flag_on(block.get('модалки_включены', True)):
                for mo in block.get('модалки') or []:
                    if _flag_on(mo.get('включено', True)):
                        add(page, mo.get('название_теста'))
    except Exception:
        return out
    return out


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

# ── Домены и поддомены ───────────────────────────────────────────────
# Если у проекта есть справочник городов (cities.csv) – даём выбрать, что
# проверять. Домен = основной сайт страны (mepen.ru, mepen.kz…); поддомен =
# город на этом домене (spb.mepen.ru). Иначе – только основной сайт.
_cities = _load_cities(pid_key)
_chosen_cities = []          # список названий городов для прогона ([] = основной сайт)
if _cities:
    _all_names = [c['city'] for c in _cities]
    _mains = _main_domains(_cities)                  # основной домен каждой страны
    _main_by_country = {c['country']: c for c in _mains}
    # группировка по странам (с сохранением порядка)
    _groups = {}
    for c in _cities:
        _groups.setdefault(c['country'], []).append(c['city'])

    st.subheader('Домены и поддомены')
    st.caption('**Домен** – основной сайт страны (например `' + _host(_mains[0]['url']) +
               '`). **Поддомен** – город на этом домене (например `' +
               (_host(_cities[1]['url']) if len(_cities) > 1 else '') + '`). '
               'Заявка с каждого домена/поддомена должна прийти на свою почту из справочника.')
    _mode = st.radio(
        'Что проверяем',
        ['Основные домены (по странам)', 'Выбрать города', 'Случайные города'],
        horizontal=True, label_visibility='collapsed',
    )

    if _mode == 'Основные домены (по странам)':
        # Главный домен каждой страны. Галочки уже стоят – можно снять лишние.
        st.caption('Проверяются главные сайты каждой страны. Галочки уже стоят – '
                   'сними те страны, которые проверять не нужно.')

        def _mk(country):
            return f'fc_main_{pid_key}_{country}'
        for c in _mains:                       # дефолт: все страны включены
            if _mk(c['country']) not in st.session_state:
                st.session_state[_mk(c['country'])] = True

        _sel = []
        _cols = st.columns(3)
        for _i, c in enumerate(_mains):
            with _cols[_i % 3]:
                _lbl = f"{_COUNTRY_FLAG.get(c['country'], '🏳')} **{c['country']}** – {c['city']}"
                if st.checkbox(_lbl, key=_mk(c['country'])):
                    _sel.append(c['city'])
                st.caption(f"`{_host(c['url'])}`")
        _chosen_cities = _sel
        st.caption(f'Выбрано доменов: **{len(_sel)} / {len(_mains)}**.')

    elif _mode == 'Случайные города':
        st.caption('Для каждой страны берётся её основной домен + случайные '
                   'поддомены-города. Состав меняется при каждом запуске.')
        _dist = st.radio(
            'Как задать количество',
            ['Общее число (распределить по странам автоматически)', 'Число по каждой стране'],
            horizontal=True, key=f'fc_rnd_mode_{pid_key}',
        )

        _counts = {}
        if _dist.startswith('Общее'):
            _total_max = len(_all_names)
            _total = st.number_input('Сколько всего доменов/поддоменов проверить',
                                     min_value=1, max_value=_total_max,
                                     value=min(7, _total_max), step=1,
                                     key=f'fc_rnd_total_{pid_key}')
            # Распределение по кругу: каждая страна получает по 1, потом снова по
            # кругу (начиная с России), пока не раздадим всё. Больше городов, чем
            # есть в стране, не даём.
            _order = list(_groups.keys())
            _counts = {k: 0 for k in _order}
            _left = int(_total)
            while _left > 0:
                _gave = False
                for k in _order:
                    if _left <= 0:
                        break
                    if _counts[k] < len(_groups[k]):
                        _counts[k] += 1
                        _left -= 1
                        _gave = True
                if not _gave:
                    break
            st.caption('Распределение: ' + ' · '.join(
                f"{_COUNTRY_FLAG.get(k, '🏳')} {k} – **{v}**"
                for k, v in _counts.items() if v))
        else:
            st.caption('Укажи, сколько доменов/поддоменов проверить в каждой стране '
                       '(0 – страну не проверяем).')
            _cols = st.columns(min(4, max(1, len(_groups))))
            for _i, (_country, _names) in enumerate(_groups.items()):
                with _cols[_i % min(4, max(1, len(_groups)))]:
                    _counts[_country] = int(st.number_input(
                        f"{_COUNTRY_FLAG.get(_country, '🏳')} {_country} (из {len(_names)})",
                        min_value=0, max_value=len(_names),
                        value=min((2 if _country == 'Россия' else 1), len(_names)),
                        step=1, key=f'fc_rnd_{pid_key}_{_country}'))

        # Сборка списка: основной домен страны идёт первым, остальное – случайно.
        for _country, _names in _groups.items():
            _k = int(_counts.get(_country, 0) or 0)
            if _k <= 0:
                continue
            _mc = _main_by_country.get(_country, {}).get('city')
            _pick = [_mc] if _mc in _names else []
            _pool = [n for n in _names if n not in _pick]
            _extra = min(max(_k - len(_pick), 0), len(_pool))
            if _extra:
                _pick += random.sample(_pool, _extra)
            _chosen_cities += _pick[:_k]
        if _chosen_cities:
            st.caption('Сейчас выпало (пересоберётся при запуске): ' + ', '.join(_chosen_cities))

    else:  # Выбрать города – СЕТКА ЧЕКБОКСОВ по странам
        def _ck(city):
            return f'fc_cb_{pid_key}_{city}'
        # один раз ставим дефолт: отмечены ВСЕ домены/поддомены
        if not st.session_state.get(f'fc_init_all_{pid_key}'):
            for nm in _all_names:
                st.session_state[_ck(nm)] = True
            st.session_state[f'fc_init_all_{pid_key}'] = True

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
            _dom = _host(_main_by_country.get(_country, {}).get('url', ''))
            st.markdown(f"**{_COUNTRY_FLAG.get(_country, '🏳')} {_country}**  ·  {len(_names)}"
                        + (f"  ·  `{_dom}`" if _dom else ''))
            _cols = st.columns(6)
            for _i, _nm in enumerate(_names):
                if _cols[_i % 6].checkbox(_nm, key=_ck(_nm)):
                    _sel.append(_nm)
        _chosen_cities = _sel
        st.caption(f'Выбрано: **{len(_sel)} / {len(_all_names)}** городов.')

    if _mode not in ('Выбрать города', 'Основные домены (по странам)') and _chosen_cities:
        st.caption(f'Будет проверено доменов/поддоменов: {len(_chosen_cities)}.')
    st.divider()

_cities_none = bool(_cities) and not _chosen_cities

# ── Формы ────────────────────────────────────────────────────────────
# Список форм проекта (в порядке прогона). По умолчанию – все; можно выбрать
# только нужные. Имена совпадают с тем, что попадает в отчёт.
_all_forms = _list_forms(pid_key)
_all_form_names = [f['name'] for f in _all_forms]
_chosen_forms = list(_all_form_names)     # по умолчанию – все формы
if _all_forms:
    st.subheader('Формы')
    _fmode = st.radio(
        'Какие формы проверяем',
        ['Все формы', 'Выбрать формы'],
        horizontal=True, label_visibility='collapsed',
        key=f'fc_forms_mode_{pid_key}',
    )
    if _fmode == 'Выбрать формы':
        def _fk(name):
            return f'ff_cb_{pid_key}_{name}'
        # дефолт – все отмечены (один раз на проект)
        if not st.session_state.get(f'ff_init_{pid_key}'):
            for nm in _all_form_names:
                st.session_state[_fk(nm)] = True
            st.session_state[f'ff_init_{pid_key}'] = True

        _fb1, _fb2, _ = st.columns([1, 1, 4])
        if _fb1.button('Выбрать все', use_container_width=True, key=f'ff_all_{pid_key}'):
            for nm in _all_form_names:
                st.session_state[_fk(nm)] = True
            st.rerun()
        if _fb2.button('Снять все', use_container_width=True, key=f'ff_none_{pid_key}'):
            for nm in _all_form_names:
                st.session_state[_fk(nm)] = False
            st.rerun()

        # группировка по странице (в порядке прогона)
        _by_page = {}
        for f in _all_forms:
            _by_page.setdefault(f['page'], []).append(f['name'])
        _sel_f = []
        for _pg, _names in _by_page.items():
            st.markdown(f"**{_pg}**  ·  {len(_names)}")
            _fcols = st.columns(2)
            for _i, _nm in enumerate(_names):
                if _fcols[_i % 2].checkbox(_nm, key=_fk(_nm)):
                    _sel_f.append(_nm)
        _chosen_forms = _sel_f
        st.caption(f'Выбрано форм: **{len(_chosen_forms)} / {len(_all_forms)}**.')
    else:
        st.caption(f'Будут проверены все формы проекта: {len(_all_forms)}.')
    st.divider()

_forms_all_selected = (len(_chosen_forms) == len(_all_form_names))
_forms_none = bool(_all_forms) and len(_chosen_forms) == 0

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

if _forms_none:
    st.warning('Не выбрано ни одной формы — отметь хотя бы одну, чтобы запустить.')
if _cities_none:
    st.warning('Не выбрано ни одного домена/города — отметь хотя бы один, чтобы запустить.')

_run_col, _cancel_col = st.columns([3, 1])
with _run_col:
    if st.button('▶ Запустить проверку', use_container_width=True,
                 disabled=_alive or _forms_none or _cities_none):
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
            # Фильтр форм: передаём только если выбрано подмножество (не все).
            if _all_forms and not _forms_all_selected and _chosen_forms:
                import json
                _ff = ROOT / 'cache' / 'forms' / pid_key / '_forms_filter.json'
                _ff.parent.mkdir(parents=True, exist_ok=True)
                _ff.write_text(json.dumps(_chosen_forms, ensure_ascii=False),
                               encoding='utf-8')
                args += ['--forms-file', str(_ff)]
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
            # Ожидаемое число форм для шкалы прогресса (учитывает выбор форм).
            _per_city = (len(_chosen_forms) if (_all_forms and not _forms_all_selected)
                         else _count_expected(pid_key))
            st.session_state['forms_expected_total'] = _per_city * max(1, len(_chosen_cities))
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
    # Если при запуске сохранили ожидаемое число форм (учитывает выбор форм) – берём его.
    _total = st.session_state.get('forms_expected_total') \
        or _count_expected(_sel) * st.session_state.get('forms_cities_n', 1)

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
        # Итоговое время прогона = последняя запись лога минус старт.
        _st_ts = st.session_state.get('forms_started_ts')
        _fin_ts = LOG_FILE.stat().st_mtime
        _spent = int(_fin_ts - _st_ts) if _st_ts else None
        _spent_txt = f' · ⏱ заняло {_spent // 60}:{_spent % 60:02d}' if _spent and _spent > 0 else ''
        st.markdown(f'**Статус:** ✅ завершено / остановлено{_spent_txt}')
        with st.expander('Подробный лог', expanded=False):
            st.code('\n'.join(LOG_FILE.read_text(encoding='utf-8', errors='ignore')
                              .splitlines()[-300:]), language='text')
        st.download_button(
            '⬇ Скачать лог (txt)',
            data=LOG_FILE.read_bytes(),
            file_name=f'{_sel}-log.txt',
            mime='text/plain',
            use_container_width=True,
        )
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
