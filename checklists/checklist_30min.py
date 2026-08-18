"""
Чек-лист - проверка сайта-проекта (объединённый 15- и 30-минутный).

Объём задаётся вручную полями ввода (города / категории / фильтры / товары на
город): минимум - быстрый тест, больше - полная еженедельная проверка.

Что делает:
  1. Доступность и визуальные ошибки - выборка URL (главная 1.1, каталог 1.2,
     категории 1.3, фильтры 1.4, товары 1.5, битые переменные 1.6) + структура
     (цена, кнопки заказа, H1, шапка/подвал). Ротация с окном 30 дней.
     Можно добавить свой список URL.
  2. Сбор уведомлений из почты (Вебмастер, GSC, Я.Бизнес, 2ГИС, Google) +
     404 из Метрики - в xlsx-отчёт.
  3. Telegram-уведомление с отчётом после прогона.

Прогон идёт ОТДЕЛЬНЫМ процессом (checklist_run.py → runner_30min), поэтому
переживает переключение вкладок и не сбрасывается.
"""
import asyncio
import hashlib
import json
import os
import pickle
import subprocess
import sys
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
# Облако перечитывает файлы страниц после обновления кода, но модули из
# sys.modules не перезагружает: страница уже новая, run_estimate - ещё старый,
# и импорт добавленной функции валит страницу ImportError. Перечитываем сами.
import importlib

import run_estimate as _re
if not hasattr(_re, 'estimate_run_seconds'):
    _re = importlib.reload(_re)
estimate_run_seconds, format_estimate = _re.estimate_run_seconds, _re.format_estimate
from sitemap import (
    load_product_pathnames, get_cached_products_info, invalidate_sitemap_cache,
)
from sitemap_sampling import discover_child_sitemaps, group_by_kind
from product_links import load_product_links
from http_checker import run_batch
from reporter import build_report, make_report_filename
from telegram_notify import format_summary_message, send_run_notification
from metrika_404 import MAILBOX_CONFIG
from webmaster_notify import (
    WEBMASTER_YANDEX_CONFIG, GSC_GMAIL_CONFIG,
    YABUSINESS_YANDEX_CONFIG, TWOGIS_YANDEX_CONFIG, GOOGLE_ACCOUNTS_CONFIG,
    GOOGLE_FOLDER_YANDEX_CONFIG, DEFAULT_FOLDERS,
    fetch_webmaster_yandex, fetch_gsc_gmail,
    fetch_yandex_folder_simple, fetch_google_accounts,
    load_notifications,
)

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from checklists import page_templates as _tpl
from checklists import ui_widgets as _ui
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


# Иногда секрет проекта назван с опечаткой зоны (imp → «inp»). Пробуем оба
# варианта + общий ключ без проекта.
_PID_ALIASES = {'imp': ('imp', 'inp')}


def _secret_pid(base, project_id):
    """Секрет вида base_<pid> с учётом алиасов pid и общим фоллбэком base.
    ПРИОРИТЕТ: настройки проекта из личного кабинета (страница «Настройки
    проекта», хранятся в БД) — потом st.secrets."""
    try:
        import auth
        v = auth.project_setting(project_id, base)
        if v:
            return v
    except Exception:
        pass
    for p in _PID_ALIASES.get(project_id, (project_id,)):
        v = _secret(f'{base}_{p}')
        if v:
            return v
    return _secret(base)


# Ящики проекта: поля личного кабинета для каждого почтовика. Настройки
# проекта ПЕРВЫЕ - руководитель заводит новый проект сам, без доступа к
# секретам приложения; секреты остаются для уже настроенных проектов.
_MAIL_FIELDS = {
    'yandex': ('mail_yandex_login', 'mail_yandex_password'),
    'google': ('mail_google_login', 'mail_google_password'),
}


def _mail_creds(kind, project_id, cfg):
    """(логин, пароль) ящика проекта: сначала настройки из кабинета, потом
    секреты приложения по именам из cfg. cfg может быть None - тогда работают
    только настройки проекта (новый проект не требует правки кода)."""
    поле_логин, поле_пароль = _MAIL_FIELDS[kind]
    логин = пароль = None
    try:
        import auth
        логин = auth.project_setting(project_id, поле_логин) or None
        пароль = auth.project_setting(project_id, поле_пароль) or None
    except Exception:
        pass
    if логин and пароль:
        return логин, пароль
    if not cfg:
        return логин, пароль
    return (логин or _secret(cfg['secret_email']),
            пароль or _secret(cfg['secret_password']))


def get_metrika_credentials(project_id):
    return _mail_creds('yandex', project_id, MAILBOX_CONFIG.get(project_id))


def get_gsc_credentials(project_id):
    return _mail_creds('google', project_id, GSC_GMAIL_CONFIG.get(project_id))


def _basic_auth_for(cfg, creds):
    """Вход по паролю для закрытого сайта (стенд за nginx-паролем) или None.

    Логин/пароль живут в «Настройках проекта» (site_basic_login /
    site_basic_password) или в секретах приложения - в git и в конфиг проекта
    они не попадают. Хост берём из самого проекта, чтобы пароль уходил только
    ему: проверка битых ссылок звонит и на чужие домены."""
    if not (cfg or {}).get('basic_auth'):
        return None
    логин = (creds or {}).get('site_basic_login') or ''
    пароль = (creds or {}).get('site_basic_password') or ''
    if not логин:
        return None
    return {'host': (cfg.get('root_domain') or '').strip().lower(),
            'login': логин, 'password': пароль}


def get_gsc_sa(project_id):
    """Сервисный аккаунт Google для Search Console API - источник «Google» в
    «404 в индексе» через API (работает на облаке, без браузера).

    Секрет gsc_service_account_<pid> (или общий gsc_service_account) в одном из
    видов: base64 JSON-ключа (удобнее всего для TOML), строка-JSON или
    TOML-секция. Возвращает разобранный ключ (dict) или None."""
    import base64
    import json as _json
    val = _secret_pid('gsc_service_account', project_id)
    if val is None:
        return None
    if isinstance(val, dict):        # TOML-секция
        return dict(val)
    s = str(val).strip()
    if not s:
        return None
    if s.startswith('{'):            # строка-JSON
        try:
            return _json.loads(s)
        except Exception:
            return None
    try:                             # иначе base64
        return _json.loads(base64.b64decode(''.join(s.split())).decode('utf-8'))
    except Exception:
        return None


def get_yabusiness_credentials(project_id):
    cfg = YABUSINESS_YANDEX_CONFIG.get(project_id)
    л, п = _mail_creds('yandex', project_id, cfg)
    return л, п, (cfg or {}).get('folder', DEFAULT_FOLDERS['yabusiness'])


def get_twogis_credentials(project_id):
    cfg = TWOGIS_YANDEX_CONFIG.get(project_id)
    л, п = _mail_creds('yandex', project_id, cfg)
    return л, п, (cfg or {}).get('folder', DEFAULT_FOLDERS['twogis'])


def get_google_accounts_credentials(project_id):
    return _mail_creds('google', project_id,
                       GOOGLE_ACCOUNTS_CONFIG.get(project_id))


def get_google_folder_credentials(project_id):
    """Яндекс-папка с письмами GSC («Гугл» / «Google Search Console»)."""
    cfg = GOOGLE_FOLDER_YANDEX_CONFIG.get(project_id)
    л, п = _mail_creds('yandex', project_id, cfg)
    return л, п, (cfg or {}).get('folder', DEFAULT_FOLDERS['google_folder'])


def _gdrive_refresh(project_id: str) -> str:
    """Ключ служебного Google-аккаунта для выкладки отчётов: сперва общая
    привязка (один аккаунт на все проекты), потом привязка самого проекта."""
    try:
        import auth
        v = auth.gdrive_account_settings(project_id)
        if v.get('refresh_token'):
            return v['refresh_token']
    except Exception:  # noqa: BLE001
        pass
    return _secret_pid('gdrive_refresh_token', project_id)


def _кто_запустил() -> str:
    """«Имя Фамилия» текущего пользователя ('' - не определён). Подпись к
    отчёту: у руководителя в чате смешиваются свои и чужие прогоны.

    Определение одно на все прогоны (tg_report.кто_запустил): подпись должна
    выглядеть одинаково и у чек-листа, и у форм/целей/КП/скорости."""
    try:
        import tg_report
        return tg_report.кто_запустил()
    except Exception:  # noqa: BLE001
        return ""


def get_telegram_recipients(project_id):
    """Кому уходит отчёт о прогоне:

      • ТОМУ, КТО ЗАПУСТИЛ - если он подключил Telegram в личном кабинете;
      • плюс руководителям/админам, выбравшим в кабинете «все прогоны по моим
        проектам» (см. db.telegram_project_subscribers) - им видны и чужие
        запуски по их проектам.

    Сотруднику выбора нет: он получает только свои запуски. Если Telegram не
    подключил никто - откатываемся на старый список из секрета
    telegram_recipients_<проект>, чтобы уже настроенные проекты не остались
    без уведомлений."""
    chats = []

    def _add(v):
        v = str(v or '').strip()
        if v and v not in chats:
            chats.append(v)

    try:
        import auth
        import telegram_link
        _u = auth.current_user()
        if _u:
            _add(telegram_link.chat_id_for_user(_u['id']))
    except Exception:  # noqa: BLE001
        pass
    try:
        from auth import db as _db
        for _s in _db.telegram_project_subscribers(project_id):
            _add(_s.get('chat_id'))
    except Exception:  # noqa: BLE001
        pass
    if chats:
        return chats

    val = _secret(f'telegram_recipients_{project_id}')
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, (list, tuple)):
        return [str(v).strip() for v in val if str(v).strip()]
    return []


def _resolve_m404_period() -> dict:
    """Период 404-Метрики из session_state → {metrika_404_date1, _date2}.
    'За день' → конкретная дата; 'За неделю' → 7daysAgo..today;
    'За период' → выбранные С/По."""
    mode = st.session_state.get('c30_m404_mode', 'За день')
    if mode == 'За день':
        d = st.session_state.get('c30_m404_day')
        ds = d.strftime('%Y-%m-%d') if d else 'yesterday'
        return {'metrika_404_date1': ds, 'metrika_404_date2': ds}
    if mode == 'За период':
        df = st.session_state.get('c30_m404_from')
        dt = st.session_state.get('c30_m404_to')
        return {
            'metrika_404_date1': df.strftime('%Y-%m-%d') if df else '7daysAgo',
            'metrika_404_date2': dt.strftime('%Y-%m-%d') if dt else 'today',
        }
    _days = {'За неделю': 7, 'За 14 дней': 14, 'За 30 дней': 30}.get(mode, 7)
    return {'metrika_404_date1': f'{_days}daysAgo', 'metrika_404_date2': 'today'}


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
    """Кто отвечает за конкретную проблему. Пусто - если страница работает.

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
# не перезапускает прогон - поток продолжает работать сам.

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
    """Выполняет прогон в фоне. НЕ обращается к st.* - все секреты переданы
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

        # Товары: база листингов → fallback sitemap. Если карточек товара у
        # проекта нет вовсе (has_products=false - напр. МПИ, где весь каталог
        # плоский), не ходим за sitemap и не пишем пугающее «0 товаров».
        if not cfg.get('has_products', True):
            append_log('Товары: у проекта нет карточек товаров - '
                       'тип «Товар» не проверяется.')
        elif not src.products:
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
            mandatory_hosts=cfg.get('mandatory_hosts'),
            cis_extra_subdomains=int(flags.get('cis_extra', 0)),
            trailing_slash=cfg.get('trailing_slash', True),
            # Ключа нет - None, и раздел каталога собирается как раньше.
            # Пустая строка в конфиге (АПС) - раздела у сайта нет, не проверяем.
            catalog_path=cfg.get('catalog_path'),
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
                f'Проверено {done} из {total_n} - '
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

        # Региональные проверки (п.1.8 верные переменные / п.1.9 СНГ-чистота)
        _chk_region = bool(flags.get('check_region', True))
        _chk_cis = bool(flags.get('check_cis', True))
        region_ctx = None
        if _chk_region or _chk_cis:
            try:
                from region_checker import build_region_context
                region_ctx = build_region_context(kp_map, src.subdomains)
            except Exception as e:
                region_ctx = None
                append_log(f'⚠ Регион-проверки не активны: {e}')

        results = asyncio.run(run_batch(
            plan.tasks, concurrency=6, timeout_ms=120000, max_attempts=3,
            retry_delay_ms=2500, check_text=True,
            check_links=bool(flags.get('check_links', False)),
            check_indexing=bool(flags.get('check_indexing', True)),
            check_region=_chk_region and region_ctx is not None,
            check_cis=_chk_cis and region_ctx is not None,
            check_meta=bool(flags.get('check_meta', True)),
            region_ctx=region_ctx,
            on_progress=on_progress, proxy_url=proxy_url, kp_map=kp_map,
            basic_auth=_basic_auth_for(cfg, creds),
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
                    {'city': r.city or '-', 'url': r.url,
                     'status': {'not_found': '404 Не найдена',
                                'client_error': 'Ошибка на сайте',
                                'server_error': 'Сервер не отвечает',
                                'timeout': 'Нет ответа',
                                'network_error': 'Нет соединения'}.get(r.status, r.status)}
                    for r in results if r.is_error][:5]
                empty_sections = [
                    {'city': r.city or '-', 'url': r.url} for r in results
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
                    report_file=report_path, proxy_url=proxy_url,
                    log=lambda lvl, msg: append_log(msg),
                )
                append_log(f'✓ Telegram: отправлено {tg_result["sent"]}, '
                           f'не доставлено {tg_result["failed"]}')
            except Exception as e:
                append_log(f'⚠ Telegram-отправка упала: {e}')
        else:
            append_log('Telegram не настроен - отправьте отчёт ответственным вручную (пункт 2).')

        # Сбор уведомлений из почты
        if flags['fetch_notifications']:
            append_log('Собираю уведомления из почты…')
            _nlog = lambda lvl, msg: append_log(msg)
            _proxy = proxy_url   # с учётом use_proxy проекта (как для страниц)

            _yw_e, _yw_p = creds['metrika']
            _yw_cfg = WEBMASTER_YANDEX_CONFIG.get(pid) or {}
            _yw_folder = _yw_cfg.get('folder', DEFAULT_FOLDERS['webmaster'])
            if _yw_e and _yw_p:
                try:
                    fetch_webmaster_yandex(pid, _yw_e, _yw_p, _yw_folder, 30, _proxy, _nlog)
                except Exception as _e:
                    append_log(f'⚠ Вебмастер: {_e}')
            else:
                append_log('⚠ Вебмастер: не задана почта проекта - впишите её в '
                           '«Настройки проекта» (почта Яндекса + пароль приложения)')

            _gsc_e, _gsc_p = creds['gsc']
            if _gsc_e and _gsc_p:
                append_log(f'GSC: креды найдены ({_gsc_e}), подключаюсь к Gmail…')
                try:
                    fetch_gsc_gmail(pid, _gsc_e, _gsc_p, 30, _nlog)
                except Exception as _e:
                    append_log(f'⚠ GSC: {_e}')
            else:
                append_log('⚠ GSC: не задан Gmail проекта - впишите его в '
                           '«Настройки проекта» (Gmail + пароль приложения). '
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
                append_log('Уведомлений нет - лист «Уведомления» в отчёт не добавлен.')
        else:
            append_log('Чекбокс «Собрать уведомления из почты» выключен - почту не проверяю.')

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


# Версия набора дефолтов галочек «Что проверять». Поднимать при добавлении
# нового пункта или смене дефолта, чтобы автовыбор применился и к уже
# открытым (сохранённым) сессиям (см. _apply_page_check_defaults).
_C30_CHECKS_DEFAULTS_VER = 15

# Галочки страничных разделов (Доступность сайта/Техничка/Вёрстка/Безопасность/
# Данные по городам/Аналитика) - включены по умолчанию. «Дополнительно» и «Админка» сюда НЕ входят
# (они по умолчанию выключены). Держим единым списком: им и force-set при заходе
# на проект, и разовый автовыбор при новой версии дефолтов - оба из ГЛАВНОГО
# потока страницы (в init_session присвоение не подхватывалось виджетами чекбоксов,
# показывая «снятые» галочки при полном счётчике). c30_check_filters включаем
# безусловно - если фильтров у проекта нет, чекбокс просто не рисуется.
_C30_PAGE_CHECKS = (
    'c30_check_main', 'c30_check_catalog', 'c30_check_categories',
    'c30_check_filters', 'c30_check_products', 'c30_check_text',
    'c30_check_indexing', 'c30_check_meta', 'c30_check_w3c', 'c30_check_static',
    'c30_check_404', 'c30_check_ps_filters', 'c30_check_link_profile',
    'c30_check_links', 'c30_check_index_404', 'c30_check_home_dupes',
    'c30_check_filter_fn',
    'c30_check_layout', 'c30_check_markup', 'c30_check_images', 'c30_check_console',
    'c30_check_security',
    'c30_check_region', 'c30_check_cis', 'c30_check_yabusiness',
    'c30_check_anomaly', 'c30_check_traffic', 'c30_check_review_priority',
    'c30_check_anomalies', 'c30_check_trust', 'c30_check_calltracking',
)


def init_session():
    defaults = {
        'c30_project_id': None,
        'c30_is_running': False,
        'c30_results': None,
        'c30_report_path': None,
        'c30_started_at': None,
        'c30_finished_at': None,
        'c30_run_sig': None,    # «подпись» показываемого прогона (проект+объём+галочки)
        # URL-проверки
        'c30_check_main': True,
        'c30_check_catalog': True,
        'c30_check_categories': True,
        'c30_check_filters': True,
        'c30_check_products': True,
        'c30_check_text': True,        # пункт 1.6 - битые переменные
        'c30_check_indexing': True,    # пункт 1.7 - индексация (robots/noindex/canonical)
        'c30_check_meta': True,        # пункт 1.8 - метаданные, дубли, единственность тегов
        'c30_check_region': True,      # пункт 1.9 - верные переменные города (по КП)
        'c30_check_cis': True,         # пункт 1.10 - СНГ-домены без РФ/СНГ/чужих стран
        'c30_check_layout': True,      # пункт 1.11 - вёрстка и адаптивность (viewport, CSS)
        'c30_check_markup': True,      # пункт 1.12 - микроразметка Schema.org + OpenGraph
        'c30_check_security': True,    # доп. 1.8 - заголовки безопасности HTTP
        'c30_check_images': True,      # пункт 1.15 - изображения (alt/webp/вес)
        'c30_check_links': True,      # «ссылки открываются (404)» - тяжёлая, по запросу
        'c30_check_index_404': True,  # 404 среди страниц в индексе (Вебмастер) - тяжёлая, по запросу
        'c30_check_yabusiness': True,  # Я.Бизнес: поддомен под свой регион (сессия)
        'c30_check_traffic': True,     # сравнение трафика день/месяц/год (Метрика)
        'c30_check_review_priority': True,  # приоритет докупки отзывов (Я.Бизнес+2ГИС)
        'c30_check_anomalies': True,   # аномалии ГСК-запросов + Метрика-рефералы
        'c30_check_trust': True,       # траст проекта: ИКС + DR (Open PageRank)
        'c30_check_gsc_pages': False,  # количество страниц в ГСК по статусам - браузер, по запросу
        'c30_check_home_dupes': True,  # дубли главной страницы (HTTP, без браузера)
        'c30_check_arsenkin': False,  # индексация URL через API Арсенкина (токен из поля)
        'c30_check_uniqueness': False,  # уникальность контента через text.ru (ключ из поля)
        'c30_check_filter_fn': True,  # фильтр-тест товаров (браузер) - по запросу
        'c30_check_console': True,    # п.1.14 - ошибки JS в консоли (браузер) - по запросу
        'c30_check_calltracking': True,  # замена рекламного номера (браузер) - по запросу
        'c30_check_stress': False,     # ошибки сервера: парсинг/нагрузка/дубли URL - по запросу
        'c30_check_w3c': True,         # п.1.16 - валидация W3C + скорость
        'c30_check_static': True,      # п.1.17 - сжатие/кеш статики
        'c30_check_404': True,         # п.1.18 - страница 404
        'c30_check_ps_filters': True,  # п.1.19 - фильтры/санкции ПС
        'c30_check_link_profile': True,   # п.1.20 - lite-профиль ссылок (Вебмастер API)
        'c30_check_anomaly': True,        # п.1.21 - аномалии Вебмастера + внезапные мусорные ссылки
        'c30_check_admin_settings': False,  # админка: функции настройки работают (рендер)
        'c30_check_admin_crud': False,      # админка: CRUD поддоменов/категорий
        'c30_check_admin_product_crud': False,  # админка: CRUD товаров (по CMS)
        'c30_check_admin_tech_crud': False,  # админка: CRUD техстраниц (наличие)
        'c30_check_admin_counters': False,  # админка: счётчики аналитики
        'c30_adm_execute': True,            # CRUD с записью (симуляция + откат)
        # Сервисные проверки
        'c30_check_webmaster': True,
        'c30_check_gsc': True,
        'c30_fetch_notifications': False,
        'c30_notify_days': 1,   # прогон ежедневный → по умолчанию забираем за 1 день
        'c30_fetch_metrika_404': False,    # 404 из Метрики (API) в отчёт
        'c30_m404_mode': 'За день',       # при включении сразу «За день» + дата
        'c30_autoclick': False,           # автокликер (локально) после проверки
        'c30_ac_wm': False,
        'c30_ac_gsc': False,
        # Свой список URL
        'c30_use_custom_urls': False,
        'c30_custom_urls_text': '',
        # Случайная проверка карт сайта (сэмпл по видам)
        'c30_check_sitemap_sample': False,
        'c30_sitemap_urls_per_map': 5,
        'c30_sitemap_mode': 'Настроить вручную',
        # Уникальность контента (text.ru): объём выборки и порог.
        # SEO-текст лежит на КАТЕГОРИЯХ; в товарах текст генерится через
        # переменные - по умолчанию товары не проверяем (0).
        'c30_uniq_cats': 5,
        'c30_uniq_prods': 0,
        'c30_uniq_threshold': 95,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v
    # ВНИМАНИЕ: разовый автовыбор галочек при новой версии дефолтов делаем НЕ здесь,
    # а в главном потоке страницы (_apply_page_check_defaults): присвоение
    # st.session_state[...] из init_session не подхватывалось виджетами чекбоксов -
    # галочки выходили «снятыми» при полном счётчике. См. _C30_PAGE_CHECKS.


init_session()


def _apply_page_check_defaults():
    """Разовый (на новую версию дефолтов) автовыбор всех галочек разделов 1-5.
    Вызывать из ГЛАВНОГО потока страницы ДО отрисовки чекбоксов - только так
    присвоение подхватывается виджетами (из init_session не срабатывало: счётчик
    показывал «17/17», а часть галочек рисовалась снятой). После применения ставим
    метку версии - дальше пользователь свободно снимает/ставит галочки."""
    if st.session_state.get('c30_checks_defaults_ver') != _C30_CHECKS_DEFAULTS_VER:
        for _k in _C30_PAGE_CHECKS:
            st.session_state[_k] = True
        st.session_state['c30_checks_defaults_ver'] = _C30_CHECKS_DEFAULTS_VER


@st.cache_data(ttl=3600, show_spinner='Загружается каталог проекта…')
def c30_load_sources(project_id: str):
    cfg = load_project_config(project_id)
    src = load_sources(cfg)
    return cfg, src


@st.cache_data(ttl=3600)
def c30_checklist_projects():
    """Проекты, пригодные для чек-листа. Чек-лист гоняет выборку по каталогу
    проекта (категории/фильтры/товары), поэтому проекту нужны catalog_csv и
    subdomains_csv. Проекты без каталога (напр. АПС - Авиапромсталь: оба поля
    не заданы) проверять нечем - не показываем их в списке, иначе выбор такого
    проекта падал с «Не удалось загрузить каталог»."""
    out = []
    for p in list_projects():
        try:
            cfg = load_project_config(p['id'])
        except Exception:  # noqa: BLE001
            continue
        if cfg.get('catalog_csv') and cfg.get('subdomains_csv'):
            out.append(p)
    return out


# ── Распределение бюджета выборки ──────────────────────────────────


def split_budget(target_urls: int, cities: int, has_filters: bool) -> dict:
    """
    Разложить общий размер выборки (300-500 URL) на параметры build_plan.

    На каждый город: главная + каталог (фикс) + категории/фильтры/товары.
    Категории - самая большая доля (все уровни вложенности), затем фильтры
    и товары. Если фильтров у проекта нет - их долю делят категории и товары.
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
    'случайной выборке URL. Объём задаёте сами - минимум для быстрого теста, '
    'больше для еженедельной проверки. Прогон идёт в фоне и переживает '
    'переключение вкладок.'
)

# Локальный CSS только для этой страницы: primary-кнопка («Запустить
# еженедельную проверку»). app.py красит белым саму кнопку, но текст лежит
# во вложенном <p>, который глобальное правило перекрашивает в тёмный -
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
    /* Зелёная кнопка скачивания отчёта - чтобы не путалась с primary */
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
    /* Подписи полей чуть крупнее и темнее - читаемо */
    [data-testid="stNumberInput"] label p,
    .stCheckbox label p, .stSelectbox label p {
        font-size: .9rem !important; color: #3A3A3A !important; font-weight: 500 !important;
    }
    /* Значение в числовом поле - крупное, в фокусе внимания */
    [data-testid="stNumberInput"] input { font-size: 1.05rem !important; font-weight: 600 !important; }
    /* Итог/оценка - спокойный вторичный текст */
    .c30-summary { font-size: .95rem; color: #1A1A1A; margin-top: .3rem; }

    /* ── Раскладка: воздух в карточках, отступы между блоками, выравнивание ── */
    /* Карточки-секции: внутренний воздух + ровный отступ между блоками */
    .block-container [data-testid="stVerticalBlockBorderWrapper"] {
        padding: 20px 24px !important; margin-bottom: 16px !important;
    }
    /* Раскрывашки делаем ЛЁГКИМИ (не вторая жирная рамка), чтобы «скрытое»
       читалось как второстепенное, а карточки-секции - как главное. */
    [data-testid="stExpander"] {
        border: 1px solid #ECEAE4 !important; background: #FBFAF8 !important;
        box-shadow: none !important; border-radius: 10px !important;
        margin-bottom: 8px !important;
    }
    [data-testid="stExpander"] summary {
        font-size: .9rem !important; font-weight: 600 !important; color: #5B5853 !important;
    }
    /* Карточки-пресеты (контейнер с рамкой в колонке) - кликабельный вид */
    [data-testid="column"] [data-testid="stVerticalBlockBorderWrapper"] {
        padding: 14px 12px !important; margin-bottom: 0 !important;
        transition: border-color .15s;
    }
    [data-testid="column"] [data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: #B9B2A6 !important;
    }
    /* Метрики каталога - числа крупные, подписи спокойные (ровный ряд) */
    [data-testid="stMetricValue"] { font-size: 1.7rem !important; }
    [data-testid="stMetricLabel"] p { font-size: .8rem !important; color: #8A867F !important; }
    /* Значок подсказки «?» глобально скрыт (app.py). У метрик «Каталог проекта»
       он нужен (откуда города/категории/…) - возвращаем точечно и аккуратно. */
    [data-testid="stMetricLabel"] [data-testid="stTooltipIcon"] {
        display: inline-flex !important; align-items: center;
        margin-left: 5px; cursor: help;
    }
    [data-testid="stMetricLabel"] [data-testid="stTooltipIcon"] svg {
        width: 15px !important; height: 15px !important; opacity: .7;
    }
    /* Подсказки «?» у пунктов чек-листа: глобально иконка скрыта в app.py -
       возвращаем её для ВСЕХ галочек на этой странице (у каждого пункта своя
       подсказка при наведении, где она задана help=). */
    [data-testid="stCheckbox"] [data-testid="stTooltipIcon"] {
        display: inline-flex !important; align-items: center;
        margin-left: 5px; cursor: help;
    }
    [data-testid="stCheckbox"] [data-testid="stTooltipIcon"] svg {
        width: 15px !important; height: 15px !important; opacity: .7;
    }

    /* Пресеты как карточки (radio с подписями): клик = выбор, выбранная -
       рамка подсвечивается; равные, по центру, без кружка и кнопки «Выбрать». */
    .st-key-c30_preset div[role="radiogroup"] { gap: 12px !important; align-items: stretch !important; }
    /* прячем кружок радио - карточка кликается целиком, заголовок по центру.
       В разных версиях Streamlit кружок - это либо первый div, либо сам input;
       гасим оба варианта, чтобы заголовок точно встал по центру карточки. */
    .st-key-c30_preset div[role="radiogroup"] > label > div:first-child,
    .st-key-c30_preset div[role="radiogroup"] > label input[type="radio"],
    .st-key-c30_preset div[role="radiogroup"] > label [data-testid="stRadioButton"] > div:first-child {
        display: none !important;
    }
    /* заголовок + подпись строго по центру карточки (перебиваем строчную раскладку) */
    .st-key-c30_preset div[role="radiogroup"] > label,
    .st-key-c30_preset div[role="radiogroup"] > label > div {
        flex-direction: column !important; align-items: center !important;
        text-align: center !important; width: 100% !important;
    }
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
    /* Заголовок карточки - крупнее, по центру, наш шрифт */
    .st-key-c30_preset div[role="radiogroup"] > label [data-testid="stMarkdownContainer"] p {
        font-family: 'Hanken Grotesk', sans-serif !important;
        font-size: 1.15rem !important; font-weight: 600 !important;
        color: #1A1A1A !important; text-align: center !important; margin: 0 !important;
    }
    /* Подпись снизу - мельче, БЕЗ жирности, по центру */
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
    projects = c30_checklist_projects()
    options = ['- выберите -'] + [p['name'] for p in projects]
    name_to_id = {p['name']: p['id'] for p in projects}

    current = '- выберите -'
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
        st.session_state.c30_last_error = None
        # Сбрасываем «подпись прогона» - чтобы при смене проекта не показывался
        # старый лог/результат от прошлого проекта или прошлой сессии.
        st.session_state.c30_run_sig = None
        # Поля выборки перечитаются под новый проект (свои лимиты городов).
        for _k in ('c30_in_cities', 'c30_in_cats', 'c30_in_filters', 'c30_in_products'):
            st.session_state.pop(_k, None)
        # Галочки «Что проверять» при заходе на проект - ВСЕ разделы 1-5 включены
        # (дефолт). Ставим ЯВНО здесь (до отрисовки чекбоксов), иначе значение из
        # init_session не подхватывалось виджетом и галочки выходили пустыми, а
        # счётчик/кнопка врали. Единый список - _C30_PAGE_CHECKS.
        for _k in _C30_PAGE_CHECKS:
            st.session_state[_k] = True

pid = st.session_state.c30_project_id

# Прокси - чек-бокс дорисовывается в место, оставленное render_account_ui
# в сайдбаре (см. auth.fill_proxy_slot) - теперь с уже известным ЗДЕСЬ pid.
import auth
_c30_effective_proxy = auth.fill_proxy_slot(pid)

if pid:
    try:
        cfg, src = c30_load_sources(pid)
    except Exception as e:
        st.error(f'Не удалось загрузить каталог: {e}')
        st.stop()
    stats = src.stats

    # ── Проектные шаблоны ──────────────────────────────────────────
    def _c30_tpl_apply(_tpl_data):
        # Пусть пресет «Объём» пере-применится под текущий проект: числа выборки
        # (города/категории/фильтры/товары) возьмутся из пресета и точно попадут
        # в пределы каталога. Галочки «что проверять» приходят из шаблона и не
        # трогаются.
        st.session_state.pop('_c30_applied', None)

    # Разрешённый список (allowlist) - сохраняем ТОЛЬКО безопасные ключи; логины,
    # пароли, токены, домен админки и «выполнять запись в БД» сюда не попадают.
    # Числа выборки (c30_in_*) намеренно не сохраняем - их задаёт пресет при
    # загрузке шаблона (всегда в пределах текущего каталога). Функция читает
    # session_state в момент клика «Сохранить» - к тому времени все галочки уже
    # отрисованы в прошлом прогоне, и их значения лежат в session_state.
    def _c30_tpl_keys():
        _allow_prefix = ('c30_check_', 'c30_ac_', 'c30_stress_', 'c30_fetch_')
        _allow_exact = {'c30_preset', 'c30_notify_days', 'c30_m404_mode',
                        'c30_use_custom_urls', 'c30_arsenkin_yandex',
                        'c30_arsenkin_google', 'c30_arsenkin_search_all',
                        'c30_arsenkin_inurl'}
        return [_k for _k in list(st.session_state.keys())
                if _k.startswith(_allow_prefix) or _k in _allow_exact]

    def _c30_tpl_reset():
        # Сброс к стандартным: сбрасываем метку версии дефолтов галочек - тогда
        # init_session() наверху страницы переставит ВСЕ c30_check_* в дефолт.
        # Остальные под-настройки убираем - init переставит их своими дефолтами.
        st.session_state.pop('c30_checks_defaults_ver', None)
        for _k in list(st.session_state.keys()):
            if (_k.startswith(('c30_ac_', 'c30_stress_', 'c30_fetch_'))
                    or _k in ('c30_preset', 'c30_notify_days', 'c30_m404_mode',
                              'c30_use_custom_urls', 'c30_autoclick',
                              'c30_adm_execute', 'c30_arsenkin_yandex',
                              'c30_arsenkin_google', 'c30_arsenkin_search_all',
                              'c30_arsenkin_inurl')):
                st.session_state.pop(_k, None)

    _tpl.render_panel(
        'checklist', pid, on_apply=_c30_tpl_apply, save_keys=_c30_tpl_keys,
        on_reset=_c30_tpl_reset,
        help_text='Шаблон запоминает объём (быстрая/стандартная/полная), все '
                  'галочки «что проверять» и под-настройки (кроме логинов, '
                  'паролей и API-токенов). Хранится на сервере проекта **до '
                  'перезапуска приложения** - после может сброситься.')

    # ── Каталог проекта: метрики + сброс кэша ──────────────────────
    with st.container(border=True):
        st.markdown('#### Каталог проекта')
        _m1, _m2, _m3, _m4 = st.columns(4)
        _m1.metric('Городов', stats['subdomains_count'],
                   help=f'Города проекта = его поддомены (spb., kazan. …). Список берём '
                        f'из справочника поддоменов проекта. В каждый прогон идёт главный '
                        f'город ({cfg.get("mandatory_city", "Москва")}) плюс выбранное '
                        f'число случайных городов из этого списка.')
        _m2.metric('Категорий', f'{stats["categories_count"]:,}'.replace(',', ' '),
                   help='Категории каталога (страницы вида /catalog/…) из выгрузки каталога '
                        'проекта. Сколько из них проверять на каждый город - задаётся ниже, '
                        'в «Объём проверки → Категорий на город».')
        _m3.metric('Фильтров',
                   f'{stats["filters_count"]:,}'.replace(',', ' ')
                   if stats['has_filters'] else 'нет',
                   help='Страницы-фильтры (теги, напр. подборки по параметру) из выгрузки '
                        'каталога проекта. Есть не у всех проектов. Сколько проверять на '
                        'город - в «Объём проверки → Фильтров на город».')
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
                       f'{_pinfo["count"]:,}'.replace(',', ' ') if _pinfo and _pinfo.get('count') else '-',
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

    # БЛОК 1 - Доступность: объём (карточки-пресеты + ручная настройка)
    with st.container(border=True):
        st.markdown('### 1. Доступность и визуальные ошибки')
        _c30_sub('Объём проверки')

        # Радио-карточки пресетов. Применяем выбор в основном потоке (по
        # возвращённому значению - оно всегда валидно), без on_change: callback
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

    # БЛОК 2 - Что проверять на страницах
    # Разовый автовыбор всех галочек разделов 1-5 при новой версии дефолтов -
    # из главного потока (до отрисовки чекбоксов), чтобы значение подхватилось
    # виджетами и счётчик совпадал с реально отмеченными пунктами.
    _apply_page_check_defaults()
    with st.container(border=True):
        # Учитываем только РЕАЛЬНО показанные галочки: фильтры есть не у всех
        # проектов (если их нет - чекбокс не рисуется, и его нельзя учитывать в
        # «Выбрать/Снять все», иначе кнопка врёт).
        # Ключи по разделам - для счётчиков «выбрано/всего» в шапках аккордеонов
        # и для кнопки «Выбрать/Снять все». Фильтры учитываем, только если они
        # есть в каталоге (иначе чекбокс не рисуется). Порядок = как в разделе.
        # «Доступность сайта» - типы страниц: открывается ли вообще то, что
        # должно открываться. Отсюда начинается любой прогон, поэтому раздел
        # первый и отделён от остальной технички.
        _SEC_DOST = ['c30_check_main', 'c30_check_catalog', 'c30_check_categories',
                     'c30_check_products', 'c30_check_sitemap_sample']
        if stats['has_filters']:
            _SEC_DOST.insert(3, 'c30_check_filters')
        _SEC_TEH = ['c30_check_text', 'c30_check_indexing',
                    'c30_check_meta', 'c30_check_markup', 'c30_check_w3c',
                    'c30_check_static', 'c30_check_404', 'c30_check_ps_filters',
                    'c30_check_links', 'c30_check_index_404', 'c30_check_home_dupes',
                    'c30_check_console']
        _SEC_VER = ['c30_check_layout', 'c30_check_filter_fn']
        _SEC_CON = ['c30_check_images']
        _SEC_SEC = ['c30_check_security']
        _SEC_KP = ['c30_check_region', 'c30_check_cis']
        _SEC_YAB = ['c30_check_yabusiness']
        _SEC_ANA = ['c30_check_link_profile', 'c30_check_anomaly', 'c30_check_traffic',
                    'c30_check_review_priority', 'c30_check_anomalies',
                    'c30_check_trust', 'c30_check_calltracking']
        _SEC_ADM = ['c30_check_admin_settings', 'c30_check_admin_crud',
                    'c30_check_admin_product_crud', 'c30_check_admin_tech_crud',
                    'c30_check_admin_counters']
        _SEC_DOP = ['c30_fetch_notifications', 'c30_fetch_metrika_404',
                    'c30_check_arsenkin', 'c30_check_stress', 'c30_autoclick',
                    'c30_use_custom_urls', 'c30_check_uniqueness']

        def _sec_n(_keys):
            return sum(1 for _k in _keys if st.session_state.get(_k, False))
        # «Выбрать/Снять все» - только страничные проверки, без Админки
        # (ручная) и Дополнительно (точечное, по умолчанию выкл).
        _CHK_KEYS = (_SEC_DOST + _SEC_TEH + _SEC_VER + _SEC_CON + _SEC_SEC
                     + _SEC_KP + _SEC_YAB + _SEC_ANA)
        # Подпись кнопки берём из session_state ДО отрисовки галочек: в одном
        # прогоне session_state консистентен (галочки покажут ровно эти значения),
        # поэтому подпись всегда совпадает. Обычную кнопку (не в st.empty) - иначе
        # плейсхолдер «залипал» со старой подписью после клика.
        _all_on = all(st.session_state.get(_k, False) for _k in _CHK_KEYS)

        def _c30_toggle_all():
            # всё включено → снять всё, иначе → выбрать всё
            _target = not all(st.session_state.get(_k, False) for _k in _CHK_KEYS)
            for _k in _CHK_KEYS:
                st.session_state[_k] = _target
        _hc1, _hc2 = st.columns([3, 1])
        with _hc1:
            st.markdown('### Что проверять на страницах')
        with _hc2:
            st.button('Снять все' if _all_on else 'Выбрать все',
                      key='c30_select_all', on_click=_c30_toggle_all,
                      use_container_width=True)
        st.caption('Технические страницы (оплата, доставка, контакты, политики) '
                   'проверяются автоматически при каждом прогоне.')
        # Цветные плашки-разделы: тонкая полоса слева у каждого аккордеона. Тему
        # сайта не меняем (остаётся светлой) - красим только левую границу.
        st.markdown(
            '<style>'
            '.st-key-c30_sec_dost [data-testid="stExpander"]{border-left:5px solid #1098AD !important}'
            '.st-key-c30_sec_teh [data-testid="stExpander"]{border-left:5px solid #3A5BD9 !important}'
            '.st-key-c30_sec_ver [data-testid="stExpander"]{border-left:5px solid #8A6BD0 !important}'
            '.st-key-c30_sec_con [data-testid="stExpander"]{border-left:5px solid #C85C9E !important}'
            '.st-key-c30_sec_sec [data-testid="stExpander"]{border-left:5px solid #D03B3B !important}'
            '.st-key-c30_sec_kp [data-testid="stExpander"]{border-left:5px solid #0F9C8E !important}'
            '.st-key-c30_sec_yab [data-testid="stExpander"]{border-left:5px solid #B5842A !important}'
            '.st-key-c30_sec_ana [data-testid="stExpander"]{border-left:5px solid #1F9D2F !important}'
            '.st-key-c30_sec_adm [data-testid="stExpander"]{border-left:5px solid #5A6B7B !important}'
            '.st-key-c30_sec_dop [data-testid="stExpander"]{border-left:5px solid #C77D2E !important}'
            # Счётчик «выбрано/всего» в шапке раздела - как отдельная «пилюля»
            # справа. В подписи он обёрнут в `код`, поэтому в разметке это <code>:
            # позиционируем его абсолютно по правому краю summary, а тексту слева
            # даём отступ, чтобы он не залезал под пилюлю.
            '[data-testid="stExpander"] summary{position:relative}'
            '[data-testid="stExpander"] summary [data-testid="stMarkdownContainer"] p{padding-right:4.5rem;margin:0}'
            '[data-testid="stExpander"] summary code{'
            'position:absolute;right:1rem;top:50%;transform:translateY(-50%);'
            'font-size:.8em;padding:1px 9px;border-radius:999px;white-space:nowrap;'
            'background:rgba(128,128,128,.16);color:inherit;'
            'font-variant-numeric:tabular-nums;font-family:inherit}'
            '</style>', unsafe_allow_html=True)
        _sec_dost = st.container(key='c30_sec_dost')
        with _sec_dost.expander(f'🌐  Доступность сайта – открываются ли основные типы страниц  `{_sec_n(_SEC_DOST)}/{len(_SEC_DOST)}`', expanded=True):
            st.checkbox('Главная', key='c30_check_main',
                        help='**Открывается ли главная и всё ли на месте.**\n\n'
                             '- код ответа 200\n'
                             '- есть H1, Title, телефон\n'
                             '- основные блоки на месте\n\n'
                             'С главной начинается проверка сайта.')
            st.checkbox('Каталог', key='c30_check_catalog',
                        help='**Открывается ли страница каталога** (список '
                             'разделов/товаров).\n\n'
                             '- есть заголовок\n'
                             '- ссылки на разделы на месте\n'
                             '- товары выводятся')
            st.checkbox('Категории всех уровней вложенности',
                        key='c30_check_categories',
                        help='**Проверяем категории и подкатегории вглубь**, не '
                             'только верхние.\n\n'
                             'У каждой должны быть:\n'
                             '- заголовок\n'
                             '- товары\n'
                             '- текст\n'
                             '- метатеги')
            if stats['has_filters']:
                st.checkbox('Фильтры', key='c30_check_filters',
                            help='**Открываются ли страницы фильтров** (например '
                                 '«швеллер оцинкованный»).\n\n'
                                 '- не отдают 404 и не пустые\n'
                                 '- свой заголовок\n'
                                 '- свои метатеги')
            else:
                st.markdown('<span style="color:#9A958C">Фильтры (нет в каталоге)</span>',
                            unsafe_allow_html=True)
            st.checkbox('Товары', key='c30_check_products',
                        help='**Открываются ли карточки товаров и полные ли они.**\n\n'
                             '- название\n'
                             '- цена\n'
                             '- кнопка заказа\n'
                             '- изображение\n'
                             '- описание')
            st.checkbox(
                'Случайная проверка карт сайта (сэмпл по видам)',
                key='c30_check_sitemap_sample',
                help='**Находит все карты сайта через robots.txt и добавляет '
                     'в проверку случайную выборку страниц из них.**\n\n'
                     '- карты группируются «по виду» по имени файла '
                     '(products-1, products-2 - один вид; category без '
                     'продолжений - свой вид)\n'
                     '- на каждый вид - один случайный файл, из него - '
                     'случайный пул ссылок\n'
                     '- отобранные страницы проходят ВСЕ обычные проверки '
                     'чек-листа (тип определяется по адресу)\n\n'
                     'Список карт, исключения и число ссылок с каждой - '
                     'только на этот запуск, не сохраняются.')
            if st.session_state.get('c30_check_sitemap_sample'):
                with st.container(border=True):
                    _sm_main = next((s for s in src.subdomains if s.city == _mcity), None)
                    _sm_host = _sm_main.host if _sm_main else None
                    if st.button('🔎 Просканировать sitemap', key='c30_sitemap_scan_btn'):
                        if not _sm_host:
                            st.error('Не найден главный домен проекта.')
                        else:
                            with st.spinner(f'Ищу карты сайта на {_sm_host}…'):
                                try:
                                    _leaves = asyncio.run(discover_child_sitemaps(
                                        _sm_host, proxy_url=_c30_effective_proxy))
                                except Exception as e:
                                    _leaves = None
                                    st.error(f'Не удалось просканировать: {e}')
                            if _leaves is not None:
                                if _leaves:
                                    st.session_state['c30_sitemap_groups'] = group_by_kind(_leaves)
                                    st.session_state['c30_sitemap_host'] = _sm_host
                                else:
                                    st.session_state.pop('c30_sitemap_groups', None)
                                    st.warning('Карты не найдены (нет строк Sitemap: в '
                                               'robots.txt или карты не разобрались).')
                    _sm_groups = st.session_state.get('c30_sitemap_groups')
                    if _sm_groups:
                        st.caption(f'Найдено видов: {len(_sm_groups)} '
                                   f'({st.session_state.get("c30_sitemap_host", "")})')
                        st.radio(
                            'Как выбирать карты для проверки',
                            ['Настроить вручную', 'Рандом'],
                            key='c30_sitemap_mode', horizontal=True,
                            help='**Вручную** - сами решаете, какие карты '
                                 'проверять (галочки ниже; по умолчанию по '
                                 'одной случайной карте на каждый вид).\n\n'
                                 '**Рандом** - на каждый вид, найденный на '
                                 'сайте, случайно выбирается одна карта (вид '
                                 'без продолжений - берётся стопроцентно); '
                                 'число карт не фиксировано - оно равно '
                                 'числу видов, а не какому-то заданному '
                                 'значению, при каждом прогоне набор разный.')
                        if st.session_state.c30_sitemap_mode == 'Настроить вручную':
                            st.caption('Снимите галочку у карты, чтобы исключить её.')
                            for _kind, _urls in _sm_groups.items():
                                with st.expander(f'{_kind} ({len(_urls)})', expanded=False):
                                    _kind_hash = hashlib.md5(_kind.encode()).hexdigest()[:8]
                                    _sm_keys = [
                                        'c30_sm_incl_' + hashlib.md5(_u.encode()).hexdigest()[:12]
                                        for _u in _urls
                                    ]
                                    _bc1, _bc2 = st.columns(2)
                                    with _bc1:
                                        if st.button('Выбрать все', key=f'c30_sm_all_{_kind_hash}',
                                                    use_container_width=True):
                                            for _k in _sm_keys:
                                                st.session_state[_k] = True
                                    with _bc2:
                                        if st.button('Снять все', key=f'c30_sm_none_{_kind_hash}',
                                                    use_container_width=True):
                                            for _k in _sm_keys:
                                                st.session_state[_k] = False
                                    for _u, _sm_key in zip(_urls, _sm_keys):
                                        st.session_state.setdefault(_sm_key, True)
                                        st.checkbox(_u, key=_sm_key)
                        else:
                            st.caption(f'Будет случайно выбрано {len(_sm_groups)} '
                                      'карт - по одной на каждый найденный вид.')
                    st.number_input('Ссылок с каждой выбранной карты', 1, 50,
                                    value=5, key='c30_sitemap_urls_per_map')

        _sec_teh = st.container(key='c30_sec_teh')
        with _sec_teh.expander(f'⚙️  Техничка – индексация, дубли, метаданные, 404, скорость  `{_sec_n(_SEC_TEH)}/{len(_SEC_TEH)}`', expanded=True):
            st.checkbox('Текстовые блоки категорий/фильтров/товаров и переменные',
                        key='c30_check_text',
                        help='**Проверяем, что в SEO-текстах категорий/фильтров/'
                             'товаров подставились переменные.**\n\n'
                             '- город, телефон и другие переменные вставлены\n'
                             '- нет «пустых скобок» вроде {city}\n'
                             '- нет незаполненных шаблонов')
            st.checkbox('Индексация страниц (robots.txt, sitemap, noindex, canonical)',
                        key='c30_check_indexing',
                        help='**Сверяем сигналы индексации страницы с эталоном - '
                             'robots.txt.**\n\n'
                             'Страница:\n'
                             '- ошибка: noindex на странице, открытой в robots, или '
                             'canonical на закрытый URL\n'
                             '- закрыта в robots и есть noindex - это норма, не '
                             'показываем\n'
                             '- canonical: ровно один тег, на себя, не на чужой домен, '
                             'полный адрес (не относительный), внутри `<head>`\n'
                             '- hreflang: если есть - валидируем (коды языков, URL, '
                             'self-reference); если нет - не ошибка\n\n'
                             'robots.txt - и правила, и сам файл:\n'
                             '- все пути каталога сверяются с robots.txt\n'
                             '- служебные адреса и мусорные параметры закрыты\n'
                             '- вес до 500 КБ, отдаётся текстом (а не HTML-страницей), '
                             'кодировка UTF-8 без BOM\n'
                             '- директивы не закомментированы через #\n'
                             '- прописан Clean-Param (дубли по GET-параметрам)\n\n'
                             'Карта сайта:\n'
                             '- формат .xml/.txt и кодировка UTF-8\n'
                             '- категории/фильтры/услуги из выгрузки есть в карте\n'
                             '- лимиты файлов, даты lastmod, HTML-карта\n'
                             '- выборочный прозвон адресов из карты: код ответа и '
                             'meta robots noindex\n\n'
                             'Адреса каталога: ЧПУ, кириллица, заглавные, подчёркивания, '
                             'спецсимволы, длина до 2048, домен внутри пути.')
            st.checkbox('Корректность вывода и дубли (заголовки, метаданные, урлы)',
                        key='c30_check_meta',
                        help='**Проверяем корректность и уникальность заголовков, '
                             'метатегов и URL.**\n\n'
                             '- title/description/H1 заполнены и не пустые, город '
                             'поддомена в title/description, длины в норме\n'
                             '- дубли внутри города - баг; полное совпадение между '
                             'городами - не подставился город\n'
                             '- единственность тегов: ровно один title/description/H1, '
                             'ищем дубли H2\n'
                             '- «текстовость» заголовков: h2-h6 не должны быть в '
                             'шапке/подвале/меню/сайдбаре, плюс скачки уровней '
                             '(после h1 сразу h3)\n\n'
                             'Дубли адресов (раздел «Дубли и редиректы»):\n'
                             '- варианты адреса (http, слэш, www, index.php) главной '
                             'и каталога должны 301-редиректить\n'
                             '- тот же слаг без раздела: /catalog/truba/profil/ '
                             'и /profil/ - одна страница\n'
                             '- один товар - один адрес: карточка не должна '
                             'открываться в чужой категории\n'
                             '- индексируемые тестовые поддомены (test./dev./stage.)\n\n'
                             'Дубли сверяются по заголовку страницы, а не по коду '
                             'ответа: 200 сам по себе дублём не делает. Дубль, '
                             'прикрытый canonical, - замечание, а не ошибка.')
            st.checkbox('Микроразметка и OpenGraph (Schema.org, og:*)',
                        key='c30_check_markup',
                        help='**Проверяем микроразметку Schema.org и OpenGraph '
                             '(ТЗ 3.5).**\n\n'
                             '- OpenGraph: все пять тегов (og:url/title/description/'
                             'image/type) на месте И ПРИГОДНЫ - значение не пустое, '
                             'og:url ведёт на эту же страницу полным адресом, '
                             'og:image - ссылка на файл (не data:), '
                             'og:description до 300 символов, og:type из известных\n'
                             '- Schema.org: данные компании везде, крошки '
                             'BreadcrumbList, листинги, на товаре Product + '
                             'характеристики + фото + цены\n'
                             '- на товаре - блок отзывов: есть ли он, размечен '
                             'ли (Review + AggregateRating) и правдоподобен ли '
                             '(одна дата у всех, один автор, одинаковые оценки, '
                             'пустые тексты = разовая генерация, за такое дают '
                             'ручные санкции)\n'
                             '- условно: видео → VideoObject, FAQ-блок → FAQPage, '
                             'адрес → PostalAddress\n'
                             '- основной формат - microdata; тип только в JSON-LD = '
                             'предупреждение\n\n'
                             'Валидность полей - инструментами Яндекса/Google вручную.')
            st.checkbox('Валидация W3C (HTML/CSS) + скорость ресурсов',
                        key='c30_check_w3c',
                        help='**Валидируем HTML/CSS и замеряем скорость загрузки '
                             'ресурсов по выборке страниц.**\n\n'
                             '- главная/категория/товар\n'
                             '- HTML через W3C Nu\n'
                             '- CSS через W3C CSS Validator\n'
                             '- время загрузки ресурсов (HTML/CSS/JS/шрифты/картинки)\n\n'
                             'Отдельный лист «Валидация и скорость».\n\n'
                             '⚠ W3C - бесплатные сервисы с лимитом запросов. При '
                             'частом прогоне возможен временный блок (HTTP 403); '
                             'тогда валидность не проверится (в отчёте «повторить '
                             'позже»), а скорость ресурсов измерится в любом '
                             'случае.')
            st.checkbox('Сжатие (Gzip/Brotli) и кеширование статики',
                        key='c30_check_static',
                        help='**Проверяем, что CSS/JS сайта отдаются сжатыми и с '
                             'кешем.**\n\n'
                             '- сжатие: Gzip/Brotli\n'
                             '- кеш: Cache-Control/ETag/Expires\n'
                             '- без этого страница грузится тяжелее и каждый раз '
                             'заново\n\n'
                             'Как проверить руками: F12 → вкладка Network → обновить '
                             'страницу → кликнуть по .css или .js → в Response Headers '
                             'смотреть Content-Encoding (сжатие) и Cache-Control '
                             '(кеш). Проще - прогнать через PageSpeed Insights.')
            st.checkbox('Страница 404 (код ответа, дизайн, тексты, навигация)',
                        key='c30_check_404',
                        help='**Запрашиваем заведомо несуществующий адрес и '
                             'проверяем страницу 404.**\n\n'
                             '- код ответа ровно 404 (200 = soft-404 шаблон, баг)\n'
                             '- дизайн совпадает с главной (общие CSS/шапка/подвал)\n'
                             '- уникальный title и description\n'
                             '- есть ссылки на разделы и форма заявки/телефон\n'
                             '- несуществующие пагинация (?PAGEN_1=999999) и фильтр '
                             'тоже отдают 404\n\n'
                             'Главный домен + один поддомен (шаблон сквозной). '
                             'Отдельный лист «Страница 404».')
            st.checkbox('Фильтры поисковых систем (санкции)',
                        key='c30_check_ps_filters',
                        help='**Проверяем, не попал ли сайт под фильтры/санкции '
                             'поисковых систем.**\n\n'
                             '- Яндекс: санкционные сигналы (угрозы, качество, '
                             'реклама) из диагностики Вебмастера - надёжный '
                             'официальный источник\n'
                             '- Google: API ручных мер нет - сканируем почтовые '
                             'уведомления GSC за 90 дней по маркерам («ручные '
                             'меры», «security issue») + ссылка на ручную сверку '
                             'в Search Console\n\n'
                             'Отдельный лист «Фильтры ПС».')
            st.checkbox('Проверять ссылки на страницах (404, редиректы, ссылки на себя)',
                        key='c30_check_links',
                        help='**Прозваниваем ссылки всех страниц прогона.**\n\n'
                             'Что ищем:\n'
                             '- битые: 404/410\n'
                             '- закрытые: 403 (предупреждение - бывает и от '
                             'защиты от ботов)\n'
                             '- ссылки на редирект: надо ставить сразу конечный '
                             'адрес\n'
                             '- редирект, который приводит к ошибке (301 → 404)\n'
                             '- ссылки страницы на саму себя (на главной не '
                             'считаем: там это логотип)\n\n'
                             'Охват:\n'
                             '- вся страница целиком: текст, блоки, шапка, подвал, '
                             'листинг\n'
                             '- внешние ссылки - тоже, но мягче и не больше 20 на '
                             'прогон: у чужих сайтов 403/429 обычно отдаёт антибот\n'
                             '- уникальные ссылки дедупятся по прогону (сквозное '
                             'меню звоним один раз)\n'
                             '- общий лимит - 2500 прозвонов\n\n'
                             'Находки - на листе «Проблемы», раздел «Ссылки». '
                             'Дольше обычного: по запросу на каждую новую ссылку.')
            st.checkbox('Проверять 404 среди страниц в индексе (Яндекс.Вебмастер + Google)',
                        key='c30_check_index_404',
                        help='**Ищем страницы, которые есть в индексе, но отдают '
                             '404/410/5xx.**\n\n'
                             '- Яндекс.Вебмастер - браузер качает выгрузку «Страницы '
                             'в поиске» (код ответа уже в ней); нужна сохранённая '
                             'сессия Яндекса («Автокликеры» → «Экспорт сессии для '
                             'облака»)\n'
                             '- Google - через Search Console API на сервисном '
                             'аккаунте: берём проиндексированные страницы и '
                             'прозваниваем на 404; работает на облаке, без браузера; '
                             'нужен секрет gsc_service_account_<проект>\n\n'
                             'Источники объединяются в один лист «404 в индексе».')
            st.checkbox('Проверка дублей главной страницы',
                        key='c30_check_home_dupes',
                        help='**Проверяем, не открывается ли главная по разным '
                             'адресам с кодом 200.**\n\n'
                             '- варианты: с www и без, http/https, со слэшем и без, '
                             '/index.php, /index.html, двойной слэш, ?параметр\n'
                             '- редирект на главную или canonical → главная = '
                             'склеено (ок)\n'
                             '- код 200 без редиректа/canonical = дубль\n\n'
                             'Как coolakov.ru и be1.ru/dubli-stranic, но точнее: '
                             'смотрим редирект и тег canonical. Быстро, без '
                             'браузера. Отдельный лист «Дубли главной».')
            st.checkbox('Ошибки JavaScript в консоли + адаптивность (браузер)',
                        key='c30_check_console',
                        help='**Открывает в браузере каждую страницу прогона и '
                             'ловит ошибки JS в консоли.**\n\n'
                             '- главная, каталог, категории, фильтры, товары, тех. '
                             'страницы\n'
                             '- ловим console.error и исключения; шум аналитики/'
                             'виджетов отсеивается\n'
                             '- той же поездкой - адаптивность на ширинах '
                             '1440/768/390: нет горизонтального скролла, блоки не '
                             'накладываются при ресайзе (масштаб Ctrl+/- покрыт той '
                             'же сеткой), на мобильном шрифт минимум 14px\n\n'
                             'Тяжёлый браузерный проход - по запросу.')

        _sec_ver = st.container(key='c30_sec_ver')
        with _sec_ver.expander(f'🎨  Вёрстка – адаптивность, навигация, фильтрация  `{_sec_n(_SEC_VER)}/{len(_SEC_VER)}`', expanded=True):
            st.checkbox('Вёрстка, адаптивность и навигация (viewport, стили, меню)',
                        key='c30_check_layout',
                        help='**Проверяем адаптивность, вёрстку и навигацию сайта.**\n\n'
                             '- ТЗ 2.1/2.1.1: задан тег viewport (мобильная версия '
                             'масштабируется)\n'
                             '- каждый подключённый CSS-файл реально грузится '
                             '(битый стиль = страница без вёрстки)\n'
                             '- в стилях есть @media-запросы (признак адаптивности)\n'
                             '- ТЗ 2.2/2.3: ссылки меню шапки (тех. страницы и '
                             'каталог) прозваниваются с главной каждого поддомена - '
                             '404 = баг\n'
                             '- favicon установлен и грузится (с главной поддомена)\n'
                             '- семантическая разметка (<header>/<footer>/<main>)\n'
                             '- инлайн-стили (много style="…" = предупреждение)\n'
                             '- единый протокол: http-ресурсы на https-странице '
                             '(mixed content = баг), внутренние http-ссылки '
                             '(предупреждение)\n'
                             '- вынос CSS/JS во внешние файлы (большие inline-блоки) '
                             'и async/defer у скриптов в <head>\n\n'
                             'Визуальный рендер не заменяет - ручной просмотр остаётся.')
            st.checkbox('Проверять фильтрацию товаров (браузер)',
                        key='c30_check_filter_fn',
                        help='**Открывает категорию в браузере и применяет фильтр, '
                             'проверяя, что выдача сужается.**\n\n'
                             '- выдача не пустая\n'
                             '- результат не дублирует исходную категорию\n'
                             '- без ошибок\n\n'
                             'Тяжёлый браузерный тест - по запросу. Селекторы '
                             'задаются на проект в catalogs/filters-<проект>.json.')

        _sec_con = st.container(key='c30_sec_con')
        with _sec_con.expander(f'🖼️  Контент – изображения  `{_sec_n(_SEC_CON)}/{len(_SEC_CON)}`', expanded=True):
            st.checkbox('Изображения (alt, webp/avif, вес, имена файлов)',
                        key='c30_check_images',
                        help='**Проверяем изображения: alt, форматы, вес и имена '
                             'файлов.**\n\n'
                             '- alt у всех <img> (баг - и пустой alt="", и полное '
                             'отсутствие атрибута)\n'
                             '- современные форматы webp/avif (иначе предупреждение)\n'
                             '- вес своих картинок по Content-Length: порог 150 КБ '
                             '(замечание), тяжелее 300 КБ = не оптимизировано\n'
                             '- имена файлов - транслит из alt (хеши CMS - одно '
                             'предупреждение)\n'
                             '- lazy loading\n\n'
                             'Отдельный лист «Изображения».')

        _sec_sec = st.container(key='c30_sec_sec')
        with _sec_sec.expander(f'🔒  Безопасность – заголовки ответа сервера  `{_sec_n(_SEC_SEC)}/{len(_SEC_SEC)}`', expanded=True):
            st.checkbox('Заголовки безопасности и SSL-сертификат',
                        key='c30_check_security',
                        help='**Заголовки ответа сервера + сам сертификат.**\n\n'
                             'Заголовки:\n'
                             '- нет HSTS / CSP / X-Content-Type-Options / защиты от '
                             'кликджекинга = предупреждение (мягко)\n'
                             '- битое значение (HSTS max-age=0, ALLOW-FROM, '
                             'не-nosniff, конфликт дублей) = баг\n\n'
                             'SSL-сертификат (один запрос на хост, без внешних '
                             'сервисов - читаем прямо из TLS-соединения):\n'
                             '- истёк или осталось меньше 14 дней = баг; меньше '
                             '30 дней = предупреждение\n'
                             '- выписан на этот домен (с учётом *.домен)\n'
                             '- сервер отдаёт полную цепочку (неполная часто '
                             'работает на десктопе, но не на мобильных)\n'
                             '- не дозвонились - пишем «не проверить», дефектом '
                             'сайта это не считаем')

        _sec_kp = st.container(key='c30_sec_kp')
        with _sec_kp.expander(f'🗺️  Данные по городам  `{_sec_n(_SEC_KP)}/{len(_SEC_KP)}`', expanded=True):
            st.checkbox('Переменные города (город, телефон, почта - по КП)',
                        key='c30_check_region',
                        help='**На странице города не должно быть подстановок '
                             'другого города.**\n\n'
                             '- чужой город в title/description/H1\n'
                             '- телефон или почта другого города\n\n'
                             'Сверка со справочником КП.')
            st.checkbox('СНГ-домены: нет упоминаний РФ, СНГ и чужих стран',
                        key='c30_check_cis',
                        help='**На сайтах стран СНГ не должно быть упоминаний РФ, '
                             'СНГ и чужих стран.**\n\n'
                             '- домены .kz/.by/.uz/… - проверяем тексты, заголовки, '
                             'метаданные и контакты\n'
                             '- не должно быть: «РФ», «Россия», аббревиатуры «СНГ», '
                             'названий других стран - только своя страна\n\n'
                             'Для доменов РФ не выполняется. Как проверить руками: '
                             'откройте сайт нужной страны и пройдитесь по главной, '
                             'каталогу, контактам и подвалу - если встречается '
                             '«Россия»/«РФ»/«СНГ» или чужая страна, это ошибка.')

        _sec_yab = st.container(key='c30_sec_yab')
        with _sec_yab.expander(f'🏢  Я.Бизнес/GMB – карточки поддоменов под свой регион  `{_sec_n(_SEC_YAB)}/{len(_SEC_YAB)}`', expanded=True):
            st.checkbox('Я.Бизнес: каждый поддомен под свой регион',
                        key='c30_check_yabusiness',
                        help='**Проверяем, что у каждого поддомена есть карточка '
                             'организации под его город.**\n\n'
                             '- тянем организации аккаунта из кабинета '
                             'Яндекс.Справочника (город/регион каждой карточки)\n'
                             '- сверяем с городами поддоменов\n'
                             '- показываем поддомены без организации\n\n'
                             'Работает на сессии Яндекса (та же, что '
                             '404-в-индексе/автокликеры: «Автокликеры» → «Экспорт '
                             'сессии для облака»). Отдельный лист «Я.Бизнес и GMB». '
                             'Партнёрский API Справочника - когда дадут доступ, '
                             'перейдём на него.')

        _sec_ana = st.container(key='c30_sec_ana')
        with _sec_ana.expander(f'📊  Аналитика – ссылочный профиль, аномалии, трафик, траст  `{_sec_n(_SEC_ANA)}/{len(_SEC_ANA)}`', expanded=True):
            st.checkbox('Ссылочный профиль (беклинки, lite)',
                        key='c30_check_link_profile',
                        help='**Проверяем ссылочный профиль по официальным данным '
                             'Яндекс.Вебмастера.**\n\n'
                             '- объём: всего внешних ссылок и доноров\n'
                             '- динамика: резкий обвал = потеря ссылок, всплеск = '
                             'возможный спам/накрутка\n'
                             '- подозрительные доноры (мусорные зоны, gambling/adult)\n\n'
                             'Тот же OAuth-токен, что и диагностика Вебмастера. '
                             'Глубокий аудит (Ahrefs/Majestic) платный - здесь его '
                             'нет. У Google API беклинков нет - ссылка на ручную '
                             'сверку в GSC. Нужен настроенный токен Вебмастера '
                             '(webmaster_oauth). Отдельный лист «Ссылочный профиль».')
            st.checkbox('Нет аномалий: Вебмастер + внезапные мусорные ссылки',
                        key='c30_check_anomaly',
                        help='**Мониторим резкие отклонения «от себя-прошлого» по '
                             'данным Вебмастера.**\n\n'
                             '- обход: всплеск 4xx/5xx, просадка доступных страниц '
                             '(историю хранит Яндекс)\n'
                             '- проблемы сайта: фатальные/критические\n'
                             '- страницы в поиске и ИКС: падение от эталона прошлых '
                             'прогонов\n'
                             '- внезапные мусорные доноры и скачки ссылочной массы\n\n'
                             'Вебмастер часто сигналит раньше, чем просядут позиции '
                             'и трафик. Результат - секция «Аномалии» в самом низу '
                             'листа «Аналитика». Нужен токен Вебмастера '
                             '(webmaster_oauth), тот же, что у 1.20.')
            st.checkbox('Сравнение трафика (день/месяц/год) в Метрике',
                        key='c30_check_traffic',
                        help='**Сравниваем трафик (визиты и посетители) по всем '
                             'счётчикам проекта из Яндекс.Метрики.**\n\n'
                             '- день: сегодня vs вчера\n'
                             '- месяц: с 1-го числа до сегодня vs тот же отрезок '
                             'прошлого месяца\n'
                             '- год: с 1 января vs прошлый год до той же даты\n\n'
                             'Отдельный лист «Динамика трафика» (группа «Аналитика»). '
                             'Нужен токен metrika_oauth_<проект>.')
            st.checkbox('Отзывы: приоритет докупки (Я.Бизнес + 2ГИС)',
                        key='c30_check_review_priority',
                        help='**Строит приоритет докупки отзывов по данным '
                             'Я.Бизнеса и 2ГИС.**\n\n'
                             '- по конфигу catalogs/reviews-<проект>.csv (city, '
                             'yandex_url, 2gis_url) тянет живьём рейтинг и число '
                             'отзывов каждого филиала\n'
                             '- приоритет: сначала филиалы с рейтингом < 4.7, затем '
                             'города от миллионников к меньшим\n'
                             '- докупаем по 2 отзыва (3 при низком рейтинге)\n\n'
                             'Таблица внизу листа «Я.Бизнес и GMB», включайте вместе '
                             'с проверкой Я.Бизнеса. Пока настроен только smu (для '
                             'mpe/imp нужен свой CSV).')
            st.checkbox('Аномалии: мусорные запросы (ГСК) + переходы с мусорных сайтов (Метрика)',
                        key='c30_check_anomalies',
                        help='**Ищем признаки негативного SEO - мусорные запросы и '
                             'переходы.**\n\n'
                             '- ГСК (Search Analytics по сервисному аккаунту): '
                             'всплеск показов по мусорным/иноязычным запросам за 14 '
                             'дней vs предыдущие 14\n'
                             '- Метрика: переходы с доменов-рефереров, похожих на '
                             'спам (мусорные TLD / gambling / adult), за 30 дней vs '
                             'предыдущие 30, + всплеск\n\n'
                             'Отдельный лист «Аномалии». Мусорные доноры из раздела '
                             'Links в GSC API не отдаёт - на листе ссылка на ручную '
                             'сверку. Нужны metrika_oauth_<проект> и '
                             'gsc_service_account_<проект>.')
            st.checkbox('Показатели и траст проекта (ИКС + DR)',
                        key='c30_check_trust',
                        help='**Проверяем траст проекта: ИКС и DR.**\n\n'
                             '- Яндекс ИКС (индекс качества сайта) по '
                             'верифицированным хостам через Вебмастер API\n'
                             '- DR (Domain Rating-подобный ранг 0-100) через Open '
                             'PageRank (бесплатный ключ openpagerank_key)\n\n'
                             'Бесплатно. Платные CheckTrust/Ahrefs/Semrush не '
                             'подключены (Ahrefs free-чекер за капчей). Отдельный '
                             'лист «Траст проекта». Нужен webmaster_oauth_<проект>; '
                             'для DR - секрет openpagerank_key.')
            st.checkbox('Замена рекламного номера работает (браузер)',
                        key='c30_check_calltracking',
                        help='**Проверяем, что рекламный номер в шапке подменяется '
                             '(коллтрекинг).**\n\n'
                             '- открываем главную каждого города прогона в браузере '
                             'с рекламной меткой (?utm_source=yandex - '
                             'Яндекс.Директ)\n'
                             '- проверяем, подменяется ли номер в шапке на '
                             'рекламный из КП (phone_ad)\n'
                             '- end-to-end проверка: JS реально выполняется\n\n'
                             'Результат - в секции «Замена рекл. номера» в конце '
                             'листа «Аналитика» (колонка «Подмена (браузер)»). '
                             'Статическая сверка конфига с КП идёт там же и без '
                             'браузера (в каждом прогоне). Тяжёлый браузерный '
                             'проход - по запросу.')

        # Админка - отдельный аккордеон (нужны креды); свёрнута, галочки вручную
        _sec_adm = st.container(key='c30_sec_adm')
        with _sec_adm.expander(f'🛠️  Админка – настройки, CRUD, счётчики (нужны креды)  `{_sec_n(_SEC_ADM)}/{len(_SEC_ADM)}`',
                               expanded=True):
            _amc1, _amc2 = st.columns([3, 2])
            with _amc1:
                _ck_adm = st.checkbox(
                    'Работают функции настройки (поддомены/категории/товары/тех.страницы)',
                    key='c30_check_admin_settings',
                    help='**Браузер заходит в админку Bitrix и проверяет, что '
                         'разделы настройки открываются и работают.**\n\n'
                         '- мастер поддоменов\n'
                         '- разделы каталога\n'
                         '- товарная подсистема (HL-блок «Ассортимент»)\n'
                         '- структура сайта\n'
                         '- редактор файлов тех.страниц\n\n'
                         'Только открытие/рендер, ничего не меняется. Отдельный '
                         'лист «Настройки в админке».')
            with _amc2:
                st.checkbox('↳ писать в БД (с откатом)',
                            key='c30_adm_execute',
                            help='**Подпункт CRUD-проверок ниже - писать реальные '
                                 'изменения в БД или только проверить наличие '
                                 'функций.**\n\n'
                                 '- ВКЛ (рекомендуется): реально '
                                 'создаёт-правит-скрывает-удаляет временный скрытый '
                                 'раздел «[ТЕСТ ЧЕКЕРА]» (без товаров, удаляется '
                                 'сразу) и прогоняет симуляцию поддомена - аудит '
                                 '«было → стало»\n'
                                 '- ВЫКЛ: только наличие CRUD-функций '
                                 '(формы/кнопки), ничего не пишется')
            _ck_crud = st.checkbox(
                'Создание, массовая загрузка, правка, удаление, скрытие – поддомены и категории',
                key='c30_check_admin_crud',
                help='**Проверяем CRUD-функции для поддоменов и категорий.**\n\n'
                     '- поддомены: создание (симуляция-dry-run, на сайте ничего '
                     'не создаётся), массовая загрузка (CSV), правка/удаление/'
                     'скрытие - наличие функции\n'
                     '- категории: полный CRUD на временном разделе «[ТЕСТ '
                     'ЧЕКЕРА]» (создать скрытым → правка → скрытие → удаление, '
                     'чистится в конце) + массовая загрузка (импорт)\n\n'
                     'Пишет в БД только при включённом подпункте «писать в БД». '
                     'В отчёт идёт аудит «было → стало».')
            _ck_pcrud = st.checkbox(
                'CRUD + сортировка + вывод в разные категории – товары (опционально по CMS)',
                key='c30_check_admin_product_crud',
                help='**Проверяем CRUD товаров на временном скрытом товаре '
                     '«[ТЕСТ ЧЕКЕРА]».**\n\n'
                     '- создание\n'
                     '- сортировка (поле SORT)\n'
                     '- вывод в разные категории (привязка к 2+ разделам)\n'
                     '- правка\n'
                     '- удаление\n\n'
                     'Товар без реальной цены, удаляется в конце. Пишет в БД '
                     'только при включённом подпункте «писать в БД». Если товары '
                     'в вашей CMS не элементы каталога - пункт помечается '
                     '«неприменимо». Аудит «было → стало».')
            _ck_tcrud = st.checkbox(
                'Создание, массовая загрузка, правка, удаление, скрытие – технические страницы',
                key='c30_check_admin_tech_crud',
                help='**Проверяем CRUD техстраниц (файлы в «Структуре '
                     'сайта»).**\n\n'
                     '- форма нового файла (имя/заголовок/сохранить)\n'
                     '- загрузчик файлов\n'
                     '- редактор существующего файла\n'
                     '- управление в структуре (удаление/скрытие)\n\n'
                     'Проверяется только наличие функций - файлы реально НЕ '
                     'создаём и не удаляем: техстраница - публичный файл в корне '
                     'сайта, на боевом проекте это небезопасно (в отличие от '
                     'скрытых записей БД у категорий/товаров). Подпункт «писать '
                     'в БД» тут не применяется.')
            _ck_cnt = st.checkbox(
                'Добавление счётчиков аналитики',
                key='c30_check_admin_counters',
                help='**Проверяем, что в админке есть где добавлять/править '
                     'счётчики аналитики.**\n\n'
                     '- счётчики: Метрика/GA/GTM/Mail.ru\n'
                     '- для СМУ - файл «Структуры сайта» '
                     '/localviews/layout/counters.php (открываем в редакторе '
                     'fileman, показываем какие счётчики в нём)\n'
                     '- для других CMS с самописным модулем - путь настраивается '
                     '(секрет admin_settings → counters)\n\n'
                     'Ничего не пишем.')

            if _ck_adm or _ck_crud or _ck_pcrud or _ck_tcrud or _ck_cnt:
                # Дефолты полей: секрет admin_settings_<pid> (JSON) →
                # локальный admin.local.json → admin.test.local.json.
                if not st.session_state.get('c30_adm_prefilled'):
                    _pre = {}
                    try:
                        _raw = _secret_pid('admin_settings', pid)
                        if _raw:
                            _pre = json.loads(_raw) if isinstance(_raw, str) \
                                else dict(_raw)
                    except Exception:
                        _pre = {}
                    if not _pre:
                        try:
                            from admin_settings_check import load_admin_creds
                            _pdir = PROJECT_ROOT / 'forms_tester' / 'projects' / pid
                            _pre = (load_admin_creds(_pdir)
                                    or load_admin_creds(_pdir, test=True) or {})
                        except Exception:
                            _pre = {}
                    for _f, _k in (('domain', 'c30_adm_domain'),
                                   ('login', 'c30_adm_login'),
                                   ('password', 'c30_adm_password'),
                                   ('basic_login', 'c30_adm_basic_login'),
                                   ('basic_password', 'c30_adm_basic_password')):
                        if _pre.get(_f) and not st.session_state.get(_k):
                            st.session_state[_k] = _pre[_f]
                    st.session_state['c30_adm_prefilled'] = True
                st.text_input('Домен админки (https://…)', key='c30_adm_domain',
                              placeholder='https://test.stalmetural.ru')
                _adm1, _adm2 = st.columns(2)
                with _adm1:
                    st.text_input('Логин Bitrix', key='c30_adm_login')
                with _adm2:
                    st.text_input('Пароль Bitrix', key='c30_adm_password',
                                  type='password')
                _adm3, _adm4 = st.columns(2)
                with _adm3:
                    st.text_input('Basic-логин (если есть заглушка)',
                                  key='c30_adm_basic_login')
                with _adm4:
                    st.text_input('Basic-пароль', key='c30_adm_basic_password',
                                  type='password')
                if _ck_crud or _ck_pcrud:
                    st.caption('CRUD категорий/товаров выполняется на временных '
                               'СКРЫТЫХ разделе/товаре и откатывается; поддомены '
                               'реально не создаются (симуляция) и не удаляются '
                               '(наличие функции). Обкатано на тестовом контуре.')

        # Дополнительно - тяжёлые/точечные проверки; свёрнуто, по умолчанию выкл
        _sec_dop = st.container(key='c30_sec_dop')
        with _sec_dop.expander(f'➕  Дополнительно – уведомления, 404 из Метрики, Арсенкин, нагрузка, свои URL, уникальность  `{_sec_n(_SEC_DOP)}/{len(_SEC_DOP)}`', expanded=True):
            _ck_notif = st.checkbox(
                'Собрать уведомления (Вебмастер, GSC, Я.Бизнес, 2ГИС, Google)',
                key='c30_fetch_notifications',
                help='**Собирает свежие уведомления из панелей вебмастера и '
                     'сервисов.**\n\n'
                     '- Яндекс.Вебмастер\n'
                     '- Google Search Console\n'
                     '- Яндекс.Бизнес\n'
                     '- 2ГИС\n'
                     '- Google\n\n'
                     'Чтобы не заходить в каждую панель вручную. Нужны '
                     'настроенные доступы/токены; без них пункт пропускается.')
            if _ck_notif:
                # Без st.columns (пустая вторая колонка оставляла «призрачный»
                # селектор при выключенном чекбоксе - как было у 404).
                st.selectbox('За какой период',
                             [1, 3, 7, 14, 30],
                             format_func=lambda x: ('1 день' if x == 1 else f'{x} дней'),
                             key='c30_notify_days', label_visibility='collapsed')
            _ck_m404 = st.checkbox(
                'Собрать 404 из Метрики',
                key='c30_fetch_metrika_404',
                help='**Берёт 404-страницы напрямую из Метрики (API, по всем '
                     'счётчикам проекта) за выбранный период.**\n\n'
                     'За день трафик на 404 мал - обычно нужен период 7+ дней.')
            if _ck_m404:
                from datetime import date as _date, timedelta as _td
                # Селектор без обёртки в колонки (пустые колонки оставляли
                # «призрачные» виджеты при выключении чекбокса). Поля даты -
                # сразу под селектором, появляются в том же ране.
                _m404_mode = st.selectbox(
                    'Период 404',
                    ['За день', 'За неделю', 'За 14 дней', 'За 30 дней', 'За период'],
                    key='c30_m404_mode', label_visibility='collapsed')
                if _m404_mode == 'За день':
                    st.date_input('Дата', value=_date.today() - _td(days=1),
                                  key='c30_m404_day', format='DD.MM.YYYY',
                                  label_visibility='collapsed')
                elif _m404_mode == 'За период':
                    _pm2, _pm3 = st.columns(2)
                    with _pm2:
                        st.date_input('С', value=_date.today() - _td(days=7),
                                      key='c30_m404_from', format='DD.MM.YYYY')
                    with _pm3:
                        st.date_input('По', value=_date.today(),
                                      key='c30_m404_to', format='DD.MM.YYYY')
            st.checkbox('Проверка индексации URL через Арсенкин (Яндекс + Google)',
                        key='c30_check_arsenkin',
                        help='**Массово проверяет через API Арсенкина, есть ли '
                             'страницы в индексе Яндекса и Google.**\n\n'
                             '- категории, теги, подфильтры поддоменов и домена\n'
                             '- без браузера, без блокировок\n\n'
                             'Токен и список URL вводятся в блоке ниже. Отдельный '
                             'лист «Индексация (Арсенкин)».')
            if st.session_state.get('c30_check_arsenkin'):
                with st.container(border=True):
                    st.markdown('**Арсенкин: токен и список URL**')
                    st.text_input(
                        'API-токен Арсенкина', key='c30_arsenkin_token',
                        type='password',
                        placeholder='вставь токен (профиль Арсенкина → API)',
                        help='Токен берётся ТОЛЬКО из этого поля (не из Secrets). '
                             'Один аккаунт СМУ подходит для СМУ / ИМП / МПЭ. '
                             'Обязателен, если галочка включена.')
                    st.text_area(
                        'URL-адреса (по одному в строке, до 5000)',
                        key='c30_arsenkin_urls', height=160,
                        placeholder='https://site.ru/catalog/…\nhttps://city.site.ru/tag/…',
                        help='Категории, отслеживаемые теги/подфильтры нужного '
                             'поддомена и домена. Какие именно – уточняй у SEO-'
                             'специалиста проекта.')
                    _ac1, _ac2, _ac3, _ac4 = st.columns(4)
                    with _ac1:
                        st.checkbox('Яндекс', value=True, key='c30_arsenkin_yandex')
                    with _ac2:
                        st.checkbox('Google', value=True, key='c30_arsenkin_google')
                    with _ac3:
                        st.checkbox('http/https/www как один', value=True,
                                    key='c30_arsenkin_search_all')
                    with _ac4:
                        st.checkbox('inurl-перепроверка', value=False,
                                    key='c30_arsenkin_inurl',
                                    help='**Для Google: перепроверяет оператором '
                                         'inurl: страницы, которых нет в индексе, '
                                         'чтобы меньше ложных «не в индексе».**')
                    st.caption('1 URL × 1 поисковая система = 2 лимита Арсенкина. '
                               'Проверка идёт в конце прогона, результат – в отчёте.')
            _ck_stress = st.checkbox(
                'Ошибки сервера: парсинг, нагрузка, дубли URL (по запросу)',
                key='c30_check_stress',
                help='**В конце прогона гоняет три сетевые пробы на прод и ищет '
                     'ошибки сервера, обрывы и деградацию скорости.**\n\n'
                     '- быстрый обход страниц парсингом\n'
                     '- параллельный залп по репрезентативным страницам\n'
                     '- кривые дубли адресов категорий/фильтров/товаров '
                     '(сдвоенный сегмент, двойной слэш, глубокая пагинация)\n\n'
                     'При первых 5xx/обрывах проба сама останавливается (не '
                     'добивает сервер); поймали бан на парсинге - нагрузку и '
                     'дубли пропускаем. Создаёт реальную нагрузку на боевой '
                     'сайт - по запросу. Результат: лист «Нагрузка и парсинг».')
            if _ck_stress:
                _sc1, _sc2 = st.columns(2)
                with _sc1:
                    st.slider('Параллельных запросов в залпе',
                              min_value=10, max_value=50, value=30, step=5,
                              key='c30_stress_concurrency',
                              help='Сколько запросов летят к ОДНОЙ странице '
                                   'одновременно. 15 - лёгкий безопасный всплеск; '
                                   '30 (по умолчанию) - заметный, показательный; '
                                   'выше - ближе к тому, что защита примет за '
                                   'атаку (риск бана). Каждая страница получает '
                                   'этот залп в 2 волны (итого запросов на '
                                   'страницу = число × 2).')
                with _sc2:
                    st.slider('Сколько страниц под залпом',
                              min_value=1, max_value=8, value=3, step=1,
                              key='c30_stress_load_pages',
                              help='На сколько разных страниц по очереди пойдёт '
                                   'залп нагрузки. Сначала берутся разнотипные '
                                   '(главная/категория/фильтр/товар), потом '
                                   'добираются остальные из прогона. Больше '
                                   'страниц - шире картина, но выше суммарная '
                                   'нагрузка на сайт.')

            # ── Автокликер (локальный Chrome или облако с сессией) ──────
            _ck_ac = st.checkbox(
                'Запустить автокликер после проверки',
                key='c30_autoclick',
                help='**Перекликивает «Проверить» по ошибкам в Вебмастере/ГСК.**\n\n'
                     '- локально - через залогиненный Chrome (CDP 9222)\n'
                     '- в облаке - headless-браузер с сессией из Secrets '
                     '(autoclick_session, экспортируется на вкладке '
                     '«Автокликеры»)\n\n'
                     'Чек-лист завершится только когда все ошибки прокликаны.')
            if _ck_ac:
                _ac1, _ac2 = st.columns(2)
                with _ac1:
                    st.checkbox('Прокликать Вебмастер', key='c30_ac_wm')
                with _ac2:
                    st.checkbox('Прокликать ГСК', key='c30_ac_gsc')
                if st.button('🌐 Открыть браузер для входа', key='c30_ac_browser',
                             use_container_width=True):
                    try:
                        subprocess.Popen([sys.executable, 'open_browser.py'],
                                         cwd=str(PROJECT_ROOT))
                        st.info('Открываю Chrome. Войди в Google (GSC) и Yandex '
                                '(Вебмастер) под аккаунтами проекта, окно не закрывай, '
                                'затем запускай проверку.')
                    except Exception as _e:
                        st.error(f'Не удалось открыть браузер: {_e}')
                if _secret('autoclick_session'):
                    st.caption('Локальный Chrome (9222) в приоритете; без него - '
                               'облачный режим (сессия autoclick_session найдена в '
                               'Secrets ✓).')
                else:
                    st.caption('Нужен залогиненный Chrome (CDP 9222) или сессия в '
                               'Secrets (autoclick_session - экспорт на вкладке '
                               '«Автокликеры»). Без них автокликер пропускается '
                               'с пометкой в отчёте.')
            st.checkbox('Добавить свой список URL', key='c30_use_custom_urls',
                        help='**Кроме обычной выборки проекта проверит и ваши '
                             'URL.**\n\n'
                             'Вставьте список ниже (по одному в строке). Удобно '
                             'точечно перепроверить конкретные страницы.')
            if st.session_state.c30_use_custom_urls:
                st.caption('Ссылки - по одной на строку. Тип по адресу: /catalog/x/ - '
                           'категория, /catalog/x/y/ - товар, …/filter/… - фильтр, / - главная.')
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
                    st.success(f'Будет добавлено {len(_typed)} URL - {_bt}')
            # ── Уникальность контента через text.ru (самый нижний пункт) ──
            st.checkbox(
                'Уникальность контента через text.ru (с каким сайтом пересечение)',
                key='c30_check_uniqueness',
                help='**Проверяет через text.ru, насколько уникален SEO-текст '
                     'страниц и с какими чужими сайтами он пересекается.**\n\n'
                     '- выборка главного домена: главная + каталог + N категорий '
                     '+ N товаров\n'
                     '- города-поддомены не трогаем (дубли)\n'
                     '- свои домены исключаются из сравнения\n\n'
                     'Ключ и объём - в блоке ниже. Каждая страница тратит символы '
                     'аккаунта text.ru. Отдельный лист «Уникальность».')
            if st.session_state.get('c30_check_uniqueness'):
                with st.container(border=True):
                    st.markdown('**text.ru: ключ и объём проверки**')
                    st.text_input(
                        'API-ключ text.ru', key='c30_textru_key', type='password',
                        placeholder='вставь ключ (личный кабинет text.ru → раздел «API»)',
                        help='Обязателен, если галочка включена. Берётся из этого поля '
                             'или из секрета textru_key. В git/отчёты не попадает.')
                    _uq1, _uq2, _uq3 = st.columns(3)
                    with _uq1:
                        st.number_input('Категорий', 0, 30, key='c30_uniq_cats',
                                        help='Случайные категории каталога (главный домен).')
                    with _uq2:
                        st.number_input('Товаров', 0, 30, key='c30_uniq_prods',
                                        help='В товарах текст генерится через переменные '
                                             '- по умолчанию 0 (не проверяем). Можно '
                                             'включить, если где-то есть ручной текст.')
                    with _uq3:
                        st.number_input('Порог, %', 50, 100, key='c30_uniq_threshold',
                                        help='Ниже порога - красным. Сайты-источники '
                                             'пересечения показываем всегда, если они есть.')
                    if not (st.session_state.get('c30_textru_key', '').strip()
                            or _secret_pid('textru_key', pid) or _secret('textru_key')):
                        st.warning('⚠ Вставьте ключ text.ru - без него проверка '
                                   'уникальности пропустится (остальной прогон пройдёт).')
                    st.caption('Проверяем SEO-текст КАТЕГОРИЙ главного домена (+ главная и '
                               'каталог): берём связный текст внизу страницы, не карточки/'
                               'меню. Товары по умолчанию не берём (там текст по '
                               'переменным). В отчёте, кроме % уникальности, - список '
                               'конкурентов, с чьим сайтом пересекается контент, и на '
                               'скольких наших страницах (сигнал «скопировали каталог»). '
                               'Свои домены исключаются. Идёт в конце прогона (несколько минут).')
            # ── Бесплатное 1-к-1 сравнение двух страниц (без text.ru и ключей) ──
            if st.checkbox('🆚 Разово сравнить 2 страницы (бесплатно, без text.ru)',
                           key='c30_cmp_pages_on',
                           help='**Мгновенно сравнивает SEO-текст двух страниц '
                                'между собой.**\n\n'
                                'В общем прогоне не участвует - работает по своей '
                                'кнопке «Сравнить».'):
                st.caption('Сравнивает SEO-текст ДВУХ конкретных страниц между собой '
                           '(как бесплатный copyscape.com/compare, но вырезает шапку/'
                           'меню - без «0% из 1134 слов меню»). Ключи и сервисы не '
                           'нужны. Удобно проверить, скопировал ли КОНКРЕТНЫЙ конкурент '
                           'нашу страницу. Ищет только точные совпадения фраз.')
                _cmp_a = st.text_input('Наша страница (URL)', key='c30_cmp_a',
                                       placeholder='https://stalmetural.ru/catalog/setka/')
                _cmp_b = st.text_input('Страница конкурента (URL)', key='c30_cmp_b',
                                       placeholder='https://konkurent.ru/catalog/setka/')
                if st.button('Сравнить', key='c30_cmp_go',
                             disabled=not ((_cmp_a or '').strip() and (_cmp_b or '').strip())):
                    try:
                        import uniqueness_checker as _UCcmp
                        with st.spinner('Скачиваю обе страницы и сравниваю…'):
                            _cr = _UCcmp.compare_urls(_cmp_a.strip(), _cmp_b.strip())
                        _cc = st.columns(3)
                        _cc[0].metric('Нашего текста у них', f'{_cr["a_in_b"]:.0f}%',
                                      help='Сколько % нашего SEO-текста есть на их странице.')
                        _cc[1].metric('Наш текст, симв.', _cr['chars_a'])
                        _cc[2].metric('Их текст, симв.', _cr['chars_b'])
                        if _cr['a_in_b'] >= 30:
                            st.error(f'⚠ {_cr["a_in_b"]:.0f}% нашего текста есть у них – '
                                     'похоже на копию.')
                        elif _cr['a_in_b'] >= 10:
                            st.warning(f'{_cr["a_in_b"]:.0f}% совпадения – частичное пересечение.')
                        else:
                            st.success('Существенных совпадений нет.')
                        if _cr['samples']:
                            st.caption('Совпавшие фразы: '
                                       + '; '.join(f'«{s}»' for s in _cr['samples']))
                    except Exception as _ce:
                        st.error(f'Не удалось сравнить: {_ce}')
            # ── Бесплатная сверка СТРУКТУРЫ каталога с конкурентом (без text.ru) ──
            if st.checkbox('🗂 Сравнить структуру каталога с конкурентом (бесплатно)',
                           key='c30_cmp_struct_on',
                           help='Сверяет наши категории с sitemap конкурента. В общем '
                                'прогоне НЕ участвует - работает по своей кнопке '
                                '«Сравнить структуру».'):
                st.caption('Берёт наши категории и sitemap конкурента и считает, '
                           'сколько НАШИХ категорий (по слагу) есть у него. Высокая '
                           'доля - вероятно, скопировали структуру каталога целиком. '
                           'Без ключей и text.ru.')
                _st_dom = st.text_input('Домен конкурента', key='c30_struct_dom',
                                        placeholder='konkurent.ru')
                if st.button('Сравнить структуру', key='c30_struct_go',
                             disabled=not (_st_dom or '').strip()):
                    try:
                        import uniqueness_checker as _UCst
                        with st.spinner('Читаю sitemap конкурента и сверяю категории…'):
                            _sr = _UCst.compare_catalog_structure(
                                list(src.categories), _st_dom.strip())
                        if _sr.get('error'):
                            st.warning(f'Структуру сверить не вышло: {_sr["error"]}')
                        else:
                            _op = _sr['overlap_pct']
                            st.metric('Наших категорий есть у конкурента',
                                      f'{_sr["matched_count"]} из {_sr["our_count"]} ({_op:.0f}%)',
                                      help='Совпадение по слагу категории.')
                            if _op >= 50:
                                st.error(f'⚠ {_op:.0f}% наших категорий есть у него – '
                                         'похоже на копию структуры каталога.')
                            elif _op >= 20:
                                st.warning(f'{_op:.0f}% совпадения категорий – частичное.')
                            else:
                                st.success('Существенного совпадения структуры нет.')
                            if _sr.get('matched'):
                                st.caption('Совпавшие категории: '
                                           + ', '.join(_sr['matched'][:40]))
                    except Exception as _se:
                        st.error(f'Не удалось сравнить структуру: {_se}')

    # «Подпись прогона» - проект + объём выборки + что проверяем. По ней решаем,
    # показывать ли блок результатов/лог: показываем только для прогона, который
    # запущен в этой сессии при текущих настройках. Сменили проект, объём или
    # набор галочек (или зашли утром заново) - старый лог не показываем.
    _cur_sig = (
        pid, int(random_cities), int(budget['cats']), int(budget['filters']),
        int(budget['products']),
        bool(st.session_state.c30_check_main), bool(st.session_state.c30_check_catalog),
        bool(st.session_state.c30_check_categories), bool(st.session_state.c30_check_filters),
        bool(st.session_state.c30_check_products), bool(st.session_state.c30_check_text),
        bool(st.session_state.get('c30_check_indexing', True)),
        bool(st.session_state.get('c30_check_meta', True)),
        bool(st.session_state.get('c30_check_region', True)),
        bool(st.session_state.get('c30_check_cis', True)),
        bool(st.session_state.get('c30_check_layout', True)),
        bool(st.session_state.get('c30_check_markup', True)),
        bool(st.session_state.get('c30_check_security', True)),
        bool(st.session_state.get('c30_check_images', True)),
        bool(st.session_state.get('c30_check_links', False)),
        bool(st.session_state.get('c30_check_filter_fn', False)),
        bool(st.session_state.get('c30_check_console', False)),
        bool(st.session_state.get('c30_check_calltracking', False)),
        bool(st.session_state.get('c30_check_stress', False)),
        int(st.session_state.get('c30_stress_concurrency', 30)),
        int(st.session_state.get('c30_stress_load_pages', 3)),
        bool(st.session_state.get('c30_check_link_profile', False)),
        bool(st.session_state.get('c30_check_anomaly', False)),
        bool(st.session_state.get('c30_check_admin_settings', False)),
        bool(st.session_state.get('c30_check_admin_crud', False)),
        bool(st.session_state.get('c30_check_admin_product_crud', False)),
        bool(st.session_state.get('c30_check_admin_tech_crud', False)),
        bool(st.session_state.get('c30_check_admin_counters', False)),
        bool(st.session_state.get('c30_check_yabusiness', False)),
        bool(st.session_state.get('c30_check_traffic', False)),
        bool(st.session_state.get('c30_check_review_priority', False)),
        bool(st.session_state.get('c30_check_anomalies', False)),
        bool(st.session_state.get('c30_check_trust', False)),
        bool(st.session_state.get('c30_check_w3c', False)),
        bool(st.session_state.get('c30_check_static', False)),
        bool(st.session_state.get('c30_check_404', True)),
        bool(st.session_state.get('c30_check_ps_filters', True)),
        bool(st.session_state.get('c30_fetch_notifications', True)),
        bool(st.session_state.get('c30_check_uniqueness', False)),
    )

    # Прокси и проверка доступа к сайту живут на странице «Настройки проекта»
    # (единственное место). Здесь блока больше нет: прогон сам берёт адрес
    # через proxy_config, с учётом use_proxy проекта.

    # ── Вход на закрытый сайт (стенд за паролем nginx) ────────────────
    # Показываем ТОЛЬКО проектам с basic_auth в карточке. Значения по
    # умолчанию - из «Настроек проекта»; введённое здесь их перебивает и
    # действует только на этот запуск (в базу не пишется).
    # Без этого блока стенд отвечал 401 на каждой странице, и весь отчёт
    # состоял из ошибок доступа.
    if (cfg or {}).get('basic_auth'):
        with st.container(border=True):
            st.markdown('**Вход на закрытый сайт**')
            st.caption('Сайт закрыт паролем: без него все страницы ответят '
                       '«401», и проверять будет нечего.')
            _sb1, _sb2 = st.columns(2)
            st.session_state.setdefault(
                'c30_site_login', _secret_pid('site_basic_login', pid) or '')
            st.session_state.setdefault(
                'c30_site_password', _secret_pid('site_basic_password', pid) or '')
            with _sb1:
                st.text_input('Логин сайта', key='c30_site_login')
            with _sb2:
                st.text_input('Пароль сайта', key='c30_site_password',
                              type='password')
            if not (st.session_state.get('c30_site_login', '').strip()
                    and st.session_state.get('c30_site_password', '')):
                st.warning('Пока не заполнено - прогон вернёт 401 по всем '
                           'страницам.')

    # БЛОК запуска
    with st.container():
        _paths = _c30_paths(pid)
        _alive = _pid_alive(_read_pidfile(_paths['pid']))
        # Идёт прогон - «присваиваем» его текущим настройкам, чтобы после
        # завершения показать его результаты/лог (даже если зашли в новой сессии).
        if _alive:
            st.session_state.c30_run_sig = _cur_sig
        # Показываем ОДНУ активную кнопку: идёт прогон → «Отменить», иначе →
        # «Запустить». Так нет «выключенной» кнопки с курсором-запретом.
        _go = False
        if _alive:
            if st.button('Отменить проверку', use_container_width=True, key='c30_cancel'):
                _kill_tree(_read_pidfile(_paths['pid']))
                try:
                    _paths['pid'].unlink(missing_ok=True)
                except Exception:
                    pass
                st.session_state.c30_last_error = 'Проверка отменена'
                st.rerun()
        else:
            _go = st.button('Запустить проверку', type='primary',
                            use_container_width=True, key='c30_run')
            # Примерное время прогона по ТЕКУЩИМ галочкам. Тяжёлые браузерные
            # проверки («ошибки JS в консоли», фильтр-тест) меняют его в разы,
            # поэтому показываем до запуска, а не после.
            _est_cities = 1 + int(random_cities)
            _est_per_city = (
                (1 if st.session_state.get('c30_check_main') else 0)
                + (1 if st.session_state.get('c30_check_catalog') else 0)
                + (budget['cats'] if st.session_state.get('c30_check_categories') else 0)
                + (budget['filters'] if st.session_state.get('c30_check_filters') else 0)
                + (budget['products'] if st.session_state.get('c30_check_products') else 0)
            )
            _est_pages = _est_cities * _est_per_city
            # Ключи галочек в session_state - c30_<флаг>; модель прогноза ждёт
            # имена без префикса (как flags прогона).
            _est_checks = {
                _k[4:]: bool(st.session_state.get(_k))
                for _k in _C30_PAGE_CHECKS + (
                    'c30_check_gsc_pages', 'c30_check_arsenkin',
                    'c30_check_uniqueness', 'c30_check_stress',
                    'c30_fetch_notifications', 'c30_fetch_metrika_404',
                )
            }
            # Число ХОСТОВ проекта: ссылочный профиль, ИКС и аномалии ходят по
            # ВСЕМ сайтам, сколько бы городов ни выбрали для страниц. На
            # проекте с 242 хостами это минуты - без этого прогноз занижался.
            _est_hosts = stats.get('subdomains_count') or _est_cities
            _est_low, _est_high = estimate_run_seconds(
                _est_pages, _est_cities, _est_checks, hosts=_est_hosts)
            # Прогноз кладём в состояние: ниже, у секундомера, он нужен для
            # остатка времени и сравнения «прогноз против факта».
            st.session_state['c30_est'] = (_est_low, _est_high)
            _ui.estimate_badge(
                format_estimate(_est_low, _est_high),
                f'{_est_pages} страниц, {_est_cities} городов, '
                f'{_est_hosts} хостов проекта. Зависит от скорости сайта, '
                f'прокси и лимитов внешних сервисов.')
        if _go:
            flags = {
                'check_main': st.session_state.c30_check_main,
                'check_catalog': st.session_state.c30_check_catalog,
                'check_categories': st.session_state.c30_check_categories,
                'check_filters': st.session_state.c30_check_filters and stats['has_filters'],
                'check_products': st.session_state.c30_check_products,
                'check_text': st.session_state.c30_check_text,
                'check_indexing': st.session_state.get('c30_check_indexing', True),
                'check_meta': st.session_state.get('c30_check_meta', True),
                'check_region': st.session_state.get('c30_check_region', True),
                'check_cis': st.session_state.get('c30_check_cis', True),
                'check_layout': st.session_state.get('c30_check_layout', True),
                'check_markup': st.session_state.get('c30_check_markup', True),
                'check_security': st.session_state.get('c30_check_security', True),
                'check_images': st.session_state.get('c30_check_images', True),
                'check_links': st.session_state.get('c30_check_links', False),
                'check_index_404': st.session_state.get('c30_check_index_404', False),
                'check_yabusiness': st.session_state.get('c30_check_yabusiness', False),
                'check_traffic': st.session_state.get('c30_check_traffic', False),
                'check_review_priority': st.session_state.get('c30_check_review_priority', False),
                'check_anomalies': st.session_state.get('c30_check_anomalies', False),
                'check_trust': st.session_state.get('c30_check_trust', False),
                'check_gsc_pages': st.session_state.get('c30_check_gsc_pages', False),
                'check_home_dupes': st.session_state.get('c30_check_home_dupes', False),
                'check_arsenkin': st.session_state.get('c30_check_arsenkin', False),
                'arsenkin_token': (st.session_state.get('c30_arsenkin_token', '') or '').strip(),
                'arsenkin_urls': st.session_state.get('c30_arsenkin_urls', '') or '',
                'arsenkin_yandex': st.session_state.get('c30_arsenkin_yandex', True),
                'arsenkin_google': st.session_state.get('c30_arsenkin_google', True),
                'arsenkin_search_all': st.session_state.get('c30_arsenkin_search_all', True),
                'arsenkin_inurl': st.session_state.get('c30_arsenkin_inurl', False),
                'check_uniqueness': st.session_state.get('c30_check_uniqueness', False),
                'textru_key': (st.session_state.get('c30_textru_key', '') or '').strip()
                or _secret_pid('textru_key', pid) or _secret('textru_key') or '',
                'uniq_cats': int(st.session_state.get('c30_uniq_cats', 3) or 0),
                'uniq_prods': int(st.session_state.get('c30_uniq_prods', 3) or 0),
                'uniq_threshold': int(st.session_state.get('c30_uniq_threshold', 95) or 95),
                'gsc_pages_indexed': int(st.session_state.get('c30_gsc_indexed', 0) or 0),
                'gsc_pages_crawled_ni': int(st.session_state.get('c30_gsc_crawled_ni', 0) or 0),
                'check_filter_fn': st.session_state.get('c30_check_filter_fn', False),
                'check_console': st.session_state.get('c30_check_console', False),
                'check_calltracking': st.session_state.get('c30_check_calltracking', False),
                'check_stress': st.session_state.get('c30_check_stress', False),
                'stress_concurrency': int(st.session_state.get('c30_stress_concurrency', 30)),
                'stress_load_pages': int(st.session_state.get('c30_stress_load_pages', 3)),
                'check_link_profile': st.session_state.get('c30_check_link_profile', False),
                'check_anomaly': st.session_state.get('c30_check_anomaly', False),
                'check_admin_settings': st.session_state.get('c30_check_admin_settings', False),
                'admin_crud': st.session_state.get('c30_check_admin_crud', False),
                'admin_product_crud': st.session_state.get('c30_check_admin_product_crud', False),
                'admin_tech_crud': st.session_state.get('c30_check_admin_tech_crud', False),
                'admin_counters': st.session_state.get('c30_check_admin_counters', False),
                'admin_execute': st.session_state.get('c30_adm_execute', True),
                'check_w3c': st.session_state.get('c30_check_w3c', False),
                'check_static': st.session_state.get('c30_check_static', False),
                'check_404': st.session_state.get('c30_check_404', True),
                'check_ps_filters': st.session_state.get('c30_check_ps_filters', True),
                'fetch_notifications': st.session_state.get('c30_fetch_notifications', True),
                'notify_days': int(st.session_state.get('c30_notify_days', 7)),
                'fetch_metrika_404': st.session_state.get('c30_fetch_metrika_404', True),
                **_resolve_m404_period(),
                'autoclick': st.session_state.get('c30_autoclick', False),
                'autoclick_wm': st.session_state.get('c30_ac_wm', False),
                'autoclick_gsc': st.session_state.get('c30_ac_gsc', False),
            }
            # Свой список URL (если включён) - добавится к обычной выборке проекта.
            _custom_urls = []
            if (st.session_state.get('c30_use_custom_urls')
                    and st.session_state.get('c30_custom_urls_text', '').strip()):
                _custom_urls = [
                    ln.strip() for ln in st.session_state.c30_custom_urls_text.split('\n')
                    if ln.strip() and not ln.strip().startswith('#')
                ]
            # Случайная проверка карт сайта - список видов + режим отбора,
            # только на этот запуск (не сохраняется).
            _sm_groups = st.session_state.get('c30_sitemap_groups') or {}
            _sm_manual = st.session_state.get('c30_sitemap_mode',
                                              'Настроить вручную') == 'Настроить вручную'
            _sm_excluded = [
                u for _urls in _sm_groups.values() for u in _urls
                if not st.session_state.get(
                    'c30_sm_incl_' + hashlib.md5(u.encode()).hexdigest()[:12], True)
            ] if _sm_manual else []
            sitemap_sample = {
                'enabled': st.session_state.get('c30_check_sitemap_sample', False),
                'groups': _sm_groups,
                'mode': 'manual' if _sm_manual else 'random',
                'excluded': _sm_excluded,
                'urls_per_map': int(st.session_state.get('c30_sitemap_urls_per_map', 5)),
            }
            try:
                _sk_hint = [k for k in list(st.secrets.keys())
                            if 'gsc' in k.lower() or pid in k.lower()]
                # Какие «webmaster/oauth»-ключи реально есть - для диагностики
                _wm_hint = [k for k in list(st.secrets.keys())
                            if 'webmaster' in k.lower() or 'oauth' in k.lower()]
            except Exception:
                _sk_hint, _wm_hint = [], []
            # Токен Яндекс OAuth (Вебмастер-API; тот же подойдёт для Метрики).
            # ПОРЯДОК ВАЖЕН: поле «OAuth-токен Вебмастера» (webmaster_oauth)
            # главнее «OAuth-токена Яндекса» (yandex_oauth). Раньше было
            # наоборот, и это стоило дня разбирательств: у проекта заполнены
            # ОБА поля, человек перевыпустил токен с недостающим правом и
            # положил его в поле с точным названием, а прогон продолжал брать
            # старый - в логе снова «нет права EXTERNAL_LINKS».
            _wm_token = (_secret_pid('webmaster_oauth', pid)
                         or _secret_pid('yandex_oauth', pid))
            creds = {
                'proxy_url': _c30_effective_proxy,
                'tg_token': _secret('telegram_bot_token'),
                'tg_recipients': get_telegram_recipients(pid),
                # Кто запустил - руководителю, который следит за всеми
                # прогонами проекта, важно понимать, чей это отчёт.
                'started_by': _кто_запустил(),
                # Google Диск для отчётов: общий диск (Shared Drive) и, если
                # задана, готовая папка проекта. Пусто = выкладка пропускается.
                'gdrive_shared_drive_id': _secret_pid('gdrive_shared_drive_id', pid),
                'gdrive_folder_id': _secret_pid('gdrive_folder_id', pid),
                # Служебный Google-аккаунт: общая привязка на всё приложение
                # (её делают один раз), иначе - привязка самого проекта.
                'gdrive_refresh_token': _gdrive_refresh(pid),
                # Ключи OAuth живут в настройках проекта (их видит руководитель),
                # секрет приложения - запасной источник; _secret_pid уже даёт
                # именно такой приоритет.
                'google_oauth_client_id': _secret_pid('google_oauth_client_id', pid),
                'google_oauth_client_secret': _secret_pid('google_oauth_client_secret', pid),
                'metrika': get_metrika_credentials(pid),
                'gsc': get_gsc_credentials(pid),
                # Сервисный аккаунт GSC для источника «Google» в «404 в индексе»
                # через Search Console API (работает на облаке, без браузера).
                'gsc_sa': get_gsc_sa(pid),
                'yab': get_yabusiness_credentials(pid),
                'twogis': get_twogis_credentials(pid),
                'google': get_google_accounts_credentials(pid),
                'google_folder': get_google_folder_credentials(pid),
                'webmaster_oauth': _wm_token,
                # Вход на закрытый стенд (nginx-пароль). Пусто у обычных
                # проектов - тогда обход идёт как раньше, без авторизации.
                # Поля на странице (блок «Вход на закрытый сайт») перебивают
                # настройки проекта: пароль стенда меняют часто, и ждать
                # сохранения в кабинете ради одного прогона незачем.
                'site_basic_login': (st.session_state.get('c30_site_login', '').strip()
                                     or _secret_pid('site_basic_login', pid)),
                'site_basic_password': (st.session_state.get('c30_site_password', '')
                                        or _secret_pid('site_basic_password', pid)),
                # DR на листе «Траст проекта» (trust_check.fetch_dr). Без этого
                # ключа ИКС приходил, а DR всегда стоял прочерком: runner читает
                # creds['openpagerank_key'], а сюда его никто не клал.
                'openpagerank_key': _secret_pid('openpagerank_key', pid),
                'metrika_oauth': _secret_pid('metrika_oauth', pid),
                'metrika_counter': _secret_pid('metrika_counter', pid),
                # Сессия для облачного автокликера (base64 storage_state).
                # По-проектная: autoclick_session_<pid> (+ общий фоллбэк).
                # Общая сессия Яндекса (автокликеры / 404-в-индексе / Я.Бизнес).
                # Новое имя yandex_session_<pid>; старое autoclick_session_<pid>
                # - fallback (не ломаем существующие секреты).
                'autoclick_session': (_secret_pid('yandex_session', pid)
                                      or _secret_pid('autoclick_session', pid)),
                'webmaster_keys_hint': _wm_hint,
                'secret_keys_hint': _sk_hint,
                # Креды админки (п.1.21): из полей UI; пустые значения не шлём
                'admin_settings': {
                    k: v for k, v in (
                        ('domain', st.session_state.get('c30_adm_domain', '').strip()),
                        ('login', st.session_state.get('c30_adm_login', '').strip()),
                        ('password', st.session_state.get('c30_adm_password', '')),
                        ('basic_login', st.session_state.get('c30_adm_basic_login', '').strip()),
                        ('basic_password', st.session_state.get('c30_adm_basic_password', '')),
                    ) if v},
            }
            # Доп. СНГ-домены по пресету (быстрая 0, стандарт/полная 1) - помимо
            # обязательного smg.az (см. mandatory_hosts в smu.json).
            _cis_extra = get_profile_kwargs(
                st.session_state.get('c30_preset', 'standard')).get('cis_extra_subdomains', 0)
            params = {'budget': budget, 'random_cities': int(random_cities),
                      'custom_urls': _custom_urls, 'cis_extra': _cis_extra,
                      'sitemap_sample': sitemap_sample, **flags}
            st.session_state.c30_results = None
            st.session_state.c30_report_path = None
            st.session_state.c30_last_error = None
            st.session_state.c30_run_sig = _cur_sig   # этот прогон - «наш», его и покажем
            _launch_checklist_bg(pid, params, creds)
            st.rerun()

    # ── Прогон: прогресс фонового ПРОЦЕССА ──────────────────────────
    _paths = _c30_paths(pid)
    _alive = _pid_alive(_read_pidfile(_paths['pid']))
    _done = _paths['result'].exists() or _paths['report'].exists()
    # Показывать результат/лог только для «нашего» прогона (текущие настройки).
    _show_run = (st.session_state.get('c30_run_sig') == _cur_sig)
    # Завершение определяем по появлению артефакта (отчёт/результат), а не только
    # по живости pid: на Linux дочерний процесс может стать zombie и «жить» в
    # таблице процессов, из-за чего _alive остаётся True и UI зависает на прогрессе.
    _est = st.session_state.get('c30_est') or (None, None)
    if _alive and not _done:
        with st.container(border=True):
            st.markdown('### ⏳ Идёт проверка')
            st.caption('Можно переключаться на другие вкладки - прогон идёт в фоне '
                       'и не прервётся.')
            _ui.elapsed_caption(_paths['pid'], _paths['log'], running=True,
                                estimate_low=_est[0], estimate_high=_est[1])
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
        # 1) Путь к отчёту - из лёгкого сайдкара (надёжно, не зависит от pickle).
        try:
            if _paths['report'].exists():
                _rp_txt = _paths['report'].read_text(encoding='utf-8').strip()
                if _rp_txt:
                    st.session_state.c30_report_path = _rp_txt
        except Exception:
            pass
        # 2) Результаты - из pickle (для метрик и блока результатов); если
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
    if _show_run and st.session_state.get('c30_last_error'):
        st.error(f'Прогон завершился с ошибкой: {st.session_state.c30_last_error}')

    # ── Запасная кнопка скачивания ──────────────────────────────────
    # Если отчёт этой сессии есть на диске, но полный блок результатов ниже
    # не отрисовался (нет распарсенных результатов ИЛИ подпись прогона не
    # совпала после смены галочек) - всё равно даём скачать xlsx. НЕ зависит
    # от _show_run: c30_report_path ставится только по завершении прогона
    # этой сессии, так что «чужой» отчёт сюда не попадёт.
    _rich_will_show = (_show_run and st.session_state.get('c30_results')
                       and not st.session_state.get('c30_is_running'))
    if st.session_state.get('c30_report_path') and not _rich_will_show:
        _rp = Path(st.session_state.c30_report_path)
        if _rp.exists():
            with open(_rp, 'rb') as _f:
                st.download_button(
                    label=f'📥 Скачать отчёт ({_rp.name})',
                    data=_f.read(), file_name=_rp.name,
                    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    use_container_width=True, type='primary', key='c30_dl_fallback')

    # ── Результаты прогона ──────────────────────────────────────────
    if _show_run and st.session_state.c30_results and not st.session_state.c30_is_running:
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
            # Сверка с прогнозом: видно, врёт оценка или нет.
            if _est[0] and _est[1]:
                if duration < _est[0]:
                    _вердикт = 'быстрее прогноза'
                elif duration > _est[1]:
                    _вердикт = 'дольше прогноза'
                else:
                    _вердикт = 'в рамках прогноза'
                st.caption(f'⏱ Прогноз был {format_estimate(_est[0], _est[1])} - '
                           f'{_вердикт}.')

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
                    extra_html = (' - ' + _html.escape(' · '.join(extra))) if extra else ''
                    # Вся строка - чистый HTML (без смешения с markdown-разметкой),
                    # иначе Streamlit иногда не дорисовывает теги-span после markdown-ссылки.
                    st.markdown(
                        f'<div style="margin:2px 0;font-size:0.9rem">'
                        f'{emoji} <b>{_html.escape(city)}</b>{_html.escape(type_label)}: '
                        f'<a href="{url_safe}" target="_blank">{url_safe}</a>'
                        f'{extra_html}{tags_html}</div>',
                        unsafe_allow_html=True,
                    )
                if len(problems) > 50:
                    st.caption(f'... и ещё {len(problems) - 50}. Все детали - в xlsx-отчёте.')

    # Уведомления из почты и 404 из Метрики - в xlsx-отчёте (лист
    # «Уведомления»), собираются по галке «Собрать уведомления» за
    # выбранный период. Отдельный блок в UI убран.

    # ── Лог прогона: самый нижний блок (под результатами). Показываем только
    #    для «нашего» прогона (текущие настройки) - иначе утром/после смены
    #    проекта висел бы старый лог. ──
    if _show_run and not _alive:
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

    # Сохранение шаблона теперь происходит ПРЯМО по клику «Сохранить» вверху
    # (render_panel(save_keys=_c30_tpl_keys)) - надёжно, не зависит от того,
    # дошла ли отрисовка до низа страницы во время прогона / зависшего PID.

else:
    st.info('Выберите проект, чтобы начать еженедельную проверку.')
