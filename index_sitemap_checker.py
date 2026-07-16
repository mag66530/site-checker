"""
index_sitemap_checker.py - источник «Sitemap» для проверки 404 в индексе.

Берём все URL из sitemap.xml проекта (парсер sitemap.collect_all_urls -
рекурсивно обходит sitemap-индекс) и прозваниваем на код ответа. Ловим то,
что МЫ САМИ заявляем в индекс (sitemap), но оно битое (404/410/5xx).

Нагрузка: sitemap может быть большим (десятки тысяч URL), прозвон всех
каждый день тяжёл для боевого сайта. Поэтому по умолчанию проверяем ПОРЦИЮ
за прогон (max_urls) с РОТАЦИЕЙ по дате: каждый день - следующее «окно»
адресов, за ceil(total/max_urls) дней покрываем весь sitemap. Состояние
хранить не нужно - окно выводится из даты (работает и в облаке/CI, где
диск между прогонами не живёт).

Результат - в форме index_export_parser (dead/soft/errors по хостам, у
каждой записи source='Sitemap'), чтобы merge_index_404 слил его с Яндексом.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
from urllib.parse import urlsplit

from sitemap import collect_all_urls
# Переиспользуем async-прозвон и классификацию кода ответа (тот же чекер,
# что для Вебмастер-API): GET url → 404/410/5xx/soft/ok.
from index_pages_checker import _check_all

DEFAULT_MAX_URLS = 3000


def _host_of(url: str) -> str:
    sp = urlsplit(url or '')
    h = (sp.netloc or '').lower()
    return h[4:] if h.startswith('www.') else h


def _window(urls: list, max_urls: int, day_ordinal: int) -> tuple:
    """(окно_адресов, всего). Ротация окна по дате: разбиваем отсортированный
    список на окна по max_urls и берём окно номер day_ordinal % n_windows."""
    total = len(urls)
    if not max_urls or total <= max_urls:
        return urls, total
    n_windows = (total + max_urls - 1) // max_urls
    wi = day_ordinal % n_windows
    return urls[wi * max_urls:(wi + 1) * max_urls], total


def _load_sitemap_url(project_id: str):
    from sources import load_project_config
    cfg = load_project_config(project_id)
    return (cfg or {}).get('sitemap_url'), cfg


def check_sitemap_404(project_id: str, proxy_url=None,
                      max_urls: int = DEFAULT_MAX_URLS,
                      day_ordinal: int = None, log=None) -> dict:
    """Проверить порцию URL из sitemap проекта на 404. Возвращает dict в форме
    index_404_check с source='sitemap' у записей."""
    def _log(msg):
        if not log:
            return
        try:
            log('info', msg)
        except TypeError:
            log(msg)

    out = {'available': False, 'source': 'sitemap', 'hosts': [],
           'total_checked': 0, 'total_dead': 0, 'total_soft': 0, 'error': None}

    sitemap_url, _cfg = _load_sitemap_url(project_id)
    if not sitemap_url:
        out['error'] = 'у проекта не задан sitemap_url'
        return out

    if day_ordinal is None:
        day_ordinal = datetime.date.today().toordinal()

    import time

    async def _work():
        urls = await collect_all_urls(sitemap_url, proxy_url=proxy_url,
                                      log=(lambda lvl, m: _log(m)) if log else None)
        urls = sorted(set(u for u in urls if u.startswith('http')))
        window, total = _window(urls, max_urls, day_ordinal)
        _log(f'Sitemap: всего URL {total}, проверяю в этот прогон {len(window)} '
             f'(ротация по дате)')
        if not window:
            return {}, total, 0
        pairs = [(_host_of(u), u) for u in window]
        # Прогресс: прозвон долгий, показываем ход и время, чтобы не выглядело
        # зависанием.
        t0 = time.monotonic()

        def _prog(done, tot):
            if done % 250 == 0 or done == tot:
                _log(f'Sitemap: прозвон {done}/{tot} '
                     f'({int(time.monotonic() - t0)}с)')
        checked = await _check_all(pairs, proxy_url, progress=_prog)
        _log(f'Sitemap: прозвон завершён за {int(time.monotonic() - t0)}с')
        return checked, total, len(window)

    try:
        checked, total, n_window = asyncio.run(_work())
    except Exception as e:
        out['error'] = f'sitemap не проверился: {e}'
        _log(f'⚠ {out["error"]}')
        return out

    # Группируем по хосту в форму index_404_check.
    by_host = {}
    for url, r in checked.items():
        host = _host_of(url)
        hb = by_host.setdefault(host, {
            'host': host, 'dead': [], 'soft': [], 'errors': [],
            'in_index_total': 0, 'checked': 0, 'ok': 0, 'redirects': 0})
        hb['checked'] += 1
        verdict = r.get('verdict')
        entry = {'url': url, 'status': r.get('status'), 'source': 'Sitemap',
                 'reason': r.get('reason', '')}
        if verdict == 'dead':
            hb['dead'].append(entry)
        elif verdict == 'soft':
            hb['soft'].append(entry)
        elif verdict in ('server_error', 'client_error', 'no_response'):
            hb['errors'].append(entry)
        elif verdict == 'redirect':
            hb['redirects'] += 1
        else:
            hb['ok'] += 1

    out['available'] = True
    for host, hb in sorted(by_host.items()):
        out['hosts'].append(hb)
        out['total_checked'] += hb['checked']
        out['total_dead'] += len(hb['dead'])
        out['total_soft'] += len(hb['soft'])
    return out


def _main():
    ap = argparse.ArgumentParser(description='404 по sitemap проекта')
    ap.add_argument('project')
    ap.add_argument('--max-urls', type=int, default=DEFAULT_MAX_URLS)
    ap.add_argument('--proxy', default=os.environ.get('proxy_url'))
    a = ap.parse_args()
    res = check_sitemap_404(a.project, proxy_url=a.proxy, max_urls=a.max_urls,
                            log=lambda lvl, m: print(m))
    if res.get('error'):
        print(f'Ошибка: {res["error"]}', file=sys.stderr)
        sys.exit(1)
    print(f'\nПроверено {res["total_checked"]} URL из sitemap; '
          f'битых 404/410: {res["total_dead"]}, soft: {res["total_soft"]}')
    for h in res['hosts']:
        for r in (h['dead'] + h['errors'])[:20]:
            print(f'   {r.get("status")}  {r["url"]}')


if __name__ == '__main__':
    _main()
