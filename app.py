"""
Site Checker — Streamlit-приложение для проверки доступности сайтов СМУ/ИМП/МПЭ.

Запуск локально:
    streamlit run app.py

На Streamlit Cloud:
    Привязать репозиторий, указать app.py главным файлом.

Структура страницы:
    Шапка с выбором проекта
    ▶ Профиль проверки (Быстрая / Стандартная / Полная / Свои настройки)
    ▶ Что включить (чек-боксы по 6 типам проверок)
    ▶ Кнопка «Запустить»
    После прогона: прогресс-бар, итоги, скачать xlsx
"""
import asyncio
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

from sources import (
    list_projects, load_project_config, load_sources,
    build_plan, build_custom_plan, TYPE_LABELS,
)
from profiles import PROFILES, get_profile_kwargs
from history import load_history, save_history
from sitemap import load_product_pathnames
from http_checker import run_batch, STATUS, SPEED
from reporter import build_report, make_report_filename


PROJECT_ROOT = Path(__file__).parent
REPORTS_DIR = PROJECT_ROOT / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)


def get_proxy_url() -> str | None:
    """
    Достать URL прокси для исходящих запросов.
    Источник в порядке приоритета:
      1. Streamlit Secrets (для деплоя): st.secrets["proxy_url"]
      2. Переменная окружения HTTP_PROXY (для локального запуска)
      3. Если ничего нет — работаем напрямую
    """
    try:
        if hasattr(st, 'secrets') and 'proxy_url' in st.secrets:
            return st.secrets['proxy_url']
    except Exception:
        pass
    import os
    return os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')


# ── Streamlit page config ─────────────────────────────────────────


st.set_page_config(
    page_title='Site Checker',
    page_icon='🔎',
    layout='centered',
    initial_sidebar_state='collapsed',
)


# ── Кастомный CSS: тема Vercel/Anthropic ─────────────────────────


CUSTOM_CSS = """
<style>
    /* Основные переменные палитры */
    :root {
        --bg: #0A0A0A;
        --bg-elev: #111111;
        --bg-elev-2: #1A1A1A;
        --border: #262626;
        --border-strong: #333333;
        --text: #EDEDED;
        --text-soft: #A1A1AA;
        --text-muted: #71717A;
        --accent: #0070F3;
        --accent-hover: #1F8CFF;
        --accent-soft: rgba(0, 112, 243, 0.10);
        --ok: #50E3C2;
        --warn: #F5A623;
        --err: #FF4D4F;
    }

    /* Убираем верхний баннер Streamlit */
    [data-testid="stHeader"] {
        background: transparent;
        height: 0;
    }
    [data-testid="stToolbar"] {
        right: 1rem;
    }

    /* Базовая типографика */
    .stApp {
        background-color: var(--bg);
    }
    h1, h2, h3 {
        font-weight: 600;
        letter-spacing: -0.02em;
    }
    h1 {
        font-size: 2.25rem !important;
        line-height: 1.1;
        margin-bottom: 0.5rem !important;
    }
    h2 {
        font-size: 1.5rem !important;
        margin-top: 0 !important;
        margin-bottom: 1rem !important;
    }
    h3 {
        font-size: 1.125rem !important;
        margin-bottom: 0.75rem !important;
    }

    /* Контейнеры-карточки — оборачиваем визуально каждую секцию */
    .scope-card {
        background: var(--bg-elev);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 24px 28px;
        margin-bottom: 16px;
    }

    /* Лейблы шагов в заголовках */
    .step-num {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 24px;
        height: 24px;
        background: var(--accent-soft);
        color: var(--accent);
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        margin-right: 10px;
        vertical-align: 2px;
    }

    /* Поля ввода / селекты */
    [data-baseweb="select"] > div,
    .stTextInput input,
    .stTextArea textarea,
    .stNumberInput input {
        background: var(--bg) !important;
        border: 1px solid var(--border) !important;
        border-radius: 8px !important;
        color: var(--text) !important;
        transition: border-color 0.15s;
    }
    [data-baseweb="select"] > div:hover,
    .stTextInput input:hover,
    .stTextArea textarea:hover,
    .stNumberInput input:hover {
        border-color: var(--border-strong) !important;
    }
    [data-baseweb="select"] > div:focus-within,
    .stTextInput input:focus,
    .stTextArea textarea:focus,
    .stNumberInput input:focus {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px var(--accent-soft) !important;
    }

    /* Радио-кнопки — карточный стиль */
    [data-testid="stRadio"] > div {
        gap: 8px;
    }
    [data-testid="stRadio"] label {
        background: var(--bg);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 12px 16px;
        margin: 0 !important;
        transition: all 0.15s;
        cursor: pointer;
    }
    [data-testid="stRadio"] label:hover {
        border-color: var(--border-strong);
        background: var(--bg-elev-2);
    }
    [data-testid="stRadio"] label[data-checked="true"],
    [data-testid="stRadio"] label:has(input:checked) {
        border-color: var(--accent);
        background: var(--accent-soft);
    }

    /* Чек-боксы */
    [data-testid="stCheckbox"] {
        padding: 4px 0;
    }
    [data-testid="stCheckbox"] label p {
        font-size: 0.95rem !important;
        font-weight: 500;
    }

    /* Главные кнопки — крупные, заметные */
    .stButton > button {
        font-weight: 600 !important;
        border-radius: 8px !important;
        border: 1px solid var(--border) !important;
        background: var(--bg-elev-2) !important;
        color: var(--text) !important;
        transition: all 0.15s !important;
        padding: 0.5rem 1.25rem !important;
    }
    .stButton > button:hover {
        border-color: var(--border-strong) !important;
        background: var(--bg-elev) !important;
    }
    /* type=primary — это «Запустить» */
    .stButton > button[kind="primary"] {
        background: var(--accent) !important;
        border-color: var(--accent) !important;
        color: white !important;
        box-shadow: 0 0 0 0 var(--accent-soft);
        font-size: 1rem !important;
        padding: 0.65rem 1.5rem !important;
    }
    .stButton > button[kind="primary"]:hover:not(:disabled) {
        background: var(--accent-hover) !important;
        border-color: var(--accent-hover) !important;
        box-shadow: 0 0 0 4px var(--accent-soft);
    }
    .stButton > button[kind="primary"]:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    /* Кнопка скачивания — стиль «success», крупная */
    .stDownloadButton > button {
        background: linear-gradient(180deg, #4CAF50 0%, #43A047 100%) !important;
        border: 1px solid #4CAF50 !important;
        color: white !important;
        font-weight: 600 !important;
        font-size: 1rem !important;
        padding: 0.75rem 1.5rem !important;
        border-radius: 8px !important;
        box-shadow: 0 2px 8px rgba(76, 175, 80, 0.25) !important;
        transition: all 0.15s !important;
    }
    .stDownloadButton > button:hover {
        background: linear-gradient(180deg, #5CBB60 0%, #4CB14F 100%) !important;
        box-shadow: 0 4px 12px rgba(76, 175, 80, 0.35) !important;
        transform: translateY(-1px);
    }

    /* Метрики — карточный вид */
    [data-testid="stMetric"] {
        background: var(--bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px 18px;
        transition: border-color 0.15s;
    }
    [data-testid="stMetric"]:hover {
        border-color: var(--border-strong);
    }
    [data-testid="stMetricLabel"] {
        color: var(--text-muted) !important;
        font-size: 0.75rem !important;
        text-transform: uppercase !important;
        letter-spacing: 0.05em !important;
        font-weight: 600 !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.875rem !important;
        font-weight: 600 !important;
        margin-top: 4px;
    }

    /* Алерты (info/warning/error/success) */
    [data-baseweb="notification"] {
        border-radius: 10px !important;
        border-width: 1px !important;
    }
    /* info */
    [data-testid="stAlert"][data-baseweb="notification"] {
        background: var(--accent-soft) !important;
        border-color: var(--accent) !important;
    }

    /* Прогресс-бар — синий */
    [data-testid="stProgress"] > div > div > div {
        background: var(--accent) !important;
    }

    /* Expander */
    [data-testid="stExpander"] {
        border: 1px solid var(--border) !important;
        border-radius: 10px !important;
        background: var(--bg-elev) !important;
    }
    [data-testid="stExpander"] summary {
        font-weight: 500;
    }

    /* Капшен — нежно-серый */
    [data-testid="stCaptionContainer"] {
        color: var(--text-muted) !important;
    }

    /* Разделитель — тоньше */
    hr {
        margin: 1.5rem 0 !important;
        border-color: var(--border) !important;
    }

    /* Подсветка ссылок в проблемах */
    .stMarkdown a {
        color: var(--accent) !important;
        text-decoration: none;
    }
    .stMarkdown a:hover {
        text-decoration: underline;
    }

    /* Брендинговая шапка — тонкая полоска с лого */
    .brand-bar {
        display: flex;
        align-items: center;
        gap: 10px;
        padding-bottom: 16px;
        margin-bottom: 8px;
    }
    .brand-logo {
        width: 36px;
        height: 36px;
        background: var(--accent);
        border-radius: 8px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 20px;
    }
    .brand-name {
        font-weight: 600;
        font-size: 1.05rem;
        letter-spacing: -0.01em;
    }
    .brand-sub {
        color: var(--text-muted);
        font-size: 0.875rem;
        margin-left: auto;
    }

    /* Большой ободок вокруг результатов */
    .results-banner {
        background: linear-gradient(180deg, rgba(80, 227, 194, 0.06) 0%, transparent 100%);
        border: 1px solid var(--ok);
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 16px;
    }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ── Session state defaults ────────────────────────────────────────


def init_session():
    """Установить дефолты в session_state при первом заходе."""
    defaults = {
        'project_id': None,
        'sources': None,
        'sources_project_id': None,    # для какого проекта загружены sources
        'profile': 'standard',
        'check_main': True,
        'check_catalog': True,
        'check_categories': True,
        'check_filters': True,
        'check_products': True,
        'check_text': True,
        'is_running': False,
        'run_results': None,            # последние результаты прогона
        'run_report_path': None,        # путь к скачиваемому файлу
        'run_started_at': None,
        'run_finished_at': None,
        'custom_urls_text': '',         # содержимое textarea в custom-режиме
        'custom_save_list': False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_session()


# ── Утилиты ────────────────────────────────────────────────────────


def format_duration(sec: int) -> str:
    """Секунды → 'X сек' / 'X,Y мин' / 'H ч M мин'."""
    if sec < 60:
        return f'{sec} сек'
    if sec < 3600:
        m = sec / 60
        if m < 10:
            return f'{m:.1f} мин'.replace('.', ',')
        return f'{int(m)} мин'
    h = sec // 3600
    m = (sec % 3600) // 60
    return f'{h} ч {m} мин' if m else f'{h} ч'


def reset_run_state():
    """Сброс баннера прошлого результата при изменении настроек."""
    st.session_state.run_results = None
    st.session_state.run_report_path = None


@st.cache_data(ttl=3600, show_spinner='Загружается каталог проекта…')
def cached_load_sources(project_id: str):
    """Кеш каталога проекта в session (1 час)."""
    cfg = load_project_config(project_id)
    src = load_sources(cfg)
    return cfg, src


# ── Шапка ──────────────────────────────────────────────────────────


st.markdown("""
<div class="brand-bar">
    <div class="brand-logo">🔎</div>
    <div>
        <div class="brand-name">Site Checker</div>
    </div>
    <div class="brand-sub">проверка доступности сайтов</div>
</div>
""", unsafe_allow_html=True)

st.markdown(
    '<p style="color:var(--text-soft);font-size:0.95rem;margin-bottom:1.5rem">'
    'Автоматическая проверка доступности страниц СМУ, ИМП, МПЭ. '
    'Главные страницы · Каталог · Категории · Фильтры · Товары · Битые переменные'
    '</p>',
    unsafe_allow_html=True,
)


# ── Шаг 1: выбор проекта ───────────────────────────────────────────


# ── Шаг 1: выбор проекта ───────────────────────────────────────────


with st.container(border=True):
    st.markdown('<h3><span class="step-num">1</span>Какой сайт проверяем</h3>', unsafe_allow_html=True)

    projects = list_projects()
    project_options = ['— выберите —'] + [p['name'] for p in projects] + ['Свой список URL']
    project_to_id = {p['name']: p['id'] for p in projects}

    # Подсчёт текущего индекса для select-box
    current_label = '— выберите —'
    if st.session_state.project_id == '__custom__':
        current_label = 'Свой список URL'
    elif st.session_state.project_id:
        for p in projects:
            if p['id'] == st.session_state.project_id:
                current_label = p['name']
                break

    selected_label = st.selectbox(
        'Проект',
        project_options,
        index=project_options.index(current_label),
        label_visibility='collapsed',
    )

    if selected_label == 'Свой список URL':
        new_pid = '__custom__'
    elif selected_label == '— выберите —':
        new_pid = None
    else:
        new_pid = project_to_id[selected_label]

    # Если проект сменился — сброс результата прошлого прогона
    if new_pid != st.session_state.project_id:
        reset_run_state()
        st.session_state.project_id = new_pid


# ── Дальше расходится логика: обычный проект ИЛИ custom-режим ──────


is_custom = (st.session_state.project_id == '__custom__')
is_project = (st.session_state.project_id is not None and not is_custom)


# ═══════════════════════════════════════════════════════════════════
# CUSTOM MODE — свой список URL
# ═══════════════════════════════════════════════════════════════════


if is_custom:
    with st.container(border=True):
        st.markdown('<h3>Список URL для проверки</h3>', unsafe_allow_html=True)
        st.caption(
            'Вставьте ссылки – по одной на строку. Можно загрузить из файла (.txt или .csv). '
            'Если протокол не указан, добавится https://. Строки после символа # игнорируются.'
        )

        uploaded = st.file_uploader(
            'Загрузить .txt / .csv',
            type=['txt', 'csv'],
            label_visibility='collapsed',
        )
        if uploaded:
            try:
                text = uploaded.read().decode('utf-8', errors='replace')
                # Для CSV — берём первое поле каждой строки
                if uploaded.name.lower().endswith('.csv'):
                    lines = []
                    for line in text.splitlines():
                        cells = line.split(',') if ',' in line else line.split(';')
                        first = cells[0].strip().strip('"').strip("'")
                        lines.append(first)
                    text = '\n'.join(lines)
                # Дописываем к существующему
                existing = st.session_state.custom_urls_text.strip()
                st.session_state.custom_urls_text = (existing + '\n' + text) if existing else text
            except Exception as e:
                st.error(f'Не удалось прочитать файл: {e}')

        custom_text = st.text_area(
            'URLs',
            value=st.session_state.custom_urls_text,
            height=240,
            label_visibility='collapsed',
            placeholder='https://example.com/page1\nhttps://example.com/page2\nexample.com/page3',
            key='custom_urls_input',
        )
        if custom_text != st.session_state.custom_urls_text:
            st.session_state.custom_urls_text = custom_text
            reset_run_state()

        # Парсим URL'ы из текста
        custom_plan = build_custom_plan(custom_text.split('\n'))
        valid_count = len(custom_plan.tasks)

        col1, col2 = st.columns([3, 1])
        with col1:
            if valid_count == 0:
                st.info('Введите URL\'ы')
            else:
                st.success(f'Готово к проверке: {valid_count} URL')
        with col2:
            save_list = st.checkbox(
                'Сохранить список',
                value=st.session_state.custom_save_list,
                help='Список останется в сессии и появится при следующем открытии',
            )
            st.session_state.custom_save_list = save_list

        check_text_custom = st.checkbox(
            'Искать битые переменные в текстах',
            value=st.session_state.check_text,
            help='{{переменная}}, %name%, undefined и [object Object]',
        )
        st.session_state.check_text = check_text_custom


# ═══════════════════════════════════════════════════════════════════
# PROJECT MODE — обычный режим с профилями и каталогом
# ═══════════════════════════════════════════════════════════════════


elif is_project:
    # Загружаем каталог из cache
    try:
        cfg, src = cached_load_sources(st.session_state.project_id)
        st.session_state.sources = src
        st.session_state.sources_project_id = st.session_state.project_id
    except Exception as e:
        st.error(f'Не удалось загрузить каталог: {e}')
        st.stop()

    # ─── Метрики проекта в одной карточке ─────────────────────
    stats = src.stats
    with st.container(border=True):
        st.markdown(
            f'<p style="color:var(--text-muted);font-size:0.875rem;'
            f'margin-bottom:0.75rem;text-transform:uppercase;letter-spacing:0.05em;'
            f'font-weight:600">Каталог проекта</p>',
            unsafe_allow_html=True,
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Городов', stats['subdomains_count'])
        c2.metric('Категорий', f'{stats["categories_count"]:,}'.replace(',', ' '))
        if stats['has_filters']:
            c3.metric('Фильтров', f'{stats["filters_count"]:,}'.replace(',', ' '))
        else:
            c3.metric('Фильтров', 'нет')
        c4.metric('Главный город', cfg.get('mandatory_city', 'Москва'))

    # ─── Шаг 2: профиль в карточке ───────────────────────────
    with st.container(border=True):
        st.markdown('<h3><span class="step-num">2</span>Что и сколько проверять</h3>', unsafe_allow_html=True)

        profile_labels = {pid: f'{p["label"]} — {p["description"]}' for pid, p in PROFILES.items()}
        profile_choices = list(profile_labels.keys()) + ['custom']

        def profile_format(pid):
            if pid == 'custom':
                return 'Свои настройки — задать вручную'
            return profile_labels[pid]

        new_profile = st.radio(
            'Профиль',
            profile_choices,
            index=profile_choices.index(st.session_state.profile) if st.session_state.profile in profile_choices else 0,
            format_func=profile_format,
            label_visibility='collapsed',
        )
        if new_profile != st.session_state.profile:
            st.session_state.profile = new_profile
            reset_run_state()

        # Если выбран профиль — подставляем его значения. Если «Свои» — даём слайдеры
        if st.session_state.profile == 'custom':
            st.markdown('<p style="color:var(--text-muted);font-size:0.875rem;margin-top:1rem;margin-bottom:0.5rem">Параметры выборки</p>', unsafe_allow_html=True)
            col1, col2 = st.columns(2)
            with col1:
                random_subs = st.number_input(
                    'Случайных городов',
                    min_value=0, max_value=stats['subdomains_count'] - 1,
                    value=5, step=1,
                    help='Москва добавится автоматически',
                )
                cats_per_sub = st.number_input(
                    'Категорий на каждый город',
                    min_value=0, max_value=50, value=5, step=1,
                )
            with col2:
                if stats['has_filters']:
                    filters_per_sub = st.number_input(
                        'Фильтров на каждый город',
                        min_value=0, max_value=50, value=5, step=1,
                    )
                else:
                    filters_per_sub = 0
                    st.markdown('_У проекта нет фильтров_')
                products_per_sub = st.number_input(
                    'Товаров на каждый город',
                    min_value=0, max_value=50, value=3, step=1,
                )
        else:
            kw = get_profile_kwargs(st.session_state.profile)
            random_subs = kw['random_subdomains_count']
            cats_per_sub = kw['categories_per_subdomain']
            filters_per_sub = kw['filters_per_subdomain'] if stats['has_filters'] else 0
            products_per_sub = kw['products_per_subdomain']

    # ─── Шаг 3: чек-боксы в карточке ────────────────────────
    with st.container(border=True):
        st.markdown('<h3><span class="step-num">3</span>Какие пункты включить</h3>', unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            check_main = st.checkbox('🏠 Главные страницы', value=st.session_state.check_main, help='Пункт 1.1 чек-листа')
            check_catalog = st.checkbox('📁 Страница /catalog/', value=st.session_state.check_catalog, help='Пункт 1.2')
            check_categories = st.checkbox('📂 Категории', value=st.session_state.check_categories, help='Пункт 1.3')
        with c2:
            if stats['has_filters']:
                check_filters = st.checkbox('🏷️ Фильтры', value=st.session_state.check_filters, help='Пункт 1.4')
            else:
                check_filters = False
                st.markdown('<span style="color:var(--text-muted)">🏷️ Фильтры _(нет в каталоге проекта)_</span>', unsafe_allow_html=True)
            check_products = st.checkbox('🛒 Карточки товаров', value=st.session_state.check_products, help='Пункт 1.5 — из sitemap.xml')
            check_text = st.checkbox('🔤 Битые переменные', value=st.session_state.check_text, help='Пункт 1.6 — {{city}}, %price%, undefined и т.д.')

        # сохраняем в state
        for key, val in [
            ('check_main', check_main), ('check_catalog', check_catalog),
            ('check_categories', check_categories), ('check_filters', check_filters),
            ('check_products', check_products), ('check_text', check_text),
        ]:
            if val != st.session_state[key]:
                st.session_state[key] = val
                reset_run_state()

    # ─── Оценка плана + кнопка запуска в одной карточке ───────
    selected_cities_count = 1 + random_subs  # Москва + случайные
    per_sub = (
        (1 if check_main else 0) +
        (1 if check_catalog else 0) +
        (cats_per_sub if check_categories else 0) +
        (filters_per_sub if check_filters else 0) +
        (products_per_sub if check_products else 0)
    )
    total_checks = selected_cities_count * per_sub
    # Реалистичная оценка:
    #   - 5 сек на запрос (с учётом тяжёлых страниц)
    #   - +20% буфер на возможные ретраи
    #   - +30% если проект через прокси (двойной хоп через сервер)
    base_per_request_sec = 5
    proxy_overhead = 1.30 if cfg.get('use_proxy') else 1.0
    retry_buffer = 1.20
    estimated_sec = max(1, int((total_checks / 6) * base_per_request_sec * proxy_overhead * retry_buffer))


# ═══════════════════════════════════════════════════════════════════
# КНОПКА ЗАПУСКА (общая для обоих режимов)
# ═══════════════════════════════════════════════════════════════════


if is_project or is_custom:
    can_run = False
    if is_custom:
        can_run = valid_count > 0
    elif is_project:
        can_run = total_checks > 0

    # Карточка с превью + кнопкой
    with st.container(border=True):
        if is_project:
            if total_checks == 0:
                st.warning('Не выбрано ни одного пункта для проверки')
            else:
                st.markdown(
                    f'<p style="color:var(--text-muted);font-size:0.875rem;'
                    f'margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:0.05em;'
                    f'font-weight:600">Готов к запуску</p>'
                    f'<p style="font-size:1.05rem;margin-bottom:0.5rem">'
                    f'Будет проверено <strong style="color:var(--accent)">{selected_cities_count} городов</strong> × '
                    f'<strong style="color:var(--accent)">{per_sub} страниц</strong> = '
                    f'<strong style="color:var(--accent)">{total_checks} проверок</strong>'
                    f'</p>'
                    f'<p style="color:var(--text-soft);font-size:0.9rem;margin-bottom:1rem">'
                    f'Примерно <strong>{format_duration(estimated_sec)}</strong>. '
                    f'На больших каталогах или при медленных серверах может быть в 1,5–2 раза дольше.'
                    f'</p>',
                    unsafe_allow_html=True,
                )
        elif is_custom and valid_count > 0:
            # Custom-режим — без прокси, но с буфером на ретраи
            est_sec = max(1, int((valid_count / 6) * 5 * 1.20))
            st.markdown(
                f'<p style="color:var(--text-muted);font-size:0.875rem;'
                f'margin-bottom:0.5rem;text-transform:uppercase;letter-spacing:0.05em;'
                f'font-weight:600">Готов к запуску</p>'
                f'<p style="font-size:1.05rem;margin-bottom:0.5rem">'
                f'Будет проверено <strong style="color:var(--accent)">{valid_count} URL</strong>'
                f'</p>'
                f'<p style="color:var(--text-soft);font-size:0.9rem;margin-bottom:1rem">'
                f'Примерно <strong>{format_duration(est_sec)}</strong>. '
                f'Точное время зависит от скорости сайтов.'
                f'</p>',
                unsafe_allow_html=True,
            )

        if st.button(
            '▶ Запустить проверку',
            type='primary',
            disabled=not can_run,
            use_container_width=True,
            key='btn_run',
        ):
            st.session_state.is_running = True
            reset_run_state()
            st.rerun()


# ═══════════════════════════════════════════════════════════════════
# ВЫПОЛНЕНИЕ ПРОГОНА
# ═══════════════════════════════════════════════════════════════════


async def run_check_async(project_id, plan, options, on_progress):
    """Async обёртка вокруг run_batch."""
    return await run_batch(
        plan.tasks,
        concurrency=options.get('concurrency', 6),
        timeout_ms=options.get('timeout_ms', 120000),
        max_attempts=options.get('max_attempts', 3),
        retry_delay_ms=options.get('retry_delay_ms', 2500),
        check_text=options.get('check_text', True),
        on_progress=on_progress,
        proxy_url=options.get('proxy_url'),
    )


if st.session_state.is_running:
    with st.container(border=True):
        st.markdown('<h3>⏳ Идёт проверка</h3>', unsafe_allow_html=True)

        # Крупное предупреждение чтобы контент-менеджер случайно не закрыл вкладку
        st.warning(
            '⚠ **Не закрывайте вкладку до окончания проверки.** '
            'Можно переключаться на другие вкладки, но эту нужно держать открытой — '
            'если её закрыть, прогон оборвётся и отчёт не сохранится.'
        )

        # Места для прогресс-бара и счётчика
        progress_bar = st.progress(0, text='Подготовка…')
        metrics_row = st.empty()
        log_expander = st.expander('Подробный лог', expanded=False)
        log_area = log_expander.empty()
        log_messages = []

    def append_log(msg: str):
        log_messages.append(msg)
        log_area.code('\n'.join(log_messages[-100:]), language='text')

    # ─── Формируем план ────────────────────────────────────────
    started_ms = int(time.time() * 1000)
    st.session_state.run_started_at = started_ms

    # Прокси применяется только когда проект явно его разрешает (use_proxy: true).
    # Custom-режим всегда без прокси.
    # Для каждого проекта в projects/{id}.json — флаг use_proxy.
    proxy_url = None

    try:
        if is_custom:
            plan = build_custom_plan(st.session_state.custom_urls_text.split('\n'))
            project_id_for_report = 'custom'
            project_name_for_report = 'Свой список URL'
            check_text_opt = st.session_state.check_text
            append_log(f'Запуск custom-прогона: {len(plan.tasks)} URL')
            append_log('Прокси: не используется (custom-режим)')

        else:
            cfg = load_project_config(st.session_state.project_id)
            src = st.session_state.sources

            # Решение про прокси на основе use_proxy в конфиге
            if cfg.get('use_proxy'):
                proxy_url = get_proxy_url()
                if proxy_url:
                    append_log(f'Прокси: включён для проекта {cfg["name"]}')
                else:
                    append_log(f'⚠ Прокси нужен для {cfg["name"]}, но не настроен в Streamlit Secrets')
            else:
                append_log(f'Прокси: не используется (для {cfg["name"]} не требуется)')

            # Загружаем sitemap если нужны товары
            if st.session_state.check_products and not src.products:
                append_log(f'Загружаю sitemap из {cfg.get("sitemap_url")}…')
                try:
                    sm = asyncio.run(load_product_pathnames(
                        cfg,
                        [c for c in src.categories],
                        [f for f in src.filters],
                        log=lambda lvl, msg: append_log(msg),
                        proxy_url=proxy_url,
                    ))
                    src.products = sm.get('pathnames', [])
                    append_log(f'Из sitemap: {len(src.products)} товаров')
                    if sm.get('warning'):
                        append_log(f'⚠ {sm["warning"]}')
                except Exception as e:
                    append_log(f'⚠ Не удалось загрузить sitemap: {e}')

            # Загружаем историю ротации
            history = load_history(st.session_state.project_id)
            recent_paths = set(history.keys())
            append_log(f'История ротации: {len(recent_paths)} URL за последние 7 дней')

            plan = build_plan(
                src,
                random_subdomains_count=random_subs,
                categories_per_subdomain=cats_per_sub,
                filters_per_subdomain=filters_per_sub,
                products_per_subdomain=products_per_sub,
                check_main=check_main,
                check_catalog=check_catalog,
                check_categories=check_categories,
                check_filters=check_filters,
                check_products=check_products,
                mandatory_city=cfg.get('mandatory_city', 'Москва'),
                rotation_history=recent_paths,
            )
            check_text_opt = check_text
            project_id_for_report = st.session_state.project_id
            project_name_for_report = cfg['name']

            cities_str = ', '.join(s.city for s in plan.selected_subdomains)
            append_log(f'Города: {cities_str}')
            append_log(f'Всего проверок: {len(plan.tasks)}')

        # ─── Запуск прогона ────────────────────────────────────
        total = len(plan.tasks)
        counters = {'ok': 0, 'warn': 0, 'err': 0}

        def on_progress(result, done, total_n):
            if result.is_ok:
                counters['ok'] += 1
            elif result.is_warning:
                counters['warn'] += 1
            else:
                counters['err'] += 1
            # Не обновляем UI на каждый чек — Streamlit сам выводит в конце
            # (обновление UI в async-функции из Streamlit делать тяжело)

        results = asyncio.run(run_check_async(
            project_id_for_report,
            plan,
            options={
                'concurrency': 6,
                'timeout_ms': 120000,
                'max_attempts': 3,
                'retry_delay_ms': 2500,
                'check_text': check_text_opt,
                'proxy_url': proxy_url,
            },
            on_progress=on_progress,
        ))

        finished_ms = int(time.time() * 1000)
        st.session_state.run_finished_at = finished_ms

        # Сохраняем историю ротации (для обычных прогонов)
        if is_project:
            checked_paths = list(set(urlparse(r.url).path for r in results))
            save_history(st.session_state.project_id, checked_paths)

        # ─── Формируем xlsx-отчёт ──────────────────────────────
        append_log('Формирую xlsx-отчёт…')
        report_filename = make_report_filename(
            project_id_for_report, started_ms, REPORTS_DIR,
        )
        report_path = REPORTS_DIR / report_filename
        build_report(
            project_name=project_name_for_report,
            started_at_ms=started_ms,
            finished_at_ms=finished_ms,
            selected_subdomains=plan.selected_subdomains,
            results=results,
            output_path=report_path,
        )

        st.session_state.run_results = results
        st.session_state.run_report_path = str(report_path)
        st.session_state.is_running = False

        append_log(f'Готово. Отчёт: {report_filename}')

        progress_bar.progress(1.0, text='Готово')
        st.rerun()

    except Exception as e:
        st.error(f'Ошибка: {e}')
        st.session_state.is_running = False
        append_log(f'❌ Ошибка: {e}')


# ═══════════════════════════════════════════════════════════════════
# РЕЗУЛЬТАТ ПРОГОНА (после завершения)
# ═══════════════════════════════════════════════════════════════════


if st.session_state.run_results and not st.session_state.is_running:
    results = st.session_state.run_results
    total = len(results)
    ok_count = sum(1 for r in results if r.is_ok)
    warn_count = sum(1 for r in results if r.is_warning)
    err_count = total - ok_count - warn_count
    text_issues_count = sum(len(r.text_issues) for r in results if r.has_text_issues)
    duration = (st.session_state.run_finished_at - st.session_state.run_started_at) // 1000

    # ─── Главная карточка результатов ─────────────────────────
    with st.container(border=True):
        # Зелёная плашка с заголовком
        any_problems = err_count > 0 or warn_count > 0 or text_issues_count > 0
        if not any_problems:
            st.markdown(
                f'<div style="background:linear-gradient(180deg, rgba(80, 227, 194, 0.10) 0%, transparent 100%);'
                f'border-left:3px solid var(--ok);padding:14px 18px;border-radius:8px;margin-bottom:1rem">'
                f'<p style="margin:0;font-size:1.1rem"><strong style="color:var(--ok)">✓ Все проверки прошли успешно</strong></p>'
                f'<p style="margin:6px 0 0 0;color:var(--text-soft)">'
                f'Проверено {total} страниц за {format_duration(duration)}. Проблем не найдено.</p>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            problems_summary = []
            if err_count > 0:
                problems_summary.append(f'{err_count} {"страница не работает" if err_count == 1 else "страниц не работают"}')
            if warn_count > 0:
                problems_summary.append(f'{warn_count} с предупреждениями')
            if text_issues_count > 0:
                problems_summary.append(f'{text_issues_count} битых переменных')
            st.markdown(
                f'<div style="background:linear-gradient(180deg, rgba(245, 166, 35, 0.10) 0%, transparent 100%);'
                f'border-left:3px solid var(--warn);padding:14px 18px;border-radius:8px;margin-bottom:1rem">'
                f'<p style="margin:0;font-size:1.1rem"><strong style="color:var(--warn)">Найдены проблемы</strong></p>'
                f'<p style="margin:6px 0 0 0;color:var(--text-soft)">'
                f'{", ".join(problems_summary)}. Проверено {total} страниц за {format_duration(duration)}.</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Метрики
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Всего', total)
        c2.metric('✅ Работает', ok_count)
        c3.metric('⚠ Предупреждений', warn_count)
        c4.metric('❌ Не работает', err_count, delta_color='inverse')

        # ─── ВЫДЕЛЕННАЯ кнопка скачивания ─────────────────────
        if st.session_state.run_report_path:
            report_path = Path(st.session_state.run_report_path)
            if report_path.exists():
                st.markdown('<div style="margin-top:1rem"></div>', unsafe_allow_html=True)
                with open(report_path, 'rb') as f:
                    st.download_button(
                        label=f'📥 Скачать полный отчёт ({report_path.name})',
                        data=f.read(),
                        file_name=report_path.name,
                        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        use_container_width=True,
                        type='primary',
                    )
                st.caption(f'В отчёте: все проверки в формате xlsx с фильтрами по статусу')

    # ─── Список проблем в отдельной карточке ──────────────
    problems = [r for r in results if r.is_error or r.is_warning or r.has_text_issues]
    if problems:
        with st.container(border=True):
            st.markdown(
                f'<h3 style="margin-bottom:1rem">Список проблем '
                f'<span style="color:var(--text-muted);font-weight:400;font-size:0.95rem">({len(problems)})</span>'
                f'</h3>',
                unsafe_allow_html=True,
            )
            status_labels = {
                'ok': 'Работает',
                'redirect': 'Перенаправление',
                'not_found': 'Страница не найдена',
                'client_error': 'Ошибка на сайте',
                'server_error': 'Сервер не отвечает',
                'timeout': 'Нет ответа',
                'network_error': 'Нет соединения',
            }
            for r in problems[:50]:
                if r.is_error:
                    emoji = '❌'
                    color = 'var(--err)'
                elif r.is_warning:
                    emoji = '⚠️'
                    color = 'var(--warn)'
                else:
                    emoji = '🔤'
                    color = 'var(--warn)'

                status_text = status_labels.get(r.status, r.status)
                extra = ''
                if r.has_text_issues:
                    extra = f' · <span style="color:var(--warn)">{len(r.text_issues)} битых переменных</span>'

                city_part = f'[{r.city}] ' if r.city else ''
                st.markdown(
                    f'<div style="padding:8px 0;border-bottom:1px solid var(--border);font-size:0.92rem">'
                    f'{emoji} <strong>{city_part}</strong>{r.type_label}: '
                    f'<a href="{r.url}" target="_blank" style="color:var(--accent);text-decoration:none">{r.url}</a> '
                    f'— <span style="color:{color}">{status_text}</span>{extra}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            if len(problems) > 50:
                st.caption(f'... и ещё {len(problems) - 50}. Все детали — в xlsx-отчёте.')


# ── Футер ──────────────────────────────────────────────────────────


st.divider()
st.caption(
    'Если приложение не открывалось дольше недели, при первом заходе '
    'может появиться сообщение «Yes, get this app back up» — нажмите на эту '
    'кнопку и подождите 30–60 секунд, пока сервис проснётся.'
)
st.caption(
    '_Проверка работоспособности сайтов · '
    'Site Checker v2.0 (Python + Streamlit)_'
)
