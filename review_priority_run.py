# -*- coding: utf-8 -*-
"""
review_priority_run.py - отдельный процесс для проверки «приоритет докупки
отзывов» (Playwright не запустить внутри asyncio-раннера in-process).

  python review_priority_run.py --project smu --out cache/review_priority/smu-run.json

Прокси - через env REVIEW_PRIORITY_PROXY. Пишет результат в --out (JSON).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
import review_priority


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    proxy = os.environ.get('REVIEW_PRIORITY_PROXY') or None
    res = review_priority.run(args.project, proxy_url=proxy,
                              log=lambda m: print(m, flush=True))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False), encoding='utf-8')
    print(f'готово: available={res.get("available")}, '
          f'филиалов={res.get("total_branches", 0)}')


if __name__ == '__main__':
    main()
