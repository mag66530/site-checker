"""
runner_30min.py - логика прогона 30-мин чек-листа БЕЗ Streamlit.

Используется фоновым подпроцессом checklist_run.py, чтобы тяжёлая async-работа
(run_batch на aiohttp) шла в отдельном ПРОЦЕССЕ - надёжно, в отличие от потока
внутри Streamlit. Возвращает результаты, путь отчёта и т.п.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sources import (
    load_project_config, load_sources, build_plan, build_custom_tasks_typed,
    get_tech_paths,
)
from history import load_history, save_history, WEEKLY_TTL_MS
from sitemap import load_product_pathnames
from product_links import load_product_links
from http_checker import run_batch
from reporter import build_report, make_report_filename
from telegram_notify import (
    format_summary_message, send_run_notification, send_message,
    format_critical_alert, format_critical_block,
)
from critical import analyze as analyze_critical
from webmaster_notify import (
    WEBMASTER_YANDEX_CONFIG,
    fetch_webmaster_yandex, fetch_gsc_gmail,
    fetch_yandex_folder_simple, fetch_google_accounts,
    load_notifications,
)
from metrika_404 import (
    MAILBOX_CONFIG, fetch_incremental,
    load_reports_for_period, get_latest_available_date,
)
from webmaster_api import fetch_webmaster_issues, load_issues

REPORTS_DIR = Path(__file__).parent / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)


def _resolve_metrika_date(s):
    """'today'|'yesterday'|'NdaysAgo'|'YYYY-MM-DD' → datetime (или None)."""
    import re
    s = (s or '').strip()
    if s == 'today':
        return datetime.now()
    if s == 'yesterday':
        return datetime.now() - timedelta(days=1)
    m = re.match(r'(\d+)daysAgo$', s)
    if m:
        return datetime.now() - timedelta(days=int(m.group(1)))
    try:
        return datetime.strptime(s, '%Y-%m-%d')
    except Exception:
        return None


def _metrika_period_display(d1, d2):
    """Человекочитаемый период: «18.06.2026» или «12.06.2026 - 18.06.2026»."""
    a, b = _resolve_metrika_date(d1), _resolve_metrika_date(d2)
    if not a or not b:
        return None
    fa, fb = a.strftime('%d.%m.%Y'), b.strftime('%d.%m.%Y')
    return fa if fa == fb else f'{fa} - {fb}'


# ── Автокликер (локально: залогиненный Chrome через CDP 9222) ────────


def _cdp_alive(host='127.0.0.1', port=9222, timeout=1.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _parse_wm_recheck_log():
    """webmaster_recheck_log.json → список сводок по сайтам."""
    import json
    p = Path(__file__).parent / 'webmaster_recheck_log.json'
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []
    out = []
    for e in data.get('entries', []):
        out.append({
            'site': e.get('site', ''), 'service': 'Вебмастер',
            'problems': e.get('problems', 0), 'clicked': e.get('clicked', 0),
            'checking': e.get('checking', 0), 'no_button': e.get('no_button', 0),
            'errors': e.get('errors', 0),
        })
    return out


def _run_autoclicker(pid, params, log, session_b64=None):
    """Прокликать ошибки (Вебмастер/ГСК) и дождаться завершения.
    Локальный залогиненный Chrome (CDP 9222) в приоритете; нет его, но есть
    сессия (Secrets: autoclick_session) - облачный headless-режим.
    Возвращает dict для листа «Автокликер»."""
    import os as _os
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    _env = None
    if not _cdp_alive():
        if session_b64:
            try:
                from autoclick_browser import (
                    session_file_from_secret, MODE_ENV, SESSION_FILE_ENV)
                _env = dict(_os.environ)
                _env['PYTHONIOENCODING'] = 'utf-8'
                _env[MODE_ENV] = 'cloud'
                _env[SESSION_FILE_ENV] = session_file_from_secret(session_b64)
                log('Автокликер: локального Chrome нет - облачный режим '
                    '(headless + сессия из Secrets).')
            except Exception as _e:
                log(f'⚠ Автокликер: сессия из Secrets не читается ({_e}) - пропускаю.')
                return {'available': False,
                        'note': f'Сессия autoclick_session не читается: {_e}. '
                                f'Пере-экспортируй её локально (вкладка '
                                f'«Автокликеры» → «Экспорт сессии для облака»).'}
        else:
            log('⚠ Автокликер: Chrome/CDP 9222 не запущен и сессии в Secrets '
                'нет - пропускаю.')
            return {'available': False,
                    'note': 'Нет ни локального Chrome (9222), ни сессии в '
                            'Secrets (autoclick_session) - автокликер пропущен. '
                            'Локально: «Автокликеры» → «Открыть браузер для '
                            'входа». Облако: там же «Экспорт сессии для облака» '
                            '+ секрет autoclick_session.'}
    args = [_sys.executable, 'autoclick_run.py', '--project', pid]
    if params.get('autoclick_wm'):
        args.append('--wm')
    if params.get('autoclick_gsc'):
        args.append('--gsc')
    # Чистим прошлый лог Вебмастера - чтобы парсить только текущий прогон.
    try:
        (root / 'webmaster_recheck_log.json').unlink(missing_ok=True)
    except Exception:
        pass
    log(f'Автокликер: запускаю ({" ".join(args[2:])})… '
        f'чек-лист завершится после перекликивания всех ошибок.')
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', env=_env)
        for line in proc.stdout:
            log(f'  [клик] {line.rstrip()}')
        proc.wait()
    except Exception as e:
        log(f'⚠ Автокликер: {e}')
        return {'available': True, 'sites': [], 'note': str(e)}
    sites = _parse_wm_recheck_log()
    log(f'✓ Автокликер: сайтов {len(sites)}, '
        f'прокликано {sum(s.get("clicked", 0) for s in sites)}')
    return {'available': True, 'sites': sites}


def _run_index404_download(pid, params, log, session_b64=None):
    """Авто-скачать выгрузку «Страницы в поиске» браузером и разобрать на 404.
    Локальный залогиненный Chrome (CDP 9222) в приоритете; нет его, но есть
    сессия (Secrets: autoclick_session) - облачный headless-режим. Логина с
    нуля нет (у Яндекса капча) - только сохранённая сессия.
    Возвращает dict в форме листа «404 в индексе»."""
    import os as _os
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    _env = dict(_os.environ)
    _env['PYTHONIOENCODING'] = 'utf-8'
    if not _cdp_alive():
        if session_b64:
            try:
                from autoclick_browser import (
                    session_file_from_secret, MODE_ENV, SESSION_FILE_ENV)
                _env[MODE_ENV] = 'cloud'
                _env[SESSION_FILE_ENV] = session_file_from_secret(session_b64)
                log('404 в индексе: локального Chrome нет - облачный режим '
                    '(headless + сессия из Secrets).')
            except Exception as _e:
                return {'available': False, 'source': 'yandex_export', 'hosts': [],
                        'error': f'сессия autoclick_session не читается: {_e}'}
        else:
            return {'available': False, 'source': 'yandex_export', 'hosts': [],
                    'error': ('нет ни локального Chrome (CDP 9222), ни сессии в '
                              'Secrets (autoclick_session). Настрой сессию один '
                              'раз: вкладка «Автокликеры» → «Экспорт сессии для '
                              'облака».')}
    (root / 'cache').mkdir(exist_ok=True)
    _res_file = root / 'cache' / f'index404_{pid}.json'
    try:
        _res_file.unlink(missing_ok=True)
    except Exception:
        pass
    args = [_sys.executable, 'index404_run.py', '--project', pid]
    if params.get('index_404_max_hosts'):
        args += ['--max-hosts', str(params['index_404_max_hosts'])]
    log('404 в индексе: запускаю браузер, качаю выгрузки «Страницы в поиске»…')
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', env=_env)
        for line in proc.stdout:
            log(f'  [404-индекс] {line.rstrip()}')
        proc.wait()
    except Exception as e:
        return {'available': False, 'source': 'yandex_export', 'hosts': [],
                'error': str(e)}
    try:
        return json.loads(_res_file.read_text(encoding='utf-8'))
    except Exception as e:
        return {'available': False, 'source': 'yandex_export', 'hosts': [],
                'error': f'результат не прочитан: {e}'}


def _run_gsc_index404(pid, params, log, session_b64=None, gsc_login=None):
    """Авто-экспорт «Не найдено (404)» / «Ошибка сервера (5xx)» из Google
    Search Console браузером. Основа — сохранённая сессия (путь B); слетела —
    автологин по логину/паролю Google (путь C, gsc_login=(email,password)).
    Возвращает dict в форме «404 в индексе» с source='Google' у записей."""
    import os as _os
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    _env = dict(_os.environ)
    _env['PYTHONIOENCODING'] = 'utf-8'
    # Запасной автовход (C) ВЫКЛ по умолчанию: Google блокирует автоматический
    # вход («Не удалось войти в аккаунт» — анти-бот стена), а повторные попытки
    # рискуют залочить аккаунт. Оставлен под флагом для робота-аккаунта без 2FA.
    if params.get('index_404_gsc_autologin', False) and gsc_login:
        _gl_email, _gl_pass = (gsc_login or (None, None))
        if _gl_email and _gl_pass:
            _env['GSC_LOGIN_EMAIL'] = _gl_email
            _env['GSC_LOGIN_PASSWORD'] = _gl_pass
    if not _cdp_alive():
        if session_b64:
            try:
                from autoclick_browser import (
                    session_file_from_secret, MODE_ENV, SESSION_FILE_ENV)
                _env[MODE_ENV] = 'cloud'
                _env[SESSION_FILE_ENV] = session_file_from_secret(session_b64)
            except Exception as _e:
                return {'available': False, 'source': 'gsc', 'hosts': [],
                        'error': f'сессия autoclick_session не читается: {_e}'}
        else:
            return {'available': False, 'source': 'gsc', 'hosts': [],
                    'error': ('нет ни локального Chrome, ни сессии в Secrets '
                              '(autoclick_session) - GSC пропущен.')}
    (root / 'cache').mkdir(exist_ok=True)
    _res_file = root / 'cache' / f'index_gsc_{pid}.json'
    try:
        _res_file.unlink(missing_ok=True)
    except Exception:
        pass
    args = [_sys.executable, 'index_gsc_run.py', '--project', pid]
    log('404 в индексе (GSC): открываю Search Console, качаю 404/5xx…')
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', env=_env)
        for line in proc.stdout:
            log(f'  [404-GSC] {line.rstrip()}')
        proc.wait()
    except Exception as e:
        return {'available': False, 'source': 'gsc', 'hosts': [], 'error': str(e)}
    try:
        return json.loads(_res_file.read_text(encoding='utf-8'))
    except Exception as e:
        return {'available': False, 'source': 'gsc', 'hosts': [],
                'error': f'результат GSC не прочитан: {e}'}


def _run_filters_test(pid, params, log, category_urls=None):
    """Фильтр-тест товаров в браузере (доп. чек-лист). Отдельный процесс
    filters_run.py (Playwright): локальный CDP-Chrome не нужен, каталог
    публичный - гоняем свой headless. category_urls - категории прогона:
    фильтр проверяется на КАЖДОЙ (полная картинка). Возвращает dict для
    секции «Фильтрация»."""
    import os as _os
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    _env = dict(_os.environ)
    _env['PYTHONIOENCODING'] = 'utf-8'
    (root / 'cache').mkdir(exist_ok=True)
    # В облаке нет локального Chrome - filters_run сам поднимет headless;
    # флаг CCR_AGENT_PROXY_ENABLED (если есть) он учитывает сам.
    args = [_sys.executable, 'filters_run.py', '--project', pid]
    if category_urls:
        _cat_file = root / 'cache' / f'filter_cats_{pid}.json'
        try:
            _cat_file.write_text(json.dumps(list(category_urls),
                                            ensure_ascii=False), encoding='utf-8')
            args += ['--categories-file', str(_cat_file)]
        except Exception:
            pass
    _res_file = root / 'cache' / f'filters_{pid}.json'
    try:
        _res_file.unlink(missing_ok=True)
    except Exception:
        pass
    log('Фильтр-тест товаров: запускаю браузер…')
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', env=_env)
        for line in proc.stdout:
            log(f'  [фильтр] {line.rstrip()}')
        proc.wait()
    except Exception as e:
        log(f'⚠ Фильтр-тест: {e}')
        return {'available': True, 'cases': [], 'note': str(e)}
    try:
        return json.loads(_res_file.read_text(encoding='utf-8'))
    except Exception as e:
        return {'available': False, 'cases': [],
                'note': f'результат фильтр-теста не прочитан: {e}'}


def _run_console_check(pid, urls, log):
    """Проверка ошибок JS в консоли (пункт 1.14). Отдельный процесс
    console_run.py (Playwright) по СТРАНИЦАМ, которые прошёл чек-лист.
    Возвращает dict для листа «Ошибки JavaScript»."""
    import os as _os
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    (root / 'cache').mkdir(exist_ok=True)
    _urls_file = root / 'cache' / f'console_urls_{pid}.json'
    _res_file = root / 'cache' / f'console_{pid}.json'
    try:
        _urls_file.write_text(json.dumps(list(urls), ensure_ascii=False),
                              encoding='utf-8')
        _res_file.unlink(missing_ok=True)
    except Exception:
        pass
    _env = dict(_os.environ)
    _env['PYTHONIOENCODING'] = 'utf-8'
    args = [_sys.executable, 'console_run.py', '--project', pid,
            '--urls-file', str(_urls_file)]
    log(f'Проверка консоли (JS): {len(urls)} страниц, запускаю браузер…')
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', env=_env)
        for line in proc.stdout:
            log(f'  [консоль] {line.rstrip()}')
        proc.wait()
    except Exception as e:
        log(f'⚠ Проверка консоли: {e}')
        return {'available': True, 'checked': 0, 'pages': [], 'note': str(e)}
    try:
        return json.loads(_res_file.read_text(encoding='utf-8'))
    except Exception as e:
        return {'available': False, 'checked': 0, 'pages': [],
                'note': f'результат проверки консоли не прочитан: {e}'}


def _run_admin_settings(pid, params, creds, log):
    """Проверка «работают функции настройки» в админке (доп. чек-лист).
    Отдельный процесс admin_settings_run.py (Playwright): креды уходят через
    env ADMIN_SETTINGS_CREDS (JSON) - пароль на диск не пишется. Возвращает
    dict для листа «Настройки в админке»."""
    import os as _os
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    (root / 'cache' / 'admin-settings').mkdir(parents=True, exist_ok=True)
    _res_file = root / 'cache' / 'admin-settings' / f'{pid}-run.json'
    try:
        _res_file.unlink(missing_ok=True)
    except Exception:
        pass
    _env = dict(_os.environ)
    _env['PYTHONIOENCODING'] = 'utf-8'
    _env['ADMIN_SETTINGS_CREDS'] = json.dumps(
        creds.get('admin_settings') or {}, ensure_ascii=False)
    args = [_sys.executable, 'admin_settings_run.py', '--project', pid,
            '--from-env', '--out', str(_res_file)]
    if params.get('admin_crud'):
        args.append('--crud')
    if params.get('admin_product_crud'):
        args.append('--product-crud')
    if params.get('admin_tech_crud'):
        args.append('--tech-crud')
    if params.get('admin_counters'):
        args.append('--counters')
    if (params.get('admin_crud') or params.get('admin_product_crud')) \
            and not params.get('admin_execute', True):
        args.append('--no-execute')
    log('Настройки в админке: запускаю браузер…')
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', env=_env)
        for line in proc.stdout:
            log(f'  [админка] {line.rstrip()}')
        proc.wait()
    except Exception as e:
        log(f'⚠ Настройки в админке: {e}')
        return {'available': False, 'note': str(e)}
    try:
        return json.loads(_res_file.read_text(encoding='utf-8'))
    except Exception as e:
        return {'available': False,
                'note': f'результат проверки админки не прочитан: {e}'}


def run_check(pid, params, creds, log, progress):
    """Выполнить прогон. log(msg), progress(frac, text) - колбэки.
    Возвращает dict с results / report_path / started_at / finished_at / error."""
    out = {'results': None, 'report_path': None,
           'started_at': int(datetime.now().timestamp() * 1000),
           'finished_at': None, 'error': None}
    try:
        cfg = load_project_config(pid)
        src = load_sources(cfg)
        started_ms = out['started_at']
        _nd = int(params.get('notify_days', 30))   # период сбора почты/404

        proxy_url = creds.get('proxy_url') if cfg.get('use_proxy') else None
        if cfg.get('use_proxy') and not proxy_url:
            log(f'⚠ Прокси нужен для {cfg["name"]}, но не настроен')
        elif proxy_url:
            log(f'Прокси: включён для {cfg["name"]}')

        if not src.products:
            base_links = load_product_links(pid)
            if base_links and base_links['pathnames']:
                src.products = base_links['pathnames']
                log(f'Товары из базы листингов: {len(src.products)}')
            else:
                log('Загружаю sitemap для товаров…')
                try:
                    sm = asyncio.run(load_product_pathnames(
                        cfg, src.categories, src.filters,
                        log=lambda lvl, msg: log(msg), proxy_url=proxy_url))
                    src.products = sm.get('pathnames', [])
                    log(f'Из sitemap: {len(src.products)} товаров')
                except Exception as e:
                    log(f'⚠ Sitemap не загрузился: {e}. Прогон без товаров.')

        recent = set(load_history(pid, ttl_ms=WEEKLY_TTL_MS).keys())
        log(f'История ротации (30 дней): {len(recent)} URL')

        b = params['budget']
        plan = build_plan(
            src,
            random_subdomains_count=int(params['random_cities']),
            categories_per_subdomain=b['cats'],
            filters_per_subdomain=b['filters'],
            products_per_subdomain=b['products'],
            check_main=params['check_main'],
            check_catalog=params['check_catalog'],
            check_categories=params['check_categories'],
            check_filters=params['check_filters'],
            check_products=params['check_products'],
            mandatory_city=cfg.get('mandatory_city', 'Москва'),
            mandatory_hosts=cfg.get('mandatory_hosts'),
            cis_extra_subdomains=int(params.get('cis_extra', 0)),
            rotation_history=recent,
        )
        # Свой список URL - добавляем к выборке проекта (тип по адресу).
        _custom = params.get('custom_urls') or []
        if _custom:
            try:
                extra = build_custom_tasks_typed(_custom, src)
                if extra:
                    plan.tasks.extend(extra)
                    log(f'Свой список URL: добавлено {len(extra)}')
            except Exception as e:
                log(f'⚠ Свой список URL не разобран: {e}')

        # Технические страницы (на главном домене) - проверяем ВСЕГДА, при любом
        # прогоне, вне зависимости от объёма выборки.
        _tech_paths = get_tech_paths(pid)
        _mcity = cfg.get('mandatory_city', 'Москва')
        _main = next((s for s in src.subdomains if s.city == _mcity), None)
        if _main and _tech_paths:
            _tech_urls = [f'https://{_main.host}{p}' for p in _tech_paths]
            try:
                _tt = build_custom_tasks_typed(_tech_urls, src)
                for _t in _tt:
                    _p = urlparse(_t.url).path.rstrip('/')
                    if _p.endswith('/specials'):
                        # Спецпредложения - это листинг товаров, проверяем как раздел.
                        _t.type_code = 'category'
                        _t.type_label = 'Спецпредложения'
                    else:
                        _t.type_code = 'tech'
                        _t.type_label = 'Тех. страница'
                plan.tasks.extend(_tt)
                log(f'Технические страницы: добавлено {len(_tt)}')
            except Exception as e:
                log(f'⚠ Тех. страницы: {e}')

        log(f'Города: {", ".join(s.city for s in plan.selected_subdomains)}')
        log(f'Всего проверок: {len(plan.tasks)}')

        counters = {'ok': 0, 'warn': 0, 'err': 0}

        def on_progress(result, done, total_n):
            if result.is_ok:
                counters['ok'] += 1
            elif result.is_warning:
                counters['warn'] += 1
            else:
                counters['err'] += 1
            progress(done / max(total_n, 1),
                     f'Проверено {done} из {total_n} - '
                     f'✅ {counters["ok"]} · ⚠ {counters["warn"]} · ❌ {counters["err"]}')

        try:
            from kp import load_kp
            kp_map = load_kp(pid) or None
            if kp_map:
                log(f'КП для сверки контактов: {len(kp_map)} городов')
        except Exception as e:
            kp_map = None
            log(f'⚠ Не удалось загрузить КП: {e}')

        # Региональные проверки: п.1.4.1 (верные переменные) и п.1.6 (СНГ-чистота).
        # Контекст (города/номера/почты по КП) строится один раз на прогон.
        _chk_region = bool(params.get('check_region', True))
        _chk_cis = bool(params.get('check_cis', True))
        region_ctx = None
        if _chk_region or _chk_cis:
            try:
                from region_checker import build_region_context
                region_ctx = build_region_context(kp_map, src.subdomains)
                log(f'Регион-проверки: городов {len(region_ctx.city_regex)}, '
                    f'номеров КП {len(region_ctx.phone_cities)}')
            except Exception as e:
                region_ctx = None
                log(f'⚠ Регион-проверки не активны: {e}')

        _chk_idx = bool(params.get('check_indexing', True))
        _chk_meta = bool(params.get('check_meta', True))
        results = asyncio.run(run_batch(
            plan.tasks, concurrency=6, timeout_ms=120000, max_attempts=3,
            retry_delay_ms=2500, check_text=bool(params.get('check_text', True)),
            check_links=bool(params.get('check_links', False)),
            check_indexing=_chk_idx, check_meta=_chk_meta,
            check_region=_chk_region and region_ctx is not None,
            check_cis=_chk_cis and region_ctx is not None,
            check_layout=bool(params.get('check_layout', True)),
            check_markup=bool(params.get('check_markup', True)),
            check_security=bool(params.get('check_security', True)),
            check_images=bool(params.get('check_images', True)),
            region_ctx=region_ctx,
            on_progress=on_progress, proxy_url=proxy_url, kp_map=kp_map))

        _sec_bad = sum(1 for r in results
                       if getattr(r, 'has_security_issues', False))
        if _sec_bad:
            log(f'Заголовки безопасности: страниц с ошибками {_sec_bad}')

        # ── Индексация (п.1.7): кросс-проверка sitemap ↔ robots.txt ──
        # Все известные пути каталога (категории/фильтры/товары) прогоняем
        # через robots главного домена: путь в sitemap = «хочу в индекс»,
        # Disallow на нём - противоречие.
        _idx_summary = None
        if _chk_idx and _main:
            try:
                from indexing_checker import check_paths_against_robots
                _all_paths = (['/'] + list(src.categories or [])
                              + list(src.filters or [])
                              + list(src.products or []))
                _idx_summary = asyncio.run(check_paths_against_robots(
                    _main.host, _all_paths, proxy_url=proxy_url,
                    sample_category=(src.categories[0] if src.categories else None),
                    project_sitemap_url=cfg.get('sitemap_url')))
                _n_dis = len(_idx_summary.get('disallowed') or [])
                _n_junk = len(_idx_summary.get('junk_open') or [])
                _pages_closed = sum(1 for r in results
                                    if getattr(r, 'has_indexing_issues', False))
                log(f'Индексация: страниц с проблемами {_pages_closed}, '
                    f'путей каталога под Disallow {_n_dis} '
                    f'(проверено {_idx_summary.get("checked", 0)}), '
                    f'мусора не закрыто {_n_junk}')
                if _idx_summary.get('blanket_disallow'):
                    log('❌ robots.txt: есть «Disallow: /» - сайт закрыт целиком')
                _n_ac = len(_idx_summary.get('assets_closed') or [])
                if _n_ac:
                    log(f'❌ robots.txt: закрыто .css/.js файлов {_n_ac} '
                        f'из {_idx_summary.get("assets_checked", 0)}')
                # ЧПУ и формат адресов - по тем же путям, без запросов.
                from indexing_checker import check_url_format
                _uf = check_url_format(_all_paths)
                _idx_summary['url_format'] = _uf
                if _uf.get('total_bad'):
                    log(f'⚠ Формат адресов: плохих URL {_uf["total_bad"]} '
                        f'из {_uf["checked"]}')
            except Exception as _e:
                log(f'⚠ Индексация (sitemap↔robots): {_e}')

        # ── Аудит sitemap (ТЗ 3.4.2/3.4.3, часть п.1.7) ──
        if _chk_idx and _main and _idx_summary is not None:
            try:
                from sitemap_audit import (audit_sitemap, analyze_lastmod,
                                           audit_html_sitemap)
                _sm_url = (cfg.get('sitemap_url')
                           or f'https://{_main.host}/sitemap.xml')
                # «Услуги» (п.6): тех.пути проекта с ключами услуг/производства.
                _svc_keys = ('uslug', 'service', 'proizvodstvo', 'rabot')
                _services = [p for p in get_tech_paths(pid)
                             if any(k in (p or '').lower() for k in _svc_keys)]
                _audit = asyncio.run(audit_sitemap(
                    _sm_url, _main.host, proxy_url=proxy_url,
                    known_categories=src.categories,
                    known_filters=src.filters,
                    known_services=_services))
                _audit['lastmod_analysis'] = analyze_lastmod(pid, _audit)
                _audit.pop('lastmod_dates', None)   # в отчёт даты не тащим
                _idx_summary['sitemap_audit'] = _audit
                log(f'Sitemap-аудит: файлов {_audit.get("files", 0)}, '
                    f'URL {_audit.get("total", 0)}, '
                    f'битых URL {len(_audit.get("bad_urls") or [])}, '
                    f'lastmod у {_audit.get("with_lastmod", 0)}')
                if _audit.get('index_types'):
                    log(f'Sitemap-индекс: типы {", ".join(_audit["index_types"])}')
                _mc = _audit.get('missing_catalog') or {}
                _n_miss = (len(_mc.get('categories') or [])
                           + len(_mc.get('filters') or [])
                           + len(_mc.get('services') or []))
                if _n_miss:
                    log(f'❌ Sitemap: не хватает {_n_miss} категорий/фильтров/'
                        f'услуг из выгрузки')
            except Exception as _e:
                log(f'⚠ Sitemap-аудит: {_e}')
            # HTML-карта сайта (доп. чек-лист)
            try:
                _hm = asyncio.run(audit_html_sitemap(
                    _main.host, proxy_url=proxy_url))
                _idx_summary['html_sitemap'] = _hm
                if _hm.get('status') != 200:
                    log(f'⚠ HTML-карта сайта не найдена '
                        f'(HTTP {_hm.get("status")})')
                elif _hm.get('junk_links'):
                    log(f'❌ HTML-карта: служебных ссылок '
                        f'{len(_hm["junk_links"])}')
            except Exception as _e:
                log(f'⚠ HTML-карта сайта: {_e}')
            # Статус sitemap в Яндекс.Вебмастере (ТЗ 3.4.4)
            _wm_tok = creds.get('webmaster_oauth')
            if _wm_tok:
                try:
                    from webmaster_api import fetch_sitemap_status
                    _idx_summary['wm_sitemaps'] = fetch_sitemap_status(
                        pid, _wm_tok, _main.host, proxy_url,
                        lambda lvl, msg: log(msg))
                except Exception as _e:
                    log(f'⚠ Sitemap в Вебмастере: {_e}')

        # ── Тексты фильтров не дублируют родительскую категорию (п.1.6) ──
        # Сравниваем «голову» нормализованного SEO-текста фильтра с текстом
        # его категории (тот же поддомен): совпала - дубль, тегу нужен свой.
        if params.get('check_text'):
            from urllib.parse import urlsplit as _us6
            _cat_heads = {}
            for r in results:
                st6 = getattr(r, 'seo_text', None)
                if r.type_code == 'category' and st6 and st6.get('text_head'):
                    _cat_heads[(r.subdomain,
                                (_us6(r.url).path or '').rstrip('/'))] = \
                        st6['text_head']
            for r in results:
                st6 = getattr(r, 'seo_text', None)
                if r.type_code != 'filter' or not st6 \
                        or not st6.get('text_head'):
                    continue
                _p6 = (_us6(r.url).path or '')
                _parent = _p6.split('/filter/')[0].rstrip('/')
                _ph = _cat_heads.get((r.subdomain, _parent))
                if _ph and st6['text_head'] == _ph:
                    st6['warnings'].append(
                        'текст страницы-фильтра дублирует родительскую '
                        'категорию - тегу нужен свой текст')

        # ── Уникальные картинки категорий/разделов (п.1.15) ──
        # «Главная» картинка категории (og:image / первая после h1) не
        # должна повторяться на других категориях того же поддомена.
        # Находки - в cat_warnings (своя секция листа «Изображения»),
        # не в warnings: чтобы не смешивались с форматами/весом.
        if params.get('check_images', True):
            from image_checker import category_image_dups
            _cats15 = [(r.subdomain, r.url, r.images.get('cat_img'))
                       for r in results
                       if r.type_code == 'category'
                       and getattr(r, 'images', None)]
            _dup15 = {}
            for (_sub, _key), _urls in category_image_dups(_cats15).items():
                for _u in _urls:
                    _dup15[_u] = {'name': _key.rsplit('/', 1)[-1],
                                  'n': len(_urls)}
            for r in results:
                if r.type_code != 'category' \
                        or not getattr(r, 'images', None):
                    continue
                _ci15 = r.images.get('cat_img')
                _cw = r.images.setdefault('cat_warnings', [])
                if r.url in _dup15:
                    r.images['cat_dup'] = _dup15[r.url]
                    _cw.append('картинка категории не уникальна - та же '
                               'картинка на других категориях (каждому '
                               'разделу нужна своя)')
                elif _ci15 and _ci15.get('placeholder'):
                    _cw.append('у категории вместо своей картинки заглушка '
                               '(no-photo/placeholder)')

        # ── Метаданные и дубли (п.1.8) ──
        # Дубли title/description/H1 - по всем результатам прогона; дубли
        # УРЛОВ - прозвон вариантов (http/слэш/www) главной и каталога
        # каждого проверенного поддомена.
        _meta_summary = None
        if _chk_meta:
            try:
                from meta_checker import (find_duplicates, check_url_duplicates,
                                          check_test_domains)
                _dups = find_duplicates(results)
                _probe_urls = [r.url for r in results
                               if r.is_ok and r.type_code in ('main', 'catalog')]
                _url_dups = asyncio.run(check_url_duplicates(
                    _probe_urls, proxy_url=proxy_url))
                # Тестовые домены (test./dev./stage.…) корневого домена.
                _tdoms = asyncio.run(check_test_domains(
                    _main.host, proxy_url=proxy_url)) if _main else []
                _n_td_open = sum(1 for t in _tdoms
                                 if t['state'] == 'indexable')
                if _n_td_open:
                    log(f'❌ Индексируемых тестовых доменов: {_n_td_open} '
                        f'({", ".join(t["host"] for t in _tdoms if t["state"] == "indexable")})')
                _meta_summary = {'duplicates': _dups, 'url_duplicates': _url_dups,
                                 'test_domains': _tdoms,
                                 'probed_urls': len(_probe_urls)}
                _m_pages_bad = sum(1 for r in results
                                   if getattr(r, 'has_meta_issues', False)
                                   or getattr(r, 'has_meta_unique_issues', False))
                _n_dup = sum(1 for d in _url_dups
                             if d.get('problem') != 'not_301')
                log(f'Метаданные: страниц с проблемами {_m_pages_bad}, '
                    f'дублей в городе {len(_dups["same_city"])}, '
                    f'межгородских {len(_dups["cross_city"])}, '
                    f'дублей URL {_n_dup}, '
                    f'временных редиректов {len(_url_dups) - _n_dup}')
            except Exception as _e:
                log(f'⚠ Метаданные/дубли: {_e}')

        finished_ms = int(datetime.now().timestamp() * 1000)
        out['finished_at'] = finished_ms
        save_history(pid, list({urlparse(r.url).path for r in results}))

        report_filename = make_report_filename(pid, started_ms, REPORTS_DIR)
        report_path = REPORTS_DIR / report_filename
        _today_404 = None   # отчёт 404 из Метрика-API
        _404_goal = None    # есть ли в Метрике цель на отслеживание 404
        _nlog = lambda lvl, msg: log(msg)
        # Прокси для почты/Метрики/Вебмастера - тот же, что и для страниц: с
        # учётом use_proxy проекта. Иначе при use_proxy=false сбор всё равно лез
        # через (возможно мёртвый) прокси из secrets, хотя страницы шли напрямую.
        _proxy = proxy_url
        # ── Сбор почты/Метрики ДО сборки отчёта - чтобы отчёт сразу полный ──
        if params['fetch_notifications']:
            log('Собираю уведомления из почты…')

            _yw_e, _yw_p = creds.get('metrika') or (None, None)
            _yw_cfg = WEBMASTER_YANDEX_CONFIG.get(pid)
            if _yw_e and _yw_p and _yw_cfg:
                try:
                    fetch_webmaster_yandex(pid, _yw_e, _yw_p, _yw_cfg['folder'], _nd, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ Вебмастер: {_e}')
            else:
                log(f'⚠ Вебмастер: креды не найдены (metrika_{pid}_*)')

            _gsc_e, _gsc_p = creds.get('gsc') or (None, None)
            if _gsc_e and _gsc_p:
                log(f'GSC: креды найдены ({_gsc_e})…')
                try:
                    fetch_gsc_gmail(pid, _gsc_e, _gsc_p, _nd, _nlog)
                except Exception as _e:
                    log(f'⚠ GSC: {_e}')
            else:
                log(f'⚠ GSC: креды не найдены (gsc_{pid}_*)')

            _yab_e, _yab_p, _yab_f = creds.get('yab') or (None, None, None)
            if _yab_e and _yab_p and _yab_f:
                try:
                    fetch_yandex_folder_simple(pid, _yab_e, _yab_p, _yab_f, 'ya_business', _nd, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ Я.Бизнес: {_e}')

            _tg_e, _tg_p, _tg_f = creds.get('twogis') or (None, None, None)
            if _tg_e and _tg_p and _tg_f:
                try:
                    fetch_yandex_folder_simple(pid, _tg_e, _tg_p, _tg_f, 'twogis', _nd, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ 2ГИС: {_e}')

            _ga_e, _ga_p = creds.get('google') or (None, None)
            if _ga_e and _ga_p:
                try:
                    fetch_google_accounts(pid, _ga_e, _ga_p, _nd, _nlog)
                except Exception as _e:
                    log(f'⚠ Google: {_e}')

            # Папка GSC в Яндекс-почте («Гугл» / «Google Search Console») -
            # то же содержимое, что GSC → пишем в source 'gsc', классифицируем.
            _gf_e, _gf_p, _gf_f = creds.get('google_folder') or (None, None, None)
            if _gf_e and _gf_p and _gf_f:
                log(f'GSC-папка «{_gf_f}»: собираю…')
                try:
                    fetch_yandex_folder_simple(pid, _gf_e, _gf_p, _gf_f, 'gsc',
                                               _nd, _proxy, _nlog, classify=True)
                except Exception as _e:
                    log(f'⚠ GSC-папка: {_e}')

            # 404-отчёты из почты Метрики (та же учётка metrika_{pid}, своя папка)
            _mb_cfg = MAILBOX_CONFIG.get(pid)
            if _yw_e and _yw_p and _mb_cfg:
                log(f'Метрика-404: собираю отчёты за {_nd} дн…')
                try:
                    _msum = fetch_incremental(
                        project_id=pid, email_addr=_yw_e, password=_yw_p,
                        folder=_mb_cfg['folder'], proxy_url=_proxy,
                        lookback_days=_nd, log=_nlog)
                    log(f'✓ Метрика-404: новых отчётов {_msum.get("fetched", 0)}')
                except Exception as _e:
                    log(f'⚠ Метрика-404: {_e}')
            else:
                log(f'⚠ Метрика-404: креды/почта не найдены (metrika_{pid}_*)')

            # Ошибки сайтов из Яндекс.Вебмастера (официальный API v4)
            _wm_token = creds.get('webmaster_oauth')
            if _wm_token:
                log('Вебмастер-API: тяну диагностику сайтов…')
                try:
                    fetch_webmaster_issues(pid, _wm_token, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ Вебмастер-API: {_e}')
            else:
                _wm_keys = creds.get('webmaster_keys_hint') or []
                log(f'⚠ Вебмастер-API: токен не задан (ожидаю секрет '
                    f'yandex_oauth_{pid}). '
                    f'Найденные похожие ключи в секретах: {_wm_keys or "нет"}')

        else:
            log('Сбор уведомлений выключен.')

        # ── 404 из Метрики (API) - отдельная галка со своим периодом ──
        _m404_disp = None
        if params.get('fetch_metrika_404', True):
            _mt_token = creds.get('metrika_oauth')
            _d1 = params.get('metrika_404_date1') or '7daysAgo'
            _d2 = params.get('metrika_404_date2') or 'today'
            if _mt_token:
                log(f'Метрика-API: тяну 404 за {_d1}…{_d2}…')
                try:
                    from metrika_api import fetch_today_404
                    _today_404 = fetch_today_404(
                        pid, _mt_token, _proxy, _nlog,
                        counter=creds.get('metrika_counter'),
                        date1=_d1, date2=_d2)
                    _m404_disp = _metrika_period_display(_d1, _d2)
                except Exception as _e:
                    log(f'⚠ Метрика-API: {_e}')
                # Цель на 404 - живым запросом к конфигурации счётчика (не
                # старый выгруженный каталог, который мог устареть) - та же
                # галочка, тот же токен/счётчик, лишнего запроса на боевой
                # сайт не делает (только к API Метрики).
                try:
                    from metrika_api import has_404_goal
                    _404_goal = has_404_goal(
                        pid, _mt_token, _proxy,
                        counter=creds.get('metrika_counter'), log=_nlog)
                except Exception as _e:
                    log(f'⚠ Метрика-API (цель 404): {_e}')
            else:
                log(f'⚠ Метрика-API: токен не задан (metrika_oauth_{pid})')
        else:
            log('Сбор 404 из Метрики выключен.')

        # ── Автокликер - блокирует до перекликивания всех ошибок.
        # Локальный Chrome в приоритете, иначе облачный headless с сессией. ──
        _autoclick = None
        if params.get('autoclick'):
            _autoclick = _run_autoclicker(
                pid, params, log,
                session_b64=creds.get('autoclick_session'))

        # ── Фильтр-тест товаров (доп. чек-лист) - тяжёлый браузер, по галочке ──
        # Только листинги-КАТЕГОРИИ (последние во вложенности, где есть
        # товары и фильтр). НЕ верхний каталог (type 'catalog' - там
        # подкатегории, фильтровать нечего) и НЕ карточки товаров.
        _filters_test = None
        if params.get('check_filter_fn'):
            _cat_urls = [r.url for r in results if r.is_ok
                         and r.type_code == 'category']
            _filters_test = _run_filters_test(pid, params, log,
                                              category_urls=_cat_urls)

        # ── Ошибки JS в консоли (п.1.14) - браузер по страницам прогона ──
        _console_check = None
        if params.get('check_console'):
            _console_urls = [r.url for r in results if r.is_ok]
            if _console_urls:
                _console_check = _run_console_check(pid, _console_urls, log)

        # ── Валидация W3C + скорость (1.16) и сжатие/кеш статики (1.17) ──
        # Обе по выборке страниц (главная/категория/товар); ресурсы качаем
        # один раз и делим между проверками. Внешние W3C-сервисы медленные,
        # поэтому 1.16/1.17 - по отдельным галочкам.
        _w3c_check = None
        _want_valid = bool(params.get('check_w3c'))
        _want_static = bool(params.get('check_static'))
        if _want_valid or _want_static:
            def _first(tc):
                return next((r.url for r in results
                             if r.is_ok and r.type_code == tc), None)
            _w3c_urls = [u for u in (_first('main'), _first('category'),
                                     _first('product')) if u]
            if _w3c_urls:
                try:
                    from w3c_perf import check_pages as _w3c_pages
                    _what = ('Валидация W3C + скорость' if _want_valid
                             and not _want_static else
                             'Валидация W3C + скорость + сжатие/кеш'
                             if _want_valid else 'Сжатие + кеш статики')
                    log(f'{_what}: {len(_w3c_urls)} страниц…')
                    _w3c_check = _w3c_pages(_w3c_urls, proxy_url,
                                            log=lambda m: log(m),
                                            want_valid=_want_valid,
                                            want_static=_want_static)
                    # Понятный сигнал, если W3C не проверил валидность
                    # (Cloudflare 403 / лимит 429 / сбой сервера 502).
                    _pp = (_w3c_check or {}).get('pages') or []
                    _errs = [str((p.get('html') or {}).get('error') or '')
                             or str((p.get('css') or {}).get('error') or '')
                             for p in _pp
                             if (p.get('html') or {}).get('error')
                             or (p.get('css') or {}).get('error')]
                    _reason = _errs[0] if _errs else 'лимит/блок'
                    if _pp and len(_errs) >= len(_pp):
                        log(f'⚠ W3C НЕ проверил валидность HTML/CSS: {_reason}. '
                            'Скорость ресурсов измерена. Повторить проверку 1.16 '
                            'позже (через час или на след. день).')
                    elif _errs:
                        log(f'⚠ W3C: часть страниц не проверена ({_reason}): '
                            f'{len(_errs)} из {len(_pp)}.')
                except Exception as _e:
                    log(f'⚠ Валидация W3C: {_e}')

        # ── Страница 404 (п.1.18) - главный домен + один поддомен ──
        # Шаблон 404 сквозной: все города гонять незачем, 2 хоста хватает.
        _p404_check = None
        if params.get('check_404'):
            def _city_cat(city):
                return next((r.url for r in results
                             if r.is_ok and r.type_code == 'category'
                             and r.city == city), None)
            _mains = [(r.city, r.url, _city_cat(r.city)) for r in results
                      if r.is_ok and r.type_code == 'main']
            _pick = _mains[:1] + ([_mains[-1]] if len(_mains) > 1 else [])
            if _pick:
                try:
                    from page404_checker import check_404_pages
                    log(f'Страница 404: проверяем {len(_pick)} хост(а)…')
                    _p404_check = asyncio.run(check_404_pages(
                        _pick, proxy_url=proxy_url))
                    _n_bad = sum(1 for h in _p404_check.get('hosts', [])
                                 if h.get('issues'))
                    _n_warn = sum(1 for h in _p404_check.get('hosts', [])
                                  if h.get('warnings'))
                    log(f'Страница 404: ошибок {_n_bad}, '
                        f'с предупреждениями {_n_warn}')
                except Exception as _e:
                    log(f'⚠ Страница 404: {_e}')

        # ── 404 среди страниц В ИНДЕКСЕ (регулярный мониторинг) ──
        # Браузер (сохранённая сессия) сам заходит на «Страницы в поиске»
        # каждого хоста в Вебмастере и качает выгрузку - там уже есть код
        # ответа (httpCode) и статус. Отмечаем строки 404/410/5xx/HTTP_ERROR.
        # Ничего на боевом сайте не пингуем - данные из выгрузки Яндекса.
        _index_404 = None
        if params.get('check_index_404'):
            import time as _time
            _t404 = _time.monotonic()
            # Источник 1 — Яндекс.Вебмастер: браузер качает выгрузку «Страницы
            # в поиске» (код ответа уже в ней, боевой сайт не пингуем).
            _wm_404 = _run_index404_download(
                pid, params, log, session_b64=creds.get('autoclick_session'))
            if _wm_404.get('error'):
                log(f'⚠ 404 в индексе (Яндекс): {_wm_404["error"]}')
            # Источник 2 — Sitemap: слепой прозвон всех URL из sitemap. По
            # умолчанию ВЫКЛ: он медленный (сайт тормозит на страницах фильтров)
            # и почти всегда находит только таймауты, а не реальные 404 -
            # sitemap и так чистый. Реальную пользу даёт перепроверка кандидатов
            # от Яндекса/Google ниже. Включается флагом index_404_sitemap.
            _sm_404 = None
            if params.get('index_404_sitemap', False):
                try:
                    from index_sitemap_checker import check_sitemap_404
                    log('404 в индексе: проверяю sitemap (порция с ротацией)…')
                    _sm_404 = check_sitemap_404(
                        pid, proxy_url=proxy_url,
                        max_urls=int(params.get('index_404_sitemap_max', 1000)),
                        log=_nlog)
                    if _sm_404.get('error'):
                        log(f'⚠ 404 в индексе (sitemap): {_sm_404["error"]}')
                    else:
                        log(f'404 в индексе (sitemap): проверено '
                            f'{_sm_404.get("total_checked", 0)}, битых '
                            f'{_sm_404.get("total_dead", 0)}')
                except Exception as _e:
                    log(f'⚠ 404 в индексе (sitemap): {_e}')
            # Источник 3 — Google Search Console: браузер экспортирует причины
            # «Не найдено (404)» и «Ошибка сервера (5xx)». Domain-ресурс покрывает
            # и поддомены, поэтому ловит 404, которых нет у Яндекса/в sitemap.
            _gsc_404 = None
            if params.get('index_404_gsc', True):
                _gsc_404 = _run_gsc_index404(
                    pid, params, log, session_b64=creds.get('autoclick_session'),
                    gsc_login=creds.get('gsc'))
                if _gsc_404.get('error'):
                    log(f'⚠ 404 в индексе (GSC): {_gsc_404["error"]}')
                else:
                    log(f'404 в индексе (GSC): битых {_gsc_404.get("total_dead", 0)}')
            # Источник 4 — Google Search Console через API (сервисный аккаунт).
            # В отличие от браузерного источника выше, работает на облаке и в
            # расписании без Chrome/сессии: берём список проиндексированных
            # страниц (Search Analytics) и прозваниваем их на 404 нашим чекером.
            _gsc_api_404 = None
            _gsc_sa = creds.get('gsc_sa')
            if _gsc_sa:
                try:
                    from index_gsc_api import check_gsc_api_404
                    log('404 в индексе (Google API): беру список страниц из '
                        'Search Console…')
                    _gsc_api_404 = check_gsc_api_404(
                        pid, _gsc_sa, proxy_url=proxy_url,
                        max_urls=int(params.get('index_404_gsc_api_max', 3000)),
                        days=int(params.get('index_404_gsc_api_days', 90)),
                        log=_nlog)
                    if _gsc_api_404.get('error'):
                        log(f'⚠ 404 в индексе (Google API): {_gsc_api_404["error"]}')
                    else:
                        log(f'404 в индексе (Google API): проверено '
                            f'{_gsc_api_404.get("total_checked", 0)}, битых '
                            f'{_gsc_api_404.get("total_dead", 0)}')
                except Exception as _e:
                    log(f'⚠ 404 в индексе (Google API): {_e}')
            # Слияние источников в одну таблицу отчёта.
            from index_export_parser import merge_index_404
            _index_404 = merge_index_404(_wm_404, _sm_404, _gsc_404, _gsc_api_404)
            # Живая перепроверка: оставляем только страницы, которые ПРЯМО СЕЙЧАС
            # отдают 404/5xx. Убирает ложные (уже починили → 200; медленные →
            # таймаут). Так в отчёте нет ссылок, которые на самом деле открываются.
            if _index_404 and _index_404.get('available') and params.get('index_404_reverify', True):
                try:
                    from index_reverify import reverify_index_404
                    _index_404 = reverify_index_404(_index_404, proxy_url=proxy_url, log=_nlog)
                except Exception as _e:
                    log(f'⚠ 404 в индексе (перепроверка): {_e}')
            if _index_404 and not _index_404.get('error'):
                log(f'404 в индексе (итог за {int(_time.monotonic() - _t404)}с): '
                    f'проверено {_index_404.get("total_checked", 0)}, битых 404/410 '
                    f'{_index_404.get("total_dead", 0)}, '
                    f'источники: {", ".join(_index_404.get("sources") or []) or "—"}')

        # ── Поиск по сайту находит категории и теги (чек-лист) ──
        # Категория - случайная из прогона; тег (страница-фильтр) - случайный
        # из прогона, а если фильтров в выборке нет - из базы каталога.
        _search_check = None
        if params.get('check_layout'):
            import random as _rnd
            _s_cats = [r.url for r in results
                       if r.is_ok and r.type_code == 'category']
            _s_cat = _rnd.choice(_s_cats) if _s_cats else None
            _s_flts = [r.url for r in results
                       if r.is_ok and r.type_code == 'filter']
            _s_flt = _rnd.choice(_s_flts) if _s_flts else None
            _tag_note = None
            if _s_flt is None and _main is not None:
                _base_flts = list(src.filters or [])
                if _base_flts:
                    _s_flt = f'https://{_main.host}' + _rnd.choice(_base_flts)
                else:
                    _tag_note = ('страниц-фильтров (тегов) у проекта нет - '
                                 'проверка тега не применима')
            if _s_cat:
                try:
                    from search_check import check_search
                    _search_check = asyncio.run(
                        check_search(_s_cat, filter_url=_s_flt,
                                     proxy_url=proxy_url))
                    if _tag_note:
                        _search_check['tag_note'] = _tag_note
                    if _search_check.get('available'):
                        log('Поиск по сайту: категория в выдаче - '
                            + ('да' if _search_check.get('found_category')
                               else 'НЕТ (только товары?)'))
                    else:
                        log(f'⚠ Поиск по сайту: '
                            f'{_search_check.get("error", "не проверился")}')
                except Exception as _e:
                    log(f'⚠ Поиск по сайту: {_e}')

        # ── Фильтры поисковых систем (п.1.19) - санкции/ручные меры ──
        # Яндекс: санкционные сигналы из диагностики Вебмастера (кеш этого
        # прогона, если сбор включён). Google: маркеры ручных мер в почтовых
        # уведомлениях GSC за 90 дней. Текстовый анализ риска НЕ делаем.
        _ps_filters = None
        if params.get('check_ps_filters'):
            try:
                from webmaster_api import (load_issues as _li,
                                           filter_sanctions, GSC_SANCTION_RE)
                # Пункт самодостаточен: если общий сбор уведомлений выключен,
                # дособираем источники сами (диагностика Вебмастера по API +
                # почта GSC за 90 дней). Включён - кеш уже свежий.
                _wm_fresh = bool(params['fetch_notifications'])
                if not params['fetch_notifications']:
                    _wm_token = creds.get('webmaster_oauth')
                    if _wm_token:
                        log('Фильтры ПС: тяну диагностику Вебмастера…')
                        try:
                            fetch_webmaster_issues(pid, _wm_token, _proxy, _nlog)
                            _wm_fresh = True
                        except Exception as _e:
                            log(f'⚠ Фильтры ПС (Вебмастер-API): {_e}')
                    _gsc_e, _gsc_p = creds.get('gsc') or (None, None)
                    if _gsc_e and _gsc_p:
                        log('Фильтры ПС: собираю почту GSC за 90 дней…')
                        try:
                            fetch_gsc_gmail(pid, _gsc_e, _gsc_p, 90, _nlog)
                        except Exception as _e:
                            log(f'⚠ Фильтры ПС (почта GSC): {_e}')
                    _gf_e, _gf_p, _gf_f = (creds.get('google_folder')
                                           or (None, None, None))
                    if _gf_e and _gf_p and _gf_f:
                        try:
                            fetch_yandex_folder_simple(
                                pid, _gf_e, _gf_p, _gf_f, 'gsc', 90,
                                _proxy, _nlog, classify=True)
                        except Exception as _e:
                            log(f'⚠ Фильтры ПС (GSC-папка): {_e}')
                _wm_all = _li(pid) or []
                _sanc = filter_sanctions(_wm_all)
                from webmaster_notify import load_notifications as _ln90
                _gsc_mail = _ln90(pid, 'gsc', 90) or []
                _gsc_hits = [
                    {'date': n.date, 'subject': n.subject}
                    for n in _gsc_mail
                    if GSC_SANCTION_RE.search(
                        f'{n.subject} {n.body_preview}')]
                _ps_filters = {
                    'yandex': _sanc,
                    'wm_issues_total': len(_wm_all),
                    'wm_hosts': len({getattr(i, 'host', '') for i in _wm_all}),
                    'wm_collected': _wm_fresh,
                    'gsc_hits': _gsc_hits,
                    'gsc_scanned': len(_gsc_mail),
                }
                log(f'Фильтры ПС: санкций Яндекса {len(_sanc)}, '
                    f'маркеров в почте GSC {len(_gsc_hits)} '
                    f'(писем просмотрено {len(_gsc_mail)})')
            except Exception as _e:
                log(f'⚠ Фильтры ПС: {_e}')

        # ── Lite-проверка ссылочного профиля (по галочке) ──
        # Официальные данные Яндекс.Вебмастера (links/external): объём,
        # доноры, динамика, спам. Тот же OAuth-токен, что и диагностика.
        _link_profile = None
        if params.get('check_link_profile'):
            _lp_token = creds.get('webmaster_oauth')
            if _lp_token:
                try:
                    from link_profile import fetch_link_profile
                    log('Ссылочный профиль: тяну данные Вебмастера…')
                    _link_profile = fetch_link_profile(
                        pid, _lp_token, _proxy, log=lambda m: log(m))
                    _lp_hosts = (_link_profile or {}).get('hosts') or []
                    _lp_warn = sum(len(h.get('warnings') or [])
                                   for h in _lp_hosts)
                    log(f'✓ Ссылочный профиль: хостов {len(_lp_hosts)}, '
                        f'предупреждений {_lp_warn}')
                except Exception as _e:
                    log(f'⚠ Ссылочный профиль: {_e}')
            else:
                log('⚠ Ссылочный профиль: OAuth-токен Вебмастера не задан '
                    '(webmaster_oauth) - пропуск.')
                _link_profile = {'available': False,
                                 'note': 'OAuth-токен Вебмастера не задан.'}

        # ── Настройки в админке (доп. чек-лист, по галочке) ──
        # Пункт 1: функции настройки работают (рендер разделов админки).
        # Пункт 2 (admin_crud): CRUD поддоменов/категорий (симуляция + запись
        # с откатом при admin_execute). Креды приходят из UI/Secrets.
        _admin_settings = None
        if (params.get('check_admin_settings') or params.get('admin_crud')
                or params.get('admin_product_crud')
                or params.get('admin_tech_crud')
                or params.get('admin_counters')):
            _adm_creds = creds.get('admin_settings') or {}
            if _adm_creds.get('login') and _adm_creds.get('domain'):
                _admin_settings = _run_admin_settings(pid, params, creds, log)
                _adm_checks = (_admin_settings or {}).get('checks') or []
                _adm_bad = sum(1 for c in _adm_checks if not c.get('ok'))
                log(f'✓ Настройки в админке: проверок {len(_adm_checks)}, '
                    f'провалов {_adm_bad}')
            else:
                log('⚠ Настройки в админке: не заданы домен/логин/пароль '
                    'админки - пропуск.')
                _admin_settings = {'available': False,
                                   'note': 'Не заданы домен/логин/пароль '
                                           'админки (поля в блоке «Админка» '
                                           'или секрет admin_settings_<pid>).'}

        # ── Ошибки сервера: парсинг / нагрузка / дубли URL (по галочке) ──
        # Тяжёлые сетевые пробы на прод: гоняем В КОНЦЕ, отчёт по страницам
        # уже собран - падение сервера/бан здесь = находка, не сбой прогона.
        _stress_check = None
        if params.get('check_stress'):
            try:
                from stress_checker import run_stress_check
                _ok = [r for r in results if r.is_ok]

                def _first_ok(tc):
                    return next((r for r in _ok if r.type_code == tc), None)
                # Парсинг - выборка каталога (категории/фильтры/товары).
                _parse = [r.url for r in _ok
                          if r.type_code in ('category', 'filter', 'product')]
                # Нагрузка - N репрезентативных страниц. Сначала по одной
                # каждого типа (главная/категория/фильтр/товар - разнообразие),
                # потом добираем остальными до нужного числа.
                _n_load = max(1, int(params.get('stress_load_pages', 3)))
                _load_rs, _seen = [], set()
                for _tc in ('main', 'category', 'filter', 'product'):
                    _r = _first_ok(_tc)
                    if _r and _r.url not in _seen:
                        _load_rs.append(_r)
                        _seen.add(_r.url)
                for _r in _ok:
                    if len(_load_rs) >= _n_load:
                        break
                    if _r.url not in _seen:
                        _load_rs.append(_r)
                        _seen.add(_r.url)
                _load_rs = _load_rs[:_n_load]
                _load_pages = [r.url for r in _load_rs]
                _baselines = {r.url: r.elapsed_ms for r in _load_rs
                              if r.elapsed_ms}
                # Дубли - по одному образцу каждого типа каталога.
                _dup = [(tc, _first_ok(tc).url)
                        for tc in ('category', 'filter', 'product')
                        if _first_ok(tc)]
                _conc = int(params.get('stress_concurrency', 30))
                if _parse or _load_pages or _dup:
                    log('Ошибки сервера (парсинг/нагрузка/дубли): '
                        f'параллельность {_conc}…')
                    _stress_check = asyncio.run(run_stress_check(
                        parse_urls=_parse, load_pages=_load_pages,
                        dup_samples=_dup, baselines=_baselines,
                        proxy_url=proxy_url, concurrency=_conc,
                        log=lambda m: log(m)))
                    _p = (_stress_check or {}).get('parsing') or {}
                    _l = (_stress_check or {}).get('load') or {}
                    _d = (_stress_check or {}).get('duplicates') or {}
                    _n5 = (len(_p.get('server_errors') or [])
                           + sum(pg.get('server_5xx', 0)
                                 for pg in (_l.get('pages') or []))
                           + len(_d.get('server_errors') or []))
                    log(f'✓ Ошибки сервера: 5xx-находок {_n5}'
                        + (', БАН на парсинге' if _p.get('banned') else ''))
            except Exception as _e:
                log(f'⚠ Ошибки сервера (стресс-пробы): {_e}')

        # ── Загружаем из кеша и строим отчёт ОДИН раз (сразу полный) ──
        # Кеш почты/Метрики/сервисов подтягиваем ТОЛЬКО при включённых
        # галочках: выключил сбор - листов «Уведомления» / «404 из Метрики» /
        # «Ошибки сервисов» в отчёте нет (раньше данные прошлых прогонов
        # вылезали из кеша даже с выключенными галочками).
        if params['fetch_notifications']:
            _notifs = (
                load_notifications(pid, 'yandex_webmaster', _nd)
                + load_notifications(pid, 'gsc', _nd)
                + load_notifications(pid, 'ya_business', _nd)
                + load_notifications(pid, 'twogis', _nd)
                + load_notifications(pid, 'google_accounts', _nd)
            )
            _service_issues = load_issues(pid) or None
            # Почтовые 404-отчёты Метрики собираются вместе с почтой
            _metrika_reports = load_reports_for_period(pid, _nd) or []
        else:
            _notifs, _service_issues, _metrika_reports = [], None, []
        if _today_404:                       # сегодняшние 404 (API) - первыми
            _metrika_reports = [_today_404] + list(_metrika_reports)
        _metrika_reports = _metrika_reports or None
        build_report(
            project_name=cfg['name'], started_at_ms=started_ms,
            finished_at_ms=finished_ms,
            selected_subdomains=plan.selected_subdomains, results=results,
            # None = сбор выключен (листа «Уведомления» не будет);
            # [] = сбор включён, писем нет (лист с заглушкой останется)
            output_path=report_path,
            notifications=(_notifs if params['fetch_notifications'] else None),
            metrika_reports=_metrika_reports,
            metrika_data_date=(_m404_disp if _today_404
                               else get_latest_available_date(pid)),
            metrika_404_goal=_404_goal,
            service_issues=_service_issues, autoclick=_autoclick,
            indexing_summary=_idx_summary, meta_summary=_meta_summary,
            filters_test=_filters_test, console_check=_console_check,
            w3c_check=_w3c_check, p404_check=_p404_check,
            ps_filters=_ps_filters, search_check=_search_check,
            index_404_check=_index_404,
            stress_check=_stress_check, link_profile=_link_profile,
            admin_settings=_admin_settings)
        _m_pages = sum(r.total_pages for r in (_metrika_reports or []))
        log(f'✓ Отчёт собран: уведомлений {len(_notifs)}, '
            f'404-страниц {_m_pages}, ошибок сервисов {len(_service_issues or [])}')

        # Критические ошибки (п.4.3) - выделяем для срочного уведомления и для
        # блока в подписи к отчёту (по всем городам).
        crit = analyze_critical(results)
        if crit.has_any:
            log(f'Критических находок: {crit.total} '
                f'(падений доступности: {len(crit.availability)})')

        # Telegram (полный отчёт - почта/метрика уже собраны выше)
        tg_token = creds.get('tg_token')
        tg_recipients = creds.get('tg_recipients') or []
        if tg_token and tg_recipients:
            # Срочное ОТДЕЛЬНОЕ сообщение о падении доступности - ДО отчёта.
            if crit.has_availability:
                _alert = format_critical_alert(cfg['name'], crit.availability)
                _a_sent = 0
                for _cid in tg_recipients:
                    try:
                        send_message(tg_token, str(_cid).strip(), _alert, proxy_url=proxy_url)
                        _a_sent += 1
                    except Exception as _e:
                        log(f'⚠ Срочное не доставлено в {_cid}: {_e}')
                log(f'🔴 Срочное о падении доступности: отправлено {_a_sent} из {len(tg_recipients)}')
            log(f'Отправляю отчёт в Telegram ({len(tg_recipients)})…')
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
                    critical_block=format_critical_block(crit),
                    indexing_issues_pages=sum(
                        1 for r in results
                        if getattr(r, 'has_indexing_issues', False)),
                    meta_issues_pages=sum(
                        1 for r in results
                        if getattr(r, 'has_meta_issues', False)
                        or getattr(r, 'has_meta_unique_issues', False)),
                    meta_duplicates=(
                        len((_meta_summary or {}).get('duplicates', {}).get('same_city', []))
                        + len((_meta_summary or {}).get('duplicates', {}).get('cross_city', []))
                        + sum(1 for d in (_meta_summary or {}).get('url_duplicates', [])
                              if d.get('problem') != 'not_301')),
                    layout_issues_pages=sum(
                        1 for r in results
                        if getattr(r, 'has_layout_issues', False)),
                    markup_issues_pages=sum(
                        1 for r in results
                        if getattr(r, 'has_markup_issues', False)),
                    index_404_dead=((_index_404 or {}).get('total_dead', 0)
                                    + (_index_404 or {}).get('total_soft', 0)))
                tg_result = send_run_notification(
                    bot_token=tg_token, recipients=tg_recipients,
                    project_name=cfg['name'], summary_text=summary_text,
                    report_file=report_path, proxy_url=proxy_url,
                    log=lambda lvl, msg: log(msg))
                log(f'✓ Telegram: отправлено {tg_result["sent"]}, не доставлено {tg_result["failed"]}')
            except Exception as e:
                log(f'⚠ Telegram-отправка упала: {e}')
        else:
            log('Telegram не настроен.')

        out['results'] = results
        out['report_path'] = str(report_path)
        progress(1.0, 'Готово')

    except Exception as e:
        out['error'] = str(e)
        log(f'❌ Ошибка: {e}')
        if out['finished_at'] is None:
            out['finished_at'] = int(datetime.now().timestamp() * 1000)
    return out
