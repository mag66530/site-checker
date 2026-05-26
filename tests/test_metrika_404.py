"""Тесты metrika_404.py — парсинг тем писем и xlsx-таблиц."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/home/claude/site-checker-py')

from metrika_404 import (
    parse_subject, parse_table_xlsx, is_table_attachment,
    Page404, Report404, save_report, load_report, list_stored_reports,
    COUNTRY_LABELS,
    _imap_utf7_encode, _imap_utf7_decode,
)


def test_imap_utf7_ascii_unchanged():
    """ASCII-имена не меняются."""
    assert _imap_utf7_encode('INBOX') == b'INBOX'
    assert _imap_utf7_encode('Sent Items') == b'Sent Items'
    assert _imap_utf7_decode(b'INBOX') == 'INBOX'
    print('✓ IMAP UTF-7: ASCII не меняется')


def test_imap_utf7_ampersand():
    """Символ & экранируется в &-."""
    assert _imap_utf7_encode('A&B') == b'A&-B'
    assert _imap_utf7_decode(b'A&-B') == 'A&B'
    print('✓ IMAP UTF-7: & → &-')


def test_imap_utf7_russian():
    """Кириллица кодируется/декодируется правильно."""
    # Реальная папка Яндекса
    encoded = _imap_utf7_encode('Я.Метрика 404 и др')
    # Должно начинаться с & и кончаться -
    assert encoded.startswith(b'&'), f'Не закодировалось: {encoded}'
    # Декод обратно
    decoded = _imap_utf7_decode(encoded)
    assert decoded == 'Я.Метрика 404 и др', f'Roundtrip сломан: {decoded!r}'
    print(f'✓ IMAP UTF-7: «Я.Метрика 404 и др» → {encoded.decode()}')


def test_imap_utf7_known_yandex_folders():
    """Roundtrip для типичных имён папок Яндекса."""
    test_cases = [
        'Входящие',
        'Отправленные',
        'Спам',
        'Корзина',
        'Я.Метрика 404 и др',
        'A&B & test',  # сочетание ASCII, & и не-ASCII
    ]
    for name in test_cases:
        encoded = _imap_utf7_encode(name)
        decoded = _imap_utf7_decode(encoded)
        assert decoded == name, f'Roundtrip сломан для {name!r}: got {decoded!r}'
    print(f'✓ IMAP UTF-7: {len(test_cases)} имён roundtrip успешно')


def test_parse_subject_basic():
    """Простой случай — Отчёт «АЗ 404 отчет» за 25.05.2026."""
    result = parse_subject('Отчёт «АЗ 404 отчет» за 25.05.2026')
    assert result == {'country': 'АЗ', 'date': '2026-05-25'}
    print('✓ parse_subject: АЗ за 25.05.2026')


def test_parse_subject_perevod():
    """Сложный случай с (перевод)."""
    result = parse_subject('Отчёт «АЗ (перевод) 404 отчет» за 25.05.2026')
    assert result is not None
    assert 'перевод' in result['country']
    assert result['date'] == '2026-05-25'
    print(f'✓ parse_subject с (перевод): {result["country"]}')


def test_parse_subject_all_countries():
    """Все страны из реальных писем."""
    for code in ['РФ', 'КЗ', 'РБ', 'УЗ', 'АЗ', 'АМ', 'КГ']:
        subj = f'Отчёт «{code} 404 отчет» за 22.05.2026'
        result = parse_subject(subj)
        assert result is not None, f'Не распарсилось: {subj}'
        assert result['country'] == code
        assert result['date'] == '2026-05-22'
    print('✓ parse_subject: все 7 стран распарсились')


def test_parse_subject_not_metrika():
    """Не наше письмо — None."""
    cases = [
        'Re: Какая-то рабочая переписка',
        '',
        'Скидки и акции в Яндекс',
        'Отчёт за прошлый месяц',  # нет «404 отчет»
    ]
    for s in cases:
        assert parse_subject(s) is None, f'Должно быть None для: {s!r}'
    print('✓ parse_subject: чужие письма игнорируются')


def test_is_table_attachment():
    assert is_table_attachment('АЗ_404_отчет_за_25_05_2026__таблица.xlsx') is True
    assert is_table_attachment('АЗ_404_отчет_за_25_05_2026__график.xlsx') is False
    assert is_table_attachment('report.xlsx') is False
    assert is_table_attachment(None) is False
    print('✓ is_table_attachment: «таблица» детектится')


def test_parse_empty_table_xlsx():
    """Пустой отчёт (как АЗ за 25.05.2026 — 0 строк)."""
    with open('/mnt/user-data/uploads/АЗ_404_отчет_за_25_05_2026__таблица.xlsx', 'rb') as f:
        pages = parse_table_xlsx(f.read())
    assert pages == [], f'Ожидался пустой список, получили {len(pages)} страниц'
    print('✓ Пустая таблица возвращает []')


def test_parse_real_table_xlsx_structure():
    """Реальный отчёт хотя бы не падает на парсинге."""
    with open('/mnt/user-data/uploads/АЗ_404_отчет_за_24_05_2026__таблица__1_.xlsx', 'rb') as f:
        pages = parse_table_xlsx(f.read())
    # Не должно упасть, может вернуть пустой список или данные
    assert isinstance(pages, list)
    print(f'✓ Реальный xlsx распарсился: {len(pages)} страниц')


def test_parse_table_with_synthetic_data():
    """Создам синтетический xlsx с данными — чтобы покрыть случай когда 404 есть."""
    from openpyxl import Workbook
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws['A1'] = 'Отчет за период с 2026-05-25 по 2026-05-25'
    ws['A2'] = 'Фильтры: Заголовок страницы содержит "404"'
    ws['A3'] = 'Атрибуция: Последний значимый переход'
    # Строка 4 пустая
    ws['A5'] = 'Заголовок страницы'
    ws['B5'] = 'Просмотры'
    ws['C5'] = 'Посетители'
    # Данные
    ws['A6'] = 'Страница не найдена | Стальметурал'
    ws['B6'] = 15
    ws['C6'] = 12
    ws['A7'] = 'Страница не найдена https://stalmetural.ru/catalog/broken/'
    ws['B7'] = 3
    ws['C7'] = 3

    buf = BytesIO()
    wb.save(buf)
    pages = parse_table_xlsx(buf.getvalue())

    assert len(pages) == 2
    assert pages[0].page_title == 'Страница не найдена | Стальметурал'
    assert pages[0].views == 15
    assert pages[0].visitors == 12
    assert pages[0].page_url is None  # нет URL в строке
    assert pages[1].page_url == 'https://stalmetural.ru/catalog/broken/'  # URL вытащился
    print(f'✓ Синтетический xlsx с данными: распарсилось {len(pages)} страниц')


def test_save_and_load_report():
    """Сохранение и загрузка отчёта."""
    import metrika_404
    original = metrika_404.CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        metrika_404.CACHE_DIR = Path(tmp)

        report = Report404(
            project_id='smu',
            country_code='РФ',
            country_name='Россия',
            report_date='2026-05-25',
            received_at='2026-05-25T10:00:00',
            pages=[
                Page404(page_title='Страница не найдена', page_url='https://example.com', views=10, visitors=8),
            ],
            total_views=10,
            total_pages=1,
        )
        save_report(report)

        loaded = load_report('smu', 'РФ', '2026-05-25')
        assert loaded is not None
        assert loaded.country_code == 'РФ'
        assert loaded.country_name == 'Россия'
        assert len(loaded.pages) == 1
        assert loaded.pages[0].views == 10

    metrika_404.CACHE_DIR = original
    print('✓ Сохранение и загрузка отчёта')


def test_list_stored_reports():
    """Листинг сохранённых отчётов."""
    import metrika_404
    original = metrika_404.CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        metrika_404.CACHE_DIR = Path(tmp)

        # Сохраняем 3 отчёта: 2 разных страны и 2 даты
        for code, date, views in [
            ('РФ', '2026-05-25', 50),
            ('РФ', '2026-05-24', 40),
            ('КЗ', '2026-05-25', 20),
        ]:
            r = Report404(
                project_id='smu', country_code=code, country_name=COUNTRY_LABELS.get(code, code),
                report_date=date, received_at='2026-05-25T10:00:00',
                pages=[Page404(page_title='x', page_url=None, views=views, visitors=views)],
                total_views=views, total_pages=1,
            )
            save_report(r)

        listing = list_stored_reports('smu')
        assert len(listing) == 3
        # Сортировка: сначала свежие даты
        assert listing[0]['date'] == '2026-05-25'
        # Внутри даты — в алфавитном порядке
        codes_on_25 = [r['country_code'] for r in listing if r['date'] == '2026-05-25']
        assert codes_on_25 == sorted(codes_on_25, reverse=True)  # desc

    metrika_404.CACHE_DIR = original
    print(f'✓ Листинг: {len(listing)} отчётов')


if __name__ == '__main__':
    test_imap_utf7_ascii_unchanged()
    test_imap_utf7_ampersand()
    test_imap_utf7_russian()
    test_imap_utf7_known_yandex_folders()
    test_parse_subject_basic()
    test_parse_subject_perevod()
    test_parse_subject_all_countries()
    test_parse_subject_not_metrika()
    test_is_table_attachment()
    test_parse_empty_table_xlsx()
    test_parse_real_table_xlsx_structure()
    test_parse_table_with_synthetic_data()
    test_save_and_load_report()
    test_list_stored_reports()
    print('\n✅ Все тесты metrika_404.py прошли')
