"""
run_scheduled.py - автономный прогон чек-листа по расписанию (GitHub Action / CLI).

Запускает runner_30min.run_check для одного или нескольких проектов БЕЗ Streamlit.
Секреты читаются из переменных окружения (в GitHub Action - из repository Secrets),
имена ключей - те же, что в st.secrets приложения:

  proxy_url                          - прокси (обязателен для ИМП; датацентр-IP блокируется)
  telegram_bot_token                 - токен бота для отправки отчёта
  telegram_recipients_<pid>          - получатель(и) chat_id, через запятую/пробел
  metrika_<pid>_email / _password    - почта Метрики/Вебмастера/Я.Бизнес/2ГИС
  gsc_<pid>_email / _password        - GSC / Google-аккаунты
  yandex_oauth_<pid>                 - токен Вебмастер-API (запасные: webmaster_oauth_<pid>,
                                       yandex_oauth, webmaster_oauth)

где <pid> ∈ {smu, imp, mpe}.

Отчёт сохраняется в reports/ и (если заданы telegram_*) отправляется в Telegram -
эта отправка уже встроена в run_check.

Запуск:
  python run_scheduled.py --projects smu,imp,mpe --profile standard --days 1
  PROJECTS=smu,imp,mpe PROFILE=standard DAYS=1 python run_scheduled.py
"""
import argparse
import os
import re
import sys
from datetime import datetime

from profiles import PROFILES, get_profile_kwargs
from runner_30min import run_check
from sources import load_project_config, load_sources
from metrika_404 import MAILBOX_CONFIG
from webmaster_notify import (
    GSC_GMAIL_CONFIG, YABUSINESS_YANDEX_CONFIG,
    TWOGIS_YANDEX_CONFIG, GOOGLE_ACCOUNTS_CONFIG,
)


def _env(key: str):
    """Значение секрета из окружения или None (пустые строки → None)."""
    v = os.environ.get(key or '')
    return v if (v and v.strip()) else None


def _pair(cfg_map: dict, pid: str):
    cfg = cfg_map.get(pid) or {}
    return (_env(cfg.get('secret_email', '')), _env(cfg.get('secret_password', '')))


def _triple(cfg_map: dict, pid: str):
    cfg = cfg_map.get(pid) or {}
    return (_env(cfg.get('secret_email', '')), _env(cfg.get('secret_password', '')),
            cfg.get('folder'))


def _recipients(pid: str) -> list[str]:
    raw = os.environ.get(f'telegram_recipients_{pid}', '') or ''
    return [x for x in re.split(r'[,;\s]+', raw.strip()) if x]


def build_creds(pid: str) -> dict:
    """Собрать creds из окружения - та же структура, что готовит UI из st.secrets."""
    return {
        'proxy_url': _env('proxy_url'),
        'tg_token': _env('telegram_bot_token'),
        'tg_recipients': _recipients(pid),
        'metrika': _pair(MAILBOX_CONFIG, pid),
        'gsc': _pair(GSC_GMAIL_CONFIG, pid),
        'yab': _triple(YABUSINESS_YANDEX_CONFIG, pid),
        'twogis': _triple(TWOGIS_YANDEX_CONFIG, pid),
        'google': _pair(GOOGLE_ACCOUNTS_CONFIG, pid),
        'webmaster_oauth': (_env(f'yandex_oauth_{pid}') or _env(f'webmaster_oauth_{pid}')
                            or _env('yandex_oauth') or _env('webmaster_oauth')),
        'webmaster_keys_hint': [],
        'secret_keys_hint': [],
    }


def build_params(pid: str, profile_id: str, days: int, fetch_notifications: bool) -> dict:
    """Параметры прогона по профилю объёма (как кнопки Быстрая/Стандартная/Полная)."""
    p = get_profile_kwargs(profile_id)
    cfg = load_project_config(pid)
    has_filters = bool(load_sources(cfg).filters)
    return {
        'budget': {
            'cats': p['categories_per_subdomain'],
            'filters': p['filters_per_subdomain'] if has_filters else 0,
            'products': p['products_per_subdomain'],
        },
        'random_cities': p['random_subdomains_count'],
        'cis_extra': p.get('cis_extra_subdomains', 0),   # доп. СНГ-домены по пресету
        'custom_urls': [],
        'check_main': True, 'check_catalog': True, 'check_categories': True,
        'check_filters': has_filters, 'check_products': True, 'check_text': True,
        'check_indexing': True,  # п.1.7 - индексация (robots/noindex/canonical)
        'check_meta': True,      # п.1.8 - метаданные, дубли, единственность тегов
        'check_links': False,   # «ссылки открываются (404)» - тяжёлая, по запросу
        'fetch_notifications': fetch_notifications,
        'notify_days': int(days),
    }


def _make_progress():
    """Колбэк прогресса для CI - печатает не чаще, чем раз в 20%, чтобы не спамить."""
    state = {'pct': -20}

    def progress(frac, text):
        pct = int(max(0.0, min(1.0, frac)) * 100)
        if pct >= state['pct'] + 20 or pct >= 100:
            state['pct'] = pct
            print(f'    … {pct}% · {text}', flush=True)
    return progress


def main():
    ap = argparse.ArgumentParser(description='Автозапуск проверки сайтов по расписанию')
    ap.add_argument('--projects', default=os.environ.get('PROJECTS', 'smu,imp,mpe'),
                    help='id проектов через запятую (smu,imp,mpe)')
    ap.add_argument('--profile', default=os.environ.get('PROFILE', 'standard'),
                    help='объём выборки: quick / standard / full')
    ap.add_argument('--days', type=int, default=int(os.environ.get('DAYS', '1') or '1'),
                    help='за сколько дней собирать почту/404 (по умолчанию 1)')
    ap.add_argument('--no-notifications', action='store_true',
                    default=(os.environ.get('FETCH_NOTIFICATIONS', '1') == '0'),
                    help='не собирать уведомления из почты (только проверка сайтов)')
    a = ap.parse_args()

    pids = [x.strip() for x in a.projects.split(',') if x.strip()]
    if a.profile not in PROFILES:
        print(f'Неизвестный профиль: {a.profile} (есть: {", ".join(PROFILES)})', flush=True)
        sys.exit(2)
    if not pids:
        print('Не указаны проекты (--projects smu,imp,mpe)', flush=True)
        sys.exit(2)

    def log(msg):
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

    log(f'Автозапуск: проекты={pids}, профиль={a.profile}, дней={a.days}, '
        f'почта={"нет" if a.no_notifications else "да"}')

    overall_ok = True
    for pid in pids:
        log(f'================ ПРОЕКТ {pid} ================')
        try:
            creds = build_creds(pid)
            if not creds['tg_token'] or not creds['tg_recipients']:
                log(f'⚠ Telegram не настроен (нужны telegram_bot_token и '
                    f'telegram_recipients_{pid}) - отчёт не отправится, '
                    f'останется в reports/ (и в artifact).')
            if not creds['proxy_url']:
                log('⚠ proxy_url не задан - ИМП с датацентр-IP вернёт 403.')
            params = build_params(pid, a.profile, a.days, not a.no_notifications)
            result = run_check(pid, params, creds, log, _make_progress())
            if result.get('error'):
                log(f'❌ {pid}: прогон с ошибкой: {result["error"]}')
                overall_ok = False
            else:
                _res = result.get('results') or []
                _err = sum(1 for r in _res if getattr(r, 'is_error', False))
                log(f'✓ {pid}: проверок {len(_res)}, ошибок {_err}, '
                    f'отчёт: {result.get("report_path")}')
        except Exception as e:
            log(f'❌ {pid}: исключение: {e}')
            overall_ok = False

    log('Готово.' if overall_ok else 'Завершено с ошибками.')
    sys.exit(0 if overall_ok else 1)


if __name__ == '__main__':
    main()
