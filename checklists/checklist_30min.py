"""
Чек-лист – проверка сайта-проекта (объединённый 15- и 30-минутный).

Объём задаётся вручную полями ввода (города / категории / фильтры / товары на
город): минимум – быстрый тест, больше – полная еженедельная проверка.

Что делает:
  1. Доступность и визуальные ошибки – выборка URL (главная 1.1, каталог 1.2,
     категории 1.3, фильтры 1.4, товары 1.5, битые переменные 1.6) + структура
     (цена, кнопки заказа, H1, шапка/подвал). Ротация с окном 30 дней.
     Можно добавить свой список URL.
  2. Сбор уведомлений из почты (Вебмастер, GSC, Я.Бизнес, 2ГИС, Google) +
     404 из Метрики – в xlsx-отчёт.
  3. Telegram-уведомление с отчётом после прогона.

Прогон идёт ОТДЕЛЬНЫМ процессом (checklist_run.py → runner_30min), поэтому
переживает переключение вкладок и не сбрасывается.
"""
import asyncio
import json
import os
import pickle
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

from sources import (
    list_projects, load_project_config, load_sources, build_plan,
    build_custom_tasks_typed,
)
from profiles import PROFILES, get_profile_kwargs
from history import load_history, save_history, WEEKLY_TTL_MS
from sitemap import (
    load_product_pathnames, get_cached_products_info, invalidate_sitemap_cache,
)
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
    """Кто отвечает за конкретную проблему. Пусто – если страница работает.

    Карта проблема → отдел:
      • сервер не отвечает / таймаут / нет соединения (5xx) → разработка
      • долгий ответ сервера (медленно)                    → разработка
      • битые переменные в шаблоне ({{city}} и т.п.)        → разработка
      • 404 / страница не найдена                           → SEO
      • редиректы (предупреждение)                          → SEO
      • прочие ошибки на сайте (4xx)                         → разработка
      • нет цены / H1 / кнопок заказа (контентные баги)     → контент
    """
    tags: list[str] = []
    if r.is_error:
        if r.status in ('server_error', 'timeout', 'network_error'):
            tags.append('разработка')
        elif r.status == 'not_found':
            tags.append('SEO')
        else:  # client_error и прочее
            tags.append('разработка')
    elif r.is_warning:
        # Предупреждение = редирект → зона SEO
        tags.append('SEO')
    if r.speed_rating in ('slow', 'very_slow') and 'разработка' not in tags:
        tags.append('разработка')
    if r.has_text_issues and 'разработка' not in tags:
        tags.append('разработка')
    if getattr(r, 'has_content_bugs', False) and 'контент' not in tags:
        tags.append('контент')
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


# ── Фоновый прогон (переживает переключение вкладок) ─────────────────
# Состояние прогона хранится в модульной переменной (живёт в процессе
# сервера, не в session_state), поэтому переход на другую вкладку и обратно
# не перезапускает прогон – поток продолжает работать сам.

_RUNS: dict = {}  # project_id -> состояние прогона (устаревший потоковый путь)

_CACHE = Path('cache')
_PROJECT_ROOT = Path(__file__).parent.parent


def _c30_paths(pid):
    return {
        'params': _CACHE / f'c30_{pid}.params.json',
        'log': _CACHE / f'c30_{pid}.log',
        'status': _CACHE / f'c30_{pid}.status.json',
        'result': _CACHE / f'c30_{pid}.result.pkl',
        'report': _CACHE / f'c30_{pid}.report.txt',  # лёгкий сайдкар: путь к xlsx
        'pid': _CACHE / f'c30_{pid}.pid',
    }


def _read_pidfile(p: Path):
    try:
        return int(p.read_text().strip())
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


def _launch_checklist_bg(pid, params, creds):
    """Запустить прогон ОТДЕЛЬНЫМ процессом (надёжнее потока для async-работы)."""
    paths = _c30_paths(pid)
    _CACHE.mkdir(parents=True, exist_ok=True)
    paths['params'].write_text(
        json.dumps({'pid': pid, 'params': params, 'creds': creds},
                   ensure_ascii=False), encoding='utf-8')
    for k in ('log', 'status', 'result', 'report'):
        try:
            paths[k].unlink(missing_ok=True)
        except Exception:
            pass
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    logf = open(paths['log'], 'a', encoding='utf-8')
    proc = subprocess.Popen(
        [sys.executable, 'checklist_run.py',
         '--params', str(paths['params']),
         '--out', str(paths['result']),
         '--status', str(paths['status']),
         '--report', str(paths['report'])],
        cwd=str(_PROJECT_ROOT), stdout=logf, stderr=subprocess.STDOUT,
        env=env, creationflags=creationflags,
    )
    paths['pid'].write_text(str(proc.pid), encoding='utf-8')
    return proc.pid


class _RunCancelled(Exception):
    pass


def _run_state_new() -> dict:
    return {'running': True, 'progress': 0.0, 'progress_text': 'Подготовка…',
            'log': [], 'results': None, 'report_path': None,
            'started_at': None, 'finished_at': None, 'error': None,
            'cancel': False, 'cancelled': False}


def _run_worker(pid, cfg, src, stats, budget, random_cities, flags, creds):
    """Выполняет прогон в фоне. НЕ обращается к st.* – все секреты переданы
    в creds из основного потока. Пишет прогресс/лог/результат в _RUNS[pid]."""
    state = _RUNS[pid]
    _run_log_path = Path('cache') / 'last_run.log'
    try:
        _run_log_path.parent.mkdir(parents=True, exist_ok=True)
        _run_log_path.write_text('', encoding='utf-8')
    except Exception:
        pass

    def append_log(msg):
        state['log'].append(msg)
        try:
            with open(_run_log_path, 'a', encoding='utf-8') as _f:
                _f.write(f'{datetime.now().strftime("%H:%M:%S")}  {msg}\n')
        except Exception:
            pass

    def set_progress(frac, text):
        state['progress'] = min(1.0, max(0.0, frac))
        state['progress_text'] = text

    started_ms = int(time.time() * 1000)
    state['started_at'] = started_ms

    try:
        proxy_url = creds['proxy_url'] if cfg.get('use_proxy') else None
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

        recent = set(load_history(pid, ttl_ms=WEEKLY_TTL_MS).keys())
        append_log(f'История ротации (30 дней): {len(recent)} URL')

        plan = build_plan(
            src,
            random_subdomains_count=int(random_cities),
            categories_per_subdomain=budget['cats'],
            filters_per_subdomain=budget['filters'],
            products_per_subdomain=budget['products'],
            check_main=flags['check_main'],
            check_catalog=flags['check_catalog'],
            check_categories=flags['check_categories'],
            check_filters=flags['check_filters'],
            check_products=flags['check_products'],
            mandatory_city=cfg.get('mandatory_city', 'Москва'),
            rotation_history=recent,
        )
        append_log(f'Города: {", ".join(s.city for s in plan.selected_subdomains)}')
        append_log(f'Всего проверок: {len(plan.tasks)}')

        counters = {'ok': 0, 'warn': 0, 'err': 0}

        def on_progress(result, done, total_n):
            if state.get('cancel'):
                raise _RunCancelled()
            if result.is_ok:
                counters['ok'] += 1
            elif result.is_warning:
                counters['warn'] += 1
            else:
                counters['err'] += 1
            set_progress(
                done / max(total_n, 1),
                f'Проверено {done} из {total_n} – '
                f'✅ {counters["ok"]} · ⚠ {counters["warn"]} · ❌ {counters["err"]}',
            )

        try:
            from kp import load_kp
            kp_map = load_kp(pid) or None
            if kp_map:
                append_log(f'КП для сверки контактов: {len(kp_map)} городов')
        except Exception as e:
            kp_map = None
            append_log(f'⚠ Не удалось загрузить КП: {e}')

        results = asyncio.run(run_batch(
            plan.tasks, concurrency=6, timeout_ms=120000, max_attempts=3,
            retry_delay_ms=2500, check_text=True, on_progress=on_progress,
            proxy_url=proxy_url, kp_map=kp_map,
        ))

        finished_ms = int(time.time() * 1000)
        save_history(pid, list({urlparse(r.url).path for r in results}))

        append_log('Формирую xlsx-отчёт…')
        report_filename = make_report_filename(pid, started_ms, REPORTS_DIR)
        report_path = REPORTS_DIR / report_filename
        _notifs = (
            load_notifications(pid, 'yandex_webmaster', 30)
            + load_notifications(pid, 'gsc', 30)
            + load_notifications(pid, 'ya_business', 30)
            + load_notifications(pid, 'twogis', 30)
            + load_notifications(pid, 'google_accounts', 3)
        )
        build_report(
            project_name=cfg['name'], started_at_ms=started_ms,
            finished_at_ms=finished_ms,
            selected_subdomains=plan.selected_subdomains, results=results,
            output_path=report_path, notifications=_notifs or None,
        )

        # Telegram
        tg_token = creds['tg_token']
        tg_recipients = creds['tg_recipients']
        if tg_token and tg_recipients:
            append_log(f'Отправляю отчёт в Telegram ({len(tg_recipients)} получателей)…')
            try:
                problems_for_tg = [
                    {'city': r.city or '–', 'url': r.url,
                     'status': {'not_found': '404 Не найдена',
                                'client_error': 'Ошибка на сайте',
                                'server_error': 'Сервер не отвечает',
                                'timeout': 'Нет ответа',
                                'network_error': 'Нет соединения'}.get(r.status, r.status)}
                    for r in results if r.is_error][:5]
                empty_sections = [
                    {'city': r.city or '–', 'url': r.url} for r in results
                    if getattr(r, 'content', None) is not None
                    and getattr(r.content, 'page_kind', '') == 'empty']
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
                    bot_token=tg_token, recipients=tg_recipients,
                    project_name=cfg['name'], summary_text=summary_text,
                    report_file=report_path, proxy_url=creds['proxy_url'],
                    log=lambda lvl, msg: append_log(msg),
                )
                append_log(f'✓ Telegram: отправлено {tg_result["sent"]}, '
                           f'не доставлено {tg_result["failed"]}')
            except Exception as e:
                append_log(f'⚠ Telegram-отправка упала: {e}')
        else:
            append_log('Telegram не настроен – отправьте отчёт ответственным вручную (пункт 2).')

        # Сбор уведомлений из почты
        if flags['fetch_notifications']:
            append_log('Собираю уведомления из почты…')
            _nlog = lambda lvl, msg: append_log(msg)
            _proxy = creds['proxy_url']

            _yw_e, _yw_p = creds['metrika']
            _yw_cfg = WEBMASTER_YANDEX_CONFIG.get(pid)
            if _yw_e and _yw_p and _yw_cfg:
                try:
                    fetch_webmaster_yandex(pid, _yw_e, _yw_p, _yw_cfg['folder'], 30, _proxy, _nlog)
                except Exception as _e:
                    append_log(f'⚠ Вебмастер: {_e}')
            else:
                append_log(f'⚠ Вебмастер: креды не найдены (metrika_{pid}_email / metrika_{pid}_password)')

            _gsc_e, _gsc_p = creds['gsc']
            if _gsc_e and _gsc_p:
                append_log(f'GSC: креды найдены ({_gsc_e}), подключаюсь к Gmail…')
                try:
                    fetch_gsc_gmail(pid, _gsc_e, _gsc_p, 30, _nlog)
                except Exception as _e:
                    append_log(f'⚠ GSC: {_e}')
            else:
                append_log(f'⚠ GSC: креды не найдены (gsc_{pid}_email / gsc_{pid}_password). '
                           f'Похожие ключи в секретах: {creds.get("secret_keys_hint") or "нет"}')

            _yab_e, _yab_p, _yab_f = creds['yab']
            if _yab_e and _yab_p and _yab_f:
                append_log(f'Я.Бизнес: подключаюсь к {_yab_e}, папка «{_yab_f}»…')
                try:
                    fetch_yandex_folder_simple(pid, _yab_e, _yab_p, _yab_f, 'ya_business', 30, _proxy, _nlog)
                except Exception as _e:
                    append_log(f'⚠ Я.Бизнес: {_e}')
            else:
                append_log(f'⚠ Я.Бизнес: креды/папка не найдены (metrika_{pid}_*)')

            _tg_e, _tg_p, _tg_f = creds['twogis']
            if _tg_e and _tg_p and _tg_f:
                append_log(f'2ГИС: подключаюсь к {_tg_e}, папка «{_tg_f}»…')
                try:
                    fetch_yandex_folder_simple(pid, _tg_e, _tg_p, _tg_f, 'twogis', 30, _proxy, _nlog)
                except Exception as _e:
                    append_log(f'⚠ 2ГИС: {_e}')
            else:
                append_log(f'⚠ 2ГИС: креды/папка не найдены (metrika_{pid}_*)')

            _ga_e, _ga_p = creds['google']
            if _ga_e and _ga_p:
                try:
                    fetch_google_accounts(pid, _ga_e, _ga_p, 3, _nlog)
                except Exception as _e:
                    append_log(f'⚠ Google: {_e}')

            _notifs2 = (
                load_notifications(pid, 'yandex_webmaster', 30)
                + load_notifications(pid, 'gsc', 30)
                + load_notifications(pid, 'ya_business', 30)
                + load_notifications(pid, 'twogis', 30)
                + load_notifications(pid, 'google_accounts', 3)
            )
            if _notifs2:
                build_report(
                    project_name=cfg['name'], started_at_ms=started_ms,
                    finished_at_ms=finished_ms,
                    selected_subdomains=plan.selected_subdomains, results=results,
                    output_path=report_path, notifications=_notifs2,
                )
                append_log(f'✓ Отчёт обновлён с уведомлениями ({len(_notifs2)} шт.)')
            else:
                append_log('Уведомлений нет – лист «Уведомления» в отчёт не добавлен.')
        else:
            append_log('Чекбокс «Собрать уведомления из почты» выключен – почту не проверяю.')

        state['results'] = results
        state['report_path'] = str(report_path)
        state['finished_at'] = finished_ms
        set_progress(1.0, 'Готово')

    except _RunCancelled:
        state['cancelled'] = True
        append_log('⛔ Прогон отменён пользователем')
    except Exception as e:
        state['error'] = str(e)
        append_log(f'❌ Ошибка: {e}')
    finally:
        if state['finished_at'] is None:
            state['finished_at'] = int(time.time() * 1000)
        state['running'] = False


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
        'c30_check_text': True,        # пункт 1.6 – битые переменные
        # Сервисные проверки
        'c30_check_webmaster': True,
        'c30_check_gsc': True,
        'c30_fetch_notifications': True,
        'c30_notify_days': 7,
        # Свой список URL
        'c30_use_custom_urls': False,
        'c30_custom_urls_text': '',
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
    Категории – самая большая доля (все уровни вложенности), затем фильтры
    и товары. Если фильтров у проекта нет – их долю делят категории и товары.
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


st.title('Чек-лист')
st.caption(
    'Проверка сайта-проекта: доступность, визуальные ошибки и структура по '
    'случайной выборке URL. Объём задаёте сами – минимум для быстрого теста, '
    'больше для еженедельной проверки. Прогон идёт в фоне и переживает '
    'переключение вкладок.'
)

# Локальный CSS только для этой страницы: primary-кнопка («Запустить
# еженедельную проверку»). app.py красит белым саму кнопку, но текст лежит
# во вложенном <p>, который глобальное правило перекрашивает в тёмный –
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
    /* Зелёная кнопка скачивания отчёта – чтобы не путалась с primary */
    div[data-testid="stDownloadButton"] > button,
    div[data-testid="stDownloadButton"] > button * {
        color: #FFFFFF !important;
    }
    div[data-testid="stDownloadButton"] > button {
        background: #16A34A !important;
        border: 1px solid #16A34A !important;
    }

    /* ── Типографика: чёткая иерархия, чтобы не сливалось ── */
    /* Заголовок секции-карточки (### …) */
    .block-container h3 { font-size: 1.3rem !important; margin: 0 0 .15rem !important; }
    .block-container h4 { font-size: 1.05rem !important; margin: 0 0 .5rem !important;
        color: #5B5853 !important; }
    /* Единый подзаголовок группы внутри карточки */
    .c30-sub {
        font-size: .76rem; font-weight: 700; text-transform: uppercase;
        letter-spacing: .06em; color: #8A867F;
        margin: 1.1rem 0 .4rem; padding-bottom: .25rem;
        border-bottom: 1px solid #ECEAE4;
    }
    .c30-sub:first-of-type { margin-top: .3rem; }
    /* Подписи полей чуть крупнее и темнее — читаемо */
    [data-testid="stNumberInput"] label p,
    .stCheckbox label p, .stSelectbox label p {
        font-size: .9rem !important; color: #3A3A3A !important; font-weight: 500 !important;
    }
    /* Значение в числовом поле — крупное, в фокусе внимания */
    [data-testid="stNumberInput"] input { font-size: 1.05rem !important; font-weight: 600 !important; }
    /* Итог/оценка — спокойный вторичный текст */
    .c30-summary { font-size: .95rem; color: #1A1A1A; margin-top: .3rem; }

    /* ── Раскладка: воздух в карточках, отступы между блоками, выравнивание ── */
    /* Карточки-секции: внутренний воздух + ровный отступ между блоками */
    .block-container [data-testid="stVerticalBlockBorderWrapper"] {
        padding: 20px 24px !important; margin-bottom: 16px !important;
    }
    /* Раскрывашки делаем ЛЁГКИМИ (не вторая жирная рамка), чтобы «скрытое»
       читалось как второстепенное, а карточки-секции — как главное. */
    [data-testid="stExpander"] {
        border: 1px solid #ECEAE4 !important; background: #FBFAF8 !important;
        box-shadow: none !important; border-radius: 10px !important;
        margin-bottom: 8px !important;
    }
    [data-testid="stExpander"] summary {
        font-size: .9rem !important; font-weight: 600 !important; color: #5B5853 !important;
    }
    /* Карточки-пресеты (контейнер с рамкой в колонке) — кликабельный вид */
    [data-testid="column"] [data-testid="stVerticalBlockBorderWrapper"] {
        padding: 14px 12px !important; margin-bottom: 0 !important;
        transition: border-color .15s;
    }
    [data-testid="column"] [data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: #B9B2A6 !important;
    }
    /* Метрики каталога — числа крупные, подписи спокойные (ровный ряд) */
    [data-testid="stMetricValue"] { font-size: 1.7rem !important; }
    [data-testid="stMetricLabel"] p { font-size: .8rem !important; color: #8A867F !important; }

    /* Пресеты как карточки (radio с подписями): клик = выбор, выбранная —
       рамка подсвечивается; равные, по центру, без кружка и кнопки «Выбрать». */
    .st-key-c30_preset div[role="radiogroup"] { gap: 12px !important; align-items: stretch !important; }
    /* прячем кружок радио — карточка кликается целиком, заголовок по центру */
    .st-key-c30_preset div[role="radiogroup"] > label > div:first-child { display: none !important; }
    .st-key-c30_preset div[role="radiogroup"] > label {
        flex: 1 1 0 !important; min-height: 90px;
        flex-direction: column !important; align-items: center !important;
        justify-content: center !important; text-align: center !important; gap: 4px !important;
        background: #FFFFFF !important; border: 1px solid #DEDBD4 !important;
        border-radius: 10px !important; padding: 16px 14px !important; margin: 0 !important;
        cursor: pointer; transition: border-color .15s, background .15s;
    }
    .st-key-c30_preset div[role="radiogroup"] > label:hover {
        border-color: #B9B2A6 !important; background: #FBFAF8 !important;
    }
    .st-key-c30_preset div[role="radiogroup"] > label:has(input:checked) {
        border-color: #1A1A1A !important; background: #ECEAE4 !important;
        box-shadow: inset 0 0 0 1px #1A1A1A;
    }
    /* Заголовок карточки — крупнее, по центру, наш шрифт */
    .st-key-c30_preset div[role="radiogroup"] > label [data-testid="stMarkdownContainer"] p {
        font-family: 'Hanken Grotesk', sans-serif !important;
        font-size: 1.15rem !important; font-weight: 600 !important;
        color: #1A1A1A !important; text-align: center !important; margin: 0 !important;
    }
    /* Подпись снизу — мельче, БЕЗ жирности, по центру */
    .st-key-c30_preset [data-testid="stCaptionContainer"],
    .st-key-c30_preset [data-testid="stCaptionContainer"] p {
        font-family: 'Hanken Grotesk', sans-serif !important;
        font-size: .78rem !important; font-weight: 400 !important;
        color: #8A867F !important; line-height: 1.35 !important; text-align: center !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _c30_sub(text: str):
    """Единый подзаголовок группы внутри карточки (для ровной иерархии)."""
    st.markdown(f'<div class="c30-sub">{text}</div>', unsafe_allow_html=True)


# ── Выбор проекта ───────────────────────────────────────────────────


with st.container(border=True):
    st.markdown('### Какой сайт проверяем')
    projects = list_projects()
    options = ['– выберите –'] + [p['name'] for p in projects]
    name_to_id = {p['name']: p['id'] for p in projects}

    current = '– выберите –'
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
        # Поля выборки перечитаются под новый проект (свои лимиты городов).
        for _k in ('c30_in_cities', 'c30_in_cats', 'c30_in_filters', 'c30_in_products'):
            st.session_state.pop(_k, None)

pid = st.session_state.c30_project_id

if pid:
    try:
        cfg, src = c30_load_sources(pid)
    except Exception as e:
        st.error(f'Не удалось загрузить каталог: {e}')
        st.stop()
    stats = src.stats

    # ── Каталог проекта: метрики + сброс кэша ──────────────────────
    with st.container(border=True):
        st.markdown('#### Каталог проекта')
        _m1, _m2, _m3, _m4 = st.columns(4)
        _m1.metric('Городов', stats['subdomains_count'])
        _m2.metric('Категорий', f'{stats["categories_count"]:,}'.replace(',', ' '))
        _m3.metric('Фильтров',
                   f'{stats["filters_count"]:,}'.replace(',', ' ')
                   if stats['has_filters'] else 'нет')
        _pbase = load_product_links(pid)
        if _pbase and _pbase['pathnames']:
            _d = datetime.fromtimestamp(_pbase['collected_at_ms'] / 1000)
            _m4.metric('Товаров',
                       f'{len(_pbase["pathnames"]):,}'.replace(',', ' ')
                       + (' ⚠' if _pbase['is_stale'] else ''),
                       help=f'База ссылок с листингов, собрана {_d.strftime("%d.%m.%Y")}. '
                            f'Обновляется скриптом collect_products.py. Через 30 дней '
                            f'помечается устаревшей (⚠).')
        else:
            _pinfo = get_cached_products_info(pid)
            _m4.metric('Товаров',
                       f'{_pinfo["count"]:,}'.replace(',', ' ') if _pinfo and _pinfo.get('count') else '–',
                       help='Из sitemap.xml (или соберите базу collect_products.py).')
        st.caption(f'Главный город (всегда в выборке): {cfg.get("mandatory_city", "Москва")}.')
        if st.button('Сбросить кэш товаров', key='c30_reset_cache',
                     help='Очищает локальный кэш (sitemap + каталог); при следующем '
                          'прогоне всё перечитается. База в репозитории не трогается.'):
            invalidate_sitemap_cache(pid)
            c30_load_sources.clear()
            st.session_state['c30_cache_reset_done'] = True
            st.rerun()
        if st.session_state.pop('c30_cache_reset_done', False):
            st.success('Кэш очищен. При следующем прогоне товары перечитаются заново.')

    # ── Пункт 1: Доступность и визуальные ошибки ───────────────────
    _maxsubs = max(0, stats['subdomains_count'] - 1)
    _mcity = cfg.get('mandatory_city', 'Москва')

    def _c30_apply_preset(profile_id):
        if profile_id not in PROFILES:
            return
        kw = get_profile_kwargs(profile_id)
        st.session_state.c30_in_cities = kw['random_subdomains_count']
        st.session_state.c30_in_cats = kw['categories_per_subdomain']
        st.session_state.c30_in_filters = kw['filters_per_subdomain']
        st.session_state.c30_in_products = kw['products_per_subdomain']

    def _c30_breakdown(profile_id):
        kw = get_profile_kwargs(profile_id)
        cities = 1 + min(kw['random_subdomains_count'], _maxsubs)
        s = (f"{cities} городов × (главная + каталог + "
             f"{kw['categories_per_subdomain']} кат.")
        if stats['has_filters']:
            s += f" + {kw['filters_per_subdomain']} фильтр."
        s += f" + {kw['products_per_subdomain']} тов.)"
        return s

    # Дефолты выборки = пресет «Стандартная» (карточка и поля совпадают).
    _std = get_profile_kwargs('standard')
    st.session_state.setdefault('c30_in_cities', min(_std['random_subdomains_count'], _maxsubs))
    st.session_state.setdefault('c30_in_cats', _std['categories_per_subdomain'])
    st.session_state.setdefault('c30_in_filters', _std['filters_per_subdomain'])
    st.session_state.setdefault('c30_in_products', _std['products_per_subdomain'])
    if st.session_state.c30_in_cities > _maxsubs:
        st.session_state.c30_in_cities = _maxsubs
    st.session_state.setdefault('c30_preset', 'standard')

    # БЛОК 1 — Доступность: объём (карточки-пресеты + ручная настройка)
    with st.container(border=True):
        st.markdown('### 1. Доступность и визуальные ошибки')
        _c30_sub('Объём проверки')

        # Радио-карточки пресетов. Применяем выбор в основном потоке (по
        # возвращённому значению — оно всегда валидно), без on_change: callback
        # иногда срабатывал со старым/чужим значением и падал.
        _choice = st.radio('Объём', ['quick', 'standard', 'full'],
                           format_func=lambda p: PROFILES[p]['label'],
                           captions=[_c30_breakdown('quick'), _c30_breakdown('standard'),
                                     _c30_breakdown('full')],
                           horizontal=True, key='c30_preset', label_visibility='collapsed')
        if st.session_state.get('_c30_applied') != _choice:
            _c30_apply_preset(_choice)
            st.session_state['_c30_applied'] = _choice
            if st.session_state.c30_in_cities > _maxsubs:
                st.session_state.c30_in_cities = _maxsubs

        with st.expander('Настроить вручную', expanded=False):
            st.caption(f'Главный город {_mcity} всегда в выборке.')
            _ec1, _ec2 = st.columns(2)
            with _ec1:
                st.number_input(f'Случайных городов (+ {_mcity})',
                                min_value=0, max_value=_maxsubs, step=1, key='c30_in_cities')
                st.number_input('Категорий на город',
                                min_value=0, max_value=50, step=1, key='c30_in_cats')
            with _ec2:
                if stats['has_filters']:
                    st.number_input('Фильтров на город',
                                    min_value=0, max_value=50, step=1, key='c30_in_filters')
                else:
                    st.caption('У проекта нет фильтров')
                st.number_input('Товаров на город',
                                min_value=0, max_value=50, step=1, key='c30_in_products')
            _ict = 1 + int(st.session_state.c30_in_cities)
            _iper = (2 + int(st.session_state.c30_in_cats)
                     + (int(st.session_state.c30_in_filters) if stats['has_filters'] else 0)
                     + int(st.session_state.c30_in_products))
            st.caption(f'Итого по этим настройкам: {_ict * _iper} проверок.')

    # Значения для запуска (поля живут в expander, но всегда созданы).
    random_cities = st.session_state.c30_in_cities
    cats_per_city = st.session_state.c30_in_cats
    products_per_city = st.session_state.c30_in_products
    filters_per_city = st.session_state.c30_in_filters if stats['has_filters'] else 0
    budget = {
        'cats': int(cats_per_city),
        'filters': int(filters_per_city) if stats['has_filters'] else 0,
        'products': int(products_per_city),
    }
    budget['per_city'] = 2 + budget['cats'] + budget['filters'] + budget['products']

    # БЛОК 2 — Что проверять на страницах (1.1–1.6)
    with st.container(border=True):
        _hc1, _hc2 = st.columns([3, 1])
        with _hc1:
            st.markdown('### Что проверять на страницах')
        with _hc2:
            def _c30_select_all():
                for _k in ('c30_check_main', 'c30_check_catalog', 'c30_check_categories',
                           'c30_check_filters', 'c30_check_products', 'c30_check_text'):
                    st.session_state[_k] = True
            st.button('Выбрать все', key='c30_select_all', on_click=_c30_select_all,
                      use_container_width=True)
        _cb_col1, _cb_col2 = st.columns(2)
        with _cb_col1:
            st.checkbox('1.1  Главная', key='c30_check_main')
            st.checkbox('1.2  Каталог', key='c30_check_catalog')
            st.checkbox('1.3  Категории всех уровней вложенности', key='c30_check_categories')
        with _cb_col2:
            if stats['has_filters']:
                st.checkbox('1.4  Фильтры', key='c30_check_filters')
            else:
                st.markdown('<span style="color:#9A958C">1.4  Фильтры (нет в каталоге)</span>',
                            unsafe_allow_html=True)
            st.checkbox('1.5  Товары', key='c30_check_products')
            st.checkbox('1.6  Текстовые блоки категорий/фильтров/товаров и переменные',
                        key='c30_check_text')

    # БЛОК 3 — Дополнительно (видимый, без раскрывашки)
    with st.container(border=True):
        st.markdown('### Дополнительно')
        _nf_col1, _nf_col2 = st.columns([3, 1], vertical_alignment='center')
        with _nf_col1:
            _ck_notif = st.checkbox(
                'Собрать уведомления (Вебмастер, GSC, Я.Бизнес, 2ГИС, Google) + 404 Метрики',
                key='c30_fetch_notifications')
        with _nf_col2:
            _nd_opts = [3, 7, 14, 30]
            st.selectbox('За период', _nd_opts,
                         format_func=lambda x: f'{x} дней', key='c30_notify_days',
                         disabled=not _ck_notif, label_visibility='collapsed')
        st.checkbox('Добавить свой список URL', key='c30_use_custom_urls')
        if st.session_state.c30_use_custom_urls:
            st.caption('Ссылки – по одной на строку. Тип по адресу: /catalog/x/ – '
                       'категория, /catalog/x/y/ – товар, …/filter/… – фильтр, / – главная.')
            _up = st.file_uploader('Загрузить .txt / .csv', type=['txt', 'csv'],
                                   label_visibility='collapsed', key='c30_custom_file')
            if _up is not None:
                try:
                    _txt = _up.read().decode('utf-8', errors='replace')
                    if _up.name.lower().endswith('.csv'):
                        _txt = '\n'.join(
                            (ln.split(',') if ',' in ln else ln.split(';'))[0].strip().strip('"\'')
                            for ln in _txt.splitlines())
                    _ex = st.session_state.c30_custom_urls_text.strip()
                    st.session_state.c30_custom_urls_text = (_ex + '\n' + _txt) if _ex else _txt
                except Exception as _e:
                    st.error(f'Не удалось прочитать файл: {_e}')
            st.text_area('URLs', height=160, key='c30_custom_urls_text',
                         label_visibility='collapsed',
                         placeholder='https://stalmetural.ru/catalog/armatura/\n'
                                     'https://orenburg.stalmetural.ru/catalog/truby/truba-20x20/')
            _typed = build_custom_tasks_typed(
                st.session_state.c30_custom_urls_text.split('\n'), src)
            if _typed:
                from collections import Counter as _Counter
                _bt = ', '.join(f'{lbl}: {n}' for lbl, n
                                in _Counter(t.type_label for t in _typed).items())
                st.success(f'Будет добавлено {len(_typed)} URL – {_bt}')

    # БЛОК запуска
    with st.container():
        _paths = _c30_paths(pid)
        _alive = _pid_alive(_read_pidfile(_paths['pid']))
        _bcol, _ccol = st.columns([3, 1])
        with _bcol:
            _go = st.button('▶ Запустить проверку', type='primary',
                            use_container_width=True, key='c30_run', disabled=_alive)
        with _ccol:
            if st.button('⛔ Отменить', use_container_width=True,
                         key='c30_cancel', disabled=not _alive):
                _kill_tree(_read_pidfile(_paths['pid']))
                try:
                    _paths['pid'].unlink(missing_ok=True)
                except Exception:
                    pass
                st.session_state.c30_last_error = 'Проверка отменена'
                st.rerun()
        if _go:
            flags = {
                'check_main': st.session_state.c30_check_main,
                'check_catalog': st.session_state.c30_check_catalog,
                'check_categories': st.session_state.c30_check_categories,
                'check_filters': st.session_state.c30_check_filters and stats['has_filters'],
                'check_products': st.session_state.c30_check_products,
                'check_text': st.session_state.c30_check_text,
                'fetch_notifications': st.session_state.get('c30_fetch_notifications', True),
                'notify_days': int(st.session_state.get('c30_notify_days', 7)),
            }
            # Свой список URL (если включён) – добавится к обычной выборке проекта.
            _custom_urls = []
            if (st.session_state.get('c30_use_custom_urls')
                    and st.session_state.get('c30_custom_urls_text', '').strip()):
                _custom_urls = [
                    ln.strip() for ln in st.session_state.c30_custom_urls_text.split('\n')
                    if ln.strip() and not ln.strip().startswith('#')
                ]
            try:
                _sk_hint = [k for k in list(st.secrets.keys())
                            if 'gsc' in k.lower() or pid in k.lower()]
            except Exception:
                _sk_hint = []
            creds = {
                'proxy_url': get_proxy_url(),
                'tg_token': _secret('telegram_bot_token'),
                'tg_recipients': get_telegram_recipients(pid),
                'metrika': get_metrika_credentials(pid),
                'gsc': get_gsc_credentials(pid),
                'yab': get_yabusiness_credentials(pid),
                'twogis': get_twogis_credentials(pid),
                'google': get_google_accounts_credentials(pid),
                'webmaster_oauth': _secret(f'webmaster_oauth_{pid}'),
                'secret_keys_hint': _sk_hint,
            }
            params = {'budget': budget, 'random_cities': int(random_cities),
                      'custom_urls': _custom_urls, **flags}
            st.session_state.c30_results = None
            st.session_state.c30_report_path = None
            st.session_state.c30_last_error = None
            _launch_checklist_bg(pid, params, creds)
            st.rerun()

    # ── Прогон: прогресс фонового ПРОЦЕССА ──────────────────────────
    _paths = _c30_paths(pid)
    _alive = _pid_alive(_read_pidfile(_paths['pid']))
    _done = _paths['result'].exists() or _paths['report'].exists()
    # Завершение определяем по появлению артефакта (отчёт/результат), а не только
    # по живости pid: на Linux дочерний процесс может стать zombie и «жить» в
    # таблице процессов, из-за чего _alive остаётся True и UI зависает на прогрессе.
    if _alive and not _done:
        with st.container(border=True):
            st.markdown('### ⏳ Идёт проверка')
            st.caption('Можно переключаться на другие вкладки – прогон идёт в фоне '
                       'и не прервётся.')
            _prog, _ptext = 0.0, 'Подготовка…'
            try:
                _s = json.loads(_paths['status'].read_text(encoding='utf-8'))
                _prog, _ptext = float(_s.get('progress', 0.0)), _s.get('text', '')
            except Exception:
                pass
            st.progress(_prog, text=_ptext)
            with st.expander('Подробный лог', expanded=False):
                _logtxt = ''
                try:
                    _logtxt = _paths['log'].read_text(encoding='utf-8', errors='ignore')
                except Exception:
                    pass
                st.code('\n'.join(_logtxt.splitlines()[-120:]) or '…', language='text')
        time.sleep(1.5)
        st.rerun()
    elif _paths['result'].exists() or _paths['report'].exists():
        # Процесс завершился.
        # 1) Путь к отчёту – из лёгкого сайдкара (надёжно, не зависит от pickle).
        try:
            if _paths['report'].exists():
                _rp_txt = _paths['report'].read_text(encoding='utf-8').strip()
                if _rp_txt:
                    st.session_state.c30_report_path = _rp_txt
        except Exception:
            pass
        # 2) Результаты – из pickle (для метрик и блока результатов); если
        #    pickle упал, кнопка скачивания всё равно работает (см. п.1).
        if _paths['result'].exists():
            try:
                with open(_paths['result'], 'rb') as _rf:
                    _res = pickle.load(_rf)
                if _res.get('results') is not None:
                    st.session_state.c30_results = _res['results']
                    st.session_state.c30_started_at = _res['started_at']
                    st.session_state.c30_finished_at = _res['finished_at']
                if _res.get('report_path'):
                    st.session_state.c30_report_path = _res['report_path']
                st.session_state.c30_last_error = _res.get('error')
            except Exception as _e:
                st.session_state.c30_last_error = f'Не удалось прочитать результат: {_e}'
        try:
            _paths['result'].unlink(missing_ok=True)
            _paths['report'].unlink(missing_ok=True)
            _paths['pid'].unlink(missing_ok=True)
        except Exception:
            pass
        st.rerun()

    # ── Ошибка прогона (если была) ──────────────────────────────────
    if st.session_state.get('c30_last_error'):
        st.error(f'Прогон завершился с ошибкой: {st.session_state.c30_last_error}')

    # ── Запасная кнопка скачивания ──────────────────────────────────
    # Если отчёт на диске есть, но блок результатов ниже не отрисовался
    # (нет распарсенных результатов) – всё равно даём скачать xlsx.
    if (st.session_state.get('c30_report_path')
            and not st.session_state.get('c30_results')):
        _rp = Path(st.session_state.c30_report_path)
        if _rp.exists():
            with open(_rp, 'rb') as _f:
                st.download_button(
                    label=f'📥 Скачать отчёт ({_rp.name})',
                    data=_f.read(), file_name=_rp.name,
                    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    use_container_width=True, type='primary', key='c30_dl_fallback')

    # ── Лог прогона: отдельной строкой внизу, виден всегда после
    #    завершения (как в автокликере) — даже если результатов нет. ──
    if not _alive:
        _lp = _c30_paths(pid)['log']
        if _lp.exists():
            _log_txt = _lp.read_text(encoding='utf-8', errors='ignore')
            if _log_txt.strip():
                with st.expander('🧾 Лог прогона (почта / Вебмастер / GSC)',
                                 expanded=True):
                    st.code('\n'.join(_log_txt.splitlines()[-250:]) or '…',
                            language='text')
                st.download_button(
                    label='Скачать полный лог прогона',
                    data=_log_txt.encode('utf-8'),
                    file_name=f'{pid}-run.log', mime='text/plain',
                    use_container_width=True, key='c30_dl_log')

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
            st.markdown('### Результаты проверки')
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
                import html as _html
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
                    url_safe = _html.escape(r.url, quote=True)
                    extra_html = (' – ' + _html.escape(' · '.join(extra))) if extra else ''
                    # Вся строка – чистый HTML (без смешения с markdown-разметкой),
                    # иначе Streamlit иногда не дорисовывает теги-span после markdown-ссылки.
                    st.markdown(
                        f'<div style="margin:2px 0;font-size:0.9rem">'
                        f'{emoji} <b>{_html.escape(city)}</b>{_html.escape(type_label)}: '
                        f'<a href="{url_safe}" target="_blank">{url_safe}</a>'
                        f'{extra_html}{tags_html}</div>',
                        unsafe_allow_html=True,
                    )
                if len(problems) > 50:
                    st.caption(f'... и ещё {len(problems) - 50}. Все детали – в xlsx-отчёте.')

    # Уведомления из почты и 404 из Метрики – в xlsx-отчёте (лист
    # «Уведомления»), собираются по галке «Собрать уведомления» за
    # выбранный период. Отдельный блок в UI убран.

else:
    st.info('Выберите проект, чтобы начать еженедельную проверку.')
