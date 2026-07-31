"""
convert_catalog.py - конвертация xlsx-каталога проекта в компактные
catalogs/{proj}-catalog.csv (+ отдельные каталоги стран) и сборка
catalogs/{proj}-subdomains.csv из уже готовой КП.

Запуск:
    python convert_catalog.py mpi /путь/к/Каталог_МПИ.xlsx

Зачем отдельный модуль (а не convert_catalogs.py): тот скрипт - разовая
миграция из старого Node.js-проекта, с жёстко прошитыми путями чужой машины
и запуском прямо на импорте. Здесь - CLI по образцу convert_kp.py: заказчик
присылает обновлённый каталог, мы прогоняем команду и коммитим CSV.

Поддомены берём НЕ из xlsx, а из catalogs/{proj}-kp.csv: КП - эталон (она сама
обновляется из Google-таблицы перед каждым прогоном), и так список доменов
гарантированно совпадает с тем, по которому идут остальные проверки.
"""
import csv
import sys
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import load_workbook

from kp import CATALOGS_DIR

# Раскладка каталога по проектам: какой лист xlsx в какой CSV кладём.
# Ключ sheets - {имя листа: имя файла в catalogs/}. Несколько листов = у части
# доменов свой каталог (см. host_catalogs в projects/<proj>.json).
CATALOG_LAYOUT = {
    # МПИ (МетПромИнтекс). Каталог плоский: у всех разделов путь /catalog/<slug>
    # без вложенности, уровни 1-5 живут в отдельных колонках и в URL не видны.
    # Листы «РФ» и «СНГ» расходятся (~30 слагов из 1259), поэтому СНГ-домены
    # ходят по своему каталогу - иначе чек-лист ловил бы ложные 404.
    'mpi': {
        'sheets': {
            'РФ':  'mpi-catalog.csv',
            'СНГ': 'mpi-cat-cis.csv',
        },
    },
}


def _url_column(ws) -> int:
    """Индекс колонки со ссылкой на страницу. Ищем по заголовку («url-адрес
    страницы»), а не по позиции: в присылаемых таблицах слева могут добавиться
    служебные колонки. Шапку ищем в первых 10 строках - выше неё бывают
    объединённые заголовки блоков («Теги», «Частные данные проекта»)."""
    for row in ws.iter_rows(min_row=1, max_row=10, values_only=True):
        for i, cell in enumerate(row):
            if cell and 'url' in str(cell).lower():
                return i
    raise RuntimeError('в листе не найдена колонка со ссылкой («url-адрес страницы»)')


def _convert_sheet(ws, dst: Path) -> int:
    """Лист каталога → CSV (url, type). Строки без http пропускаем - так
    отсеиваются шапка, строка счётчиков и «ссылка на правила» под ней.

    Тип у всех строк - «категория»: тегов/фильтров в каталоге МПИ нет (колонка
    «подбор по параметрам» пустая). Если в будущей выгрузке появятся теги -
    сюда добавится их распознавание, как у ИМП (parse_catalog читает тип)."""
    col = _url_column(ws)
    rows, seen = [], set()
    for row in ws.iter_rows(values_only=True):
        url = row[col] if len(row) > col else None
        if not url or not str(url).strip().lower().startswith('http'):
            continue
        u = str(url).strip()
        if u in seen:
            continue
        seen.add(u)
        rows.append([u, 'категория'])

    with open(dst, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['url', 'type'])
        w.writerows(rows)
    return len(rows)


def convert(project_id: str, xlsx_path: str) -> list[Path]:
    """Каталог проекта из xlsx → CSV-файлы в catalogs/. Возвращает пути."""
    if project_id not in CATALOG_LAYOUT:
        raise SystemExit(f'Нет раскладки каталога для проекта «{project_id}». '
                         f'Известные: {", ".join(CATALOG_LAYOUT)}')
    layout = CATALOG_LAYOUT[project_id]
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)

    out = []
    for sheet, filename in layout['sheets'].items():
        if sheet not in wb.sheetnames:
            raise SystemExit(f'в таблице нет листа «{sheet}» '
                             f'(есть: {", ".join(wb.sheetnames)})')
        dst = CATALOGS_DIR / filename
        n = _convert_sheet(wb[sheet], dst)
        print(f'{project_id}: лист «{sheet}» → {dst.name}, категорий: {n}')
        out.append(dst)
    return out


def build_subdomains(project_id: str) -> Path:
    """catalogs/{proj}-subdomains.csv из catalogs/{proj}-kp.csv.

    В КП домен лежит без схемы (metpromintex.ru, spb.metpromintex.ru) - здесь
    приводим к виду, который ждёт sources.parse_subdomains: https://<хост>/."""
    kp_path = CATALOGS_DIR / f'{project_id}-kp.csv'
    if not kp_path.is_file():
        raise SystemExit(f'нет базы КП {kp_path} - сначала прогоните convert_kp.py')

    rows, seen = [], set()
    with open(kp_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            host = (r.get('domain') or '').strip().strip('/').lower()
            if not host or host in seen:
                continue
            seen.add(host)
            rows.append([f'https://{host}/', (r.get('city') or '').strip(),
                         (r.get('country') or '').strip()])

    dst = CATALOGS_DIR / f'{project_id}-subdomains.csv'
    with open(dst, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['url', 'city', 'country'])
        w.writerows(rows)
    print(f'{project_id}: КП → {dst.name}, доменов: {len(rows)}')
    return dst


def main():
    if len(sys.argv) != 3:
        projects = '|'.join(CATALOG_LAYOUT)
        print(f'Использование: python convert_catalog.py <{projects}> <путь_к_xlsx>')
        sys.exit(1)
    project_id, xlsx_path = sys.argv[1], sys.argv[2]
    convert(project_id, xlsx_path)
    build_subdomains(project_id)


if __name__ == '__main__':
    main()
