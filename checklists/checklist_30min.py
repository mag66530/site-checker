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
    PRIORITY_LABELS, PRIORITY_ORDER, CATEGORY_LABELS,
    fetch_webmaster_yandex, fetch_gsc_gmail,
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


# ── Session state ───────────────────────────────────────────────────


def init_session():
    defaults = {
        'c30_project_id': None,
        'c30_is_running': False,
        'c30_results': None,
        'c30_report_path': None,
        'c30_started_at': None,
        'c30_finished_at': None,
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
                check_main=True, check_catalog=True,
                check_categories=True,
                check_filters=stats['has_filters'],
                check_products=True,
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
                    st.markdown(
                        f'{emoji} **{city}**{type_label}: [{r.url}]({r.url})'
                        + (' — ' + ' · '.join(extra) if extra else '')
                    )
                if len(problems) > 50:
                    st.caption(f'... и ещё {len(problems) - 50}. Все детали — в xlsx-отчёте.')

    # ── Пункт 3: уведомления из почты ──────────────────────────────
    yw_email, yw_password = get_metrika_credentials(pid)
    gsc_email, gsc_password = get_gsc_credentials(pid)
    yw_cfg = WEBMASTER_YANDEX_CONFIG.get(pid)
    has_yw = bool(yw_email and yw_password and yw_cfg)
    has_gsc = bool(gsc_email and gsc_password)
    has_metrika = bool(yw_email and yw_password)   # Метрика — тот же ящик

    if has_yw or has_gsc or has_metrika:
        with st.container(border=True):
            st.markdown('### 3. Уведомления из почты')
            st.caption(
                'Яндекс.Вебмастер, Google Search Console и 404-отчёты Метрики '
                'за выбранный период — из кеша. Нажмите «Обновить» чтобы '
                'забрать новые письма.'
            )

            _col_period, _col_btn = st.columns([1, 1])
            with _col_period:
                _nb_period = st.selectbox(
                    'Период',
                    ['7 дней', '14 дней', '30 дней'],
                    index=1,
                    label_visibility='collapsed',
                    key='c30_notify_period',
                )
            _nb_days = {'7 дней': 7, '14 дней': 14, '30 дней': 30}[_nb_period]

            with _col_btn:
                _nb_refresh = st.button(
                    '🔄 Обновить из почты',
                    key='c30_btn_notify_refresh',
                    use_container_width=True,
                )

            # ── Обновление при нажатии ───────────────────────────────
            if _nb_refresh:
                _nb_log: list[str] = []

                def _nlog(lvl, msg):
                    _nb_log.append(msg)

                _steps = (1 if has_yw else 0) + (1 if has_gsc else 0) + (1 if has_metrika else 0)
                _done = 0
                _pb = st.progress(0, text='Подключаюсь…')

                if has_yw:
                    _pb.progress(_done / _steps, text='Яндекс.Вебмастер…')
                    try:
                        _r = fetch_webmaster_yandex(
                            project_id=pid,
                            email_addr=yw_email,
                            password=yw_password,
                            folder=yw_cfg['folder'],
                            lookback_days=_nb_days,
                            proxy_url=get_proxy_url(),
                            log=_nlog,
                        )
                        _nlog('info', f'Вебмастер: +{_r["fetched"]} новых')
                    except Exception as _e:
                        _nlog('error', f'❌ Вебмастер: {_e}')
                    _done += 1

                if has_gsc:
                    _pb.progress(_done / _steps, text='GSC (Gmail)…')
                    try:
                        _r = fetch_gsc_gmail(
                            project_id=pid,
                            email_addr=gsc_email,
                            password=gsc_password,
                            lookback_days=_nb_days,
                            log=_nlog,
                        )
                        _nlog('info', f'GSC: +{_r["fetched"]} новых')
                    except Exception as _e:
                        _nlog('error', f'❌ GSC: {_e}')
                    _done += 1

                if has_metrika:
                    _pb.progress(_done / _steps, text='Метрика 404…')
                    try:
                        _r = fetch_incremental(
                            project_id=pid,
                            email_addr=yw_email,
                            password=yw_password,
                            folder=MAILBOX_CONFIG[pid]['folder'],
                            proxy_url=get_proxy_url(),
                            lookback_days=_nb_days,
                            log=_nlog,
                            upgrade_if_better=True,
                        )
                        _nlog('info', f'Метрика: +{_r["fetched"]} новых')
                    except Exception as _e:
                        _nlog('error', f'❌ Метрика: {_e}')
                    _done += 1

                _pb.empty()
                st.success('✅ Обновление завершено')
                with st.expander('Лог', expanded=False):
                    st.code('\n'.join(_nb_log[-100:]) or '(пусто)', language='text')
                st.rerun()

            # ── Рендер уведомлений ───────────────────────────────────

            _P_COLOR = {
                'critical':       '#DC2626',
                'important':      '#D97706',
                'recommendation': '#CA8A04',
                'info':           '#6B7280',
            }
            _P_BG = {
                'critical':       'rgba(220,38,38,0.07)',
                'important':      'rgba(217,119,6,0.07)',
                'recommendation': 'rgba(202,138,4,0.07)',
                'info':           'rgba(107,114,128,0.07)',
            }

            def _render_source(notifs, title: str, icon: str):
                if not notifs:
                    st.caption(f'Нет уведомлений от {title} за выбранный период')
                    return
                groups = group_by_priority(notifs)
                crit_n = len(groups.get('critical', []))
                hdr_color = '#DC2626' if crit_n else '#1A1A1A'
                crit_badge = (
                    f' <span style="color:#DC2626">({crit_n} критических)</span>'
                    if crit_n else ''
                )
                st.markdown(
                    f'<p style="font-weight:600;font-size:1rem;color:{hdr_color}">'
                    f'{icon} {title} — {len(notifs)} уведомлений{crit_badge}</p>',
                    unsafe_allow_html=True,
                )
                for priority in PRIORITY_ORDER:
                    items = groups.get(priority, [])
                    if not items:
                        continue
                    with st.expander(
                        f'{PRIORITY_LABELS[priority]} ({len(items)})',
                        expanded=(priority in ('critical', 'important')),
                    ):
                        for n in items:
                            cat = CATEGORY_LABELS.get(n.category, n.category)
                            color = _P_COLOR[priority]
                            bg = _P_BG[priority]
                            st.markdown(
                                f'<div style="padding:10px 14px;margin-bottom:8px;'
                                f'border-left:3px solid {color};border-radius:0 6px 6px 0;'
                                f'background:{bg}">'
                                f'<span style="font-size:0.8rem;color:#6B7280">{n.date}</span>'
                                f' · <span style="font-size:0.8rem;font-weight:600;'
                                f'color:{color}">{cat}</span>'
                                f'<p style="margin:4px 0 0 0;font-weight:600;'
                                f'font-size:0.95rem;color:#1A1A1A">{n.subject}</p>'
                                + (
                                    f'<p style="margin:4px 0 0 0;font-size:0.85rem;'
                                    f'color:#5B5853;white-space:pre-wrap">'
                                    f'{n.body_preview[:300]}</p>'
                                    if n.body_preview else ''
                                )
                                + '</div>',
                                unsafe_allow_html=True,
                            )

            # Яндекс.Вебмастер
            if has_yw:
                _render_source(
                    load_notifications(pid, 'yandex_webmaster', _nb_days),
                    'Яндекс.Вебмастер', '🔍',
                )

            # GSC
            if has_gsc:
                if has_yw:
                    st.divider()
                _render_source(
                    load_notifications(pid, 'gsc', _nb_days),
                    'Google Search Console', '🌐',
                )

            # Метрика 404
            if has_metrika:
                if has_yw or has_gsc:
                    st.divider()
                _m_reports = load_reports_for_period(pid, days=_nb_days)
                if _m_reports:
                    _m_total = sum(r.total_pages for r in _m_reports)
                    _m_dates = len({r.report_date for r in _m_reports})
                    st.markdown(
                        f'<p style="font-weight:600;font-size:1rem;color:#1A1A1A">'
                        f'📊 Метрика 404 — {_m_total} страниц за {_m_dates} дней</p>',
                        unsafe_allow_html=True,
                    )
                    with st.expander('Детали по странам', expanded=False):
                        for rep in sorted(_m_reports,
                                          key=lambda r: r.report_date, reverse=True):
                            st.markdown(
                                f'**{rep.report_date}** · {rep.country_name} — '
                                f'{rep.total_pages} страниц, {rep.total_views} просмотров'
                            )
                else:
                    st.caption('Нет данных Метрики 404 за выбранный период. '
                               'Нажмите «Обновить из почты».')

else:
    st.info('Выберите проект, чтобы начать еженедельную проверку.')
