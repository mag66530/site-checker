"""
convert_kp.py — разовая конвертация «Карты присутствия» (xlsx) в компактный
catalogs/{proj}-kp.csv для сверки контактов.

Запуск:
    python convert_kp.py smu /путь/к/КП_СМУ.xlsx
    python convert_kp.py imp /путь/к/КП_ИМП.xlsx
    python convert_kp.py mpe /путь/к/КП_МПЭ.xlsx

В CSV кладём ТОЛЬКО контактные поля (домен, город, телефоны SEO/реклама/общий,
почта, адрес) — исходный xlsx с внутренними данными в репозиторий не идёт.
"""
import csv
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

from kp import KP_LAYOUT, CATALOGS_DIR, _norm_host
from kp import split_phones as _split_phones


def _phone_columns(headers):
    """Индексы всех телефонных колонок (Общий/Реклама/SEO/Сотовый/основной/
    подменные/ватсап). Городскую колонку «Город» (название города) исключаем."""
    out = []
    for i, h in enumerate(headers):
        if h is None:
            continue
        ht = str(h).lower().replace('\n', ' ').strip()
        if ht == 'город':
            continue
        if any(k in ht for k in ('город', 'сотов', 'мобильн', 'основн',
                                 'подменн', 'ватсап', 'для ватсап')):
            out.append(i)
    return out


def _find_header_row(ws, max_scan=6):
    """Найти строку заголовков — где встречаются 'город' и 'адрес'."""
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan, values_only=True), 1):
        cells = [str(c).lower() if c else '' for c in row]
        joined = ' '.join(cells)
        if 'город' in joined and 'адрес' in joined:
            return i, row
    # запасной вариант — первая строка
    first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return 1, first


def _col(headers, *keywords, exact=None):
    """Индекс колонки, чей заголовок содержит все keywords (или равен exact)."""
    for i, h in enumerate(headers):
        if h is None:
            continue
        ht = str(h).lower().replace('\n', ' ').strip()
        if exact is not None and ht == exact:
            return i
        if keywords and all(k in ht for k in keywords):
            return i
    return None


def convert(project_id: str, xlsx_path: str) -> Path:
    layout = KP_LAYOUT[project_id]
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[layout['sheet']]

    hdr_row_idx, headers = _find_header_row(ws)

    ci_city = _col(headers, exact='город') or _col(headers, 'город')
    ci_addr = _col(headers, exact='адрес') or _col(headers, 'адрес')
    ci_email = _col(headers, 'e-mail') or _col(headers, 'почта') or _col(headers, 'email')
    ci_url = (_col(headers, 'url', 'магазин') or _col(headers, 'домен')
              or _col(headers, 'ссылка') or _col(headers, 'url'))
    ci_seo = _col(headers, *layout['phone_seo'])
    ci_ad = _col(headers, *layout['phone_ad'])
    ci_common = _col(headers, *layout['phone_common'])
    phone_cols = _phone_columns(headers)

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return ''
        v = row[idx]
        return '' if v is None else str(v).strip()

    rows_out = []
    seen = set()
    for row in ws.iter_rows(min_row=hdr_row_idx + 1, values_only=True):
        if not row or not any(row):
            continue
        city = cell(row, ci_city)
        url = cell(row, ci_url)
        # домен: из url-колонки, либо ищем по строке любой домен сети
        # (.ru/.uz/.kz/.by — у МПЭ есть Узбекистан и Казахстан)
        host = _norm_host(url)
        if not host:
            joined = ' '.join(str(c) for c in row if c)
            m = re.search(r'([a-z0-9-]+\.)*(?:inmetprom|stalmetural|mepen)\.(?:ru|uz|kz|by)', joined)
            host = _norm_host(m.group(0)) if m else ''
        if not host or host in seen:
            continue
        seen.add(host)
        # Все телефоны города (нормализованные, 10 цифр) из всех тел. колонок —
        # сайт может статически показывать любой из них (Общий/SEO/Сотовый).
        all_norm = []
        for idx in phone_cols:
            for n in _split_phones(cell(row, idx)):
                if n not in all_norm:
                    all_norm.append(n)
        rows_out.append({
            'domain': host,
            'city': city,
            'phone_seo': cell(row, ci_seo),
            'phone_ad': cell(row, ci_ad),
            'phone_common': cell(row, ci_common),
            'all_phones': ';'.join(all_norm),
            'email': cell(row, ci_email),
            'address': cell(row, ci_addr),
        })
    wb.close()

    CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = CATALOGS_DIR / f'{project_id}-kp.csv'
    with open(out, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['domain', 'city', 'phone_seo',
                                          'phone_ad', 'phone_common', 'all_phones',
                                          'email', 'address'])
        w.writeheader()
        w.writerows(rows_out)
    print(f'{project_id}: {len(rows_out)} городов → {out}')
    # маленькая сводка качества
    no_phone = sum(1 for r in rows_out if not (r['phone_seo'] or r['phone_ad'] or r['phone_common']))
    with_addr = sum(1 for r in rows_out if r['address'])
    print(f'  без телефона в КП: {no_phone}, с адресом: {with_addr}')
    return out


def main():
    if len(sys.argv) != 3:
        print('Использование: python convert_kp.py <smu|imp|mpe> <путь_к_xlsx>')
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])


if __name__ == '__main__':
    main()
