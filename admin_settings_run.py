# -*- coding: utf-8 -*-
"""
admin_settings_run.py - запуск проверки «работают функции настройки»
в админке (поддомены/категории/товары/тех.страницы).

    python admin_settings_run.py --project smu --test        # тестовый контур
    python admin_settings_run.py --project smu               # прод (admin.local.json)
    python admin_settings_run.py --project smu --test --no-roundtrip
    python admin_settings_run.py --project smu --from-env    # креды из env

Креды: forms_tester/projects/<pid>/admin.local.json (прод) или
admin.test.local.json (тест); с --from-env - JSON из переменной окружения
ADMIN_SETTINGS_CREDS (так их передаёт прогон чек-листа: пароль не на диск).
Результат: консоль + cache/admin-settings/<pid>.json (или --out).
"""
import argparse
import json
import os
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
    ap.add_argument('--crud', action='store_true',
                    help='проверять CRUD-операции поддоменов/категорий')
    ap.add_argument('--product-crud', action='store_true',
                    help='CRUD товаров (создание/сортировка/мультикатегория)')
    ap.add_argument('--tech-crud', action='store_true',
                    help='CRUD техстраниц (наличие функций, файлы не трогаем)')
    ap.add_argument('--no-execute', action='store_true',
                    help='CRUD без записи - только наличие функций')
    ap.add_argument('--headed', action='store_true',
                    help='показывать браузер')
    ap.add_argument('--from-env', action='store_true',
                    help='креды из env ADMIN_SETTINGS_CREDS (JSON)')
    ap.add_argument('--out', default=None,
                    help='путь JSON-результата (по умолчанию cache/…)')
    args = ap.parse_args()

    proj_dir = BASE / 'forms_tester' / 'projects' / args.project
    if args.from_env:
        try:
            creds = json.loads(os.environ.get('ADMIN_SETTINGS_CREDS') or '{}')
        except Exception:
            creds = {}
        if not (creds.get('login') and creds.get('password')):
            print('✗ ADMIN_SETTINGS_CREDS пуст или без login/password')
            sys.exit(2)
    else:
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

    _src = ('креды из прогона' if args.from_env
            else 'тест' if args.test else 'прод')
    _parts = []
    if args.crud:
        _parts.append('CRUD подд./кат.')
    if args.product_crud:
        _parts.append('CRUD товаров')
    if args.tech_crud:
        _parts.append('CRUD техстраниц')
    _crud_txt = (', '.join(_parts) + (' с записью' if not args.no_execute
                 else ' (наличие)')) if _parts else 'без CRUD'
    print(f'Проверка настроек в админке: {creds["domain"]} ({_src}, {_crud_txt})')
    res = check_admin_settings(creds, crud=args.crud,
                               product_crud=args.product_crud,
                               tech_crud=args.tech_crud,
                               execute=not args.no_execute,
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
    out = (Path(args.out) if args.out
           else CACHE_DIR / f'{args.project}{"-test" if args.test else ""}.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {'saved_at': datetime.now().isoformat(), **res},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nРезультат сохранён: {out}')
    sys.exit(0 if res['verdict'] != 'fail' else 1)


if __name__ == '__main__':
    main()
