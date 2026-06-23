"""
runner_30min.py – логика прогона 30-мин чек-листа БЕЗ Streamlit.

Используется фоновым подпроцессом checklist_run.py, чтобы тяжёлая async-работа
(run_batch на aiohttp) шла в отдельном ПРОЦЕССЕ – надёжно, в отличие от потока
внутри Streamlit. Возвращает результаты, путь отчёта и т.п.
"""
import asyncio
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
    """Человекочитаемый период: «18.06.2026» или «12.06.2026 – 18.06.2026»."""
    a, b = _resolve_metrika_date(d1), _resolve_metrika_date(d2)
    if not a or not b:
        return None
    fa, fb = a.strftime('%d.%m.%Y'), b.strftime('%d.%m.%Y')
    return fa if fa == fb else f'{fa} – {fb}'


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


def _run_autoclicker(pid, params, log):
    """Прокликать ошибки (Вебмастер/ГСК) залогиненным локальным Chrome и
    дождаться завершения. Возвращает dict для листа «Автокликер»."""
    import subprocess
    import sys as _sys
    root = Path(__file__).parent
    if not _cdp_alive():
        log('⚠ Автокликер: Chrome/CDP 9222 не запущен — пропускаю '
            '(нужен локальный залогиненный браузер).')
        return {'available': False,
                'note': 'Chrome/CDP 9222 не запущен — автокликер пропущен. '
                        'Нужен локальный залогиненный браузер (вкладка '
                        '«Автокликеры» → «Открыть браузер для входа»).'}
    args = [_sys.executable, 'autoclick_run.py', '--project', pid]
    if params.get('autoclick_wm'):
        args.append('--wm')
    if params.get('autoclick_gsc'):
        args.append('--gsc')
    # Чистим прошлый лог Вебмастера — чтобы парсить только текущий прогон.
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
            errors='replace')
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


def run_check(pid, params, creds, log, progress):
    """Выполнить прогон. log(msg), progress(frac, text) – колбэки.
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
            rotation_history=recent,
        )
        # Свой список URL – добавляем к выборке проекта (тип по адресу).
        _custom = params.get('custom_urls') or []
        if _custom:
            try:
                extra = build_custom_tasks_typed(_custom, src)
                if extra:
                    plan.tasks.extend(extra)
                    log(f'Свой список URL: добавлено {len(extra)}')
            except Exception as e:
                log(f'⚠ Свой список URL не разобран: {e}')

        # Технические страницы (на главном домене) – проверяем ВСЕГДА, при любом
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
                        # Спецпредложения – это листинг товаров, проверяем как раздел.
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
                     f'Проверено {done} из {total_n} – '
                     f'✅ {counters["ok"]} · ⚠ {counters["warn"]} · ❌ {counters["err"]}')

        try:
            from kp import load_kp
            kp_map = load_kp(pid) or None
            if kp_map:
                log(f'КП для сверки контактов: {len(kp_map)} городов')
        except Exception as e:
            kp_map = None
            log(f'⚠ Не удалось загрузить КП: {e}')

        results = asyncio.run(run_batch(
            plan.tasks, concurrency=6, timeout_ms=120000, max_attempts=3,
            retry_delay_ms=2500, check_text=bool(params.get('check_text', True)),
            check_links=bool(params.get('check_links', False)),
            on_progress=on_progress, proxy_url=proxy_url, kp_map=kp_map))

        finished_ms = int(datetime.now().timestamp() * 1000)
        out['finished_at'] = finished_ms
        save_history(pid, list({urlparse(r.url).path for r in results}))

        report_filename = make_report_filename(pid, started_ms, REPORTS_DIR)
        report_path = REPORTS_DIR / report_filename
        _today_404 = None   # отчёт 404 из Метрика-API
        _nlog = lambda lvl, msg: log(msg)
        # Прокси для почты/Метрики/Вебмастера – тот же, что и для страниц: с
        # учётом use_proxy проекта. Иначе при use_proxy=false сбор всё равно лез
        # через (возможно мёртвый) прокси из secrets, хотя страницы шли напрямую.
        _proxy = proxy_url
        # ── Сбор почты/Метрики ДО сборки отчёта – чтобы отчёт сразу полный ──
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

            # Папка GSC в Яндекс-почте («Гугл» / «Google Search Console») —
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

        # ── 404 из Метрики (API) — отдельная галка со своим периодом ──
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
            else:
                log(f'⚠ Метрика-API: токен не задан (metrika_oauth_{pid})')
        else:
            log('Сбор 404 из Метрики выключен.')

        # ── Автокликер (локально) — блокирует до перекликивания всех ошибок ──
        _autoclick = None
        if params.get('autoclick'):
            _autoclick = _run_autoclicker(pid, params, log)

        # ── Загружаем из кеша и строим отчёт ОДИН раз (сразу полный) ──
        _notifs = (
            load_notifications(pid, 'yandex_webmaster', _nd)
            + load_notifications(pid, 'gsc', _nd)
            + load_notifications(pid, 'ya_business', _nd)
            + load_notifications(pid, 'twogis', _nd)
            + load_notifications(pid, 'google_accounts', _nd)
        )
        _metrika_reports = load_reports_for_period(pid, _nd) or []
        if _today_404:                       # сегодняшние 404 (API) — первыми
            _metrika_reports = [_today_404] + list(_metrika_reports)
        _metrika_reports = _metrika_reports or None
        _service_issues = load_issues(pid) or None
        build_report(
            project_name=cfg['name'], started_at_ms=started_ms,
            finished_at_ms=finished_ms,
            selected_subdomains=plan.selected_subdomains, results=results,
            output_path=report_path, notifications=_notifs or None,
            metrika_reports=_metrika_reports,
            metrika_data_date=(_m404_disp if _today_404
                               else get_latest_available_date(pid)),
            service_issues=_service_issues, autoclick=_autoclick)
        _m_pages = sum(r.total_pages for r in (_metrika_reports or []))
        log(f'✓ Отчёт собран: уведомлений {len(_notifs)}, '
            f'404-страниц {_m_pages}, ошибок сервисов {len(_service_issues or [])}')

        # Критические ошибки (п.4.3) – выделяем для срочного уведомления и для
        # блока в подписи к отчёту (по всем городам).
        crit = analyze_critical(results)
        if crit.has_any:
            log(f'Критических находок: {crit.total} '
                f'(падений доступности: {len(crit.availability)})')

        # Telegram (полный отчёт – почта/метрика уже собраны выше)
        tg_token = creds.get('tg_token')
        tg_recipients = creds.get('tg_recipients') or []
        if tg_token and tg_recipients:
            # Срочное ОТДЕЛЬНОЕ сообщение о падении доступности – ДО отчёта.
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
                    critical_block=format_critical_block(crit))
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
