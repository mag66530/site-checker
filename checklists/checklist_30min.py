"""
Чек-лист 30 мин — еженедельная проверка сайта-проекта помощником/джуном.

Пункты чек-листа:
  1. Доступность сайта/поддоменов и визуальные ошибки — парсинг по случайной
     выборке 300–500 URL (главная, каталог, категории всех уровней, фильтры,
     товары) + проверка текстовых блоков и переменных. Выборка не повторяется
     от недели к неделе: ротация с окном 30 дней.
  2. Сбор найденных ошибок и отправка seo-специалисту и руководителю проекта —
     автоматически: Telegram-уведомление с xlsx-отчётом после прогона.
  3. Проверка почты проекта и уведомлений вебмастера, метрики, GSC — руками.
  4. Проверка вебмастера и GSC на ошибки — руками.
  5. Проверка метрики (или GA) — сравнение трафика — руками.
  6. Проверка замены рекламного номера — руками.

Сейчас вкладка автоматизирует пункты 1–2 (прогон + отправка отчёта).
Ручная часть (пункты 3–6 чек-листом с галочками) временно убрана.
"""
import asyncio
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

from sources import list_projects, load_project_config, load_sources, build_plan
from history import load_history, save_history, WEEKLY_TTL_MS
from sitemap import load_product_pathnames
from product_links import load_product_links
from http_checker import run_batch
from reporter import build_report, make_report_filename
from telegram_notify import format_summary_message, send_run_notification
from metrika_404 import (
    MAILBOX_CONFIG,
    fetch_incremental, get_latest_available_date,
    load_reports_for_date, load_reports_for_period,
)
from webmaster_notify import (
    WEBMASTER_YANDEX_CONFIG, GSC_GMAIL_CONFIG,
    YABUSINESS_YANDEX_CONFIG, TWOGIS_YANDEX_CONFIG, GOOGLE_ACCOUNTS_CONFIG,
    PRIORITY_LABELS, PRIORITY_ORDER, CATEGORY_LABELS,
    fetch_webmaster_yandex, fetch_gsc_gmail,
    fetch_yandex_folder_simple, fetch_google_accounts,
    load_notifications, group_by_priority,
)

PROJECT_ROOT = Path(__file__).parent.parent
REPORTS_DIR = PROJECT_ROOT / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)


# ── Секреты (тот же подход, что в 15-минутном чек-листе) ───────────


def _secret(key):
    try:
        if hasattr(st, 'secrets') and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def get_proxy_url():
    val = _secret('proxy_url')
    if val:
        return val
    import os
    return os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')


def get_metrika_credentials(project_id):
    cfg = MAILBOX_CONFIG.get(project_id)
    if not cfg:
        return None, None
    return _secret(cfg['secret_email']), _secret(cfg['secret_password'])


def get_gsc_credentials(project_id):
    cfg = GSC_GMAIL_CONFIG.get(project_id)
    if not cfg:
        return None, None
    return _secret(cfg['secret_email']), _secret(cfg['secret_password'])


def get_yabusiness_credentials(project_id):
    cfg = YABUSINESS_YANDEX_CONFIG.get(project_id)
    if not cfg:
        return None, None, None
    return _secret(cfg['secret_email']), _secret(cfg['secret_password']), cfg['folder']


def get_twogis_credentials(project_id):
    cfg = TWOGIS_YANDEX_CONFIG.get(project_id)
    if not cfg:
        return None, None, None
    return _secret(cfg['secret_email']), _secret(cfg['secret_password']), cfg['folder']


def get_google_accounts_credentials(project_id):
    cfg = GOOGLE_ACCOUNTS_CONFIG.get(project_id)
    if not cfg:
        return None, None
    return _secret(cfg['secret_email']), _secret(cfg['secret_password'])


def get_telegram_recipients(project_id):
    val = _secret(f'telegram_recipients_{project_id}')
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, (list, tuple)):
        return [str(v).strip() for v in val if str(v).strip()]
    return []


def format_duration(sec: int) -> str:
    if sec < 60:
        return f'{sec} сек'
    if sec < 3600:
        m = sec / 60
        return (f'{m:.1f} мин'.replace('.', ',')) if m < 10 else f'{int(m)} мин'
    h, m = sec // 3600, (sec % 3600) // 60
    return f'{h} ч {m} мин' if m else f'{h} ч'


# ── Теги отдела ──────────────────────────────────────────────────────

_TAG_META = {
    'разработка': ('💻', '#1D4ED8', 'rgba(29,78,216,0.09)'),
    'SEO':        ('🔎', '#16A34A', 'rgba(22,163,74,0.09)'),
    'контент':    ('✏️', '#D97706', 'rgba(217,119,6,0.09)'),
}


def _tags_html(tags: list[str]) -> str:
    if not tags:
        return ''
    parts = ['<span style="margin-left:10px;font-size:0.75rem;color:#9CA3AF">Отдел:</span>']
    for t in tags:
        if t in _TAG_META:
            icon, color, bg = _TAG_META[t]
            parts.append(
                f'<span style="display:inline-block;padding:2px 10px;margin-left:4px;'
                f'border-radius:10px;background:{bg};color:{color};'
                f'font-size:0.78rem;font-weight:700;vertical-align:middle">'
                f'{icon} {t}</span>'
            )
    return ''.join(parts)


def _dept_tags_result(r) -> list[str]:
    tags: list[str] = []
    if r.is_error:
        if r.status in ('server_error', 'timeout', 'network_error'):
            tags.append('разработка')
        elif r.status == 'not_found':
            tags += ['SEO', 'разработка']
        else:
            tags.append('разработка')
    elif r.is_warning:
        tags.append('SEO')
    if r.speed_rating in ('slow', 'very_slow') and 'разработка' not in tags:
        tags.append('разработка')
    if r.has_text_issues:
        tags.append('разработка')
    if getattr(r, 'has_content_bugs', False):
        if 'разработка' not in tags:
            tags.append('разработка')
    return list(dict.fromkeys(tags))


_NOTIF_CAT_DEPT = {
    'server':    ['разработка'],
    'speed':     ['разработка'],
    'security':  ['разработка'],
    'indexing':  ['SEO'],
    'coverage':  ['SEO'],
    'structure': ['SEO'],
    'other':     ['SEO'],
}


def _dept_tags_notif(n) -> list[str]:
    return _NOTIF_CAT_DEPT.get(n.category, ['SEO'])


# ── Session state ───────────────────────────────────────────────────


def init_session():
    defaults = {
        'c30_project_id': None,
        'c30_is_running': False,
        'c30_results': None,
        'c30_report_path': None,
        'c30_started_at': None,
        'c30_finished_at': None,
        # URL-проверки
        'c30_check_main': True,
        'c30_check_catalog': True,
        'c30_check_categories': True,
        'c30_check_filters': True,
        'c30_check_products': True,
        # Сервисные проверки
        'c30_check_webmaster': True,
        'c30_check_gsc': True,
        'c30_fetch_notifications': True,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_session()


@st.cache_data(ttl=3600, show_spinner='Загружается каталог проекта…')
def c30_load_sources(project_id: str):
    cfg = load_project_config(project_id)
    src = load_sources(cfg)
    return cfg, src


# ── Распределение бюджета выборки ──────────────────────────────────


def split_budget(target_urls: int, cities: int, has_filters: bool) -> dict:
    """
    Разложить общий размер выборки (300–500 URL) на параметры build_plan.

    На каждый город: главная + каталог (фикс) + категории/фильтры/товары.
    Категории — самая большая доля (все уровни вложенности), затем фильтры
    и товары. Если фильтров у проекта нет — их долю делят категории и товары.
    """
    per_city = max(target_urls // max(cities, 1), 4)
    rest = per_city - 2          # минус главная и каталог
    if has_filters:
        cats = max(round(rest * 0.45), 1)
        filters = max(round(rest * 0.30), 1)
        products = max(rest - cats - filters, 1)
    else:
        cats = max(round(rest * 0.60), 1)
        filters = 0
        products = max(rest - cats, 1)
    return {
        'cats': cats,
        'filters': filters,
        'products': products,
        'per_city': 2 + cats + filters + products,
    }


# ── Шапка ───────────────────────────────────────────────────────────


st.title('Чек-лист 30 мин')
st.caption(
    'Еженедельная 30-минутная проверка сайта-проекта помощником/джуном. '
    'Доступность, визуальные ошибки и структура — по случайной выборке URL.'
)

# Локальный CSS только для этой страницы: primary-кнопка («Запустить
# еженедельную проверку»). app.py красит белым саму кнопку, но текст лежит
# во вложенном <p>, который глобальное правило перекрашивает в тёмный —
# получалась чёрная кнопка без видимого текста. Здесь явно белим и текст.
st.markdown(
    """
    <style>
    div[data-testid="stButton"] > button[kind="primary"],
    div[data-testid="stButton"] > button[data-testid="stBaseButton-primary"],
    div[data-testid="stButton"] > button[data-testid="baseButton-primary"] {
        background: #1A1A1A !important;
        border: 1px solid #1A1A1A !important;
        color: #FFFFFF !important;
    }
    div[data-testid="stButton"] > button[kind="primary"] *,
    div[data-testid="stButton"] > button[data-testid="stBaseButton-primary"] *,
    div[data-testid="stButton"] > button[data-testid="baseButton-primary"] * {
        color: #FFFFFF !important;
    }
    div[data-testid="stButton"] > button[kind="primary"]:hover {
        background: #000000 !important;
        border-color: #000000 !important;
    }
    /* Зелёная кнопка скачивания отчёта — чтобы не путалась с primary */
    div[data-testid="stDownloadButton"] > button,
    div[data-testid="stDownloadButton"] > button * {
        color: #FFFFFF !important;
    }
    div[data-testid="stDownloadButton"] > button {
        background: #16A34A !important;
        border: 1px solid #16A34A !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Выбор проекта ───────────────────────────────────────────────────


with st.container(border=True):
    st.markdown('### Какой сайт проверяем')
    projects = list_projects()
    options = ['— выберите —'] + [p['name'] for p in projects]
    name_to_id = {p['name']: p['id'] for p in projects}

    current = '— выберите —'
    for p in projects:
        if p['id'] == st.session_state.c30_project_id:
            current = p['name']

    label = st.selectbox('Проект', options, index=options.index(current),
                         label_visibility='collapsed', key='c30_project_select')
    new_pid = name_to_id.get(label)
    if new_pid != st.session_state.c30_project_id:
        st.session_state.c30_project_id = new_pid
        st.session_state.c30_results = None
        st.session_state.c30_report_path = None

pid = st.session_state.c30_project_id

if pid:
    try:
        cfg, src = c30_load_sources(pid)
    except Exception as e:
        st.error(f'Не удалось загрузить каталог: {e}')
        st.stop()
    stats = src.stats

    # ── Пункт 1: доступность и визуальные ошибки ───────────────────
    with st.container(border=True):
        st.markdown('### 1. Доступность и визуальные ошибки')
        st.caption(
            'Случайная выборка 300–500 URL: главная (1.1), каталог (1.2), '
            'категории всех уровней (1.3), фильтры (1.4), товары (1.5) '
            'и текстовые блоки с переменными (1.6). На каждой странице — '
            'код ответа, скорость и структура: цена, кнопки заказа, H1, '
            'шапка/подвал. Выборка не повторяется: URL, проверенные за '
            'последние 30 дней, попадают в неё в 3 раза реже.'
        )

        col1, col2 = st.columns(2)
        with col1:
            target_urls = st.slider(
                'Размер выборки, URL', min_value=300, max_value=500,
                value=400, step=50, key='c30_target',
            )
        with col2:
            random_cities = st.number_input(
                'Случайных городов (Москва добавится сама)',
                min_value=4, max_value=min(30, stats['subdomains_count'] - 1),
                value=min(9, stats['subdomains_count'] - 1), step=1,
                key='c30_cities',
            )

        cities_total = 1 + int(random_cities)
        budget = split_budget(target_urls, cities_total, stats['has_filters'])
        plan_total = cities_total * budget['per_city']

        products_base = load_product_links(pid)
        products_note = ''
        if products_base and products_base['pathnames']:
            d = datetime.fromtimestamp(products_base['collected_at_ms'] / 1000)
            products_note = (
                f'Товары — из базы листингов ({len(products_base["pathnames"])} шт., '
                f'собрана {d.strftime("%d.%m.%Y")}'
                + (', ⚠ старше 30 дней' if products_base['is_stale'] else '')
                + ').'
            )
        else:
            products_note = 'Базы листингов нет — товары возьмём из sitemap.xml.'

        st.markdown(
            f'На каждый из **{cities_total} городов**: главная + каталог + '
            f'{budget["cats"]} категорий'
            + (f' + {budget["filters"]} фильтров' if budget['filters'] else '')
            + f' + {budget["products"]} товаров — итого **{plan_total} проверок**. '
            f'{products_note}'
        )
        est_sec = max(60, int((plan_total / 6) * 5 * (1.3 if cfg.get('use_proxy') else 1.0) * 1.2))
        st.caption(f'Примерное время: {format_duration(est_sec)}. '
                   f'Не закрывайте вкладку до конца прогона.')

        st.markdown('**Что проверяем:**')
        _cb_col1, _cb_col2 = st.columns(2)
        with _cb_col1:
            _ck_main = st.checkbox('🏠 Главные страницы', value=st.session_state.c30_check_main, key='c30_ck_main', help='Пункт 1.1')
            _ck_catalog = st.checkbox('📁 Страница /catalog/', value=st.session_state.c30_check_catalog, key='c30_ck_catalog', help='Пункт 1.2')
            _ck_cats = st.checkbox('📂 Категории', value=st.session_state.c30_check_categories, key='c30_ck_cats', help='Пункт 1.3')
            _ck_wm = st.checkbox('🔍 Ошибки Вебмастера', value=st.session_state.c30_check_webmaster, key='c30_ck_wm', help='Сайтмапы, дубли, покрытие — из кеша почты')
        with _cb_col2:
            if stats['has_filters']:
                _ck_filters = st.checkbox('🏷️ Фильтры', value=st.session_state.c30_check_filters, key='c30_ck_filters', help='Пункт 1.4')
            else:
                _ck_filters = False
                st.markdown('<span style="color:#71717A">🏷️ Фильтры _(нет в каталоге)_</span>', unsafe_allow_html=True)
            _ck_products = st.checkbox('🛒 Карточки товаров', value=st.session_state.c30_check_products, key='c30_ck_products', help='Пункт 1.5')
            _ck_gsc = st.checkbox('🌐 Ошибки GSC', value=st.session_state.c30_check_gsc, key='c30_ck_gsc', help='Критические и важные уведомления GSC — из кеша почты')

        _ck_notif = st.checkbox(
            '📬 Собрать уведомления из почты (Вебмастер, GSC, Я.Бизнес, 2ГИС, Google)',
            value=st.session_state.c30_fetch_notifications,
            key='c30_ck_notif',
            help='Подключится к почте и заберёт новые письма во время прогона',
        )

        # Сохраняем в session_state
        for _k, _v in [
            ('c30_check_main', _ck_main), ('c30_check_catalog', _ck_catalog),
            ('c30_check_categories', _ck_cats), ('c30_check_filters', _ck_filters),
            ('c30_check_products', _ck_products),
            ('c30_check_webmaster', _ck_wm), ('c30_check_gsc', _ck_gsc),
            ('c30_fetch_notifications', _ck_notif),
        ]:
            st.session_state[_k] = _v

        if st.button('▶ Запустить еженедельную проверку', type='primary',
                     use_container_width=True, key='c30_run',
                     disabled=st.session_state.c30_is_running):
            st.session_state.c30_is_running = True
            st.session_state.c30_results = None
            st.session_state.c30_report_path = None
            st.rerun()

    # ── Прогон ──────────────────────────────────────────────────────
    if st.session_state.c30_is_running:
        with st.container(border=True):
            st.markdown('### ⏳ Идёт проверка')
            st.warning('⚠ **Не закрывайте вкладку до окончания проверки** — '
                       'иначе прогон оборвётся и отчёт не сохранится.')
            progress_bar = st.progress(0, text='Подготовка…')
            log_expander = st.expander('Подробный лог', expanded=False)
            log_area = log_expander.empty()
            log_messages = []

        def append_log(msg):
            log_messages.append(msg)
            log_area.code('\n'.join(log_messages[-100:]), language='text')

        started_ms = int(time.time() * 1000)
        st.session_state.c30_started_at = started_ms

        try:
            proxy_url = get_proxy_url() if cfg.get('use_proxy') else None
            if cfg.get('use_proxy') and not proxy_url:
                append_log(f'⚠ Прокси нужен для {cfg["name"]}, но не настроен в Secrets')
            elif proxy_url:
                append_log(f'Прокси: включён для проекта {cfg["name"]}')

            # Товары: база листингов → fallback sitemap
            if not src.products:
                base_links = load_product_links(pid)
                if base_links and base_links['pathnames']:
                    src.products = base_links['pathnames']
                    append_log(f'Товары из базы листингов: {len(src.products)}')
                else:
                    append_log('Загружаю sitemap для товаров…')
                    try:
                        sm = asyncio.run(load_product_pathnames(
                            cfg, src.categories, src.filters,
                            log=lambda lvl, msg: append_log(msg),
                            proxy_url=proxy_url,
                        ))
                        src.products = sm.get('pathnames', [])
                        append_log(f'Из sitemap: {len(src.products)} товаров')
                    except Exception as e:
                        append_log(f'⚠ Sitemap не загрузился: {e}. Прогон без товаров.')

            # Ротация: окно 30 дней, чтобы недельные выборки не повторялись
            recent = set(load_history(pid, ttl_ms=WEEKLY_TTL_MS).keys())
            append_log(f'История ротации (30 дней): {len(recent)} URL')

            plan = build_plan(
                src,
                random_subdomains_count=int(random_cities),
                categories_per_subdomain=budget['cats'],
                filters_per_subdomain=budget['filters'],
                products_per_subdomain=budget['products'],
                check_main=st.session_state.c30_check_main,
                check_catalog=st.session_state.c30_check_catalog,
                check_categories=st.session_state.c30_check_categories,
                check_filters=st.session_state.c30_check_filters and stats['has_filters'],
                check_products=st.session_state.c30_check_products,
                mandatory_city=cfg.get('mandatory_city', 'Москва'),
                rotation_history=recent,
            )
            append_log(f'Города: {", ".join(s.city for s in plan.selected_subdomains)}')
            append_log(f'Всего проверок: {len(plan.tasks)}')

            counters = {'ok': 0, 'warn': 0, 'err': 0}

            def on_progress(result, done, total_n):
                if result.is_ok:
                    counters['ok'] += 1
                elif result.is_warning:
                    counters['warn'] += 1
                else:
                    counters['err'] += 1
                try:
                    progress_bar.progress(
                        min(1.0, done / max(total_n, 1)),
                        text=f'Проверено {done} из {total_n} — '
                             f'✅ {counters["ok"]} · ⚠ {counters["warn"]} · ❌ {counters["err"]}',
                    )
                except Exception:
                    pass

            results = asyncio.run(run_batch(
                plan.tasks,
                concurrency=6,
                timeout_ms=120000,
                max_attempts=3,
                retry_delay_ms=2500,
                check_text=True,
                on_progress=on_progress,
                proxy_url=proxy_url,
            ))

            finished_ms = int(time.time() * 1000)
            st.session_state.c30_finished_at = finished_ms

            # История ротации
            save_history(pid, list({urlparse(r.url).path for r in results}))

            # xlsx-отчёт
            append_log('Формирую xlsx-отчёт…')
            report_filename = make_report_filename(pid, started_ms, REPORTS_DIR)
            report_path = REPORTS_DIR / report_filename
            _notifs_for_report = (
                load_notifications(pid, 'yandex_webmaster', 30)
                + load_notifications(pid, 'gsc', 30)
                + load_notifications(pid, 'ya_business', 30)
                + load_notifications(pid, 'twogis', 30)
                + load_notifications(pid, 'google_accounts', 3)
            )
            build_report(
                project_name=cfg['name'],
                started_at_ms=started_ms,
                finished_at_ms=finished_ms,
                selected_subdomains=plan.selected_subdomains,
                results=results,
                output_path=report_path,
                notifications=_notifs_for_report or None,
            )

            # Пункт 2 чек-листа: отправка ошибок ответственным
            tg_token = _secret('telegram_bot_token')
            tg_recipients = get_telegram_recipients(pid)
            if tg_token and tg_recipients:
                append_log(f'Отправляю отчёт в Telegram ({len(tg_recipients)} получателей)…')
                try:
                    problems_for_tg = [
                        {'city': r.city or '—', 'url': r.url,
                         'status': {'not_found': '404 Не найдена',
                                    'client_error': 'Ошибка на сайте',
                                    'server_error': 'Сервер не отвечает',
                                    'timeout': 'Нет ответа',
                                    'network_error': 'Нет соединения'}.get(r.status, r.status)}
                        for r in results if r.is_error
                    ][:5]
                    empty_sections = [
                        {'city': r.city or '—', 'url': r.url}
                        for r in results
                        if getattr(r, 'content', None) is not None
                        and getattr(r.content, 'page_kind', '') == 'empty'
                    ]
                    summary_text = format_summary_message(
                        project_name=f'{cfg["name"]} · еженедельная проверка',
                        started_at=datetime.fromtimestamp(started_ms / 1000).strftime('%d.%m.%Y %H:%M'),
                        duration_sec=(finished_ms - started_ms) // 1000,
                        total_checks=len(results),
                        ok_count=sum(1 for r in results if r.is_ok),
                        warn_count=sum(1 for r in results if r.is_warning),
                        err_count=sum(1 for r in results if r.is_error),
                        text_issues_count=sum(len(r.text_issues) for r in results if r.has_text_issues),
                        top_problems=problems_for_tg,
                        content_bugs_count=sum(getattr(r, 'content_bugs', 0) or 0 for r in results),
                        content_bug_pages=sum(1 for r in results if getattr(r, 'has_content_bugs', False)),
                        empty_sections=empty_sections,
                    )
                    tg_result = send_run_notification(
                        bot_token=tg_token,
                        recipients=tg_recipients,
                        project_name=cfg['name'],
                        summary_text=summary_text,
                        report_file=report_path,
                        proxy_url=get_proxy_url(),
                        log=lambda lvl, msg: append_log(msg),
                    )
                    append_log(f'✓ Telegram: отправлено {tg_result["sent"]}, '
                               f'не доставлено {tg_result["failed"]}')
                except Exception as e:
                    append_log(f'⚠ Telegram-отправка упала: {e}')
            else:
                append_log('Telegram не настроен — отправьте отчёт ответственным вручную (пункт 2).')

            # ── Сбор уведомлений из почты (если чекбокс включён) ────
            if st.session_state.get('c30_fetch_notifications', True):
                append_log('Собираю уведомления из почты…')
                _nlog_run = lambda lvl, msg: append_log(msg)
                _proxy = get_proxy_url()

                _yw_e, _yw_p = get_metrika_credentials(pid)
                _yw_cfg = WEBMASTER_YANDEX_CONFIG.get(pid)
                if _yw_e and _yw_p and _yw_cfg:
                    try:
                        fetch_webmaster_yandex(pid, _yw_e, _yw_p, _yw_cfg['folder'], 30, _proxy, _nlog_run)
                    except Exception as _e:
                        append_log(f'⚠ Вебмастер: {_e}')

                _gsc_e, _gsc_p = get_gsc_credentials(pid)
                if _gsc_e and _gsc_p:
                    try:
                        fetch_gsc_gmail(pid, _gsc_e, _gsc_p, 30, _nlog_run)
                    except Exception as _e:
                        append_log(f'⚠ GSC: {_e}')

                _yab_e, _yab_p, _yab_f = get_yabusiness_credentials(pid)
                if _yab_e and _yab_p and _yab_f:
                    try:
                        fetch_yandex_folder_simple(pid, _yab_e, _yab_p, _yab_f, 'ya_business', 30, _proxy, _nlog_run)
                    except Exception as _e:
                        append_log(f'⚠ Я.Бизнес: {_e}')

                _tg_e, _tg_p, _tg_f = get_twogis_credentials(pid)
                if _tg_e and _tg_p and _tg_f:
                    try:
                        fetch_yandex_folder_simple(pid, _tg_e, _tg_p, _tg_f, 'twogis', 30, _proxy, _nlog_run)
                    except Exception as _e:
                        append_log(f'⚠ 2ГИС: {_e}')

                _ga_e, _ga_p = get_google_accounts_credentials(pid)
                if _ga_e and _ga_p:
                    try:
                        fetch_google_accounts(pid, _ga_e, _ga_p, 3, _nlog_run)
                    except Exception as _e:
                        append_log(f'⚠ Google: {_e}')

                # Обновляем уведомления в отчёте с актуальным кешем
                _notifs_for_report = (
                    load_notifications(pid, 'yandex_webmaster', 30)
                    + load_notifications(pid, 'gsc', 30)
                    + load_notifications(pid, 'ya_business', 30)
                    + load_notifications(pid, 'twogis', 30)
                    + load_notifications(pid, 'google_accounts', 3)
                )
                if _notifs_for_report:
                    build_report(
                        project_name=cfg['name'],
                        started_at_ms=started_ms,
                        finished_at_ms=finished_ms,
                        selected_subdomains=plan.selected_subdomains,
                        results=results,
                        output_path=report_path,
                        notifications=_notifs_for_report,
                    )
                    append_log(f'✓ Отчёт обновлён с уведомлениями ({len(_notifs_for_report)} шт.)')

            st.session_state.c30_results = results
            st.session_state.c30_report_path = str(report_path)
            st.session_state.c30_is_running = False
            progress_bar.progress(1.0, text='Готово')
            st.rerun()

        except Exception as e:
            st.session_state.c30_is_running = False
            st.error(f'Ошибка: {e}')
            append_log(f'❌ Ошибка: {e}')

    # ── Результаты прогона ──────────────────────────────────────────
    if st.session_state.c30_results and not st.session_state.c30_is_running:
        results = st.session_state.c30_results
        total = len(results)
        ok_count = sum(1 for r in results if r.is_ok)
        warn_count = sum(1 for r in results if r.is_warning)
        err_count = total - ok_count - warn_count
        text_issues_count = sum(len(r.text_issues) for r in results if r.has_text_issues)
        content_bugs_count = sum(getattr(r, 'content_bugs', 0) or 0 for r in results)
        duration = (st.session_state.c30_finished_at - st.session_state.c30_started_at) // 1000

        with st.container(border=True):
            st.markdown('### Результаты еженедельной проверки')
            any_problems = (err_count or warn_count or text_issues_count or content_bugs_count)
            if any_problems:
                st.warning(f'Найдены проблемы. Проверено {total} страниц за {format_duration(duration)}.')
            else:
                st.success(f'✓ Все проверки прошли успешно: {total} страниц за {format_duration(duration)}.')

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric('Всего', total)
            c2.metric('✅ Работает', ok_count)
            c3.metric('⚠ Предупр.', warn_count)
            c4.metric('❌ Не работает', err_count)
            c5.metric('🧩 Контент', content_bugs_count,
                      help='Структурные проблемы: нет цены, кнопки заказа, H1, шапки…')

            if st.session_state.c30_report_path:
                rp = Path(st.session_state.c30_report_path)
                if rp.exists():
                    with open(rp, 'rb') as f:
                        st.download_button(
                            label=f'📥 Скачать полный отчёт ({rp.name})',
                            data=f.read(), file_name=rp.name,
                            mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            use_container_width=True, type='primary',
                        )

            problems = [
                r for r in results
                if r.is_error or r.is_warning or r.has_text_issues
                or getattr(r, 'has_content_bugs', False)
                or r.speed_rating in ('slow', 'very_slow')
            ]
            if problems:
                kind_labels = {'listing': 'Листинг', 'section': 'Раздел каталога',
                               'empty': 'Пустой раздел'}
                st.markdown(f'**Список проблем ({len(problems)})**')
                for r in problems[:50]:
                    has_struct = getattr(r, 'has_content_bugs', False)
                    emoji = '❌' if r.is_error else '⚠️' if r.is_warning else '🧩' if has_struct else '🔤'
                    extra = []
                    if r.has_text_issues:
                        extra.append(f'{len(r.text_issues)} битых переменных')
                    if has_struct and r.content is not None:
                        extra.append('нет: ' + ', '.join(b.label for b in r.content.bugs))
                    type_label = kind_labels.get(
                        getattr(getattr(r, 'content', None), 'page_kind', ''), r.type_label)
                    city = f'[{r.city}] ' if r.city else ''
                    tags_html = _tags_html(_dept_tags_result(r))
                    st.markdown(
                        f'{emoji} **{city}**{type_label}: [{r.url}]({r.url})'
                        + (' — ' + ' · '.join(extra) if extra else '')
                        + tags_html,
                        unsafe_allow_html=True,
                    )
                if len(problems) > 50:
                    st.caption(f'... и ещё {len(problems) - 50}. Все детали — в xlsx-отчёте.')

    # ── Блок 2: уведомления из почты ───────────────────────────────
    _yw_e2, _yw_p2 = get_metrika_credentials(pid)
    _gsc_e2, _gsc_p2 = get_gsc_credentials(pid)
    _has_any_notif = bool(_yw_e2 or _gsc_e2)

    if _has_any_notif:
        with st.container(border=True):
            st.markdown('### 2. Уведомления из почты')
            st.caption('Данные из кеша — обновляются при запуске прогона (чекбокс «Собрать уведомления»).')

            _nb_days = st.selectbox(
                'Период',
                [7, 14, 30],
                index=1,
                format_func=lambda x: f'{x} дней',
                key='c30_notify_period',
                label_visibility='collapsed',
            )

            # ── Ошибки Вебмастера / GSC (всегда раскрыты, если есть) ─
            _P_ERR = {'critical': ('#DC2626', 'rgba(220,38,38,0.07)'),
                      'important': ('#D97706', 'rgba(217,119,6,0.07)')}

            _PRIO_C = {'critical': '#DC2626', 'important': '#D97706',
                       'recommendation': '#CA8A04', 'info': '#6B7280'}
            _PRIO_BG = {'critical': 'rgba(220,38,38,0.06)', 'important': 'rgba(217,119,6,0.06)',
                        'recommendation': 'rgba(202,138,4,0.06)', 'info': 'rgba(107,114,128,0.04)'}

            def _group_by_subject(items):
                """Группировка по теме: одинаковые subject → одна запись с датами."""
                from collections import OrderedDict
                groups: dict = OrderedDict()
                for n in items:
                    key = n.subject.strip()
                    if key not in groups:
                        groups[key] = {'rep': n, 'dates': [], 'count': 0}
                    groups[key]['dates'].append(n.date)
                    groups[key]['count'] += 1
                return list(groups.values())

            def _notif_card_grouped(rep_n, dates, count, color, bg):
                cat = CATEGORY_LABELS.get(rep_n.category, rep_n.category)
                tags_h = _tags_html(_dept_tags_notif(rep_n))
                date_str = dates[0] if count == 1 else f'{min(dates)} – {max(dates)} · ×{count}'
                st.markdown(
                    f'<div style="padding:7px 12px;margin-bottom:5px;border-left:3px solid {color};'
                    f'border-radius:0 5px 5px 0;background:{bg}">'
                    f'<span style="font-size:0.78rem;color:#6B7280">{date_str} · {cat}</span>'
                    f'{tags_h}'
                    f'<p style="margin:3px 0 0;font-size:0.9rem;font-weight:600;color:{color}">'
                    f'{rep_n.subject}</p>'
                    + (f'<p style="margin:2px 0 0;font-size:0.82rem;color:#5B5853">'
                       f'{rep_n.body_preview[:220]}</p>' if rep_n.body_preview and count == 1 else '')
                    + '</div>', unsafe_allow_html=True,
                )

            def _render_service_block(source_key, title, icon, days=None, with_priority=False):
                """Один сворачиваемый блок на сервис. Критические — раскрыт по умолчанию."""
                items = load_notifications(pid, source_key, days or _nb_days)
                if not items:
                    st.caption(f'{icon} {title} — нет уведомлений за период')
                    return
                crit_n = sum(1 for n in items if n.priority == 'critical')
                imp_n = sum(1 for n in items if n.priority == 'important')
                badge_parts = []
                if crit_n:
                    badge_parts.append(f'🔴 {crit_n} крит.')
                if imp_n:
                    badge_parts.append(f'🟠 {imp_n} важных')
                badge = '  ' + '  '.join(badge_parts) if badge_parts else ''
                expand_default = bool(crit_n)
                with st.expander(f'{icon} **{title}** — {len(items)} уведомлений{badge}',
                                 expanded=expand_default):
                    if with_priority:
                        groups = group_by_priority(items)
                        _prio_labels = {
                            'critical': '🔴 Критические',
                            'important': '🟠 Важные',
                            'recommendation': '🟡 Рекомендации',
                            'info': '⚪ Инфо',
                        }
                        for priority in PRIORITY_ORDER:
                            prio_items = groups.get(priority, [])
                            if not prio_items:
                                continue
                            st.markdown(f'**{_prio_labels[priority]}**')
                            c, bg = _PRIO_C[priority], _PRIO_BG[priority]
                            for g in _group_by_subject(prio_items):
                                _notif_card_grouped(g['rep'], g['dates'], g['count'], c, bg)
                    else:
                        for g in _group_by_subject(items):
                            _notif_card_grouped(g['rep'], g['dates'], g['count'],
                                                '#6B7280', 'rgba(107,114,128,0.04)')

            if _yw_e2:
                _render_service_block('yandex_webmaster', 'Яндекс.Вебмастер', '🔍',
                                      with_priority=True)
                _render_service_block('ya_business', 'Я.Бизнес', '🏢')
                _render_service_block('twogis', '2ГИС', '🗺')
            if _gsc_e2:
                _render_service_block('gsc', 'Google Search Console', '🌐',
                                      with_priority=True)
                _render_service_block('google_accounts', 'Google', '📧', days=3)

            # Метрика 404
            if _yw_e2:
                _m_reports = load_reports_for_period(pid, days=_nb_days)
                if _m_reports:
                    _m_total = sum(r.total_pages for r in _m_reports)
                    with st.expander(f'📊 Метрика 404 — {_m_total} страниц', expanded=False):
                        for rep in sorted(_m_reports, key=lambda r: r.report_date, reverse=True):
                            st.markdown(
                                f'**{rep.report_date}** · {rep.country_name} — '
                                f'{rep.total_pages} стр., {rep.total_views} просмотров'
                            )

else:
    st.info('Выберите проект, чтобы начать еженедельную проверку.')
