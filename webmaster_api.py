"""
webmaster_api.py - ошибки сайтов из Яндекс.Вебмастера через официальный API v4.

В отличие от webmaster_recheck.py (браузер + локальный Chrome) - работает на
облаке: только HTTPS + OAuth-токен. Тянет «Диагностику → проблемы сайта»
(сайтмапы, дубли, мусорные ссылки, ошибки сервера/индексации и т.п.).

Авторизация: OAuth-токен Яндекса со scope `webmaster:hostinfo`.
Хранится в Streamlit Secrets как `webmaster_oauth_<pid>`.

Кеш: cache/service-issues/<project_id>/webmaster.json
"""
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

try:
    import requests
except ImportError:                 # requests идёт со streamlit; на всякий
    requests = None

API_BASE = 'https://api.webmaster.yandex.net/v4'
CACHE_DIR = Path(__file__).parent / 'cache' / 'service-issues'
SUBDOMAINS_DIR = Path(__file__).parent / 'catalogs'


def oauth_secret_key(project_id: str) -> str:
    return f'webmaster_oauth_{project_id}'


# ── Модель проблемы сервиса (общая для Вебмастер/GSC/Метрика в будущем) ──
@dataclass
class ServiceIssue:
    project_id: str
    service: str          # 'webmaster' | 'gsc' | 'metrika'
    host: str             # хост сайта (без схемы), напр. spb.example.ru
    severity: str         # fatal | critical | possible | recommendation | info
    code: str             # машинный тип проблемы
    title: str            # человекочитаемое название
    detail: str = ''      # пояснение/значение
    url: str = ''         # ссылка в панель Вебмастера
    date: str = ''        # дата последнего изменения (YYYY-MM-DD)
    state: str = ''       # состояние: «на проверке» / «проблема актуальна» / …

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ServiceIssue':
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


SEVERITY_ORDER = {'fatal': 0, 'critical': 1, 'possible': 2,
                  'recommendation': 3, 'info': 4}
SEVERITY_LABEL = {
    'fatal': '🔴 Фатальная', 'critical': '🔴 Критическая',
    'possible': '🟠 Возможная', 'recommendation': '🟡 Рекомендация',
    'info': '⚪ Инфо',
}
_SEV_FROM_YANDEX = {
    'FATAL': 'fatal', 'CRITICAL': 'critical', 'ERROR': 'critical',
    'POSSIBLE_PROBLEM': 'possible', 'RECOMMENDATION': 'recommendation',
}

# Человеческие названия типов диагностики (известные). Неизвестные -
# гуманизируем сам код (подчёркивания → пробелы, нижний регистр).
_PROBLEM_TITLES = {
    'DISALLOW_IN_INDEXING_BY_USER': 'Запрет индексирования (вебмастер)',
    'DISALLOW_IN_ROBOTS': 'Запрет в robots.txt',
    'DNS_ERROR': 'Ошибка DNS',
    'SITE_NOT_LOADED': 'Сайт не загружается',
    'SITE_ERROR': 'Ошибки сервера (5xx)',
    'THREATS': 'Угрозы безопасности (вирусы/вредонос)',
    'THREATS_DANGEROUS': 'Опасные угрозы безопасности',
    'NO_SITEMAP': 'Не указан sitemap',
    'SITEMAP_NOT_SET': 'Не задан файл sitemap',
    'ERRORS_IN_SITEMAP': 'Ошибки в sitemap',
    'NO_ROBOTS_TXT': 'Нет robots.txt',
    'SLOW_AVG_RESPONSE_TIME': 'Медленный ответ сервера',
    'DGN_SLOW_RESPONSE': 'Медленный ответ сервера',
    'MAIN_MIRROR_IS_NOT_HTTPS': 'Главное зеркало не на HTTPS',
    'DUPLICATE_PAGES': 'Дубли страниц',
    'DUPLICATE_TITLES': 'Дубли тегов title',
    'MANY_BROKEN_LINKS': 'Много битых ссылок (4xx)',
    'NO_METRIKA_COUNTER_CRAWL_ENABLED': 'Нет счётчика Метрики для обхода',
    'ERRORS_IN_MICRODATA': 'Ошибки в микроразметке',
    'NOT_MOBILE_FRIENDLY': 'Сайт не оптимизирован для мобильных',
    'EXTERNAL_LINKS_SPAM': 'Мусорные ссылки в донорах',
    'TURBO_NO_FEED': 'Нет Турбо-страниц',
    'NOT_IN_SPRAV': 'Организация не добавлена в Яндекс Бизнес',
    'NO_SPRAV_COMPANIES': 'Организация не добавлена в Яндекс Бизнес',
    'HOST_NOT_VERIFIED': 'Права на сайт не подтверждены',
    'DOMAIN_NOT_VERIFIED': 'Права на сайт не подтверждены',
    'NO_METRIKA_COUNTER': 'Нет счётчика Яндекс.Метрики',
    'FAVICON_ERROR': 'Проблема с фавиконкой',
    'SOFT_404': 'Страницы-обманки (soft 404)',
    'USELESS_PAGES': 'Малополезные страницы',
    'MANY_REDIRECTS': 'Много редиректов',
    'URL_ERRORS': 'Ошибки в URL',
    'DOCS_IN_SEARCH_DECREASED': 'Снизилось число страниц в поиске',
    'TURBO_HOST_INACTIVE': 'Турбо-страницы неактивны',
    # Реальные коды из панели Вебмастера:
    'URL_ALERT_5XX': 'Ошибки сервера 5xx на страницах',
    'MAIN_PAGE_ERROR': 'Ошибка на главной странице',
    'ERROR_IN_ROBOTS_TXT': 'Ошибка в robots.txt',
    'ERRORS_IN_SITEMAPS': 'Ошибки в файлах sitemap',
    'NO_SITEMAPS': 'Не добавлены файлы sitemap',
    'DOCUMENTS_MISSING_TITLE': 'Страницы без тега title',
    'NO_SITEMAP_MODIFICATIONS': 'Sitemap давно не обновлялся',
    'MAIN_PAGE_REDIRECTS': 'Главная перенаправляет (редирект)',
    'DOCUMENTS_MISSING_DESCRIPTION': 'Страницы без meta description',
    'DUPLICATE_CONTENT_ATTRS': 'Дубли страниц (одинаковый контент)',
}


# Состояние проблемы (verification_state / state) → человекочитаемо.
_STATE_LABELS = {
    'IN_PROGRESS': 'на проверке',
    'CHECKING': 'на проверке',
    'PROBLEM_ACTUAL': 'проблема актуальна',
    'PRESENT': 'проблема актуальна',
    'NEW': 'проблема актуальна',
    'ACTUAL': 'проблема актуальна',
}


def _state_label(state: str) -> str:
    """Состояние из API → текст. Неизвестное - как есть (нижний регистр)."""
    s = (state or '').upper()
    if s in _STATE_LABELS:
        return _STATE_LABELS[s]
    return (state or '-').replace('_', ' ').lower()


def _humanize_code(code: str) -> str:
    """Человеческое название по коду диагностики. Неизвестный код показываем
    как есть (UPPER_SNAKE) - это явно «код», а не ломаный текст вроде
    'Not in sprav'. Когда узнаем новый код - добавляем в _PROBLEM_TITLES."""
    if not code:
        return 'Проблема (без кода)'
    return _PROBLEM_TITLES.get(code) or code.upper()


def _norm_host(s: str) -> str:
    """Хост без схемы/порта/www/слеша - для матчинга."""
    s = (s or '').strip()
    for pre in ('https://', 'http://'):
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.split('/')[0]
    if s.startswith('https:'):           # host_id вида https:host:443
        s = s[len('https:'):]
    if s.startswith('http:'):
        s = s[len('http:'):]
    s = s.rstrip(':').split(':')[0]
    if s.startswith('www.'):
        s = s[4:]
    return s.lower().strip('.')


def _project_hosts(project_id: str) -> set:
    """Хосты проекта из catalogs/<pid>-subdomains.csv."""
    import csv
    path = SUBDOMAINS_DIR / f'{project_id}-subdomains.csv'
    hosts = set()
    if not path.exists():
        return hosts
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            u = (row.get('url') or '').strip()
            if u:
                h = _norm_host(u)
                if h:
                    hosts.add(h)
    return hosts


# ── Кеш ──────────────────────────────────────────────────────────────
def _cache_path(project_id: str, service: str = 'webmaster') -> Path:
    return CACHE_DIR / project_id / f'{service}.json'


def save_issues(project_id: str, issues: list, service: str = 'webmaster'):
    p = _cache_path(project_id, service)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({'saved_at': datetime.now().isoformat(),
                    'issues': [i.to_dict() for i in issues]},
                   ensure_ascii=False, indent=2),
        encoding='utf-8')


def load_issues(project_id: str, service: str = 'webmaster') -> list:
    p = _cache_path(project_id, service)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return [ServiceIssue.from_dict(d) for d in data.get('issues', [])]
    except Exception:
        return []


# ── HTTP ─────────────────────────────────────────────────────────────
def _get(token: str, path: str, proxy_url: Optional[str] = None,
         params: dict = None) -> dict:
    if requests is None:
        raise RuntimeError('requests не установлен')
    headers = {'Authorization': f'OAuth {token}'}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    import time
    delay = 5
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(API_BASE + path, headers=headers, params=params,
                             proxies=proxies, timeout=30)
        except Exception as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 40)
            continue
        if r.status_code == 401:
            raise PermissionError('OAuth токен невалиден или просрочен (401)')
        if r.status_code == 403:
            raise PermissionError('Нет доступа (403): проверь scope webmaster:hostinfo')
        if r.status_code == 429:
            time.sleep(delay)
            delay = min(delay * 2, 40)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f'HTTP {r.status_code}: {r.text[:200]}')
        return r.json()
    raise RuntimeError(f'Запрос не прошёл: {last_err or "429 после ретраев"}')


def _panel_url(host_id: str) -> str:
    return f'https://webmaster.yandex.ru/site/{host_id}/diagnostics/'


def _extract_inprogress_codes(payload) -> set:
    """Из ответа /diagnostics/checks/ собрать коды проблем «на проверке».
    Структура у Яндекса может отличаться - разбираем максимально терпимо."""
    codes = set()
    if not payload:
        return codes
    # Кандидаты-контейнеры со списком проверок
    items = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (payload.get('checks') or payload.get('problems')
                 or payload.get('items') or payload.get('tasks') or [])
        if isinstance(items, dict):       # вдруг {CODE: {...}}
            items = [{**v, 'code': k} for k, v in items.items()]
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        status = str(it.get('status') or it.get('state')
                     or it.get('check_state') or '').upper()
        # «на проверке»: IN_PROGRESS / CHECKING / PROGRESS
        if 'PROGRESS' in status or 'CHECK' in status or status == 'IN_PROGRESS':
            code = (it.get('code') or it.get('type') or it.get('problem_type')
                    or it.get('problem') or '')
            if code:
                codes.add(str(code).upper())
    return codes


def _parse_diagnostics(project_id: str, host: str, host_id: str,
                       payload: dict, inprogress_codes: set = None) -> list:
    """Из ответа /diagnostics/ собрать список ServiceIssue (только активные).
    inprogress_codes - коды, что сейчас «на проверке» (из /diagnostics/checks/)."""
    issues = []
    inprogress_codes = inprogress_codes or set()
    problems = (payload or {}).get('problems')
    if not problems:
        return issues

    # Ответ бывает dict {CODE: {...}} или list [{...}] - поддерживаем оба.
    items = []
    if isinstance(problems, dict):
        items = list(problems.items())
    elif isinstance(problems, list):
        items = [(p.get('type') or p.get('code') or '', p) for p in problems]

    for code, info in items:
        if not isinstance(info, dict):
            continue
        # Состояние проверки - из нескольких возможных полей (у Яндекса плавает:
        # state / verification_state / check_status / recheck_state).
        raw_state = (info.get('state') or info.get('verification_state')
                     or info.get('check_status') or info.get('recheck_state')
                     or info.get('status') or '')
        st_up = str(raw_state).upper()
        # Исключаем только ЯВНО решённые/отсутствующие - всё прочее берём.
        if st_up in ('ABSENT', 'OK', 'NONE', 'RESOLVED', 'GONE', 'FIXED'):
            continue
        # «На проверке» приходит из /diagnostics/checks/ - перебивает state.
        if str(code).upper() in inprogress_codes:
            st_up = 'IN_PROGRESS'
        sev_raw = (info.get('severity') or '').upper()
        severity = _SEV_FROM_YANDEX.get(sev_raw, 'info')
        date = ''
        upd = info.get('last_state_update') or info.get('last_update') or ''
        if upd:
            date = str(upd)[:10]
        issues.append(ServiceIssue(
            project_id=project_id, service='webmaster', host=host,
            severity=severity, code=str(code),
            title=_humanize_code(str(code)),
            detail='', url=_panel_url(host_id), date=date,
            state=st_up))               # храним СЫРОЙ код, маппинг - в reporter
    return issues


def fetch_webmaster_issues(project_id: str, token: str,
                           proxy_url: Optional[str] = None,
                           log: Optional[Callable] = None) -> list:
    """Забрать диагностику по всем хостам проекта. Возвращает список ServiceIssue
    и сохраняет в кеш. При ошибке - пишет в лог и возвращает прежний кеш."""
    def _log(msg):
        if log:
            log('info', msg)

    if not token:
        _log('⚠ Вебмастер-API: токен не задан (webmaster_oauth_<pid>)')
        return load_issues(project_id)

    try:
        user = _get(token, '/user/', proxy_url)
        user_id = user.get('user_id')
        if not user_id:
            raise RuntimeError('user_id не получен')

        hosts_resp = _get(token, f'/user/{user_id}/hosts/', proxy_url)
        api_hosts = hosts_resp.get('hosts', []) or []
        _log(f'Вебмастер-API: в аккаунте {len(api_hosts)} сайтов')

        want = _project_hosts(project_id)
        selected = []
        for h in api_hosts:
            host_url = h.get('ascii_host_url') or h.get('unicode_host_url') or ''
            host_norm = _norm_host(host_url) or _norm_host(h.get('host_id', ''))
            if not want or host_norm in want:
                selected.append((host_norm, h.get('host_id'), host_url))
        if want and not selected:
            _log(f'⚠ Вебмастер-API: ни один сайт аккаунта не совпал с проектом - '
                 f'беру все {len(api_hosts)}')
            selected = [(_norm_host(h.get('ascii_host_url', '')),
                         h.get('host_id'), h.get('ascii_host_url', ''))
                        for h in api_hosts]

        all_issues = []
        _dumped = False
        _dumped_checks = False
        _checks_warned = False
        _checks_dead = False        # /diagnostics/checks/ вернул 404 - не дёргаем
        for host_norm, host_id, _url in selected:
            if not host_id:
                continue
            try:
                diag = _get(token, f'/user/{user_id}/hosts/{host_id}/diagnostics/',
                            proxy_url)
                _raw = (diag or {}).get('problems')
                _raw_n = len(_raw) if isinstance(_raw, (dict, list)) else 0
                # Дамп: сырой problem-объект первой АКТИВНОЙ проблемы (state≠ABSENT)
                # - увидеть все поля (вдруг есть признак «на проверке»).
                if not _dumped and isinstance(_raw, dict) and _raw_n:
                    import json as _j
                    _act = None
                    for _c, _v in _raw.items():
                        if isinstance(_v, dict) and str(_v.get('state', '')).upper() != 'ABSENT':
                            _act = (_c, _v)
                            break
                    if _act:
                        _log(f'  RAW активная проблема [{_act[0]}]: '
                             f'{_j.dumps(_act[1], ensure_ascii=False)[:500]}')
                        _dumped = True

                # Коды «на проверке» - из отдельного эндпоинта /diagnostics/checks/
                # (если он есть; на части аккаунтов отдаёт 404 - тогда не дёргаем).
                _inprogress = set()
                if not _checks_dead:
                    try:
                        _checks = _get(
                            token, f'/user/{user_id}/hosts/{host_id}/diagnostics/checks/',
                            proxy_url)
                        if not _dumped_checks and _checks:
                            import json as _j
                            _log(f'  RAW checks пример: '
                                 f'{_j.dumps(_checks, ensure_ascii=False)[:400]}')
                            _dumped_checks = True
                        _inprogress = _extract_inprogress_codes(_checks)
                    except Exception as _ce:
                        if '404' in str(_ce):
                            _checks_dead = True   # эндпоинта нет - больше не пробуем
                        if not _checks_warned:
                            _log(f'  /diagnostics/checks/: {_ce}')
                            _checks_warned = True

                hi = _parse_diagnostics(project_id, host_norm, host_id, diag,
                                        inprogress_codes=_inprogress)
                all_issues.extend(hi)
                # Диагностика: если сырых проблем много, а активных 0 - видно в логе
                _log(f'  {host_norm}: в ответе {_raw_n}, активных {len(hi)}')
                if _raw_n and not hi and isinstance(_raw, dict):
                    _log(f'    ключи/шаблон: {list(_raw)[:6]}')
            except Exception as e:
                _log(f'⚠ Вебмастер-API ({host_norm}): {e}')

        # Какие значения state встречаются + пары severity×state - чтобы понять,
        # бывает ли у КРИТИЧЕСКИХ состояние UNDEFINED («на проверке»).
        _seen_states = sorted({(i.state or '∅') for i in all_issues})
        _log(f'Вебмастер-API: встреченные state активных проблем: {_seen_states}')
        from collections import Counter as _C
        _pairs = _C((i.severity, i.state or '∅') for i in all_issues)
        _log(f'Вебмастер-API: severity×state: '
             f'{sorted(_pairs.items())}')

        all_issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.host))
        save_issues(project_id, all_issues)
        _log(f'✓ Вебмастер-API: проблем собрано {len(all_issues)} '
             f'по {len(selected)} сайтам')
        return all_issues

    except Exception as e:
        _log(f'❌ Вебмастер-API: {e}')
        return load_issues(project_id)


if __name__ == '__main__':
    # Самотест парсинга без сети
    sample = {'problems': {
        'DISALLOW_IN_ROBOTS': {'severity': 'CRITICAL', 'state': 'PRESENT',
                               'last_state_update': '2026-06-15T10:00:00'},
        'NO_SITEMAP': {'severity': 'POSSIBLE_PROBLEM', 'state': 'PRESENT'},
        'OLD_FIXED': {'severity': 'CRITICAL', 'state': 'ABSENT'},
    }}
    out = _parse_diagnostics('mpe', 'example.ru', 'https:example.ru:443', sample)
    for i in out:
        print(i.severity, i.code, '→', i.title, '|', i.date)
    print('норм host:', _norm_host('https://spb.inmetprom.ru/'),
          _norm_host('https:example.ru:443'))
