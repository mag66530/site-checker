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

# Open PageRank переехал под Keywords Everywhere: ключ выдают там же, где ключ
# KE, и он выглядит как «opr_live_…». Новый API - POST с bearer-токеном.
OPR_URL_KE = 'https://openpagerank.keywordseverywhere.com/v1/domains/bulk'
# Старый адрес с заголовком API-OPR и ключом из 32 символов. Оставлен для
# ключей, выданных до переезда: у кого он ещё работает, тому менять нечего.
OPR_URL_LEGACY = 'https://openpagerank.com/api/v1.0/getPageRank'


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
            # ИКС живёт в /summary/, а НЕ в корне хоста: корень отдаёт только
            # ascii_host_url, host_display_name, main_mirror, verified - поля
            # sqi там нет вовсе. Раньше читали корень, и ИКС всегда выходил
            # пустым: прогон отчитывался «хостов 242», а в отчёте стояли
            # прочерки.
            sqi = _get(token, f'/user/{uid}/hosts/{hid}/summary/',
                       proxy_url).get('sqi')
        except Exception as e:
            _log(f'⚠ ИКС ({host_norm}): {e}')
        out.append({'host': host_norm, 'host_id': hid, 'sqi': sqi})
    _с_иксом = sum(1 for h in out if h['sqi'] is not None)
    if out and not _с_иксом:
        _log('⚠ ИКС: ни у одного хоста нет значения - проверьте права токена '
             'или подтверждение прав на сайты.')
    return out


def _quota_hint(resp):
    """Остаток квоты из заголовков ответа - его видно только в них. Пусто,
    если заголовков нет (старый API их не шлёт)."""
    left = resp.headers.get('X-Domains-Remaining')
    total = resp.headers.get('X-Domains-Limit')
    if left is None:
        return ''
    return f' (доменов в месяце осталось {left}' + (f' из {total})' if total else ')')


def _read_quota(resp, state):
    """Остаток и размер месячной квоты из заголовков → state. Заголовков может
    не быть (старый API, ошибка шлюза) - тогда просто ничего не трогаем."""
    for ключ, заголовок in (('remaining', 'X-Domains-Remaining'),
                            ('limit', 'X-Domains-Limit')):
        сырое = resp.headers.get(заголовок)
        if сырое is None:
            continue
        try:
            state[ключ] = int(str(сырое).strip())
        except (TypeError, ValueError):
            pass


def _dr_ke(domains, api_key, proxies, log, state):
    """Новый API (Keywords Everywhere): POST c bearer-токеном, до 100 доменов
    за раз. Ранг лежит в open_page_rank; found=false - домена нет в индексе,
    это ответ, а не сбой.

    state - словарь, куда складываем состояние квоты: она считается В ДОМЕНАХ
    за месяц, а не в запросах, поэтому один прогон крупного проекта способен
    её выбрать целиком. Прогон от этого не падает, но в отчёте DR у части
    хостов будет прочерк - и это надо объяснить, иначе выглядит как поломка."""
    out = {}
    headers = {'Authorization': f'Bearer {api_key}',
               'Content-Type': 'application/json'}
    for i in range(0, len(domains), 100):
        chunk = domains[i:i + 100]
        try:
            r = requests.post(OPR_URL_KE, headers=headers, proxies=proxies,
                              json={'domains': chunk, 'include_history': False},
                              timeout=60)
        except Exception as e:
            log(f'⚠ Open PageRank: сеть - {e}')
            continue
        _read_quota(r, state)
        if r.status_code == 401:
            log('⚠ Open PageRank: ключ не принят (HTTP 401). Проверьте, что в '
                'настройках проекта лежит ключ вида «opr_live_…» из кабинета '
                'Keywords Everywhere.')
            return out
        if r.status_code == 429:
            state['quota_out'] = True
            log('⚠ Open PageRank: исчерпан лимит (HTTP 429) - месячная квота '
                'доменов или запросы в минуту. DR останется прочерком.')
            return out
        if r.status_code >= 400:
            log(f'⚠ Open PageRank: HTTP {r.status_code}: {r.text[:160]}')
            continue
        try:
            data = r.json() or {}
        except ValueError:
            log('⚠ Open PageRank: ответ не разобрался как JSON')
            continue
        for row in data.get('results') or []:
            dom = (row.get('domain') or '').lower()
            if not row.get('found'):
                out[dom] = None
                continue
            try:
                out[dom] = float(row.get('open_page_rank'))
            except (TypeError, ValueError):
                out[dom] = None
        for dom in data.get('invalid') or []:
            out[str(dom).lower()] = None
        if i == 0:
            log(f'Open PageRank: ответ получен{_quota_hint(r)}')
        # Остаток дошёл до нуля - следующие куски уже не посчитаются, и
        # честнее сказать об этом сразу, а не молча отдать прочерки.
        if state.get('remaining') == 0:
            state['quota_out'] = True
            log('⚠ Open PageRank: месячная квота доменов исчерпана - '
                'остальные хосты остались без DR.')
            return out
    return out


def _dr_legacy(domains, api_key, proxies, log):
    """Старый API openpagerank.com: GET с заголовком API-OPR."""
    out = {}
    headers = {'API-OPR': api_key}
    for i in range(0, len(domains), 100):
        chunk = domains[i:i + 100]
        params = [('domains[]', d) for d in chunk]
        try:
            r = requests.get(OPR_URL_LEGACY, headers=headers, params=params,
                             proxies=proxies, timeout=40)
        except Exception as e:
            log(f'⚠ Open PageRank: сеть - {e}')
            continue
        if r.status_code >= 400:
            log(f'⚠ Open PageRank: HTTP {r.status_code}: {r.text[:160]}')
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


def fetch_dr(domains, api_key, proxy_url=None, log=None, state=None):
    """DR-ранг (0-10) по доменам через Open PageRank. → {domain: rank|None}.
    Пусто, если ключа/requests нет.

    Ключ «opr_live_…» - новый API под Keywords Everywhere; ключ старого
    образца (32 символа) - прежний адрес. Если старый ключ там уже не
    принимают, пробуем его же на новом API: у части аккаунтов ключ перенесли
    как есть.

    state - необязательный словарь под состояние квоты (quota_out, remaining,
    limit); заполняется только новым API, старый таких заголовков не шлёт."""
    def _log(m):
        if log:
            log(m)
    if state is None:
        state = {}
    if requests is None or not api_key or not domains:
        return {}
    key = str(api_key).strip()
    proxies = {'https': proxy_url, 'http': proxy_url} if proxy_url else None
    if key.startswith('opr_'):
        return _dr_ke(domains, key, proxies, _log, state)
    out = _dr_legacy(domains, key, proxies, _log)
    if not out:
        _log('Open PageRank: старый адрес молчит - пробую новый API.')
        out = _dr_ke(domains, key, proxies, _log, state)
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
    quota = {}
    dr = fetch_dr([_bare(h['host']) for h in hosts], opr_key, proxy_url, log,
                  state=quota)
    for h in hosts:
        h['dr'] = dr.get(_bare(h['host']))
    if opr_key:
        _с_dr = sum(1 for h in hosts if h.get('dr') is not None)
        _log(f'DR получен у {_с_dr} хостов из {len(hosts)}'
             + ('' if _с_dr == len(hosts) else
                ' - остальных нет в индексе Open PageRank либо кончилась квота.'))
    return {
        'available': True, 'hosts': hosts, 'has_dr': bool(opr_key),
        # Сообщение для отчёта - ТОЛЬКО когда квота кончилась. В остальных
        # случаях ключа нет вовсе или всё посчиталось, и лишняя плашка на
        # листе только мешает.
        'dr_quota_note': _quota_note(quota, hosts) if quota.get('quota_out')
                         else None,
        'note_paid': 'CheckTrust / Ahrefs / Semrush - платные API, не '
                     'подключены. Ahrefs free-чекер за капчей (Turnstile).',
    }


def _quota_note(quota, hosts):
    """Текст плашки в отчёте: сколько хостов успели получить DR до того, как
    кончилась месячная квота Open PageRank. Держим коротким - плашка стоит в
    узкой колонке блока траста (40 знаков в строке), длинный текст раздувает
    строку на пол-экрана. «У 1 из 12 хостов» вместо «1 хостов из 12» - заодно
    снимает вопрос со склонением."""
    посчитано = sum(1 for h in hosts if h.get('dr') is not None)
    предел = quota.get('limit')
    хвост = (f' Лимит - {предел} доменов в месяц, обновится 1-го числа.'
             if предел else ' Лимит обновится в начале месяца.')
    return (f'Квота Open PageRank исчерпана: DR посчитан у {посчитано} из '
            f'{len(hosts)} хостов.{хвост}')
