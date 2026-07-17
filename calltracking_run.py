"""
calltracking_run.py - браузерная проверка замены рекламного номера
(уровень 2), отдельным процессом с Playwright. Пункт чек-листа «Проверка
работы замены рекламного номера (мониторинг)».

30-мин прогон ходит по HTTP без браузера и видит только статический
(SEO) номер. Реальную подмену на рекламный номер выполняет JS коллтрекинга
(Sipuni) при рекламном визите - поэтому проверяем в настоящем браузере:
открываем главную города с меткой ?utm_source=yandex и смотрим, стал ли
номер равен рекламному phone_ad из КП.

Запуск (хосты приходят файлом-списком от runner_30min):
    python calltracking_run.py --project smu --hosts-file cache/ct_hosts_smu.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / 'cache'
CACHE.mkdir(exist_ok=True)

MAX_CITIES = 25       # верхняя граница, чтобы прогон не растянулся


def _build_cities(project: str, hosts: list) -> list:
    """(город, https://host/, phone_ad) по хостам, у которых в КП есть
    рекламный номер. Без КП или без phone_ad город пропускаем."""
    from kp import load_kp, _norm_host
    kp = load_kp(project) or {}
    cities, seen = [], set()
    for h in hosts:
        host = _norm_host(h)
        if not host or host in seen:
            continue
        seen.add(host)
        row = kp.get(host)
        if not row or not (row.phone_ad or '').strip():
            continue
        cities.append((row.city or host, f'https://{host}/', row.phone_ad))
        if len(cities) >= MAX_CITIES:
            break
    return cities


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--hosts-file', required=True)
    a = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    out_path = CACHE / f'calltracking_{a.project}.json'
    try:
        hosts = json.loads(Path(a.hosts_file).read_text(encoding='utf-8-sig')) or []
    except Exception as e:  # noqa: BLE001
        hosts = []
        log(f'⚠ список хостов не прочитан: {e}')

    cities = _build_cities(a.project, hosts)
    if not cities:
        log('Замена рекл. номера: городов с рекламным номером в КП нет - пропуск.')
        out_path.write_text(json.dumps(
            {'available': False, 'results': [],
             'note': 'в КП нет городов с рекламным номером (phone_ad)'},
            ensure_ascii=False), encoding='utf-8')
        return

    # Убедимся, что браузер готов (в облаке доустанавливает Chromium).
    try:
        from browser_setup import ensure_browser
        ok, msg = ensure_browser()
        log(f'Браузер: {msg}')
        if not ok:
            out_path.write_text(json.dumps(
                {'available': False, 'results': [], 'note': f'браузер: {msg}'},
                ensure_ascii=False), encoding='utf-8')
            return
    except Exception as e:  # noqa: BLE001
        log(f'⚠ браузер не готов: {e}')

    try:
        from calltracking_browser import run
        results = run(cities, log=log)
        res = {'available': True, 'results': results}
    except Exception as e:  # noqa: BLE001
        log(f'⚠ Замена рекл. номера: {e}')
        res = {'available': True, 'results': [],
               'note': f'браузер не запустился: {e}'}

    out_path.write_text(json.dumps(res, ensure_ascii=False), encoding='utf-8')
    _ok = sum(1 for r in res['results'] if r.get('status') == 'replaced_ok')
    _bad = sum(1 for r in res['results'] if r.get('status') == 'not_replaced')
    log(f'✓ Замена рекл. номера: работает {_ok}, не работает {_bad}, '
        f'всего {len(res["results"])}')


if __name__ == '__main__':
    main()
