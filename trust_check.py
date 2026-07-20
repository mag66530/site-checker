# -*- coding: utf-8 -*-
"""
trust_check.py - «Проверка показателей и траста проекта» (бесплатно).

Платные CheckTrust/Ahrefs/Semrush API не используем. Бесплатные источники:
  • Яндекс ИКС (индекс качества сайта) - поле sqi в Вебмастер API v4
    (/user/{uid}/hosts/{host_id}/). Токен webmaster_oauth (тот же, что
    «Ссылочный профиль»). Официальный траст-показатель, надёжно.
  • DR (Domain Rating-подобный ранг 0-100) - Open PageRank API
    (openpagerank.com), бесплатный ключ (секрет openpagerank_key). До 100
    доменов за запрос. Ahrefs free-чекер отпадает - Cloudflare Turnstile
    (капча), headless-скрейп не проходит.

Домены = верифицированные хосты проекта в Вебмастере (как в link_profile).
"""
try:
    import requests
except ImportError:
    requests = None

OPR_URL = 'https://openpagerank.com/api/v1.0/getPageRank'


def fetch_sqi(project_id, token, proxy_url=None, log=None):
    """ИКС (sqi) по верифицированным хостам проекта. → [{host, host_id, sqi}].
    Пустой список - если API недоступен."""
    def _log(m):
        if log:
            log(m)
    from webmaster_api import _get, _norm_host, _project_hosts
    user = _get(token, '/user/', proxy_url)
    uid = user.get('user_id')
    if not uid:
        raise RuntimeError('user_id не получен')
    hosts = _get(token, f'/user/{uid}/hosts/', proxy_url).get('hosts', []) or []
    want = _project_hosts(project_id)
    out = []
    for hh in hosts:
        host_url = (hh.get('ascii_host_url') or hh.get('unicode_host_url') or '')
        host_norm = _norm_host(host_url) or _norm_host(hh.get('host_id', ''))
        if want and host_norm not in want:
            continue
        hid = hh.get('host_id')
        sqi = None
        try:
            sqi = _get(token, f'/user/{uid}/hosts/{hid}/', proxy_url).get('sqi')
        except Exception as e:
            _log(f'⚠ ИКС ({host_norm}): {e}')
        out.append({'host': host_norm, 'host_id': hid, 'sqi': sqi})
    return out


def fetch_dr(domains, api_key, proxy_url=None, log=None):
    """DR-ранг (0-100) по доменам через Open PageRank. → {domain: rank|None}.
    Пусто, если ключа/requests нет."""
    def _log(m):
        if log:
            log(m)
    if requests is None or not api_key or not domains:
        return {}
    out = {}
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    headers = {'API-OPR': api_key}
    for i in range(0, len(domains), 100):
        chunk = domains[i:i + 100]
        params = [('domains[]', d) for d in chunk]
        try:
            r = requests.get(OPR_URL, headers=headers, params=params,
                             proxies=proxies, timeout=40)
        except Exception as e:
            _log(f'⚠ Open PageRank: сеть - {e}')
            continue
        if r.status_code >= 400:
            _log(f'⚠ Open PageRank: HTTP {r.status_code}: {r.text[:120]}')
            continue
        for row in (r.json() or {}).get('response', []) or []:
            dom = (row.get('domain') or '').lower()
            if row.get('status_code') == 200:
                try:
                    out[dom] = float(row.get('page_rank_decimal')
                                     or row.get('rank') or 0)
                except (TypeError, ValueError):
                    out[dom] = None
            else:
                out[dom] = None
    return out


def _bare(host):
    h = (host or '').lower()
    return h[4:] if h.startswith('www.') else h


def run(project_id, wm_token=None, opr_key=None, proxy_url=None, log=None):
    """Траст проекта: ИКС (Яндекс) + DR (Open PageRank). → dict для листа
    «Траст проекта» или {'available': False, 'note': ...}."""
    def _log(m):
        if log:
            log(m)
    if not wm_token:
        return {'available': False,
                'note': 'OAuth-токен Вебмастера не задан (webmaster_oauth_'
                        '<pid>) - ИКС недоступен.'}
    try:
        hosts = fetch_sqi(project_id, wm_token, proxy_url, log)
    except PermissionError as e:
        return {'available': False, 'note': f'Доступ к API Вебмастера: {e}'}
    except Exception as e:
        return {'available': False, 'note': f'ИКС не получен: {e}'}
    if not hosts:
        return {'available': False,
                'note': 'Верифицированных хостов проекта в Вебмастере нет.'}
    _log(f'Траст: хостов {len(hosts)}, тяну ИКС; '
         + ('DR через Open PageRank' if opr_key else 'DR пропущен (нет ключа)'))
    dr = fetch_dr([_bare(h['host']) for h in hosts], opr_key, proxy_url, log)
    for h in hosts:
        h['dr'] = dr.get(_bare(h['host']))
    return {
        'available': True, 'hosts': hosts, 'has_dr': bool(opr_key),
        'note_paid': 'CheckTrust / Ahrefs / Semrush - платные API, не '
                     'подключены. Ahrefs free-чекер за капчей (Turnstile).',
    }
