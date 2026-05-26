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
from metrika_404 import (
    fetch_metrika_emails, save_reports_batch, list_stored_reports,
    load_report, MAILBOX_CONFIG, COUNTRY_LABELS,
)


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


def get_metrika_credentials(project_id: str) -> tuple[str | None, str | None]:
    """
    Достать email и пароль приложения для чтения почты Метрики проекта.
    
    Хранятся в Streamlit Secrets под именами:
      metrika_smu_email, metrika_smu_password
      metrika_imp_email, metrika_imp_password (когда добавим)
      metrika_mpe_email, metrika_mpe_password
    
    Возвращает (email, password) или (None, None) если креды не настроены.
    """
    cfg = MAILBOX_CONFIG.get(project_id)
    if not cfg:
        return None, None
    try:
        email = st.secrets.get(cfg['secret_email']) if hasattr(st, 'secrets') else None
        password = st.secrets.get(cfg['secret_password']) if hasattr(st, 'secrets') else None
        return email, password
    except Exception:
        return None, None


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
    /* Светлая палитра — мягкая корпоративная */
    :root {
        --bg: #FFFFFF;
        --bg-elev: #F7FBFE;
        --bg-elev-2: #EEF4FB;
        --border: #E1E8F0;
        --border-strong: #C7D3E1;
        --text: #1E212E;
        --text-soft: #4B5366;
        --text-muted: #7A8294;
        --accent: #1A56E8;
        --accent-hover: #1148C9;
        --accent-soft: rgba(26, 86, 232, 0.08);
        --accent-ring: rgba(26, 86, 232, 0.15);
        --ok: #16A34A;
        --ok-soft: rgba(22, 163, 74, 0.08);
        --warn: #D97706;
        --warn-soft: rgba(217, 119, 6, 0.08);
        --err: #DC2626;
        --err-soft: rgba(220, 38, 38, 0.08);
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
        color: var(--text);
    }
    h1 {
        font-size: 2.4rem !important;
        line-height: 1.1;
        margin-bottom: 0.5rem !important;
    }
    h2 {
        font-size: 1.625rem !important;
        margin-top: 0 !important;
        margin-bottom: 1rem !important;
    }
    h3 {
        font-size: 1.25rem !important;
        margin-bottom: 0.75rem !important;
    }
    /* Базовый размер шрифта чуть крупнее (было ~14px, стало ~16px) */
    p, span, div, label, li {
        color: var(--text);
        font-size: 1rem;
    }
    /* Streamlit использует CSS-переменную --default-font-size */
    .stApp {
        font-size: 16px;
    }
    /* Капшен — чуть крупнее чем штатный мелкий */
    [data-testid="stCaptionContainer"] {
        font-size: 0.9rem !important;
    }
    /* Markdown-абзацы — крупнее */
    .stMarkdown p {
        font-size: 1rem !important;
        line-height: 1.55;
    }

    /* Контейнеры-карточки */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: var(--bg-elev) !important;
        border: 1px solid var(--border) !important;
        border-radius: 12px !important;
        padding: 24px 28px !important;
        margin-bottom: 16px !important;
        transition: border-color 0.15s, box-shadow 0.15s;
    }

    /* Лейблы шагов в заголовках */
    .step-num {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 26px;
        height: 26px;
        background: var(--accent-soft);
        color: var(--accent);
        border-radius: 7px;
        font-size: 13px;
        font-weight: 700;
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
        font-size: 1rem !important;
        transition: border-color 0.15s, box-shadow 0.15s;
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
        box-shadow: 0 0 0 3px var(--accent-ring) !important;
    }
    /* Текст внутри селекта */
    [data-baseweb="select"] [class*="ValueContainer"],
    [data-baseweb="select"] [class*="SingleValue"],
    [data-baseweb="select"] span {
        color: var(--text) !important;
    }

    /* ════════════════════════════════════════════════════════════════
       ВЫПАДАЮЩИЙ СПИСОК (popover селекта)
       Streamlit использует BaseWeb который ставит inline-стили с тёмным
       фоном (#262730). Перебиваем максимально специфичными селекторами.
       ════════════════════════════════════════════════════════════════ */
    div[data-baseweb="popover"] {
        background: transparent !important;
    }
    div[data-baseweb="popover"] > div,
    div[data-baseweb="popover"] ul,
    div[data-baseweb="popover"] [role="listbox"] {
        background: #FFFFFF !important;
        background-color: #FFFFFF !important;
        border: 1px solid #E1E8F0 !important;
        border-radius: 8px !important;
        box-shadow: 0 8px 24px rgba(30, 33, 46, 0.12) !important;
    }
    /* Каждая опция */
    div[data-baseweb="popover"] li,
    div[data-baseweb="popover"] [role="option"],
    div[data-baseweb="popover"] ul > li {
        background: #FFFFFF !important;
        background-color: #FFFFFF !important;
        color: #1E212E !important;
        font-size: 1rem !important;
    }
    div[data-baseweb="popover"] li *,
    div[data-baseweb="popover"] [role="option"] * {
        background: transparent !important;
        background-color: transparent !important;
        color: #1E212E !important;
    }
    /* Hover на опции — голубая подсветка */
    div[data-baseweb="popover"] li:hover,
    div[data-baseweb="popover"] [role="option"]:hover,
    div[data-baseweb="popover"] li[aria-selected="true"],
    div[data-baseweb="popover"] [role="option"][aria-selected="true"] {
        background: #EEF3FB !important;
        background-color: #EEF3FB !important;
        color: #1A56E8 !important;
    }
    div[data-baseweb="popover"] li:hover *,
    div[data-baseweb="popover"] [role="option"]:hover *,
    div[data-baseweb="popover"] li[aria-selected="true"] *,
    div[data-baseweb="popover"] [role="option"][aria-selected="true"] * {
        color: #1A56E8 !important;
    }

    /* ════════════════════════════════════════════════════════════════
       РАДИО-КНОПКИ ПРОФИЛЕЙ — карточный стиль
       ════════════════════════════════════════════════════════════════ */
    [data-testid="stRadio"] > div {
        gap: 8px;
    }
    [data-testid="stRadio"] label {
        background: #FFFFFF !important;
        background-color: #FFFFFF !important;
        border: 1px solid #E1E8F0;
        border-radius: 8px;
        padding: 14px 18px !important;
        margin: 0 !important;
        transition: all 0.15s;
        cursor: pointer;
        /* Flex-выравнивание: точка слева, текст справа, не налезают */
        display: flex !important;
        align-items: flex-start !important;
        gap: 12px !important;
    }
    [data-testid="stRadio"] label:hover {
        border-color: #C7D3E1;
        background: #F7FBFE !important;
        background-color: #F7FBFE !important;
    }
    /* Выбранная карточка профиля */
    [data-testid="stRadio"] label:has(input:checked) {
        border-color: #1A56E8 !important;
        background: #EEF3FB !important;
        background-color: #EEF3FB !important;
    }
    /* Радио-точка (кружок) — фиксированная ширина, не сжимается */
    [data-testid="stRadio"] label > div:first-child {
        flex-shrink: 0 !important;
        margin-top: 2px !important;
    }
    /* Текст внутри label — нормальная ширина, выравнивание по верху */
    [data-testid="stRadio"] label > div:last-child,
    [data-testid="stRadio"] label p {
        flex: 1 !important;
        color: #1E212E !important;
        background: transparent !important;
        background-color: transparent !important;
        font-size: 1rem !important;
        line-height: 1.5 !important;
        margin: 0 !important;
    }

    /* Чек-боксы */
    [data-testid="stCheckbox"] {
        padding: 4px 0;
    }
    [data-testid="stCheckbox"] label p {
        font-size: 1rem !important;
        font-weight: 500;
        color: var(--text) !important;
    }
    /* Сам квадратик чек-бокса */
    [data-testid="stCheckbox"] [data-baseweb="checkbox"] > div:first-child {
        background: var(--bg) !important;
        border-color: var(--border-strong) !important;
    }

    /* Текст радио-кнопок */
    [data-testid="stRadio"] label p,
    [data-testid="stRadio"] label div {
        font-size: 1rem !important;
        color: var(--text) !important;
    }

    /* Главные кнопки */
    .stButton > button {
        font-weight: 600 !important;
        border-radius: 8px !important;
        border: 1px solid var(--border) !important;
        background: var(--bg) !important;
        color: var(--text) !important;
        transition: all 0.15s !important;
        padding: 0.5rem 1.25rem !important;
    }
    .stButton > button:hover {
        border-color: var(--border-strong) !important;
        background: var(--bg-elev) !important;
    }
    /* ════════════════════════════════════════════════════════════════
       Кодовые блоки (st.code) — подробный лог
       Streamlit по умолчанию делает тёмный фон даже на светлой теме.
       ════════════════════════════════════════════════════════════════ */
    [data-testid="stCodeBlock"],
    [data-testid="stCode"],
    pre {
        background: #F7FBFE !important;
        background-color: #F7FBFE !important;
        border: 1px solid #E1E8F0 !important;
        border-radius: 8px !important;
    }
    [data-testid="stCodeBlock"] pre,
    [data-testid="stCode"] pre {
        background: #F7FBFE !important;
        background-color: #F7FBFE !important;
        color: #1E212E !important;
    }
    [data-testid="stCodeBlock"] code,
    [data-testid="stCode"] code,
    pre code {
        color: #1E212E !important;
        background: transparent !important;
        background-color: transparent !important;
        font-size: 0.875rem !important;
    }
    /* Inline-код тоже на всякий случай */
    .stMarkdown code {
        background: #F7FBFE !important;
        color: #1A56E8 !important;
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 0.9rem;
    }

    /* type=primary — это «Запустить» */
    .stButton > button[kind="primary"] {
        background: #1A56E8 !important;
        background-color: #1A56E8 !important;
        border-color: #1A56E8 !important;
        color: #FFFFFF !important;
        box-shadow: 0 1px 3px rgba(26, 86, 232, 0.20);
        font-size: 1.05rem !important;
        font-weight: 700 !important;
        padding: 0.85rem 1.5rem !important;
    }
    /* ВСЕ вложенные элементы внутри кнопки primary — чисто белый текст */
    .stButton > button[kind="primary"] *,
    .stButton > button[kind="primary"] p,
    .stButton > button[kind="primary"] span,
    .stButton > button[kind="primary"] div {
        color: #FFFFFF !important;
        font-weight: 700 !important;
    }
    .stButton > button[kind="primary"]:hover:not(:disabled) {
        background: #1148C9 !important;
        background-color: #1148C9 !important;
        border-color: #1148C9 !important;
        box-shadow: 0 4px 12px rgba(26, 86, 232, 0.30);
        transform: translateY(-1px);
    }
    .stButton > button[kind="primary"]:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    /* Кнопка скачивания — зелёный градиент */
    .stDownloadButton > button {
        background: linear-gradient(180deg, #22C55E 0%, #16A34A 100%) !important;
        background-color: #16A34A !important;
        border: 1px solid #16A34A !important;
        color: #FFFFFF !important;
        font-weight: 700 !important;
        font-size: 1.05rem !important;
        padding: 0.95rem 1.5rem !important;
        border-radius: 8px !important;
        box-shadow: 0 2px 8px rgba(22, 163, 74, 0.20) !important;
        transition: all 0.15s !important;
    }
    /* Все вложенные элементы внутри кнопки — белый текст */
    .stDownloadButton > button *,
    .stDownloadButton > button p,
    .stDownloadButton > button span,
    .stDownloadButton > button div {
        color: #FFFFFF !important;
        font-weight: 700 !important;
        background: transparent !important;
        background-color: transparent !important;
    }
    .stDownloadButton > button:hover {
        background: linear-gradient(180deg, #34D365 0%, #1FB158 100%) !important;
        background-color: #1FB158 !important;
        box-shadow: 0 6px 16px rgba(22, 163, 74, 0.30) !important;
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
        color: var(--text) !important;
    }

    /* Алерты */
    [data-baseweb="notification"] {
        border-radius: 10px !important;
        border-width: 1px !important;
    }

    /* Прогресс-бар */
    [data-testid="stProgress"] > div > div > div {
        background: var(--accent) !important;
    }

    /* Expander */
    [data-testid="stExpander"] {
        border: 1px solid var(--border) !important;
        border-radius: 10px !important;
        background: var(--bg) !important;
    }
    [data-testid="stExpander"] summary {
        font-weight: 500;
    }

    /* Капшен */
    [data-testid="stCaptionContainer"] {
        color: var(--text-muted) !important;
    }

    /* Разделитель */
    hr {
        margin: 1.5rem 0 !important;
        border-color: var(--border) !important;
    }

    /* Ссылки */
    .stMarkdown a {
        color: var(--accent) !important;
        text-decoration: none;
        font-weight: 500;
    }
    .stMarkdown a:hover {
        text-decoration: underline;
    }

    /* Брендинговая шапка */
    .brand-bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding-bottom: 16px;
        margin-bottom: 8px;
    }
    .brand-logo {
        width: 40px;
        height: 40px;
        background: linear-gradient(135deg, var(--accent) 0%, var(--accent-hover) 100%);
        border-radius: 9px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 21px;
        box-shadow: 0 2px 8px rgba(26, 86, 232, 0.25);
    }
    .brand-name {
        font-weight: 700;
        font-size: 1.1rem;
        letter-spacing: -0.01em;
        color: var(--text);
    }
    .brand-sub {
        color: var(--text-muted);
        font-size: 0.875rem;
        margin-left: auto;
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


# ══════════════════════════════════════════════════════════════════
# РАЗДЕЛ: 404 из Метрики
# ══════════════════════════════════════════════════════════════════


with st.container(border=True):
    st.markdown(
        '<h3>📧 404-страницы из Яндекс.Метрики</h3>',
        unsafe_allow_html=True,
    )
    st.caption(
        'Загрузка 404-отчётов из почты Яндекс.Метрики и просмотр истории. '
        'Пока подключён только проект СМУ — ИМП и МПЭ добавим позже.'
    )

    # Селектор проекта (пока только СМУ доступен)
    metrika_project_options = ['СМУ — Сталметурал']
    metrika_project_ids = {'СМУ — Сталметурал': 'smu'}
    metrika_selected_label = st.selectbox(
        'Проект',
        metrika_project_options,
        key='metrika_project',
        label_visibility='collapsed',
    )
    metrika_pid = metrika_project_ids[metrika_selected_label]

    # Проверяем что креды для этого проекта настроены в Secrets
    m_email, m_password = get_metrika_credentials(metrika_pid)
    creds_ok = bool(m_email and m_password)

    if not creds_ok:
        st.warning(
            f'⚠ Для проекта **{metrika_selected_label}** не настроены креды почты. '
            f'Добавьте в Streamlit Secrets:\n\n'
            f'`metrika_{metrika_pid}_email = "адрес@yandex.ru"`\n\n'
            f'`metrika_{metrika_pid}_password = "пароль приложения"`'
        )

    # Кнопка загрузки и инфо-строка
    col_btn, col_info = st.columns([1, 2])
    with col_btn:
        load_clicked = st.button(
            '📥 Загрузить новые из почты',
            type='primary',
            disabled=not creds_ok,
            use_container_width=True,
            key='btn_load_metrika',
        )
    with col_info:
        # Сколько отчётов уже сохранено
        stored = list_stored_reports(metrika_pid)
        if stored:
            countries_count = len(set(r['country_code'] for r in stored))
            st.markdown(
                f'<p style="color:var(--text-soft);margin:8px 0">'
                f'В хранилище: <strong>{len(stored)}</strong> отчётов '
                f'по <strong>{countries_count}</strong> странам</p>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<p style="color:var(--text-muted);margin:8px 0">'
                'Хранилище пустое — нажмите кнопку, чтобы загрузить первые отчёты</p>',
                unsafe_allow_html=True,
            )

    # Обработка нажатия кнопки
    if load_clicked and creds_ok:
        progress_bar = st.progress(0, text='Подключаюсь к почте…')
        log_messages = []
        log_expander = st.expander('Подробный лог', expanded=True)
        log_area = log_expander.empty()

        def append_log(msg: str):
            log_messages.append(msg)
            log_area.code('\n'.join(log_messages[-100:]), language='text')

        def on_log(level, msg):
            append_log(msg)

        def on_progress(done, total):
            if total > 0:
                pct = min(1.0, done / total)
                progress_bar.progress(pct, text=f'Обрабатываю письма ({done}/{total})…')

        try:
            reports = fetch_metrika_emails(
                project_id=metrika_pid,
                email_addr=m_email,
                password=m_password,
                folder=MAILBOX_CONFIG[metrika_pid]['folder'],
                since_days=30,
                log=on_log,
                progress=on_progress,
            )
            new_count = save_reports_batch(reports)
            progress_bar.progress(1.0, text='Готово')
            st.success(
                f'✅ Загружено отчётов: **{len(reports)}** '
                f'(из них новых: **{new_count}**, обновлено: **{len(reports) - new_count}**)'
            )
            # Перезагрузим страницу чтобы список обновился
            st.rerun()
        except PermissionError as e:
            st.error(f'❌ {e}')
        except FileNotFoundError as e:
            st.error(f'❌ {e}')
        except Exception as e:
            st.error(f'❌ Не удалось загрузить отчёты: {e}')

    # ─── История загруженных отчётов ────────────────────────────
    if stored:
        st.markdown('<div style="margin-top:1.5rem"></div>', unsafe_allow_html=True)
        st.markdown(
            f'<p style="color:var(--text-muted);font-size:0.875rem;'
            f'margin-bottom:0.75rem;text-transform:uppercase;letter-spacing:0.05em;'
            f'font-weight:600">Последние отчёты</p>',
            unsafe_allow_html=True,
        )

        # Группируем по дате
        from collections import defaultdict
        by_date = defaultdict(list)
        for r in stored[:40]:  # последние 40 = ~5 дней × 8 стран
            by_date[r['date']].append(r)

        for date_str in sorted(by_date.keys(), reverse=True)[:7]:
            # Заголовок даты
            display_date = date_str
            try:
                d = datetime.strptime(date_str, '%Y-%m-%d')
                display_date = d.strftime('%d.%m.%Y')
            except ValueError:
                pass
            st.markdown(
                f'<p style="font-weight:600;margin:0.75rem 0 0.5rem">📅 {display_date}</p>',
                unsafe_allow_html=True,
            )

            # Метрики по странам этой даты — в одной строке
            day_reports = by_date[date_str]
            cols = st.columns(min(4, len(day_reports)))
            for i, r in enumerate(day_reports):
                col = cols[i % len(cols)]
                with col:
                    badge = '⚠️' if r['total_pages'] > 0 else '✓'
                    color = 'var(--warn)' if r['total_pages'] > 0 else 'var(--ok)'
                    st.markdown(
                        f'<div style="background:var(--bg);border:1px solid var(--border);'
                        f'border-radius:8px;padding:10px 12px;margin-bottom:6px">'
                        f'<div style="font-size:0.875rem;color:var(--text-muted);font-weight:600">'
                        f'{badge} {r["country_code"]} · {r["country_name"]}</div>'
                        f'<div style="font-size:1.1rem;font-weight:600;color:{color};margin-top:2px">'
                        f'{r["total_pages"]} стр. / {r["total_views"]} просм.</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        # Кнопка раскрытия с деталями последнего дня — если есть страницы
        latest = stored[0]
        if latest['total_pages'] > 0:
            with st.expander(
                f'Открыть последний отчёт: {latest["country_code"]} за {latest["date"]} ({latest["total_pages"]} стр.)',
                expanded=False,
            ):
                report = load_report(metrika_pid, latest['country_code'], latest['date'])
                if report:
                    for p in report.pages[:50]:
                        url_part = f' · <a href="{p.page_url}" target="_blank">{p.page_url}</a>' if p.page_url else ''
                        st.markdown(
                            f'<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:0.92rem">'
                            f'<strong>{p.views}</strong> просмотров · <span style="color:var(--text-soft)">{p.page_title}</span>{url_part}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    if len(report.pages) > 50:
                        st.caption(f'... и ещё {len(report.pages) - 50} страниц')


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
