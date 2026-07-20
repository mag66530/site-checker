"""
webmaster_metrics.py - аномалии в Яндекс.Вебмастере (Блок B пункта чек-листа
«Нет аномалий в Вебмастере / внезапных мусорных доноров»).

Идея (по практике SEO-мониторинга): аномалия = резкое отклонение метрики от
себя-прошлой. Вебмастер часто сигналит о проблеме РАНЬШЕ, чем просядут
позиции и трафик (страницы выпали из индекса, всплеск ошибок обхода).

Источники данных (Webmaster API v4, переиспользуем _get из webmaster_api):
  • GET …/hosts/{host_id}/summary        → sqi (ИКС), searchable_pages_count
    (страниц в поиске), excluded_pages_count, site_problems {FATAL, CRITICAL,
    POSSIBLE_PROBLEM, RECOMMENDATION} - ТЕКУЩИЙ снимок.
  • GET …/hosts/{host_id}/indexing/history → HTTP_2XX/3XX/4XX/5XX/OTHER во
    времени (историю хранит Яндекс) - ошибки обхода.

Что считаем аномалией:
  1. Обход (по истории Яндекса, эталон хранить не нужно):
     • всплеск 5xx (сервер отдаёт ошибки роботу) - ×2 и хотя бы +MIN_ABS;
     • всплеск 4xx (массовые 404 - страницы удалили/выпали);
     • просадка 2xx (роботу доступно меньше страниц) - −CRAWL_DROP_PCT%.
  2. Проблемы сайта (текущий снимок): FATAL/CRITICAL > 0 - красный флаг сам
     по себе (эталон не нужен).
  3. Страницы в поиске / ИКС (нужен эталон): падение от медианы прошлых
     прогонов на PAGES_DROP_PCT / SQI_DROP_PCT. Эталон - локальный кэш
     cache/wm-baseline-{pid}.json (best-effort: живёт, пока жив контейнер;
     после передеплоя обнуляется - тогда «эталон записан»). Для страниц это
     дополняет надёжный сигнал обхода.

Пороги подобраны по рекомендациям SEO-мониторинга: −15% для страниц/трафика,
меньший порог для редких метрик (ИКС), сравнение с медианой нескольких точек,
а не с одной (меньше ложных срабатываний).
"""
import json
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
CACHE_DIR = PROJECT_ROOT / 'cache'

PAGES_DROP_PCT = 15         # падение страниц в поиске от эталона - аномалия
SQI_DROP_PCT = 10           # падение ИКС от эталона
CRAWL_DROP_PCT = 15         # падение 2xx в обходе
CRAWL_SPIKE_FACTOR = 2.0    # рост 4xx/5xx относительно фона
MIN_ABS = 5                 # мелкие абсолютные сдвиги игнорируем (не шумим)
BASELINE_KEEP = 12          # сколько последних снимков храним в кэше


# ── Разбор рядов ─────────────────────────────────────────────────────


def _series(points):
    """[{date, value}] -> отсортированные пары + сводка latest/peak/min/median."""
    pts = []
    for p in points or []:
        try:
            pts.append((str(p.get('date', '')), int(p.get('value', 0))))
        except (TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    vals = [v for _, v in pts]
    if not vals:
        return {'points': 0, 'latest': None, 'peak': None, 'min': None,
                'median': None, 'baseline': None}
    s = sorted(vals)
    median = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
    # «Фон» для всплеска - медиана без последней точки (чтобы сам всплеск не
    # задирал фон); если точек мало - медиана по всем.
    base_vals = vals[:-1] if len(vals) > 2 else vals
    bs = sorted(base_vals)
    baseline = (bs[len(bs) // 2] if len(bs) % 2
                else (bs[len(bs) // 2 - 1] + bs[len(bs) // 2]) / 2) if bs else vals[-1]
    return {'points': len(vals), 'latest': vals[-1], 'peak': max(vals),
            'min': min(vals), 'median': median, 'baseline': baseline}


def _median(vals):
    s = sorted(v for v in vals if isinstance(v, (int, float)))
    if not s:
        return None
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def analyze_crawl(indicators):
    """Аномалии обхода из /indexing/history. Возвращает список
    {metric, before, after, delta_pct, severity, text}."""
    ind = indicators or {}
    out = []
    # 2xx - просадка (роботу доступно меньше страниц).
    s2 = _series(ind.get('HTTP_2XX'))
    if s2['points'] >= 2 and s2['peak']:
        drop = round((s2['peak'] - s2['latest']) / s2['peak'] * 100)
        if drop >= CRAWL_DROP_PCT and s2['peak'] - s2['latest'] >= MIN_ABS:
            out.append({'metric': 'Обход: страницы 2xx',
                        'before': s2['peak'], 'after': s2['latest'],
                        'delta_pct': -drop, 'severity': 'critical',
                        'text': f'роботу доступно меньше страниц: было {s2["peak"]}, '
                                f'стало {s2["latest"]} (−{drop}%) - часть страниц '
                                f'выпала из обхода'})
    # 4xx / 5xx - всплеск относительно фона.
    for code, label, sev in (('HTTP_4XX', 'Обход: ошибки 404 (4xx)', 'critical'),
                             ('HTTP_5XX', 'Обход: ошибки сервера (5xx)', 'fatal')):
        s = _series(ind.get(code))
        base = s['baseline'] or 0
        if (s['points'] >= 1 and s['latest'] and s['latest'] >= MIN_ABS
                and s['latest'] >= max(CRAWL_SPIKE_FACTOR * base, MIN_ABS)):
            out.append({'metric': label, 'before': int(base), 'after': s['latest'],
                        'delta_pct': None, 'severity': sev,
                        'text': f'всплеск: было ~{int(base)}, стало {s["latest"]} - '
                                f'{"сервер отдаёт ошибки роботу" if code == "HTTP_5XX" else "рост 404, страницы недоступны"}'})
    return out


def analyze_summary(summary, base_searchable=None, base_sqi=None):
    """Аномалии из текущего снимка /summary. base_* - медиана прошлых
    прогонов (или None, если эталона ещё нет)."""
    summary = summary or {}
    sqi = summary.get('sqi')
    searchable = summary.get('searchable_pages_count')
    excluded = summary.get('excluded_pages_count')
    problems = summary.get('site_problems') or {}
    out = []
    fatal = int(problems.get('FATAL', 0) or 0)
    crit = int(problems.get('CRITICAL', 0) or 0)
    if fatal:
        out.append({'metric': 'Фатальные проблемы', 'before': None, 'after': fatal,
                    'delta_pct': None, 'severity': 'fatal',
                    'text': f'фатальных проблем сайта: {fatal} (детали - лист '
                            f'«Ошибки сервисов»)'})
    if crit:
        out.append({'metric': 'Критические проблемы', 'before': None, 'after': crit,
                    'delta_pct': None, 'severity': 'critical',
                    'text': f'критических проблем сайта: {crit} (детали - лист '
                            f'«Ошибки сервисов»)'})
    # Страницы в поиске - падение от эталона.
    if (base_searchable and isinstance(searchable, int)
            and base_searchable - searchable >= MIN_ABS):
        drop = round((base_searchable - searchable) / base_searchable * 100)
        if drop >= PAGES_DROP_PCT:
            out.append({'metric': 'Страницы в поиске', 'before': base_searchable,
                        'after': searchable, 'delta_pct': -drop, 'severity': 'critical',
                        'text': f'страниц в поиске стало меньше: было ~{base_searchable}, '
                                f'стало {searchable} (−{drop}%) - страницы выпали из индекса'})
    # ИКС - падение от эталона.
    if (base_sqi and isinstance(sqi, int) and base_sqi > sqi):
        drop = round((base_sqi - sqi) / base_sqi * 100)
        if drop >= SQI_DROP_PCT:
            out.append({'metric': 'ИКС (SQI)', 'before': base_sqi, 'after': sqi,
                        'delta_pct': -drop, 'severity': 'possible',
                        'text': f'ИКС просел: было ~{base_sqi}, стало {sqi} (−{drop}%)'})
    return out, {'sqi': sqi, 'searchable': searchable, 'excluded': excluded,
                 'fatal': fatal, 'critical': crit}


# ── Эталон (best-effort, локальный кэш) ──────────────────────────────


def _baseline_path(project_id):
    return CACHE_DIR / f'wm-baseline-{project_id}.json'


def load_baseline(project_id):
    """{host: [{date, sqi, searchable, excluded}, ...]} или {}."""
    p = _baseline_path(project_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8')) or {}
    except Exception:
        return {}


def append_baseline(project_id, host, snap, today=None):
    """Дописать текущий снимок хоста; вернуть медианы searchable/sqi по
    ПРОШЛЫМ прогонам (до текущего) - или (None, None), если эталона ещё нет."""
    data = load_baseline(project_id)
    rows = data.get(host, [])
    prev_searchable = _median([r.get('searchable') for r in rows])
    prev_sqi = _median([r.get('sqi') for r in rows])
    rows.append({'date': (today or date.today()).isoformat(),
                 'sqi': snap.get('sqi'), 'searchable': snap.get('searchable'),
                 'excluded': snap.get('excluded')})
    data[host] = rows[-BASELINE_KEEP:]
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _baseline_path(project_id).write_text(
            json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass
    return prev_searchable, prev_sqi


# ── Сбор по всем хостам проекта ──────────────────────────────────────


def _panel_url(host_id):
    return f'https://webmaster.yandex.ru/site/{host_id}/dashboard/'


def fetch_webmaster_metrics(project_id, token, proxy_url=None, log=None):
    """Аномалии Вебмастера по всем верифицированным хостам проекта.
    Возвращает dict для отчёта (лист «Аналитика», секция «Аномалии»)."""
    def _log(m):
        if log:
            log(m)
    if not token:
        return {'available': False,
                'note': 'OAuth-токен Вебмастера не задан - аномалии по API '
                        'недоступны.'}
    from webmaster_api import _get, _norm_host, _project_hosts
    try:
        user = _get(token, '/user/', proxy_url)
        user_id = user.get('user_id')
        if not user_id:
            raise RuntimeError('user_id не получен')
        hosts_resp = _get(token, f'/user/{user_id}/hosts/', proxy_url)
        want = _project_hosts(project_id)
        selected = []
        for hh in hosts_resp.get('hosts', []) or []:
            host_url = hh.get('ascii_host_url') or hh.get('unicode_host_url') or ''
            host_norm = _norm_host(host_url) or _norm_host(hh.get('host_id', ''))
            if not want or host_norm in want:
                selected.append((host_norm, hh.get('host_id')))
        _log(f'Аномалии Вебмастера: хостов к проверке {len(selected)}')

        rows = []
        for host_norm, host_id in selected:
            if not host_id:
                continue
            try:
                summary = _get(
                    token, f'/user/{user_id}/hosts/{host_id}/summary', proxy_url)
            except Exception as e:
                _log(f'⚠ Аномалии ({host_norm}) summary: {e}')
                summary = None
            try:
                hist = _get(
                    token, f'/user/{user_id}/hosts/{host_id}/indexing/history',
                    proxy_url)
            except Exception as e:
                _log(f'⚠ Аномалии ({host_norm}) indexing/history: {e}')
                hist = None
            if summary is None and hist is None:
                continue
            sum_an, snap = analyze_summary(summary)
            prev_s, prev_q = append_baseline(project_id, host_norm, snap)
            # пересчёт с эталоном (если появился)
            sum_an, snap = analyze_summary(summary, prev_s, prev_q)
            crawl_an = analyze_crawl((hist or {}).get('indicators'))
            rows.append({
                'host': host_norm, 'panel_url': _panel_url(host_id),
                'sqi': snap['sqi'], 'searchable': snap['searchable'],
                'excluded': snap['excluded'],
                'anomalies': crawl_an + sum_an,
                'has_baseline': bool(prev_s or prev_q),
            })
        return {'available': True, 'hosts': rows}
    except PermissionError as e:
        return {'available': False, 'note': f'Доступ к API Вебмастера: {e}'}
    except Exception as e:
        return {'available': False, 'note': f'Аномалии Вебмастера не получены: {e}'}
