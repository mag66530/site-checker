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
import json
import os
import random
import re
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
    'Украина': '🇺🇦',
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
    'avia': {'name': 'АПС – Авиапромсталь', 'domain': 'aviastal.ru'},
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
                    # проверить_цель НЕ считаем: строки целей идут на отдельный
                    # лист «Цели», а прогресс считает строки основного лога.
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


# Где искать форму НА странице (для подсказки «?»). Ключ – имя формы, как в отчёте.
# Если формы нет в словаре, подсказка не показывается.
_FORM_WHERE = {
    # СМУ
    'Оставить заявку': 'Кнопка в шапке сайта, открывает окно',
    'Заказ звонка': 'Кнопка «Заказать звонок» в шапке, открывает окно',
    'Обратная связь': 'Внизу главной, блок с картой и контактами',
    'Заказать обратный звонок': 'Форма на главной странице',
    'Заказ расчета': 'Форма «Заказ расчёта» на главной странице',
    'Срочный заказ': 'Форма на странице каталога',
    'Не нашли что искали?': 'Форма внизу страницы листинга',
    'Купить в один клик (товарная)': 'Кнопка у товара, открывает окно',
    'Консультация со специалистом': 'Кнопка на карточке товара, открывает окно',
    'Нашли дешевле': 'Ссылка у цены товара, открывает окно',
    'Купить в один клик (листинг)': 'Кнопка на карточке в листинге, открывает окно',
    'Вакансии': 'Кнопка «Откликнуться» на странице вакансий, открывает окно',
    'Расчет стоимости доставки': 'Кнопка на странице «Доставка», открывает окно',
    'Подписка на рассылку': 'Форма в подвале (домен Хабаровска)',
    'Связаться с менеджером': 'Кнопка на главной (СНГ-домены), открывает окно',
    'Оформление заказа': 'Товар в корзину, дальше оформление заказа',
    # ИМП
    'Нужна консультация?': 'Синяя форма на главной странице',
    'Предпочитаете устное обсуждение?': 'Форма внизу страницы',
    'Заявка (шапка)': 'Кнопка «Оставить заявку» в шапке, открывает окно',
    'Нет времени выбирать?': 'Баннер на странице листинга, открывает окно',
    'Нужна консультация? (товарная)': 'Форма на карточке товара',
    'Не нашли ответа на свой вопрос?': 'Форма внизу карточки товара',
    'Быстрый заказ': 'Кнопка у товара, открывает окно',
    'Оставить отзыв или задать вопрос': 'Вкладка «Отзывы» на карточке товара',
    'Интересует вакансия?': 'Страница вакансий, после кнопки «Узнать подробнее»',
    # МПЭ (Заявка (шапка) уже выше)
    'Рассчитать стоимость доставки': 'Форма в подвале главной страницы',
    'Рассчитать заказ': 'Кнопка в подвале (та же форма, с прикреплением файла)',
    'Обратная связь (Контакты)': 'Форма на странице «Контакты»',
    'Нужна консультация по этому товару?': 'Кнопка на карточке товара, открывает окно',
    'Нашли дешевле? Снизим цену!': 'Кнопка на карточке товара, открывает окно',
    'Оформление заказа через корзину': 'Товар в корзину, дальше оформление заказа',
    # АПС (Авиапромсталь)
    'Заказать звонок': 'Кнопка «Звонок» в шапке, открывает окно «Заявка на обратный звонок»',
    'Экспресс заявка': 'Кнопка в шапке, открывает окно',
    'Оформить заявку': 'Товар в корзину, дальше форма «Ваш заказ» на странице корзины',
}


def _rows_done(xlsx: Path):
    """Сколько форм уже записано в лог (строки минус шапка). None – не прочиталось."""
    if not xlsx.exists():
        return 0
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xlsx, read_only=True)
        # Считаем строки ИМЕННО листа «Логи» (не активного и не «Цели»):
        # строки целей на отдельном листе не должны раздувать прогресс.
        ws = wb["Логи"] if "Логи" in wb.sheetnames else wb.active
        n = (ws.max_row or 1) - 1
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

# ── Данные проекта (нужны сценариям, доменам и формам ниже) ──────────
_cities = _load_cities(pid_key)
_chosen_cities = []          # список названий городов для прогона ([] = основной сайт)
_all_names = [c['city'] for c in _cities]
_mains = _main_domains(_cities) if _cities else []   # основной домен каждой страны
_main_by_country = {c['country']: c for c in _mains}
_groups = {}                                         # страны в порядке справочника
for c in _cities:
    _groups.setdefault(c['country'], []).append(c['city'])
_all_forms = _list_forms(pid_key)
_all_form_names = [f['name'] for f in _all_forms]

_MODE_OPTIONS = ['Основные домены (по странам)', 'Выбрать города', 'Случайные города']
_FORMS_MODE_OPTIONS = ['Все формы', 'Выбрать формы']
# Запоминаются ТОЛЬКО настройки «Случайных городов» (числа по странам).
# Остальные галочки каждый заход начинаются с дефолта – так предсказуемее.
_RND_FILE = ROOT / 'cache' / 'forms' / pid_key / 'random_cities.json'


def _round_robin(total):
    """Раздаёт total по странам по кругу (начиная с России), не больше,
    чем городов в стране."""
    _counts = {k: 0 for k in _groups}
    _left = int(total)
    while _left > 0:
        _gave = False
        for k in _groups:
            if _left <= 0:
                break
            if _counts[k] < len(_groups[k]):
                _counts[k] += 1
                _left -= 1
                _gave = True
        if not _gave:
            break
    return _counts


def _rnd_save():
    """Автосохранение чисел «Случайных городов» для проекта: сколько всего и
    по каждой стране. Подхватываются при следующем заходе."""
    try:
        data = {
            'всего': int(st.session_state.get(f'fc_rnd_total_{pid_key}', 0) or 0),
            'по_странам': {k: int(st.session_state.get(f'fc_rnd_{pid_key}_{k}', 0) or 0)
                           for k in _groups},
        }
        _RND_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RND_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


# Сохранённые числа «Случайных городов» подхватываем сами (раз за сессию).
if not st.session_state.get(f'fc_rnd_loaded_{pid_key}'):
    st.session_state[f'fc_rnd_loaded_{pid_key}'] = True
    if _RND_FILE.exists():
        try:
            _d = json.loads(_RND_FILE.read_text(encoding='utf-8'))
            _by = _d.get('по_странам') or {}
            if _by:
                for k in _groups:
                    if k in _by:
                        st.session_state[f'fc_rnd_{pid_key}_{k}'] = max(
                            0, min(int(_by[k] or 0), len(_groups[k])))
                st.session_state[f'fc_rnd_total_{pid_key}'] = min(
                    sum(int(st.session_state.get(f'fc_rnd_{pid_key}_{k}', 0) or 0)
                        for k in _groups),
                    len(_all_names))
        except Exception:
            pass

# Подсказки «?» на этой странице нужны (в app.py они выключены глобально).
# Английскую техподсказку «Press Enter to apply» у числовых полей прячем –
# значение и так применяется по Enter или клику мимо поля.
st.markdown(
    '<style>[data-testid="stTooltipIcon"], [data-testid="stTooltipHoverTarget"] '
    '{ display: inline-flex !important; }\n'
    '[data-testid="InputInstructions"] { display: none !important; }</style>',
    unsafe_allow_html=True,
)

# ── Домены и поддомены ───────────────────────────────────────────────
# Домен = основной сайт страны (mepen.ru, mepen.kz…); поддомен = город на
# этом домене (spb.mepen.ru). Без справочника городов – только основной сайт.
if _cities:
    _example_sub = _host(_cities[1]['url']) if len(_cities) > 1 else ''
    st.subheader(
        'Домены и поддомены',
        help='Домен – основной сайт страны (например ' + _host(_mains[0]['url']) + '). '
             'Поддомен – город на этом домене (например ' + _example_sub + '). '
             'Заявка с каждого выбранного сайта должна прийти на свою почту из справочника.',
    )
    st.session_state.setdefault(f'fc_mode_{pid_key}', _MODE_OPTIONS[0])
    _mode = st.radio(
        'Что проверяем', _MODE_OPTIONS,
        horizontal=True, label_visibility='collapsed',
        key=f'fc_mode_{pid_key}',
    )

    if _mode == 'Основные домены (по странам)':
        # Одна строка на страну: галочка «Страна – домен». Галочки уже стоят.
        def _mk(country):
            return f'fc_main_{pid_key}_{country}'
        for c in _mains:                       # дефолт: все страны включены
            if _mk(c['country']) not in st.session_state:
                st.session_state[_mk(c['country'])] = True

        # Список доменов СЛЕВА, кнопка-переключатель СПРАВА (в той же строке –
        # без пустого пространства над списком). «Снять все», если всё отмечено,
        # иначе «Выбрать все».
        _all_on = all(st.session_state.get(_mk(c['country']), True) for c in _mains)
        _left, _right = st.columns([4.2, 1.3], vertical_alignment='top')
        with _right:
            if st.button('Снять все' if _all_on else 'Выбрать все',
                         use_container_width=True, key=f'fc_main_toggle_{pid_key}'):
                for c in _mains:
                    st.session_state[_mk(c['country'])] = not _all_on
                st.rerun()
        _sel = []
        with _left:
            for c in _mains:
                _lbl = (f"{_COUNTRY_FLAG.get(c['country'], '🏳')} **{c['country']}** – "
                        f"`{_host(c['url'])}`")
                _hlp = f"Главный сайт страны, город: {c['city']}."
                if c.get('mail'):
                    _hlp += f" Заявка должна прийти на {c['mail']}."
                if st.checkbox(_lbl, key=_mk(c['country']), help=_hlp):
                    _sel.append(c['city'])
        _chosen_cities = _sel
        st.caption(f'Выбрано доменов: **{len(_sel)} / {len(_mains)}**.')

    elif _mode == 'Случайные города':
        # Один экран: сверху общее число (само распределяется по странам),
        # ниже строки стран с числом справа. Правишь число у страны – общее
        # пересчитывается. Основной домен страны всегда идёт первым.
        _tkey = f'fc_rnd_total_{pid_key}'

        def _ckey(country):
            return f'fc_rnd_{pid_key}_{country}'

        def _apply_total():
            for k, v in _round_robin(st.session_state[_tkey]).items():
                st.session_state[_ckey(k)] = v
            _rnd_save()

        def _apply_country():
            st.session_state[_tkey] = sum(
                int(st.session_state.get(_ckey(k), 0) or 0) for k in _groups)
            _rnd_save()

        def _apply_recommended_rnd():
            """Рекомендованный сценарий случайной проверки: Россия – 2 (Москва +
            случайный поддомен), остальные страны – по 1 (их основные домены)."""
            for k in _groups:
                st.session_state[_ckey(k)] = min(2 if k == 'Россия' else 1,
                                                 len(_groups[k]))
            st.session_state[_tkey] = sum(
                int(st.session_state.get(_ckey(k), 0) or 0) for k in _groups)
            _rnd_save()

        if _tkey not in st.session_state:      # первичная инициализация
            st.session_state[_tkey] = min(7, len(_all_names))
            _apply_total()

        # Поле «сколько всего» и кнопка рекомендованного – в одной строке рядом,
        # кнопка выровнена по нижнему краю поля. Звезда ★ текстовая – рисуется
        # цветом текста сайта (эмодзи ⭐ всегда жёлтая).
        # ВАЖНО: значение поля _tkey нельзя менять после его отрисовки, поэтому
        # кнопка работает через колбэк on_click (он выполняется ДО отрисовки полей).
        _tc, _rec, _ = st.columns([2.2, 2.3, 1.5], vertical_alignment='bottom')
        with _tc:
            st.number_input(
                'Сколько всего доменов/поддоменов проверить',
                min_value=0, max_value=len(_all_names), step=1,
                key=_tkey, on_change=_apply_total,
                help='Введи число и нажми Enter (или кликни по пустому месту) – '
                     'оно применится и само распределится по странам ниже, начиная '
                     'с России. Число любой страны можно поправить вручную – общее '
                     'пересчитается.')
        _rec.button('★ Рекомендованный сценарий', use_container_width=True,
                    key=f'fc_rnd_rec_{pid_key}', on_click=_apply_recommended_rnd,
                    help='Поставит рекомендованные числа: Россия – 2, остальные '
                         'страны – по 1 (основной домен каждой страны + случайный '
                         'поддомен России).')

        _counts = {}
        for _country, _names in _groups.items():
            _row_l, _row_n, _row_c = st.columns([3, 1, 0.9], vertical_alignment='center')
            _dom = _host(_main_by_country.get(_country, {}).get('url', ''))
            _row_l.markdown(
                f"{_COUNTRY_FLAG.get(_country, '🏳')} **{_country}** – `{_dom}`")
            if _ckey(_country) not in st.session_state:
                st.session_state[_ckey(_country)] = 0
            _counts[_country] = int(_row_n.number_input(
                _country, min_value=0, max_value=len(_names), step=1,
                key=_ckey(_country), on_change=_apply_country,
                label_visibility='collapsed'))
            _row_c.caption(f'из {len(_names)}')

        # Сборка списка: основной домен страны первым, остальное – случайно.
        _parts = []
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
            _pick = _pick[:_k]
            _chosen_cities += _pick
            _parts.append(f"**{_country}**: {', '.join(_pick)}")
        if _chosen_cities:
            st.caption(f'Выбрано {len(_chosen_cities)} – ' + ' · '.join(_parts) +
                       '. Случайные города пересоберутся при запуске.')
        st.caption('Числа запоминаются для этого проекта и подставятся при '
                   'следующем заходе.')

    else:  # Выбрать города – СЕТКА ЧЕКБОКСОВ по странам
        _main_cities = {c['city'] for c in _mains}

        def _ck(city):
            return f'fc_cb_{pid_key}_{city}'
        # один раз ставим дефолт: отмечены ОСНОВНЫЕ домены каждой страны
        # (Москва, aviastal.kz, Минск…), остальные города добираются вручную.
        if not st.session_state.get(f'fc_init_osn_{pid_key}'):
            for nm in _all_names:
                st.session_state[_ck(nm)] = (nm in _main_cities)
            st.session_state[f'fc_init_osn_{pid_key}'] = True

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
            st.markdown(
                f"**{_COUNTRY_FLAG.get(_country, '🏳')} {_country}**  ·  {len(_names)} "
                f"<span title='Основной домен: {_dom}' "
                f"style='color:#8A8782;cursor:help;font-size:.85em'>?</span>",
                unsafe_allow_html=True)
            _cols = st.columns(6)
            for _i, _nm in enumerate(_names):
                if _cols[_i % 6].checkbox(_nm, key=_ck(_nm)):
                    _sel.append(_nm)
        _chosen_cities = _sel
        st.caption(f'Выбрано: **{len(_sel)} / {len(_all_names)}** городов.')

    st.divider()

_cities_none = bool(_cities) and not _chosen_cities

# ── Формы ────────────────────────────────────────────────────────────
# Список форм проекта (в порядке прогона) уже собран выше (_all_forms).
# По умолчанию – все; можно выбрать только нужные. Имена совпадают с отчётом.
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

        # Группировка по СТРАНИЦЕ сайта (без техназваний: Главная_расчёты и
        # Главная – это одна «Главная»). Всё в карточке с рамкой, между
        # группами тонкая линия. Подсказка «?» – где форма НА странице
        # (шапка/подвал/кнопка-окно), только если она есть в _FORM_WHERE.
        def _pg_group(page):
            return (page or '').split('_')[0] or 'Прочее'

        def _disp_name(nm):
            """Имя формы для показа: без уточнения в скобках на конце
            («Обратная связь (Контакты)» – показываем «Обратная связь»,
            уточнение живёт в подсказке «?»). В отчёт и фильтр идёт полное имя."""
            _b = re.sub(r'\s*\([^()]*\)\s*$', '', nm).strip()
            return _b or nm

        _by_group = {}
        for f in _all_forms:
            _by_group.setdefault(_pg_group(f['page']), []).append(f)
        _sel_f = []
        with st.container(border=True):
            for _gi, (_grp, _items) in enumerate(_by_group.items()):
                if _gi:
                    st.markdown(
                        '<hr style="margin:4px 0 10px 0; border:none; '
                        'border-top:1px solid #ECEAE4">',
                        unsafe_allow_html=True)
                _gcol, _ccol = st.columns([1.1, 4.9], vertical_alignment='top')
                # небольшой отступ сверху, чтобы название страницы стояло
                # ровно по первой строке галочек
                _gcol.markdown(
                    f"<div style='padding-top:.4rem;font-weight:600'>{_grp}</div>",
                    unsafe_allow_html=True)
                _fcols = _ccol.columns(3)
                for _i, _f in enumerate(_items):
                    _hint = _FORM_WHERE.get(_f['name'])
                    if _fcols[_i % 3].checkbox(
                            _disp_name(_f['name']), key=_fk(_f['name']),
                            help=_hint):
                        _sel_f.append(_f['name'])
        _chosen_forms = _sel_f
        st.caption(f'Выбрано форм: **{len(_chosen_forms)} / {len(_all_forms)}**.')
    else:
        st.caption(f'Будут проверены все формы проекта: {len(_all_forms)}.')
    st.divider()

_forms_all_selected = (len(_chosen_forms) == len(_all_form_names))
_forms_none = bool(_all_forms) and len(_chosen_forms) == 0

# ── Запуск ──────────────────────────────────────────────────────────
st.subheader('Запуск проверки')

st.session_state.setdefault(f'fc_clear_{pid_key}', True)
st.session_state.setdefault(f'fc_show_{pid_key}', False)
clear_log = st.checkbox('Очищать лог Excel перед прогоном', key=f'fc_clear_{pid_key}')
show_browser = st.checkbox('Показывать окно браузера', key=f'fc_show_{pid_key}')
st.caption('По умолчанию браузер работает скрыто (headless) – окно не '
           'показывается, отчёт всё равно формируется. Включи галочку выше, '
           'если хочешь видеть, как он заполняет формы.')

st.caption('Запуск фоновый – интерфейс сразу свободен. Можно уйти в чек-листы '
           'и работать параллельно, проверка крутится сама. Заявки '
           'отправляются по-настоящему (формы оформления заказа – без отправки).')

_alive = _pid_alive(_read_pid())

if _forms_none:
    st.warning('Не выбрано ни одной формы – отметь хотя бы одну, чтобы запустить.')
if _cities_none:
    st.warning('Не выбрано ни одного домена/города – отметь хотя бы один, чтобы запустить.')

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
