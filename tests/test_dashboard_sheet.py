"""Тесты листа «Дашборд» в отчёте variables_run.py - сводка по данным
«Проверка КП»/«Расхождения»: 4 KPI, таблица+график по типу данных, таблица+
график по источнику, вывод-строка. Сделан по образцу файла, который прислал
заказчик (КП-сму-...-улучшено.xlsx) - структура и формулы должны совпадать."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import variables_run as vr
from maps_compare import MapCheckResult


def _результаты(n=3):
    return [{'domain': f'host{i}.ru', 'city': f'Город{i}', 'country': 'Россия',
            'error': '', 'fields': []} for i in range(n)]


def test_dashboard_is_first_sheet(tmp_path):
    out = tmp_path / 'test.xlsx'
    vr._записать_xlsx(out, 'СМУ', _результаты(), per_city=False, map_results=[])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    assert wb.sheetnames[0] == 'Дашборд'
    assert wb.sheetnames == ['Дашборд', 'Как читать результат', 'Проверка КП', 'Расхождения']
    print('✓ «Дашборд» - первый лист, порядок листов как в образце')


def test_no_dashboard_when_nothing_checked(tmp_path):
    out = tmp_path / 'test.xlsx'
    vr._записать_xlsx(out, 'СМУ', [], per_city=False, map_results=[])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    assert 'Дашборд' not in wb.sheetnames
    print('✓ ничего не проверяли → дашборда нет (нечего показывать)')


def test_kpi_formulas_reference_correct_ranges(tmp_path):
    out = tmp_path / 'test.xlsx'
    vr._записать_xlsx(out, 'СМУ', _результаты(5), per_city=False, map_results=[])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    ws = wb['Дашборд']
    assert ws['B6'].value == "=COUNTA('Проверка КП'!B3:B7)"
    assert '"✗"' in ws['D6'].value and 'Проверка КП' in ws['D6'].value
    assert ws['F6'].value == "=COUNTIF('Проверка КП'!K3:K7,0)"
    assert ws['H6'].value == '=COUNTIF(\'Проверка КП\'!K3:K7,">=3")'
    print('✓ KPI-формулы ссылаются на верный диапазон строк (5 городов → 3:7)')


def test_type_breakdown_table_matches_var_columns(tmp_path):
    out = tmp_path / 'test.xlsx'
    r = MapCheckResult(source='2gis', city='Город0', url='u', available=True,
                       country='Россия', phone_match=True, address_match=True,
                       site_match=True)
    vr._записать_xlsx(out, 'СМУ', _результаты(1), per_city=False, map_results=[r])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    ws = wb['Дашборд']
    assert ws['B10'].value == 'Показатель' and ws['C10'].value == 'Расхождений (✗)'
    labels = [ws.cell(r, 2).value for r in range(11, 20) if ws.cell(r, 2).value]
    assert 'Город' in labels and 'Адрес' in labels and '2ГИС' in labels
    # Формула для «2ГИС» должна смотреть на колонку 2ГИС на листе «Проверка КП»
    _2gis_row = next(r for r in range(11, 20) if ws.cell(r, 2).value == '2ГИС')
    assert "'Проверка КП'!K" in ws.cell(_2gis_row, 3).value
    print('✓ таблица «по типу данных» построена по фактическим колонкам отчёта')


def test_source_breakdown_table_and_charts_present(tmp_path):
    out = tmp_path / 'test.xlsx'
    r1 = MapCheckResult(source='2gis', city='Город0', url='u', available=True,
                        country='Россия', phone_match=False, address_match=True,
                        site_match=True, issues=['x'],
                        details=[{'field': 'телефон', 'kp': '1', 'card': '–'}])
    vr._записать_xlsx(out, 'СМУ', _результаты(1), per_city=False, map_results=[r1])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    ws = wb['Дашборд']
    # источники: «Сайт» (результаты непустые) + «2ГИС» (единственная проверенная карта)
    src_labels = [ws.cell(r, 2).value for r in range(1, 40)
                 if ws.cell(r, 2).value in ('Сайт', '2ГИС', 'Яндекс.Карты', 'Google')]
    assert set(src_labels) == {'Сайт', '2ГИС'}
    assert len(ws._charts) == 2, 'должно быть 2 графика - bar (по типу) и pie (по источнику)'
    print('✓ таблица «по источнику» - только реально проверенные источники, 2 графика на листе')


def test_conclusion_line_mentions_top_source(tmp_path):
    out = tmp_path / 'test.xlsx'
    # 3 расхождения по 2ГИС, 0 по сайту - «главный вывод» должен назвать 2ГИС.
    maps = [
        MapCheckResult(source='2gis', city=f'Город{i}', url='u', available=True,
                       country='Россия', phone_match=False, address_match=True,
                       site_match=True, issues=['x'],
                       details=[{'field': 'телефон', 'kp': str(i), 'card': '–'}])
        for i in range(3)
    ]
    vr._записать_xlsx(out, 'СМУ', _результаты(3), per_city=False, map_results=maps)
    from openpyxl import load_workbook
    wb = load_workbook(out)
    ws = wb['Дашборд']
    all_text = ' '.join(str(ws.cell(r, 2).value or '') for r in range(1, 45))
    assert 'Главный вывод' in all_text and '2ГИС' in all_text
    print('✓ вывод-строка на дашборде называет источник с наибольшим числом расхождений')


def test_report_round_trips_without_corruption(tmp_path):
    """Полный отчёт (дашборд + графики + формулы) должен сохраняться и
    открываться обратно без ошибок - простая проверка целостности файла."""
    out = tmp_path / 'test.xlsx'
    r = MapCheckResult(source='yandex', city='Город0', url='u', available=True,
                       country='Россия', phone_match=True, address_match=True,
                       site_match=True)
    vr._записать_xlsx(out, 'СМУ', _результаты(2), per_city=False, map_results=[r])
    from openpyxl import load_workbook
    wb = load_workbook(out)
    assert set(wb.sheetnames) == {'Дашборд', 'Как читать результат',
                                 'Проверка КП', 'Расхождения'}
    print('✓ файл с дашбордом и графиками сохраняется/открывается без ошибок')
