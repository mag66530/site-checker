"""Тесты convert_kp.py: колонки ссылок на карточки Яндекс.Карт/2ГИС.

Структура заголовков в реальной таблице - ДВЕ строки: секционная («Яндекс
Бизнес»/«2ГИС»/«Google», объединённая ячейка) НАД строкой Аккаунт/Карта/Статус,
повторяющейся под каждой секцией. convert_kp.convert() должен брать колонку
«Карта» строго ИЗ ЭТОЙ пары строк - определяя секцию по ближайшей непустой
ячейке слева на строке выше.

Отдельная ловушка (уже случившийся баг): в той же таблице есть ДРУГАЯ колонка
«Ссылка для яндекс-карт» - iframe-виджет для встройки на сайт
(src=.../map-widget/v1/?um=...), не прямая ссылка на карточку. Первая версия
кода цепляла её же по совпадению слов «яндекс»+«карта» - тест на это отдельно.

ВАЖНО: convert_kp.convert() пишет в CATALOGS_DIR/{pid}-kp.csv - это РЕАЛЬНЫЕ,
закоммиченные файлы проекта (боевые данные КП). Каждый тест обязан подменять
convert_kp.CATALOGS_DIR на tmp_path (monkeypatch), иначе тест перезапишет
живой catalogs/smu-kp.csv тестовой строкой - ровно это здесь однажды и
случилось при первом запуске без подмены."""
import csv
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert_kp

_YANDEX_ORG_URL = 'https://yandex.ru/maps/org/stalmetural/128446144797/'
_TWOGIS_ORG_URL = 'https://2gis.ru/moscow/firm/70000001077855514/'
_IFRAME_DECOY = ('<iframe loading="lazy" src="https://yandex.ru/map-widget/v1/'
                 '?um=constructor%3A14ea1950"></iframe>')

_HEADERS_ROW1 = ['', '', '', '', '', '', 'Яндекс Бизнес', '', '', '2ГИС', '', '', '']
_HEADERS_ROW2 = ['Город', 'url', 'Общий Город', 'Реклама Город', 'SEO Город', 'Адрес',
                'Аккаунт', 'Карта', 'Статус', 'Аккаунт', 'Карта', 'Статус',
                'Ссылка для яндекс-карт']
_DATA_ROW = ['Москва', 'https://stalmetural.ru/', '+7 499 130-36-69', '', '',
            'Люблинская ул., 151', 'https://yandex.ru/sprav/x/edit/', _YANDEX_ORG_URL,
            'Активная', 'https://account.2gis.com/x', _TWOGIS_ORG_URL, 'Активная',
            _IFRAME_DECOY]


@contextmanager
def _xlsx_file(sheet_name: str, rows: list[list]):
    """Временный xlsx с явной очисткой через os.remove (не TemporaryDirectory):
    openpyxl (read_only) держит файл замапленным дольше своего .close(), и
    Windows не даёт удалить папку с ещё занятым файлом - та же причина, по
    которой kp_sheets.py использует NamedTemporaryFile(delete=False) + os.remove
    в finally, а не автоматическую уборку директории."""
    fd, path = tempfile.mkstemp(suffix='.xlsx')
    os.close(fd)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for r in rows:
        ws.append(r)
    wb.save(path)
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_map_urls_extracted_from_correct_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(convert_kp, 'CATALOGS_DIR', tmp_path)
    with _xlsx_file('Справочники', [_HEADERS_ROW1, _HEADERS_ROW2, _DATA_ROW]) as xlsx:
        out = convert_kp.convert('smu', xlsx)
        assert out.parent == tmp_path, 'тест обязан писать в tmp_path, не в repo'
        with open(out, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]['yandex_map_url'] == _YANDEX_ORG_URL
        assert rows[0]['twogis_map_url'] == _TWOGIS_ORG_URL
        # Главный регресс: колонка ссылки на карту НЕ должна попасть в домен сайта.
        assert rows[0]['domain'] == 'stalmetural.ru'
        print('✓ обе карты взяты из правильных колонок (Аккаунт/Карта/Статус)')


def test_iframe_widget_column_is_not_picked_as_map_link(monkeypatch, tmp_path):
    """Регресс: «Ссылка для яндекс-карт» (iframe-виджет) похожа по названию,
    но это НЕ карточка организации - не должна попасть в yandex_map_url."""
    monkeypatch.setattr(convert_kp, 'CATALOGS_DIR', tmp_path)
    with _xlsx_file('Справочники', [_HEADERS_ROW1, _HEADERS_ROW2, _DATA_ROW]) as xlsx:
        out = convert_kp.convert('smu', xlsx)
        with open(out, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert '<iframe' not in rows[0]['yandex_map_url']
        assert rows[0]['yandex_map_url'] == _YANDEX_ORG_URL
        print('✓ iframe-виджет не перепутан с прямой ссылкой на карточку')


def test_missing_map_section_is_not_an_error(monkeypatch, tmp_path):
    """У проекта без блока Яндекс Бизнес/2ГИС - просто пустые поля,
    конвертация не падает (секционной строки может не быть вовсе)."""
    monkeypatch.setattr(convert_kp, 'CATALOGS_DIR', tmp_path)
    headers = ['Город', 'url', 'Общий Город', 'Реклама Город', 'SEO Город', 'Адрес']
    data = ['Москва', 'https://stalmetural.ru/', '+7 499 130-36-69', '', '',
           'Люблинская ул., 151']
    with _xlsx_file('Справочники', [headers, data]) as xlsx:
        out = convert_kp.convert('smu', xlsx)
        with open(out, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert rows[0]['yandex_map_url'] == ''
        assert rows[0]['twogis_map_url'] == ''
        print('✓ без секции карт - пустые поля, не падение')


def test_kprow_backward_compat_with_old_csv_without_columns():
    """Уже закоммиченные catalogs/*-kp.csv - без этих колонок. KPRow должен
    подставлять пустую строку, а не падать на KeyError."""
    import kp
    row = kp._row_from_csv({'domain': 'stalmetural.ru', 'city': 'Москва'})
    assert row is not None
    assert row.yandex_map_url == ''
    assert row.twogis_map_url == ''
    print('✓ старый csv без колонок читается, оба поля = ""')
