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
    layout='wide',
    initial_sidebar_state='collapsed',
)


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


st.title('🔎 Site Checker')
st.caption(
    'Автоматическая проверка доступности страниц СМУ, ИМП, МПЭ. '
    'Главные страницы · Каталог · Категории · Фильтры · Товары · Битые переменные'
)


# ── Шаг 1: выбор проекта ───────────────────────────────────────────


st.subheader('Шаг 1. Какой сайт проверяем')

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
    st.divider()
    st.subheader('Список URL для проверки')
    st.caption(
        'Вставьте ссылки – по одной на строку. Можно загрузить из файла (.txt или .csv). '
        'Если протокол не указан, добавится https://. Строки после символа # игнорируются.'
    )

    uploaded = st.file_uploader(
        'Загрузить .txt / .csv',
        type=['txt', 'csv'],
        label_visibility='visible',
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

    # Парсим URL'ы из текста (та же логика что в sources.build_custom_plan)
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

    st.divider()


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

    # Метрики проекта
    stats = src.stats
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Городов', stats['subdomains_count'])
    c2.metric('Категорий', f'{stats["categories_count"]:,}'.replace(',', ' '))
    if stats['has_filters']:
        c3.metric('Фильтров', f'{stats["filters_count"]:,}'.replace(',', ' '))
    else:
        c3.metric('Фильтров', 'нет')
    c4.metric('Главный город', cfg.get('mandatory_city', 'Москва'))

    st.divider()

    # ─── Шаг 2: профиль ──────────────────────────────────────────
    st.subheader('Шаг 2. Что и сколько проверять')

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
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            random_subs = st.number_input(
                'Случайных городов',
                min_value=0, max_value=stats['subdomains_count'] - 1,
                value=5, step=1,
                help='Москва добавится автоматически',
            )
        with col2:
            cats_per_sub = st.number_input(
                'Категорий на каждый город',
                min_value=0, max_value=50, value=5, step=1,
            )
        with col3:
            if stats['has_filters']:
                filters_per_sub = st.number_input(
                    'Фильтров на каждый город',
                    min_value=0, max_value=50, value=5, step=1,
                )
            else:
                filters_per_sub = 0
                st.markdown('_У проекта нет фильтров_')
        with col4:
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

    st.divider()

    # ─── Шаг 3: какие пункты включать ───────────────────────────
    st.subheader('Шаг 3. Какие пункты включить')

    c1, c2, c3 = st.columns(3)
    with c1:
        check_main = st.checkbox('🏠 Главные страницы', value=st.session_state.check_main, help='1.1')
        check_catalog = st.checkbox('📁 Страница /catalog/', value=st.session_state.check_catalog, help='1.2')
    with c2:
        check_categories = st.checkbox('📂 Категории', value=st.session_state.check_categories, help='1.3')
        if stats['has_filters']:
            check_filters = st.checkbox('🏷️ Фильтры', value=st.session_state.check_filters, help='1.4')
        else:
            check_filters = False
    with c3:
        check_products = st.checkbox('🛒 Карточки товаров', value=st.session_state.check_products, help='1.5 — из sitemap.xml')
        check_text = st.checkbox('🔤 Битые переменные в текстах', value=st.session_state.check_text, help='1.6')

    # сохраняем в state
    for key, val in [
        ('check_main', check_main), ('check_catalog', check_catalog),
        ('check_categories', check_categories), ('check_filters', check_filters),
        ('check_products', check_products), ('check_text', check_text),
    ]:
        if val != st.session_state[key]:
            st.session_state[key] = val
            reset_run_state()

    st.divider()

    # ─── Оценка плана ───────────────────────────────────────────
    selected_cities_count = 1 + random_subs  # Москва + случайные
    per_sub = (
        (1 if check_main else 0) +
        (1 if check_catalog else 0) +
        (cats_per_sub if check_categories else 0) +
        (filters_per_sub if check_filters else 0) +
        (products_per_sub if check_products else 0)
    )
    total_checks = selected_cities_count * per_sub
    estimated_sec = max(1, (total_checks // 6) * 3)  # 3 сек на запрос при concurrency 6

    if total_checks == 0:
        st.warning('Сейчас не выбрано ни одного пункта для проверки')
    else:
        st.info(
            f'Будет проверено **{selected_cities_count} городов** × **{per_sub} страниц** = '
            f'**{total_checks} проверок**. Примерное время: **{format_duration(estimated_sec)}**'
        )


# ═══════════════════════════════════════════════════════════════════
# КНОПКА ЗАПУСКА (общая для обоих режимов)
# ═══════════════════════════════════════════════════════════════════


if is_project or is_custom:
    can_run = False
    if is_custom:
        can_run = valid_count > 0
    elif is_project:
        can_run = total_checks > 0

    btn_label = '▶ Запустить проверку'
    if st.button(btn_label, type='primary', disabled=not can_run, use_container_width=True):
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
    st.divider()
    st.subheader('Идёт проверка')

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

    # Достаём прокси один раз (нужен для всех async-вызовов в этом блоке)
    proxy_url = get_proxy_url()
    if proxy_url:
        # Не показываем сам URL (там креды), просто факт что прокси настроен
        append_log(f'Прокси: настроен')

    try:
        if is_custom:
            plan = build_custom_plan(st.session_state.custom_urls_text.split('\n'))
            project_id_for_report = 'custom'
            project_name_for_report = 'Свой список URL'
            check_text_opt = st.session_state.check_text
            append_log(f'Запуск custom-прогона: {len(plan.tasks)} URL')

        else:
            cfg = load_project_config(st.session_state.project_id)
            src = st.session_state.sources

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
    st.divider()
    st.subheader('✅ Результаты проверки')

    results = st.session_state.run_results
    total = len(results)
    ok_count = sum(1 for r in results if r.is_ok)
    warn_count = sum(1 for r in results if r.is_warning)
    err_count = total - ok_count - warn_count
    text_issues_count = sum(len(r.text_issues) for r in results if r.has_text_issues)

    duration = (st.session_state.run_finished_at - st.session_state.run_started_at) // 1000

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Всего', total)
    c2.metric('✅ Работает', ok_count)
    c3.metric('⚠ Предупреждений', warn_count)
    c4.metric('❌ Не работает', err_count, delta_color='inverse')

    info_parts = [f'**Длительность:** {format_duration(duration)}']
    if text_issues_count > 0:
        info_parts.append(f'**Битых переменных:** {text_issues_count}')
    st.info(' · '.join(info_parts))

    # Скачивание отчёта
    if st.session_state.run_report_path:
        report_path = Path(st.session_state.run_report_path)
        if report_path.exists():
            with open(report_path, 'rb') as f:
                st.download_button(
                    label=f'⬇ Скачать отчёт ({report_path.name})',
                    data=f.read(),
                    file_name=report_path.name,
                    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    use_container_width=True,
                )

    # Показываем проблемы списком
    problems = [r for r in results if r.is_error or r.is_warning or r.has_text_issues]
    if problems:
        with st.expander(f'Список проблем ({len(problems)})', expanded=True):
            for r in problems[:50]:
                emoji = '❌' if r.is_error else '⚠'
                if r.has_text_issues and not (r.is_error or r.is_warning):
                    emoji = '🔤'
                status_text = {
                    'ok': 'Работает',
                    'redirect': 'Перенаправление',
                    'not_found': 'Страница не найдена',
                    'client_error': 'Ошибка на сайте',
                    'server_error': 'Сервер не отвечает',
                    'timeout': 'Нет ответа',
                    'network_error': 'Нет соединения',
                }.get(r.status, r.status)

                extra = ''
                if r.has_text_issues:
                    extra = f' · {len(r.text_issues)} битых переменных'
                st.markdown(f'{emoji} **[{r.city}]** {r.type_label}: [{r.url}]({r.url}) — {status_text}{extra}')

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
