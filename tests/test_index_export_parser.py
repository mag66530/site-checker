"""Тесты index_export_parser.py - разбор выгрузки «Страницы в поиске».

Проверяем классификацию строк, разбор CSV и XLSX (синтетические, без
внешних файлов) и агрегацию по хостам.
"""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_export_parser as p


# ── classify_export_row ──────────────────────────────────────────────

@pytest.mark.parametrize('http,status,verdict', [
    (404, 'HTTP_ERROR', 'dead'),
    (410, 'SEARCHABLE', 'dead'),
    (500, 'HTTP_ERROR', 'server_error'),
    (200, 'HTTP_ERROR', 'server_error'),   # статус важнее «200»
    (403, 'SEARCHABLE', 'client_error'),
    (0,   'UNKNOWN_URL', 'not_fetched'),
    (200, 'SEARCHABLE', 'ok'),
    (200, 'LOW_DEMAND', 'ok'),             # выкинута как малополезная - не наш 404
    ('404', 'HTTP_ERROR', 'dead'),         # код строкой
    ('', '', 'not_fetched'),
])
def test_classify(http, status, verdict):
    assert p.classify_export_row(http, status) == verdict


# ── CSV ──────────────────────────────────────────────────────────────

_CSV = (
    '"updateDate","url","httpCode","status","target","lastAccess","title","event"\n'
    '"14.07.2026","https://smg.az/catalog/ok/","200","SEARCHABLE","","09.07.2026","OK","ADD"\n'
    '"14.07.2026","https://smg.az/catalog/gone/","404","HTTP_ERROR","","09.07.2026","404","DELETE"\n'
    '"14.07.2026","https://smg.az/catalog/boom/","500","HTTP_ERROR","","09.07.2026","500","DELETE"\n'
    '"14.07.2026","https://smg.az/catalog/new/","0","UNKNOWN_URL","","","","DELETE"\n'
)


def test_parse_csv_and_aggregate():
    res = p.analyze_exports([('smg.az.csv', _CSV.encode('utf-8'))])
    assert res['available'] is True
    assert res['total_checked'] == 4
    assert res['total_dead'] == 1               # только 404
    h = res['hosts'][0]
    assert h['host'] == 'smg.az'
    assert h['in_index_total'] == 1             # один SEARCHABLE
    assert [d['url'] for d in h['dead']] == ['https://smg.az/catalog/gone/']
    assert len(h['errors']) == 1                # 500
    assert h['ok'] == 1
    # код 0 / UNKNOWN_URL не попал ни в dead, ни в errors, ни в ok
    assert h['checked'] == 4


def test_csv_semicolon_delimiter():
    csv_semi = _CSV.replace('","', '";"').replace('"\n', '"\n')
    res = p.analyze_exports([('x.csv', csv_semi.encode('utf-8'))])
    assert res['total_checked'] == 4
    assert res['total_dead'] == 1


# ── XLSX (пишем synthetic и парсим обратно) ─────────────────────────

def _make_xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['updateDate', 'url', 'httpCode', 'status', 'target',
               'lastAccess', 'title', 'event'])
    ws.append(['14.07', 'https://mepen.ru/a/', 200, 'SEARCHABLE', '', '', 'A', 'ADD'])
    ws.append(['14.07', 'https://mepen.ru/dead/', 404, 'HTTP_ERROR', '', '', 'X', 'DELETE'])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_xlsx():
    res = p.analyze_exports([('mepen.xlsx', _make_xlsx())])
    assert res['available'] is True
    assert res['total_checked'] == 2
    assert res['total_dead'] == 1
    assert res['hosts'][0]['dead'][0]['url'] == 'https://mepen.ru/dead/'


# ── Пустые/битые входы ──────────────────────────────────────────────

def test_empty_and_garbage():
    res = p.analyze_exports([('empty.csv', b''), ('junk.csv', b'hello world')])
    assert res['available'] is False
    assert res['error']


def test_multi_host_split():
    csv2 = (
        '"url","httpCode","status"\n'
        '"https://a.ru/x/","404","HTTP_ERROR"\n'
        '"https://b.ru/y/","200","SEARCHABLE"\n'
    )
    res = p.analyze_exports([('m.csv', csv2.encode('utf-8'))])
    hosts = {h['host']: h for h in res['hosts']}
    assert set(hosts) == {'a.ru', 'b.ru'}
    assert len(hosts['a.ru']['dead']) == 1
    assert not hosts['b.ru']['dead']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
