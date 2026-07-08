"""
runner_30min.py - логика прогона 30-мин чек-листа БЕЗ Streamlit.

Используется фоновым подпроцессом checklist_run.py, чтобы тяжёлая async-работа
(run_batch на aiohttp) шла в отдельном ПРОЦЕССЕ - надёжно, в отличие от потока
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

        # ── Метаданные и дубли (п.1.8) ──
        # Дубли title/description/H1 - по всем результатам прогона; дубли
        # УРЛОВ - прозвон вариантов (http/слэш/www) главной и каталога
        # каждого проверенного поддомена.
        _meta_summary = None
        if _chk_meta:
            try:
                from meta_checker import find_duplicates, check_url_duplicates
                _dups = find_duplicates(results)
                _probe_urls = [r.url for r in results
                               if r.is_ok and r.type_code in ('main', 'catalog')]
                _url_dups = asyncio.run(check_url_duplicates(
                    _probe_urls, proxy_url=proxy_url))
                _meta_summary = {'duplicates': _dups, 'url_duplicates': _url_dups,
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
            service_issues=_service_issues, autoclick=_autoclick,
            indexing_summary=_idx_summary, meta_summary=_meta_summary)
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
                        if getattr(r, 'has_markup_issues', False)))
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
