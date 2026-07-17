# -*- coding: utf-8 -*-
"""
yabusiness_run.py - запуск проверки Я.Бизнеса (лист «Я.Бизнес/GMB»).

    python yabusiness_run.py --project smu
    python yabusiness_run.py --project smu --from-env   # сессия из env

Сессия Яндекса: cache/autoclick_session_<pid>.b64 (та же, что автокликеры),
или env YABUSINESS_SESSION (base64 storage_state). Результат: консоль +
cache/yabusiness/<pid>.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from yabusiness_check import run

BASE = Path(__file__).parent
CACHE_DIR = BASE / 'cache' / 'yabusiness'


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True, help='id проекта (smu/imp/mpe)')
    ap.add_argument('--from-env', action='store_true',
                    help='сессия из env YABUSINESS_SESSION (base64)')
    ap.add_argument('--out', default=None, help='путь JSON-результата')
    args = ap.parse_args()

    b64 = os.environ.get('YABUSINESS_SESSION') if args.from_env else None
    proxy = os.environ.get('YABUSINESS_PROXY') or None
    res = run(args.project, session_b64=b64, proxy_url=proxy, log=print)

    if not res.get('available'):
        print('✗', res.get('note'))
    else:
        print(f'\nПоддоменов: {res["total_subdomains"]} · с орг под свой '
              f'город: {len(res["matched"])} · без орг: {len(res["missing"])} '
              f'· активных карточек: {res["active_orgs"]}')
        for m in res['matched']:
            print(f'  ✓ {m["city"]} → орг {m["org"]["permalink"]} '
                  f'[{m["org"]["region"]}]')
        if res['missing']:
            print(f'  ✗ без орг: '
                  + ', '.join(m['city'] for m in res['missing'][:15])
                  + (' …' if len(res['missing']) > 15 else ''))

    out = (Path(args.out) if args.out
           else CACHE_DIR / f'{args.project}.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'saved_at': datetime.now().isoformat(), **res},
                              ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nРезультат сохранён: {out}')
    sys.exit(0 if res.get('available') else 2)


if __name__ == '__main__':
    main()
