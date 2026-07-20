"""
link_profile.py - Lite-проверка ссылочного профиля (доп. чек-лист).

«Lite» = только официальные бесплатные данные Яндекс.Вебмастера через API
v4 (Ahrefs/Majestic платные - не трогаем). Переиспользует OAuth-токен,
резолв host_id и _get из webmaster_api.

Эндпоинты (подтверждены по докам Яндекса):
  • /links/external/samples  → {count, links:[{source_url, destination_url,
                                discovery_date, source_last_access_date}]}
                                count = всего внешних ссылок на хост;
                                links - выборка (доноры) до 100 штук.
  • /links/external/history?indicator=LINKS_TOTAL_COUNT
                             → {indicators:{LINKS_TOTAL_COUNT:[{date,value}]}}
                                динамика числа ссылок во времени.

Проверки (по каждому верифицированному хосту проекта):
  1. Профиль есть вообще (count > 0). Ноль у молодого сайта - инфо.
  2. Объём - всего ссылок + число доноров-хостов (по выборке samples).
  3. Динамика (history) - резкий обвал (>30% от пика) = потеря ссылок;
     резкий всплеск (≥×3 от старта) = возможный спам / негативное SEO.
  4. Спам-доноры - грубая эвристика по хостам доноров (мусорные зоны и
     ключевые слова gambling/adult/фарма). Для «lite» - сигнальная.
  5. ВНЕЗАПНЫЕ мусорные доноры (пункт «нет аномалий»): в выборке у каждой
     ссылки Яндекс отдаёт discovery_date (когда ссылка найдена). Донор
     считаем «внезапным», если его самая ранняя ссылка в выборке появилась
     за последние RECENT_DAYS дней. Внезапный + спамный = сигнал негативного
     SEO / закупки мусора между прогонами (эталон хранить не нужно -
     «новизну» берём прямо из discovery_date Яндекса).

Google беклинков по API не отдаёт - в отчёте только ссылка на ручную
сверку в GSC.
"""
from datetime import date, timedelta
from urllib.parse import urlsplit

# Пороги динамики (доли/разы).
DROP_PCT = 30           # падение от пика больше этого - «обвал»
SPIKE_FACTOR = 3.0      # рост от старта больше этого - «всплеск» (спам?)
SAMPLE_LIMIT = 100      # сколько доноров тянем в выборку
SPAM_SHOW = 10          # сколько спам-доноров показываем в отчёте
RECENT_DAYS = 30        # донор «внезапный», если найден за столько последних дней


def _recent_cutoff(days: int = RECENT_DAYS, today: date = None) -> str:
    """Граничная дата (YYYY-MM-DD): доноры с discovery_date не раньше неё -
    «внезапные». today - для тестов (по умолчанию сегодня)."""
    today = today or date.today()
    return (today - timedelta(days=days)).isoformat()

GSC_LINKS_URL = 'https://search.google.com/search-console/links'


def _links_panel_url(host_id: str) -> str:
    """Раздел «Ссылки → Внешние ссылки» в панели Вебмастера.
    /links/ редиректит на /links/incoming/ (проверено)."""
    return f'https://webmaster.yandex.ru/site/{host_id}/links/incoming/'

# Мусорные доменные зоны и ключевые слова спам-доноров (сигнальная эвристика).
_SPAM_TLDS = (
    '.xyz', '.top', '.loan', '.click', '.link', '.gq', '.tk', '.ml', '.cf',
    '.ga', '.work', '.bid', '.stream', '.download', '.racing', '.win',
    '.review', '.date', '.faith', '.men', '.party', '.trade', '.webcam',
    '.science', '.accountant', '.cricket', '.kim', '.mom', '.wang',
)
_SPAM_WORDS = (
    'porn', 'xxx', 'sex', 'escort', 'casino', 'poker', 'gambl', 'bet365',
    'betting', 'viagra', 'cialis', 'pharm', 'payday', 'loan', 'replica',
)


def _host_of(url: str) -> str:
    """Хост донора без схемы/www."""
    h = (urlsplit(url).hostname or '').lower()
    return h[4:] if h.startswith('www.') else h


def looks_spam_host(host: str) -> bool:
    """Хост донора похож на спам: мусорная зона ИЛИ ключевое слово."""
    h = (host or '').lower()
    if not h:
        return False
    if any(h.endswith(tld) for tld in _SPAM_TLDS):
        return True
    return any(w in h for w in _SPAM_WORDS)


def analyze_samples(count, links, recent_cutoff: str = None):
    """Из ответа samples: всего ссылок, доноры в выборке, уникальные хосты,
    спам-доноры, а также ВНЕЗАПНЫЕ (недавно найденные) доноры и внезапные
    спам-доноры. recent_cutoff='YYYY-MM-DD' - граница «новизны» по
    discovery_date; None - новизну не считаем."""
    links = links or []
    hosts = []
    host_first = {}          # хост донора -> самая ранняя его discovery_date
    for ln in links:
        h = _host_of(ln.get('source_url', ''))
        if not h:
            continue
        hosts.append(h)
        dd = (ln.get('discovery_date') or '')[:10]     # 'YYYY-MM-DD' из ISO
        if dd and (h not in host_first or dd < host_first[h]):
            host_first[h] = dd
    distinct = sorted(set(hosts))
    spam = sorted({h for h in distinct if looks_spam_host(h)})
    # Внезапные: донор, чья самая ранняя ссылка в выборке не старше cutoff.
    recent = sorted(h for h, d in host_first.items()
                    if recent_cutoff and d >= recent_cutoff)
    recent_spam = sorted(h for h in recent if looks_spam_host(h))
    return {
        'total': int(count or 0),
        'sample_size': len(links),
        'distinct_hosts': len(distinct),
        'spam_hosts': spam,
        'recent_hosts': recent,
        'recent_spam_hosts': recent_spam,
    }


def analyze_history(indicators):
    """Из ответа history (LINKS_TOTAL_COUNT): последнее значение, пик, старт,
    обвал от пика и всплеск от старта."""
    series = ((indicators or {}).get('LINKS_TOTAL_COUNT')) or []
    pts = []
    for p in series:
        try:
            pts.append((str(p.get('date', '')), int(p.get('value', 0))))
        except (TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    if not pts:
        return {'points': 0, 'latest': None, 'peak': None, 'first': None,
                'drop_pct': 0, 'spike_factor': 0.0,
                'dropped': False, 'spiked': False}
    vals = [v for _, v in pts]
    latest, peak, first = vals[-1], max(vals), vals[0]
    drop_pct = round((peak - latest) / peak * 100) if peak else 0
    spike = round(latest / first, 1) if first else 0.0
    return {
        'points': len(pts), 'latest': latest, 'peak': peak, 'first': first,
        'drop_pct': drop_pct, 'spike_factor': spike,
        'dropped': bool(peak and drop_pct >= DROP_PCT and peak - latest >= 5),
        'spiked': bool(first and spike >= SPIKE_FACTOR and latest - first >= 5),
    }


def build_host_profile(host, panel_url, samples_resp, history_resp,
                       recent_cutoff: str = None):
    """Собрать профиль одного хоста + список предупреждений/инфо для отчёта.
    recent_cutoff - граница «новизны» доноров (по умолчанию последние
    RECENT_DAYS дней)."""
    if recent_cutoff is None:
        recent_cutoff = _recent_cutoff()
    s = analyze_samples((samples_resp or {}).get('count'),
                        (samples_resp or {}).get('links'), recent_cutoff)
    h = analyze_history((history_resp or {}).get('indicators'))
    warnings, infos = [], []
    if s['total'] == 0:
        infos.append('внешних ссылок Яндекс не знает - ссылочного профиля '
                     'пока нет (норма для молодого сайта)')
    if h['dropped']:
        warnings.append(f'ссылочная масса просела: было {h["peak"]}, стало '
                        f'{h["latest"]} (−{h["drop_pct"]}% от пика) - '
                        f'потеря доноров')
    if h['spiked']:
        warnings.append(f'резкий рост ссылок: с {h["first"]} до {h["latest"]} '
                        f'(×{h["spike_factor"]}) - проверить на спам/накрутку '
                        f'(негативное SEO)')
    # ВНЕЗАПНЫЕ мусорные доноры - головной сигнал пункта «нет аномалий».
    if s['recent_spam_hosts']:
        warnings.append(
            f'ВНЕЗАПНЫЕ мусорные доноры: {len(s["recent_spam_hosts"])} новых '
            f'спам-хостов за ~{RECENT_DAYS} дн. '
            f'({", ".join(s["recent_spam_hosts"][:SPAM_SHOW])}) - '
            f'похоже на негативное SEO / закупку мусорных ссылок')
    if s['spam_hosts']:
        warnings.append(f'подозрительные доноры в выборке: {len(s["spam_hosts"])} '
                        f'(мусорные зоны / gambling / adult)')
    return {
        'host': host, 'panel_url': panel_url,
        'total': s['total'], 'sample_size': s['sample_size'],
        'distinct_hosts': s['distinct_hosts'],
        'spam_hosts': s['spam_hosts'][:SPAM_SHOW],
        'spam_count': len(s['spam_hosts']),
        'recent_spam_hosts': s['recent_spam_hosts'][:SPAM_SHOW],
        'recent_spam_count': len(s['recent_spam_hosts']),
        'recent_new_count': len(s['recent_hosts']),
        'history': h, 'warnings': warnings, 'infos': infos,
    }


def fetch_link_profile(project_id, token, proxy_url=None, log=None):
    """Забрать ссылочный профиль по всем верифицированным хостам проекта.
    Возвращает dict для отчёта (или {'available': False, ...})."""
    def _log(m):
        if log:
            log(m)
    if not token:
        return {'available': False,
                'note': 'OAuth-токен Вебмастера не задан (webmaster_oauth_'
                        '<pid>) - ссылочный профиль по API недоступен.'}
    from webmaster_api import _get, _norm_host, _project_hosts
    try:
        user = _get(token, '/user/', proxy_url)
        user_id = user.get('user_id')
        if not user_id:
            raise RuntimeError('user_id не получен')
        hosts_resp = _get(token, f'/user/{user_id}/hosts/', proxy_url)
        api_hosts = hosts_resp.get('hosts', []) or []
        want = _project_hosts(project_id)
        selected = []
        for hh in api_hosts:
            host_url = (hh.get('ascii_host_url')
                        or hh.get('unicode_host_url') or '')
            host_norm = _norm_host(host_url) or _norm_host(hh.get('host_id', ''))
            if not want or host_norm in want:
                selected.append((host_norm, hh.get('host_id')))
        if want and not selected:
            selected = [(_norm_host(hh.get('ascii_host_url', '')),
                         hh.get('host_id')) for hh in api_hosts]
        _log(f'Ссылочный профиль: хостов к проверке {len(selected)}')

        _cutoff = _recent_cutoff()       # граница «внезапности» доноров - раз на прогон
        hosts_out = []
        for host_norm, host_id in selected:
            if not host_id:
                continue
            try:
                samples = _get(
                    token, f'/user/{user_id}/hosts/{host_id}/links/external/samples',
                    proxy_url, params={'offset': 0, 'limit': SAMPLE_LIMIT})
            except Exception as e:
                _log(f'⚠ Ссылочный профиль ({host_norm}) samples: {e}')
                samples = None
            try:
                history = _get(
                    token, f'/user/{user_id}/hosts/{host_id}/links/external/history',
                    proxy_url, params={'indicator': 'LINKS_TOTAL_COUNT'})
            except Exception as e:
                _log(f'⚠ Ссылочный профиль ({host_norm}) history: {e}')
                history = None
            if samples is None and history is None:
                continue
            hosts_out.append(build_host_profile(
                host_norm, _links_panel_url(host_id), samples, history,
                recent_cutoff=_cutoff))
        return {'available': True, 'hosts': hosts_out,
                'gsc_links_url': GSC_LINKS_URL}
    except PermissionError as e:
        return {'available': False, 'note': f'Доступ к API Вебмастера: {e}'}
    except Exception as e:
        return {'available': False,
                'note': f'Ссылочный профиль не получен: {e}'}
