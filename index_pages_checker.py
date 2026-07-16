"""
index_pages_checker.py - мониторинг 404 среди страниц В ИНДЕКСЕ Яндекса
(пункт чек-листа «Проверка страниц в индексе на 404-ошибку, регулярный
мониторинг»).

Идея: берём выборку URL, которые Яндекс СЕЙЧАС держит в поиске (Вебмастер
API v4, `search-urls/in-search/samples`), и прозваниваем каждый на код
ответа. Страница, которая числится в индексе, но отдаёт 404/410 (или
soft-404 - код 200 на заглушке «страница не найдена»), - баг: посетитель
из выдачи попадает в никуда, а поисковик тратит на неё обход.

Почему Яндекс, а не Google: у GSC нет публичного API со списком
проиндексированных URL (URL Inspection - поштучно, с суточной квотой),
поэтому источником «в индексе» служит Яндекс.Вебмастер. Токен - тот же
`webmaster:hostinfo`, что и в webmaster_api.py (секрет
`yandex_oauth_<pid>` / `webmaster_oauth_<pid>`).

Регулярность обеспечивает планировщик (run_scheduled.py + GitHub Actions):
проверка включена в ежедневный прогон, результат - лист «404 в индексе» в
xlsx-отчёте и строка в Telegram.

Отдельно от sitemap_audit.py (тот валидирует ФОРМАТ sitemap, не прозванивает
URL) и от page404_checker.py (тот проверяет ШАБЛОН 404 на несуществующем
адресе). Здесь - живые URL, которые реально в индексе.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Optional

import aiohttp

# Переиспользуем авторизацию/утилиты Вебмастера (тот же аккаунт и токен):
# _get - HTTPS GET с OAuth и ретраями, _norm_host/_project_hosts - матчинг
# хостов проекта из catalogs/<pid>-subdomains.csv.
from webmaster_api import _get, _norm_host, _project_hosts

IN_SEARCH_SAMPLES = '/user/{uid}/hosts/{hid}/search-urls/in-search/samples/'

# Сколько URL из индекса брать на один хост по умолчанию. Эндпоинт отдаёт
# ВЫБОРКУ (не весь индекс) - берём столько, сколько успеваем прозвонить в
# ежедневном прогоне, без чрезмерной нагрузки на боевой сайт.
DEFAULT_MAX_PER_HOST = 300
_PAGE_LIMIT = 100          # максимум записей на страницу у Вебмастера (1..100)
_CONCURRENCY = 12          # одновременных прозвонов (баланс скорость/вежливость)
_TIMEOUT_S = 25
_HEAD_BYTES = 16384        # сколько байт тела читаем ради <title> (soft-404)

_RE_TITLE = re.compile(r'<title\b[^>]*>(.*?)</title>', re.I | re.S)

# Маркеры soft-404 в <title>. Сознательно консервативно - по заголовку, а не
# по любому вхождению «404» (у товара «404» бывает в артикуле/размере).
_SOFT_404_MARKERS = (
    'страница не найдена', 'страница не существует', 'страница удалена',
    'ничего не найдено', 'нет такой страницы', 'page not found',
    'ошибка 404', '404 ошибка', 'not found',
)


# ── Чистая логика (тестируется без сети) ─────────────────────────────

def parse_samples(resp: dict) -> list:
    """URL-ы из ответа `.../in-search/samples`. Терпимо к схеме."""
    out = []
    for s in (resp or {}).get('samples', []) or []:
        if isinstance(s, dict):
            u = s.get('url') or s.get('page') or ''
        else:
            u = str(s or '')
        u = u.strip()
        if u:
            out.append(u)
    return out


def _extract_title(html: str) -> str:
    m = _RE_TITLE.search(html or '')
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''


def looks_soft_404(title: str) -> bool:
    """Похоже ли на soft-404 по <title>: заглушка «страница не найдена»,
    отдающая 200. Консервативно - только явные маркеры."""
    t = (title or '').strip().lower()
    return any(m in t for m in _SOFT_404_MARKERS)


def classify_index_url(status: Optional[int], redirected: bool,
                       soft404: bool, error: Optional[str]) -> tuple:
    """(вердикт, человекочитаемая причина) для одного URL из индекса.

    Вердикты:
      'dead'         - 404/410: страница в индексе, но её нет (баг);
      'soft'         - 200, но контент «страница не найдена» (soft-404, баг);
      'client_error' - прочие 4xx (401/403/429): закрыта или блокировка бота;
      'server_error' - 5xx: сервер не отдал страницу;
      'no_response'  - таймаут/сеть: не ответила;
      'redirect'     - 2xx после переадресации (обычно норма, инфо);
      'ok'           - 2xx без переадресации.

    Только 404/410 = 'dead' (как в http_checker._link_status): 403/401
    чаще анти-бот блок, а не реально удалённая страница - в отдельный бакет,
    чтобы не плодить ложные «битые в индексе».
    """
    if error == 'timeout':
        return 'no_response', 'не ответила (таймаут)'
    if error:
        return 'no_response', 'нет соединения'
    if status is None:
        return 'no_response', 'нет ответа'
    if status in (404, 410):
        return 'dead', f'отдаёт {status} - страницы нет, но она в индексе'
    if status >= 500:
        return 'server_error', f'ошибка сервера {status}'
    if 200 <= status < 300:
        if soft404:
            return 'soft', 'код 200, но это заглушка «страница не найдена» (soft-404)'
        if redirected:
            return 'redirect', f'переадресация на рабочую (итоговый код {status})'
        return 'ok', ''
    if 300 <= status < 400:
        return 'redirect', f'переадресация (код {status})'
    if 400 <= status < 500:
        return 'client_error', (f'отдаёт {status} - закрытая страница '
                                f'или блокировка бота (проверить вручную)')
    return 'no_response', f'код {status}'


# ── Выборка «страниц в поиске» из Вебмастера ─────────────────────────

def _resolve_hosts(token: str, project_id: str, proxy_url: Optional[str]):
    """(user_id, [(host_norm, host_id)]) - хосты проекта из аккаунта."""
    user = _get(token, '/user/', proxy_url)
    uid = user.get('user_id')
    if not uid:
        raise RuntimeError('user_id не получен')
    resp = _get(token, f'/user/{uid}/hosts/', proxy_url)
    api_hosts = resp.get('hosts', []) or []
    want = _project_hosts(project_id)
    selected = []
    for h in api_hosts:
        host_url = h.get('ascii_host_url') or h.get('unicode_host_url') or ''
        host_norm = _norm_host(host_url) or _norm_host(h.get('host_id', ''))
        if not host_norm:
            continue
        if not want or host_norm in want:
            selected.append((host_norm, h.get('host_id')))
    # Ни один не совпал с каталогом проекта - берём все (как webmaster_api).
    if want and not selected:
        selected = [(_norm_host(h.get('ascii_host_url', '')), h.get('host_id'))
                    for h in api_hosts if h.get('host_id')]
    return uid, selected


def fetch_indexed_sample(token: str, uid, host_id: str,
                         proxy_url: Optional[str], max_urls: int):
    """Выборка URL в поиске одного хоста + общее число страниц в индексе.
    Возвращает (urls, total_count). Пагинация по _PAGE_LIMIT."""
    urls, offset, total = [], 0, 0
    while len(urls) < max_urls:
        want = min(_PAGE_LIMIT, max_urls - len(urls))
        resp = _get(token, IN_SEARCH_SAMPLES.format(uid=uid, hid=host_id),
                    proxy_url, params={'offset': offset, 'limit': want})
        total = resp.get('count', total) or total
        batch = parse_samples(resp)
        if not batch:
            break
        urls.extend(batch)
        offset += len(batch)
        if len(batch) < want or (total and offset >= total):
            break
    return urls[:max_urls], total


# ── Прозвон URL (async) ──────────────────────────────────────────────

async def _check_one(session, url: str, proxy_url, sem) -> dict:
    to = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    async with sem:
        try:
            async with session.get(url, timeout=to, proxy=proxy_url,
                                   allow_redirects=True) as r:
                status = r.status
                redirected = bool(r.history)
                soft = False
                if 200 <= status < 300:
                    raw = await r.content.read(_HEAD_BYTES)
                    soft = looks_soft_404(
                        _extract_title(raw.decode('utf-8', 'replace')))
                verdict, reason = classify_index_url(status, redirected, soft, None)
                return {'url': url, 'status': status, 'redirected': redirected,
                        'final_url': str(r.url), 'verdict': verdict,
                        'reason': reason}
        except asyncio.TimeoutError:
            v, rz = classify_index_url(None, False, False, 'timeout')
            return {'url': url, 'status': None, 'redirected': False,
                    'final_url': '', 'verdict': v, 'reason': rz}
        except Exception:
            v, rz = classify_index_url(None, False, False, 'error')
            return {'url': url, 'status': None, 'redirected': False,
                    'final_url': '', 'verdict': v, 'reason': rz}


async def _check_all(pairs: list, proxy_url, progress=None) -> dict:
    """pairs: [(host_norm, url)] → {url: результат}. Одна общая сессия.
    progress(done, total) - опциональный колбэк по мере готовности (для лога
    прогресса при долгом прозвоне)."""
    from http_checker import make_browser_headers
    sem = asyncio.Semaphore(_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=_CONCURRENCY, ttl_dns_cache=300)
    results = {}
    total = len(pairs)
    async with aiohttp.ClientSession(headers=make_browser_headers(),
                                     connector=connector) as session:
        tasks = [asyncio.create_task(_check_one(session, u, proxy_url, sem))
                 for _, u in pairs]
        done = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results[r['url']] = r
            done += 1
            if progress:
                try:
                    progress(done, total)
                except Exception:
                    pass
    return results


# ── Точка входа ──────────────────────────────────────────────────────

def check_index_404(project_id: str, token: str, proxy_url: Optional[str] = None,
                    max_urls_per_host: int = DEFAULT_MAX_PER_HOST,
                    max_hosts: Optional[int] = None, log=None) -> dict:
    """Проверить страницы в индексе на 404. Возвращает:
    {'available', 'source', 'hosts': [{host, in_index_total, checked, dead[],
    soft[], errors[], redirects, ok}], 'total_checked', 'total_dead',
    'total_soft', 'error'}.
    Сеть/токен недоступны → available=False + error (прогон не падает)."""
    def _log(msg):
        if not log:
            return
        try:
            log('info', msg)
        except TypeError:
            log(msg)

    out = {'available': False, 'source': 'yandex_webmaster', 'hosts': [],
           'total_checked': 0, 'total_dead': 0, 'total_soft': 0, 'error': None}
    if not token:
        out['error'] = 'нет OAuth-токена Вебмастера (yandex_oauth_<pid>)'
        return out

    try:
        uid, hosts = _resolve_hosts(token, project_id, proxy_url)
    except Exception as e:
        out['error'] = f'не удалось получить список хостов: {e}'
        _log(f'⚠ 404-в-индексе: {out["error"]}')
        return out

    if max_hosts:
        hosts = hosts[:max_hosts]
    if not hosts:
        out['error'] = 'в аккаунте Вебмастера нет хостов проекта'
        return out

    # 1) Собрать выборки URL из индекса (последовательные запросы к API).
    host_urls = []   # [(host_norm, [urls], total_in_index)]
    for host_norm, host_id in hosts:
        if not host_id:
            continue
        try:
            urls, total = fetch_indexed_sample(token, uid, host_id, proxy_url,
                                               max_urls_per_host)
            host_urls.append((host_norm, urls, total))
            _log(f'  {host_norm}: в индексе ~{total}, взял выборку {len(urls)}')
        except Exception as e:
            host_urls.append((host_norm, [], 0))
            _log(f'⚠ 404-в-индексе ({host_norm}): выборку не получил: {e}')

    # 2) Прозвонить все URL (async, одна сессия на все хосты).
    pairs = [(hn, u) for hn, urls, _ in host_urls for u in urls]
    checked = asyncio.run(_check_all(pairs, proxy_url)) if pairs else {}

    # 3) Сгруппировать по хосту.
    out['available'] = True
    for host_norm, urls, total in host_urls:
        hres = [checked[u] for u in urls if u in checked]
        dead = [r for r in hres if r['verdict'] == 'dead']
        soft = [r for r in hres if r['verdict'] == 'soft']
        errs = [r for r in hres if r['verdict'] in
                ('server_error', 'no_response', 'client_error')]
        redirs = sum(1 for r in hres if r['verdict'] == 'redirect')
        out['hosts'].append({
            'host': host_norm, 'in_index_total': total, 'checked': len(hres),
            'dead': dead, 'soft': soft, 'errors': errs,
            'redirects': redirs,
            'ok': sum(1 for r in hres if r['verdict'] == 'ok'),
        })
        out['total_checked'] += len(hres)
        out['total_dead'] += len(dead)
        out['total_soft'] += len(soft)
    return out


def _resolve_token(pid: str) -> Optional[str]:
    return (os.environ.get(f'yandex_oauth_{pid}')
            or os.environ.get(f'webmaster_oauth_{pid}')
            or os.environ.get('yandex_oauth') or os.environ.get('webmaster_oauth'))


def _main():
    ap = argparse.ArgumentParser(
        description='404 среди страниц в индексе (Яндекс.Вебмастер)')
    ap.add_argument('project', help='id проекта (smu/imp/mpe)')
    ap.add_argument('--max-per-host', type=int, default=DEFAULT_MAX_PER_HOST)
    ap.add_argument('--max-hosts', type=int, default=None)
    ap.add_argument('--proxy', default=os.environ.get('proxy_url'))
    a = ap.parse_args()

    token = _resolve_token(a.project)
    if not token:
        print(f'Нет токена: задай yandex_oauth_{a.project} в окружении',
              file=sys.stderr)
        sys.exit(2)

    res = check_index_404(a.project, token, proxy_url=a.proxy,
                          max_urls_per_host=a.max_per_host,
                          max_hosts=a.max_hosts,
                          log=lambda lvl, m: print(m))
    if res.get('error'):
        print(f'Ошибка: {res["error"]}', file=sys.stderr)
        sys.exit(1)
    print(f'\nПроверено {res["total_checked"]} страниц из индекса; '
          f'битых 404/410: {res["total_dead"]}, soft-404: {res["total_soft"]}')
    for h in res['hosts']:
        if h['dead'] or h['soft']:
            print(f'\n{h["host"]}: 404/410={len(h["dead"])}, '
                  f'soft-404={len(h["soft"])} (из {h["checked"]} проверенных)')
            for r in h['dead'][:20]:
                print(f'   404  {r["url"]}')
            for r in h['soft'][:20]:
                print(f'   soft {r["url"]}')


if __name__ == '__main__':
    _main()
