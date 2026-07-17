"""Тесты разбора ответов API Арсенкина (arsenkin_checker) - без сети."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arsenkin_checker import (  # noqa: E402
    _extract_status, _extract_task_id, _to_bool, parse_result,
)


def test_to_bool_variants():
    assert _to_bool('да') is True
    assert _to_bool('yes') is True
    assert _to_bool(1) is True
    assert _to_bool(True) is True
    assert _to_bool('нет') is False
    assert _to_bool('no') is False
    assert _to_bool(0) is False
    assert _to_bool('что-то') is None
    # вложенный dict вида {"index": true}
    assert _to_bool({'index': 'да'}) is True
    assert _to_bool({'status': 0}) is False


def test_extract_task_id_various_keys():
    assert _extract_task_id({'task_id': 12345}) == 12345
    assert _extract_task_id({'data': {'report_id': 'abc'}}) == 'abc'
    assert _extract_task_id({'result': {'hash': 'zz'}}) == 'zz'
    assert _extract_task_id({'nope': 1}) is None


def test_extract_status():
    assert _extract_status({'status': 'process'}) == 'process'
    assert _extract_status({'data': {'state': 'Готово'}}) == 'готово'
    assert _extract_status({'x': 1}) is None


def test_parse_result_list_of_rows():
    result = {'data': [
        {'url': 'https://a.ru/', 'yandex': 'да', 'google': 'нет'},
        {'url': 'https://b.ru/', 'yandex': 'нет', 'google': 'да'},
    ]}
    rows = parse_result(result, want_yandex=True, want_google=True)
    assert rows[0] == {'url': 'https://a.ru/', 'yandex': True, 'google': False}
    assert rows[1] == {'url': 'https://b.ru/', 'yandex': False, 'google': True}


def test_parse_result_dict_keyed_by_url():
    result = {'result': {
        'https://a.ru/': {'yandex': 1, 'google': 0},
    }}
    rows = parse_result(result)
    assert rows[0]['url'] == 'https://a.ru/'
    assert rows[0]['yandex'] is True
    assert rows[0]['google'] is False


def test_parse_result_bare_list_and_alt_keys():
    result = [
        {'query': 'https://a.ru/', 'y': 'yes', 'g': 'no'},
    ]
    rows = parse_result(result)
    assert rows[0]['url'] == 'https://a.ru/'
    assert rows[0]['yandex'] is True
    assert rows[0]['google'] is False


def test_parse_result_respects_engine_selection():
    result = [{'url': 'https://a.ru/', 'yandex': 'да', 'google': 'да'}]
    rows = parse_result(result, want_yandex=True, want_google=False)
    assert rows[0]['yandex'] is True
    assert rows[0]['google'] is None       # Google не запрашивали
