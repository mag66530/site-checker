"""Тесты источника GSC для «404 в индексе» (парсинг выгрузок, без сети)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_gsc_run as g


def test_parse_gsc_csv():
    """CSV drilldown: URL в первой колонке, заголовок пропускаем."""
    csv = ('URL,Последнее сканирование\n'
           '"https://a.ru/x/",2026-07-11\n'
           '"https://spb.a.ru/y/",2026-07-10\n'
           ',\n').encode('utf-8')
    assert g.parse_gsc_export(csv) == ['https://a.ru/x/', 'https://spb.a.ru/y/']


def test_parse_gsc_csv_bom():
    """CSV с BOM (utf-8-sig) – заголовок всё равно распознан, URL взяты."""
    csv = '﻿URL,x\nhttps://a.ru/1,2026\nhttps://a.ru/2,2026\n'.encode('utf-8')
    assert g.parse_gsc_export(csv) == ['https://a.ru/1', 'https://a.ru/2']


def test_host_of_subdomain():
    assert g._host_of('https://vladivostok.stalmetural.ru/catalog/x/') == \
        'vladivostok.stalmetural.ru'
    assert g._host_of('https://www.a.ru/y') == 'a.ru'


def test_gsc_target_from_config():
    """Ресурс/аккаунт берутся из конфига проекта (у СМУ заданы явно)."""
    res, acct = g._gsc_target('smu')
    assert res == 'sc-domain:stalmetural.ru'
    assert acct == '2'


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
