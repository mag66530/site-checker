"""
index_export_parser.py - разбор выгрузки «Страницы в поиске» из Яндекс.Вебмастера
(кнопка «Скачать таблицу → XLSX / CSV» на webmaster.yandex.ru/site/…/indexing/searchable/).

Это САМЫЙ точный источник для пункта «Проверка страниц в индексе на 404»:
в выгрузке уже есть колонки `httpCode` (код ответа, который видел Яндекс) и
`status` (SEARCHABLE / LOW_DEMAND / HTTP_ERROR / UNKNOWN_URL). Ничего
прозванивать не нужно - берём строки, где код ответа 404/410/5xx или
status = HTTP_ERROR.

Колонки выгрузки (CSV и XLSX одинаковы):
    updateDate, url, httpCode, status, target, lastAccess, title, event

Файл может прийти двумя путями:
  • ручной загрузкой в UI (пользователь скачал сам);
  • автоскачиванием headless-браузером (index404_run.py) - тем же
    механизмом сессии, что автокликеры.

Разбор не зависит от способа получения файла: на входе байты + имя.
Результат совместим по форме с index_pages_checker.check_index_404 - оба
источника кладутся в один лист отчёта «404 в индексе».
"""
from __future__ import annotations

import csv
import io
from urllib.parse import urlsplit

# Заголовки колонок → наши ключи (терпимо к рус/англ и регистру).
_COL_ALIASES = {
    'url': 'url', 'адрес': 'url', 'адрес страницы': 'url',
    'httpcode': 'http', 'http_code': 'http', 'код': 'http', 'код ответа': 'http',
    'status': 'status', 'статус': 'status',
    'title': 'title', 'заголовок': 'title',
    'lastaccess': 'last_access', 'последнее посещение': 'last_access',
    'event': 'event', 'target': 'target', 'updatedate': 'update_date',
}


def _host_of(url: str) -> str:
    """host без схемы/www для группировки."""
    sp = urlsplit((url or '').strip())
    h = (sp.netloc or sp.path.split('/')[0] or '').lower()
    return h[4:] if h.startswith('www.') else h


def _norm_header(h: str) -> str:
    return _COL_ALIASES.get((h or '').strip().lower().lstrip('﻿'), '')


def classify_export_row(http_code, status) -> str:
    """Вердикт по строке выгрузки:
      'dead'         - код 404/410: страница отдаёт «не найдено»;
      'server_error' - 5xx или status HTTP_ERROR;
      'client_error' - прочие 4xx (403/401);
      'not_fetched'  - код 0 / UNKNOWN_URL: робот ещё не скачал;
      'ok'           - 2xx (SEARCHABLE / LOW_DEMAND и т.п.).
    Только 404/410 = «битая» (dead); 5xx и прочее - отдельно (см. пункт)."""
    st = (str(status or '')).strip().upper()
    try:
        code = int(str(http_code).strip() or 0)
    except (ValueError, TypeError):
        code = 0
    if code in (404, 410):
        return 'dead'
    if code >= 500 or st == 'HTTP_ERROR':
        return 'server_error'
    if 400 <= code < 500:
        return 'client_error'
    if code == 0 or st == 'UNKNOWN_URL':
        return 'not_fetched'
    return 'ok'


def _rows_from_csv(data: bytes) -> list:
    text = data.decode('utf-8-sig', errors='replace')
    # автоопределение разделителя (Яндекс отдаёт запятую; бывает ;)
    sample = text[:2048]
    delim = ';' if sample.count(';') > sample.count(',') else ','
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def _rows_from_xlsx(data: bytes) -> list:
    import warnings
    import openpyxl

    def _read(read_only):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')          # яндексовский xlsx без default style
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=read_only,
                                        data_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if read_only:
            wb.close()
        return rows

    # read_only быстрее, но на выгрузке Яндекса иногда недосчитывает колонки
    # (кривой dimension). Если так - перечитываем в обычном режиме.
    rows = _read(True)
    if not rows or len(rows[0]) <= 1:
        rows = _read(False)
    if not rows:
        return []
    header = [str(c) if c is not None else '' for c in rows[0]]
    out = []
    for row in rows[1:]:
        d = {header[i]: row[i] for i in range(min(len(header), len(row)))}
        if any(v not in (None, '') for v in d.values()):
            out.append(d)
    return out


def parse_export(data: bytes, filename: str = '') -> list:
    """Строки выгрузки → [{'url','http','status','title','last_access',...}].
    Формат определяется по расширению/содержимому. Терпимо к схеме."""
    name = (filename or '').lower()
    is_xlsx = name.endswith('.xlsx') or data[:2] == b'PK'
    raw = _rows_from_xlsx(data) if is_xlsx else _rows_from_csv(data)
    out = []
    for r in raw:
        rec = {}
        for k, v in r.items():
            key = _norm_header(str(k))
            if key:
                rec[key] = v
        if rec.get('url'):
            out.append(rec)
    return out


def analyze_exports(files: list, log=None) -> dict:
    """files: [(filename, bytes)]. Разбирает все выгрузки, группирует по хосту.
    Возвращает структуру, совместимую с check_index_404 (один лист отчёта)."""
    def _log(msg):
        if log:
            try:
                log('info', msg)
            except TypeError:
                log(msg)

    out = {'available': False, 'source': 'yandex_export', 'hosts': [],
           'total_checked': 0, 'total_dead': 0, 'total_soft': 0,
           'total_files': 0, 'error': None}
    by_host = {}
    parsed_any = False
    for name, data in files or []:
        try:
            rows = parse_export(data or b'', name)
        except Exception as e:
            _log(f'⚠ Выгрузка {name}: не разобралась ({e})')
            continue
        if not rows:
            _log(f'⚠ Выгрузка {name}: строк не найдено (не тот файл?)')
            continue
        parsed_any = True
        out['total_files'] += 1
        _log(f'Выгрузка {name}: строк {len(rows)}')
        for r in rows:
            host = _host_of(r.get('url', ''))
            hb = by_host.setdefault(host, {
                'host': host, 'dead': [], 'soft': [], 'errors': [],
                'searchable': 0, 'checked': 0, 'ok': 0, 'redirects': 0})
            hb['checked'] += 1
            v = classify_export_row(r.get('http'), r.get('status'))
            st = (str(r.get('status') or '')).upper()
            if st == 'SEARCHABLE':
                hb['searchable'] += 1
            entry = {'url': r.get('url', ''),
                     'status': r.get('http'),
                     'reason': f'httpCode {r.get("http")}, статус {r.get("status")}'}
            if v == 'dead':
                hb['dead'].append(entry)
            elif v in ('server_error', 'client_error'):
                hb['errors'].append(entry)
            elif v == 'ok':
                hb['ok'] += 1
            # not_fetched (код 0) - в свод не тащим, это «робот ещё не заходил»

    if not parsed_any:
        out['error'] = ('ни одной корректной выгрузки не разобрано '
                        '(нужны XLSX/CSV со «Скачать таблицу» Вебмастера)')
        return out

    out['available'] = True
    for host, hb in sorted(by_host.items()):
        hb['in_index_total'] = hb.pop('searchable')
        out['hosts'].append(hb)
        out['total_checked'] += hb['checked']
        out['total_dead'] += len(hb['dead'])
    return out
