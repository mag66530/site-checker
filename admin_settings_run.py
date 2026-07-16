# -*- coding: utf-8 -*-
"""
admin_settings_run.py - запуск проверки «работают функции настройки»
в админке (поддомены/категории/товары/тех.страницы).

    python admin_settings_run.py --project smu --test        # тестовый контур
    python admin_settings_run.py --project smu               # прод (admin.local.json)
    python admin_settings_run.py --project smu --test --no-roundtrip

Креды: forms_tester/projects/<pid>/admin.local.json (прод) или
admin.test.local.json (тест). Результат: консоль + cache/admin-settings/<pid>.json.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from admin_settings_check import (check_admin_settings, load_admin_creds,
                                  summarize)

BASE = Path(__file__).parent
CACHE_DIR = BASE / 'cache' / 'admin-settings'


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True, help='id проекта (smu/mpe/…)')
    ap.add_argument('--test', action='store_true',
                    help='тестовый контур (admin.test.local.json)')
    ap.add_argument('--domain', default=None,
                    help='домен админки (если не задан в json-файле)')
    ap.add_argument('--no-roundtrip', action='store_true',
                    help='без тест-сохранения (только рендер разделов)')
    ap.add_argument('--headed', action='store_true',
                    help='показывать браузер')
    args = ap.parse_args()

    proj_dir = BASE / 'forms_tester' / 'projects' / args.project
    creds = load_admin_creds(proj_dir, test=args.test)
    if not creds:
        name = 'admin.test.local.json' if args.test else 'admin.local.json'
        print(f'✗ Нет кредов: заполни {proj_dir / name} '
              f'({{"domain": "...", "login": "...", "password": "..."}})')
        sys.exit(2)
    if args.domain:
        creds['domain'] = args.domain
    if not creds.get('domain'):
        print('✗ Домен админки не задан (--domain или поле domain в json)')
        sys.exit(2)

    print(f'Проверка настроек в админке: {creds["domain"]} '
          f'({"тест" if args.test else "прод"}, '
          f'{"без" if args.no_roundtrip else "с"} тест-сохранением)')
    res = check_admin_settings(creds, roundtrip=not args.no_roundtrip,
                               log=print, headless=not args.headed)

    if not res.get('available'):
        print('✗', res.get('note'))
        sys.exit(2)

    print(f'\nИтог: {res["verdict"].upper()}')
    for c in res['checks']:
        mark = '✅' if c['ok'] else '❌'
        print(f'  {mark} {c["title"]}: {c["detail"]}')
        for w in c.get('warnings') or []:
            print(f'     ⚠ {w}')

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f'{args.project}{"-test" if args.test else ""}.json'
    out.write_text(json.dumps(
        {'saved_at': datetime.now().isoformat(), **res},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nРезультат сохранён: {out}')
    sys.exit(0 if res['verdict'] != 'fail' else 1)


if __name__ == '__main__':
    main()
