"""
collect_products.py – ручной сбор базы товарных ссылок с листингов.

Запускается локально РАЗ В МЕСЯЦ (на Streamlit Cloud месячный кэш не живёт,
поэтому база хранится в репозитории):

    python collect_products.py smu --proxy http://login:pass@host:port
    python collect_products.py imp
    python collect_products.py mpe
    python collect_products.py all          # все проекты подряд

Что делает:
  1. Берёт ВСЕ категории проекта из catalogs/{proj}-*.csv.
  2. Загружает первую страницу каждой категории на главном домене
     (пагинацию не обходим – договорённость: первой страницы достаточно).
  3. Собирает ссылки карточек товаров, складывает в
     catalogs/{proj}-products.csv + catalogs/{proj}-products-meta.json.

После сбора: git add catalogs/ → commit → push → передеплой приложения.

Прокси: --proxy или переменная окружения HTTP_PROXY. Для СМУ прокси
обязателен, если запускаете с зарубежного IP (сайт блокирует).
"""
import argparse
import asyncio
import sys
import time

from product_links import collect_product_links, save_product_links
from sources import load_project_config, load_sources, list_projects


def run_for_project(project_id: str, proxy: str | None, concurrency: int) -> bool:
    cfg = load_project_config(project_id)
    src = load_sources(cfg)
    total = len(src.categories)
    print(f'\n=== {cfg["name"]} ===')
    print(f'Категорий: {total}, фильтров: {len(src.filters)}')
    if cfg.get('use_proxy') and not proxy:
        print('⚠ В конфиге проекта use_proxy=true, а прокси не задан – '
              'с зарубежного IP сайт может блокировать. Продолжаю без прокси.')

    started = time.time()
    last_shown = {'pct': -1}

    def progress(done, n):
        pct = done * 100 // n
        if pct != last_shown['pct'] and pct % 5 == 0:
            last_shown['pct'] = pct
            elapsed = time.time() - started
            print(f'  {pct}% ({done}/{n}) – {elapsed:.0f} с')

    def log(level, msg):
        if level == 'warn':
            print(f'  ⚠ {msg}')

    collected = asyncio.run(collect_product_links(
        cfg, src.categories, src.filters,
        concurrency=concurrency,
        proxy_url=proxy,
        log=log,
        progress=progress,
    ))

    out = save_product_links(project_id, collected)
    elapsed = time.time() - started
    print(f'Готово за {elapsed:.0f} с: товаров {len(collected["links"])}, '
          f'категорий обработано {collected["categories_ok"]}/{collected["categories_total"]}')
    if collected['categories_failed']:
        print(f'⚠ Не загрузились {collected["categories_failed"]} категорий '
              f'(первые 10): {collected["failed_categories"][:10]}')
    print(f'Сохранено: {out}')
    print('Не забудьте закоммитить catalogs/ и задеплоить приложение.')
    return collected['categories_failed'] < collected['categories_total']


def main():
    parser = argparse.ArgumentParser(description='Сбор товарных ссылок с листингов')
    parser.add_argument('project', help="id проекта (smu / imp / mpe) или 'all'")
    parser.add_argument('--proxy', default=None, help='http://login:pass@host:port')
    parser.add_argument('--concurrency', type=int, default=8)
    args = parser.parse_args()

    if args.project == 'all':
        ids = [p['id'] for p in list_projects()]
    else:
        ids = [args.project]

    ok = True
    for pid in ids:
        try:
            ok = run_for_project(pid, args.proxy, args.concurrency) and ok
        except FileNotFoundError as e:
            print(f'❌ {pid}: не найден конфиг или каталог: {e}')
            ok = False
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
