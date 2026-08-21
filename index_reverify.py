"""
index_reverify.py - живая перепроверка кандидатов «404 в индексе».

Яндекс и Google дают КАНДИДАТОВ - страницы, которые поисковик считает битыми.
Но их код ответа - это СНИМОК на момент обхода и может устареть: страницу
починили, а поисковик ещё помнит 404. Плюс медленные страницы фильтров при
прозвоне легко принять за ошибку (таймаут ≠ «страница битая»).

Поэтому перед отчётом КАЖДЫЙ кандидат перепроверяется живым запросом сейчас,
и в отчёт попадают только те, что и правда отдают 404/410/5xx. Всё остальное
(200 - уже работает; таймаут/сеть - не подтвердилось) убирается. Так в отчёте
не остаётся ссылок, которые на самом деле открываются.

404-страницы отвечают быстро (сервер сразу отдаёт 404, без тяжёлого рендера),
поэтому перепроверка кандидатов быстрая - в отличие от слепого прозвона всего
sitemap, где почти все URL это медленные рабочие страницы фильтров.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

_TIMEOUT = 15          # сек на запрос (быстрее слепого прозвона)
_CONCURRENCY = 15


async def _check(session, url, proxy, sem):
    """(вердикт, код): 'dead'(404/410) / 'server'(5xx) / 'ok'(живёт) /
    'timeout' / 'error'."""
    to = aiohttp.ClientTimeout(total=_TIMEOUT)
    async with sem:
        # HEAD дешевле; сервер не любит HEAD (405/501/403) - добираем GET.
        for method in ('HEAD', 'GET'):
            try:
                async with session.request(method, url, timeout=to, proxy=proxy,
                                           allow_redirects=True) as r:
                    st = r.status
                    if method == 'HEAD' and st in (405, 501, 403):
                        continue
                    if st in (404, 410):
                        return 'dead', st
                    if st >= 500:
                        return 'server', st
                    return 'ok', st          # 2xx/3xx/прочее - не 404, работает
            except asyncio.TimeoutError:
                return 'timeout', None
            except Exception:
                return 'error', None
        return 'error', None


async def _check_all(urls, proxy):
    from http_checker import make_browser_headers
    sem = asyncio.Semaphore(_CONCURRENCY)
    conn = aiohttp.TCPConnector(limit=_CONCURRENCY, ttl_dns_cache=300)
    out = {}
    # url= первого адреса: все они с одного сайта, а он может быть под паролем.
    async with aiohttp.ClientSession(
            headers=make_browser_headers(url=(urls[0] if urls else '')),
            connector=conn) as s:
        tasks = [(u, asyncio.create_task(_check(s, u, proxy, sem))) for u in urls]
        for u, t in tasks:
            out[u] = await t
    return out


def reverify_index_404(check: dict, proxy_url=None, log=None) -> dict:
    """Перепроверить кандидатов живьём. Возвращает новый check, где остались
    только подтверждённые 404/410 (dead) и 5xx (errors), с ЖИВЫМ кодом ответа.
    200/таймаут/сеть - убраны как неподтверждённые."""
    def _log(m):
        if not log:
            return
        try:
            log('info', m)
        except TypeError:
            log(m)

    if not check or not check.get('available') or not check.get('hosts'):
        return check

    cand = []
    for h in check['hosts']:
        for k in ('dead', 'soft', 'errors'):
            for e in h.get(k) or []:
                if e.get('url'):
                    cand.append(e['url'])
    cand = list(dict.fromkeys(cand))
    if not cand:
        return check

    t0 = time.monotonic()
    _log(f'Перепроверяю вживую {len(cand)} кандидатов от поисковиков…')
    try:
        live = asyncio.run(_check_all(cand, proxy_url))
    except Exception as e:
        _log(f'⚠ перепроверка не удалась ({e}) - оставляю список как есть')
        return check

    new_hosts, kept, dropped = [], 0, 0
    for h in check['hosts']:
        # tech (служебные страницы в индексе) переносим как есть: это ДРУГАЯ
        # проверка со своей перепроверкой (index_tech_pages), здесь её находки
        # не судим - но и терять их нельзя.
        nh = {'host': h.get('host', ''), 'dead': [], 'soft': [], 'errors': [],
              'tech': list(h.get('tech') or []),
              'in_index_total': h.get('in_index_total', 0),
              'checked': h.get('checked', 0), 'ok': h.get('ok', 0),
              'redirects': h.get('redirects', 0)}
        for k in ('dead', 'soft', 'errors'):
            for e in h.get(k) or []:
                u = e.get('url')
                if not u:
                    continue
                verdict, st = live.get(u, ('error', None))
                if verdict == 'dead':
                    nh['dead'].append({**e, 'status': str(st)})
                    kept += 1
                elif verdict == 'server':
                    nh['errors'].append({**e, 'status': str(st)})
                    kept += 1
                else:
                    dropped += 1          # 200 / таймаут / сеть - не подтвердилось
        # Хост без 404 оставляем, если у него есть служебные страницы в
        # индексе - иначе находки другой проверки исчезли бы вместе с хостом.
        if nh['dead'] or nh['errors'] or nh['tech']:
            new_hosts.append(nh)

    out = dict(check)
    out['hosts'] = new_hosts
    out['reverified'] = True
    out['total_dead'] = sum(len(h['dead']) for h in new_hosts)
    out['total_soft'] = 0
    out['total_tech'] = sum(len(h['tech']) for h in new_hosts)
    _log(f'Перепроверка за {int(time.monotonic() - t0)}с: подтверждено '
         f'{kept}, убрано неподтверждённых {dropped}')
    return out
