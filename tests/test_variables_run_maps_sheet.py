"""Тесты листа «Карты» в отчёте variables_run.py: одна строка на город
(Страна|Город|Яндекс.Карты|Google|2ГИС), оформление то же, что у «Проверки
КП» - совпало = чисто без комментария, не совпало = цвет + комментарий,
источник не проверяли в этом прогоне = пустая ячейка (не путать с «–»).

Отдельного листа «Расхождения» в отчёте больше нет - эту тему тесты здесь
не покрывают, потому что покрывать нечего."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import variables_run as vr
from maps_compare import MapCheckResult


def _wb_with_thin():
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Side
    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="EEF3FB")
    thin = Side(style="thin", color="C9CFDB")
    return wb, hdr_fill, thin


def test_no_map_results_means_no_sheet():
    wb, hdr_fill, thin = _wb_with_thin()
    vr._записать_карты_лист(wb, hdr_fill, thin, [])
    assert 'Карты' not in wb.sheetnames
    print('✓ без карт - лист не создаётся')


def test_header_row_has_three_source_columns():
    wb, hdr_fill, thin = _wb_with_thin()
    r = MapCheckResult(source='yandex', city='Москва', url='u', available=True,
                       country='Россия', name='X', phone_match=True,
                       address_match=True, site_match=True)
    vr._записать_карты_лист(wb, hdr_fill, thin, [r])
    ws = wb['Карты']
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert header == ['Страна', 'Город', 'Яндекс.Карты', 'Google', '2ГИС', 'Детали']
    print('✓ заголовок: Страна/Город + 3 источника + Детали')


def test_one_row_per_city_multiple_sources_combined():
    """Яндекс и 2ГИС для ОДНОГО города должны лечь в ОДНУ строку, не в две."""
    wb, hdr_fill, thin = _wb_with_thin()
    results = [
        MapCheckResult(source='yandex', city='Москва', url='ya', available=True,
                       country='Россия', name='X', phone_match=True,
                       address_match=True, site_match=True),
        MapCheckResult(source='2gis', city='Москва', url='gis', available=True,
                       country='Россия', name='X', phone_match=False,
                       address_match=True, site_match=True,
                       issues=['телефон на карте (+7 000) не совпал с КП'],
                       details=[{'field': 'телефон', 'kp': '4991303669',
                                'card': '+7 000'}]),
    ]
    vr._записать_карты_лист(wb, hdr_fill, thin, results)
    ws = wb['Карты']
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1, f'ожидали 1 строку на город, получили {len(rows)}'
    country, city, ya, goog, gis, details = rows[0]
    assert country == 'Россия' and city == 'Москва'
    assert ya == '✓'    # Яндекс совпал
    assert goog is None  # Google не проверяли в этом прогоне - пусто, не «–»
    assert gis == '✗'   # 2ГИС - расхождение по телефону
    assert '2ГИС' in details and 'телефон' in details
    print('✓ Яндекс+2ГИС одного города - одна строка, Google пуст (не проверяли)')


def test_ok_cell_has_no_comment():
    """Совпало - ячейка чистая: без заливки и без комментария."""
    wb, hdr_fill, thin = _wb_with_thin()
    r = MapCheckResult(source='yandex', city='Москва', url='u', available=True,
                       country='Россия', name='X', phone_match=True,
                       address_match=True, site_match=True)
    vr._записать_карты_лист(wb, hdr_fill, thin, [r])
    ws = wb['Карты']
    cell = list(ws.iter_rows(min_row=2, max_row=2))[0][2]  # колонка «Яндекс.Карты»
    assert cell.value == '✓'
    assert cell.comment is None, 'совпало → комментария быть не должно'
    print('✓ совпадение: без комментария')


def test_mismatch_cell_has_comment_with_details():
    wb, hdr_fill, thin = _wb_with_thin()
    r = MapCheckResult(source='yandex', city='Казань', url='https://yandex.ru/x',
                       available=True, country='Россия', name='Стальметурал',
                       phone_match=False, address_match=True, site_match=True,
                       issues=['телефон на карте (+7 999) не совпал с КП'])
    vr._записать_карты_лист(wb, hdr_fill, thin, [r])
    ws = wb['Карты']
    cell = list(ws.iter_rows(min_row=2, max_row=2))[0][2]
    assert cell.value == '✗'
    assert cell.comment is not None
    assert 'телефон' in cell.comment.text
    assert 'Стальметурал' in cell.comment.text
    print('✓ расхождение: комментарий содержит причину и название организации')


def test_unavailable_card_is_warning_with_error_comment():
    wb, hdr_fill, thin = _wb_with_thin()
    r = MapCheckResult(source='2gis', city='Уфа', url='u', available=False,
                       country='Россия', error='таймаут')
    vr._записать_карты_лист(wb, hdr_fill, thin, [r])
    ws = wb['Карты']
    cell = list(ws.iter_rows(min_row=2, max_row=2))[0][4]  # колонка «2ГИС»
    assert cell.value == '⚠'
    assert 'таймаут' in cell.comment.text
    print('✓ недоступная карточка: ⚠ + причина в комментарии')


def test_no_kp_data_shown_as_ok_not_dash():
    """compare() трактует «города нет в КП» как issue → ✗, но здесь просто
    проверяем, что колонка отражает ИМЕННО статус MapCheckResult, а не
    придумывает свою логику поверх available/is_ok/is_warning/is_error."""
    from kp import KPRow
    import maps_compare as mc
    card = {'available': True, 'name': 'X', 'phone': '', 'address': '', 'site': ''}
    r = mc.compare('yandex', 'Незнакомый город', 'u', card, None)
    wb, hdr_fill, thin = _wb_with_thin()
    vr._записать_карты_лист(wb, hdr_fill, thin, [r])
    ws = wb['Карты']
    cell = list(ws.iter_rows(min_row=2, max_row=2))[0][2]
    assert cell.value == '✗'  # available=True + issues=['города нет в КП'] → is_error
    assert 'нет в КП' in cell.comment.text
    print('✓ город без КП - показан как расхождение с понятной причиной')


def test_city_order_matches_first_appearance():
    wb, hdr_fill, thin = _wb_with_thin()
    results = [
        MapCheckResult(source='yandex', city='Казань', url='u', available=True,
                       country='Россия', phone_match=True, address_match=True,
                       site_match=True),
        MapCheckResult(source='yandex', city='Москва', url='u', available=True,
                       country='Россия', phone_match=True, address_match=True,
                       site_match=True),
        MapCheckResult(source='2gis', city='Казань', url='u', available=True,
                       country='Россия', phone_match=True, address_match=True,
                       site_match=True),
    ]
    vr._записать_карты_лист(wb, hdr_fill, thin, results)
    ws = wb['Карты']
    cities = [row[1] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert cities == ['Казань', 'Москва'], f'порядок городов нарушен: {cities}'
    print('✓ порядок городов - как при первом появлении в результатах')


def test_no_link_shown_as_dash_without_fill_or_comment():
    """Брянск, 2ГИС - ссылки на карту в КП просто нет: «–», без цвета/заливки
    и без комментария (не путать с ⚠ «карточка недоступна»)."""
    wb, hdr_fill, thin = _wb_with_thin()
    r = MapCheckResult(source='2gis', city='Брянск', url='', available=False,
                       country='Россия', no_link=True)
    vr._записать_карты_лист(wb, hdr_fill, thin, [r])
    ws = wb['Карты']
    cell = list(ws.iter_rows(min_row=2, max_row=2))[0][4]  # колонка «2ГИС»
    assert cell.value == '–'
    assert cell.comment is None
    assert cell.fill.fgColor.rgb in (None, '00000000'), \
        'no_link не должен заливаться цветом (не ⚠ и не ✗)'
    print('✓ нет ссылки на карту → «–» без заливки и комментария')
