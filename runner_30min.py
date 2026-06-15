"""
runner_30min.py — логика прогона 30-мин чек-листа БЕЗ Streamlit.

Используется фоновым подпроцессом checklist_run.py, чтобы тяжёлая async-работа
(run_batch на aiohttp) шла в отдельном ПРОЦЕССЕ — надёжно, в отличие от потока
внутри Streamlit. Возвращает результаты, путь отчёта и т.п.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from sources import load_project_config, load_sources, build_plan
from history import load_history, save_history, WEEKLY_TTL_MS
from sitemap import load_product_pathnames
from product_links import load_product_links
from http_checker import run_batch
from reporter import build_report, make_report_filename
from telegram_notify import format_summary_message, send_run_notification
from webmaster_notify import (
    WEBMASTER_YANDEX_CONFIG,
    fetch_webmaster_yandex, fetch_gsc_gmail,
    fetch_yandex_folder_simple, fetch_google_accounts,
    load_notifications,
)

REPORTS_DIR = Path(__file__).parent / 'reports'
REPORTS_DIR.mkdir(exist_ok=True)


def run_check(pid, params, creds, log, progress):
    """Выполнить прогон. log(msg), progress(frac, text) — колбэки.
    Возвращает dict с results / report_path / started_at / finished_at / error."""
    out = {'results': None, 'report_path': None,
           'started_at': int(datetime.now().timestamp() * 1000),
           'finished_at': None, 'error': None}
    try:
        cfg = load_project_config(pid)
        src = load_sources(cfg)
        started_ms = out['started_at']

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
                     f'Проверено {done} из {total_n} — '
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
            retry_delay_ms=2500, check_text=True, on_progress=on_progress,
            proxy_url=proxy_url, kp_map=kp_map))

        finished_ms = int(datetime.now().timestamp() * 1000)
        out['finished_at'] = finished_ms
        save_history(pid, list({urlparse(r.url).path for r in results}))

        log('Формирую xlsx-отчёт…')
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
            output_path=report_path, notifications=_notifs or None)

        # Telegram
        tg_token = creds.get('tg_token')
        tg_recipients = creds.get('tg_recipients') or []
        if tg_token and tg_recipients:
            log(f'Отправляю отчёт в Telegram ({len(tg_recipients)})…')
            try:
                problems_for_tg = [
                    {'city': r.city or '—', 'url': r.url,
                     'status': {'not_found': '404 Не найдена',
                                'client_error': 'Ошибка на сайте',
                                'server_error': 'Сервер не отвечает',
                                'timeout': 'Нет ответа',
                                'network_error': 'Нет соединения'}.get(r.status, r.status)}
                    for r in results if r.is_error][:5]
                empty_sections = [
                    {'city': r.city or '—', 'url': r.url} for r in results
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
                    empty_sections=empty_sections)
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

        # Почта
        if params['fetch_notifications']:
            log('Собираю уведомления из почты…')
            _nlog = lambda lvl, msg: log(msg)
            _proxy = creds.get('proxy_url')

            _yw_e, _yw_p = creds.get('metrika') or (None, None)
            _yw_cfg = WEBMASTER_YANDEX_CONFIG.get(pid)
            if _yw_e and _yw_p and _yw_cfg:
                try:
                    fetch_webmaster_yandex(pid, _yw_e, _yw_p, _yw_cfg['folder'], 30, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ Вебмастер: {_e}')
            else:
                log(f'⚠ Вебмастер: креды не найдены (metrika_{pid}_*)')

            _gsc_e, _gsc_p = creds.get('gsc') or (None, None)
            if _gsc_e and _gsc_p:
                log(f'GSC: креды найдены ({_gsc_e})…')
                try:
                    fetch_gsc_gmail(pid, _gsc_e, _gsc_p, 30, _nlog)
                except Exception as _e:
                    log(f'⚠ GSC: {_e}')
            else:
                log(f'⚠ GSC: креды не найдены (gsc_{pid}_*)')

            _yab_e, _yab_p, _yab_f = creds.get('yab') or (None, None, None)
            if _yab_e and _yab_p and _yab_f:
                try:
                    fetch_yandex_folder_simple(pid, _yab_e, _yab_p, _yab_f, 'ya_business', 30, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ Я.Бизнес: {_e}')

            _tg_e, _tg_p, _tg_f = creds.get('twogis') or (None, None, None)
            if _tg_e and _tg_p and _tg_f:
                try:
                    fetch_yandex_folder_simple(pid, _tg_e, _tg_p, _tg_f, 'twogis', 30, _proxy, _nlog)
                except Exception as _e:
                    log(f'⚠ 2ГИС: {_e}')

            _ga_e, _ga_p = creds.get('google') or (None, None)
            if _ga_e and _ga_p:
                try:
                    fetch_google_accounts(pid, _ga_e, _ga_p, 3, _nlog)
                except Exception as _e:
                    log(f'⚠ Google: {_e}')

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
                    output_path=report_path, notifications=_notifs2)
                log(f'✓ Отчёт обновлён с уведомлениями ({len(_notifs2)} шт.)')
            else:
                log('Уведомлений нет — лист «Уведомления» в отчёт не добавлен.')
        else:
            log('Сбор уведомлений выключен.')

        out['results'] = results
        out['report_path'] = str(report_path)
        progress(1.0, 'Готово')

    except Exception as e:
        out['error'] = str(e)
        log(f'❌ Ошибка: {e}')
        if out['finished_at'] is None:
            out['finished_at'] = int(datetime.now().timestamp() * 1000)
    return out
