"""Отчёт «Скорости страниц» уходит в Telegram и на Google Диск.

Раньше прогон только складывал xlsx на диск: чтобы отдать его клиенту, файл
доставали руками. Теперь как у форм, целей и КП - сводка в Telegram с
приложенным файлом плюс выкладка на Диск, обе части необязательные.
"""
import sys
import types
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import pagespeed_run as pr


АГРЕГАТ = {
    'overall': {'count': 8, 'desktop_avg': 74.2, 'mobile_avg': 41.6},
    'by_type': {
        'main': {'count': 1, 'desktop_avg': 88.0, 'mobile_avg': 52.0},
        'category': {'count': 4, 'desktop_avg': 71.0, 'mobile_avg': 39.0},
    },
}
ДЕЛЬТЫ = {
    'overall': {'desktop': 5.0, 'mobile': -3.0},
    'by_type': {'main': {'desktop': 0.0, 'mobile': None},
                'category': {'desktop': -2.0, 'mobile': 1.0}},
}
РЕКОМЕНДАЦИИ = [{'title': 'Уменьшите размер изображений', 'pages': 7},
                {'title': 'Уберите неиспользуемый JS', 'pages': 5},
                {'title': 'Включите сжатие текста', 'pages': 2}]


def test_дельта_читается_человеком():
    assert pr._дельта(4.0) == ' (▲ +4)'
    assert pr._дельта(-4.0) == ' (▼ -4)'
    assert pr._дельта(0.0) == ' (=)'
    assert pr._дельта(None) == ''
    print('✓ рост, падение, «без изменений» и «не с чем сравнить» различимы')


def test_сводка_содержит_главное():
    текст = pr._сводка_для_telegram('SM - SHOPMET', АГРЕГАТ, ДЕЛЬТЫ,
                                    None, РЕКОМЕНДАЦИИ)

    assert 'SM - SHOPMET' in текст
    assert 'Проверено страниц: 8' in текст
    assert '<b>74</b> (▲ +5)' in текст          # компьютер вырос
    assert '<b>42</b> (▼ -3)' in текст          # телефон просел
    assert 'Первый прогон' in текст
    assert 'Уменьшите размер изображений (7 стр.)' in текст
    assert текст.count('•') >= 4                # типы страниц + рекомендации
    print('✓ в сводке средние баллы, динамика, разбивка и топ рекомендаций')


def test_в_сводке_нет_сырого_html():
    """Имя проекта и заголовки рекомендаций эскейпятся - parse_mode=HTML."""
    текст = pr._сводка_для_telegram(
        'Проект <script>', АГРЕГАТ, ДЕЛЬТЫ, None,
        [{'title': 'Ужмите <img> & <b>', 'pages': 3}])

    assert '<script>' not in текст
    assert '&lt;script&gt;' in текст
    assert '&lt;img&gt; &amp; &lt;b&gt;' in текст
    print('✓ угловые скобки и амперсанд экранированы')


def test_ссылка_на_диск_попадает_в_сообщение(monkeypatch, tmp_path):
    файл = tmp_path / 'SM-скорость-12.08.2026.xlsx'
    файл.write_bytes(b'xlsx')
    отправлено = {}

    диск = types.ModuleType('drive_reports')
    диск.upload_from_env = lambda p, t, log=None: {'link': 'https://drive/файл'}
    monkeypatch.setitem(sys.modules, 'drive_reports', диск)

    тг = types.ModuleType('telegram_notify')
    тг.escape_html = lambda s: s

    def _send(project_name, summary_text, report_file=None, **kw):
        отправлено.update(текст=summary_text, файл=report_file,
                          имя=kw.get('report_filename'))
        return {'sent': 2, 'failed': 0}

    тг.send_report_from_env = _send
    monkeypatch.setitem(sys.modules, 'telegram_notify', тг)

    pr._отправить_отчёт(файл, файл.name, 'SM - SHOPMET', АГРЕГАТ, ДЕЛЬТЫ,
                        None, РЕКОМЕНДАЦИИ, lambda m: None)

    assert 'https://drive/файл' in отправлено['текст']
    assert отправлено['файл'] == файл and отправлено['имя'] == файл.name
    print('✓ файл приложен, ссылка на Диск добавлена в текст')


def test_ошибка_диска_не_мешает_телеграму(monkeypatch, tmp_path):
    файл = tmp_path / 'отчёт.xlsx'
    файл.write_bytes(b'xlsx')
    записи, отправлено = [], {}

    диск = types.ModuleType('drive_reports')

    def _падает(*a, **kw):
        raise RuntimeError('Диск недоступен')

    диск.upload_from_env = _падает
    monkeypatch.setitem(sys.modules, 'drive_reports', диск)

    тг = types.ModuleType('telegram_notify')
    тг.escape_html = lambda s: s
    тг.send_report_from_env = lambda **kw: отправлено.update(kw) or {'sent': 1}
    monkeypatch.setitem(sys.modules, 'telegram_notify', тг)

    pr._отправить_отчёт(файл, файл.name, 'SM', АГРЕГАТ, ДЕЛЬТЫ, None, [],
                        записи.append)

    assert any('Google Диск' in s for s in записи)
    assert отправлено, 'сообщение должно уйти даже без Диска'
    print('✓ недоступный Диск не отменяет отправку в Telegram')


def test_ничего_не_настроено_прогон_не_падает(monkeypatch, tmp_path):
    файл = tmp_path / 'отчёт.xlsx'
    файл.write_bytes(b'xlsx')
    записи = []

    тг = types.ModuleType('telegram_notify')
    тг.escape_html = lambda s: s
    тг.send_report_from_env = lambda **kw: {'skipped': True}
    monkeypatch.setitem(sys.modules, 'telegram_notify', тг)
    диск = types.ModuleType('drive_reports')
    диск.upload_from_env = lambda p, t, log=None: {}
    monkeypatch.setitem(sys.modules, 'drive_reports', диск)

    pr._отправить_отчёт(файл, файл.name, 'SM', АГРЕГАТ, ДЕЛЬТЫ, None, [],
                        записи.append)

    assert not any('Telegram: отправлено' in s for s in записи)
    print('✓ без настроенных Telegram/Диска прогон тихо пропускает отправку')
