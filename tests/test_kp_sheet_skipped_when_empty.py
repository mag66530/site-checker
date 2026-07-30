"""Тест: лист «Проверка КП» не создаётся, если сайт не проверяли
(результаты=[]) - раньше пустая вкладка с одной шапкой всё равно появлялась,
даже если смотрели только карты (--no-check-site)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import variables_run as vr


def test_no_sheet_when_results_empty(tmp_path):
    out = tmp_path / 'test.xlsx'
    vr._записать_xlsx(out, 'СМУ', [], per_city=False, map_results=[])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    assert 'Проверка КП' not in wb.sheetnames
    print('✓ результаты=[] → лист «Проверка КП» не создаётся')


def test_sheet_created_when_results_present(tmp_path):
    out = tmp_path / 'test.xlsx'
    результаты = [{'domain': 'stalmetural.ru', 'city': 'Москва', 'country': 'Россия',
                  'error': '', 'fields': []}]
    vr._записать_xlsx(out, 'СМУ', результаты, per_city=False, map_results=[])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    assert 'Проверка КП' in wb.sheetnames
    print('✓ результаты непустые → лист создаётся как раньше')
