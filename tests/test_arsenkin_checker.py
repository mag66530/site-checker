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


def test_parse_result_real_arsenkin_format():
    """Реальный ответ Арсенкина: данные по URL в result.resp, а g/y - флаги ПС."""
    real = {
        'code': 'TASK_RESULT', 'task_id': 30660436,
        'result': {
            'g': 1, 'y': 1,
            'resp': {
                'https://stalmetural.ru/soglasie-na-obrabotku-personalnyh-dannyh/':
                    {'yandex': 1, 'google': 1, 'yandex_doc': '...',
                     'indexdate': '2026-02-19', 'google_doc': ''},
                'https://stalmetural.ru/polzovatelskoe-soglashenie/':
                    {'yandex': 1, 'google': 0, 'indexdate': '2026-03-13'},
            },
        },
        'created_at': '2026-07-17 14:41:49', 'finished_at': None,
    }
    rows = parse_result(real, want_yandex=True, want_google=True)
    by_url = {r['url']: r for r in rows}
    assert len(rows) == 2
    assert by_url['https://stalmetural.ru/soglasie-na-obrabotku-personalnyh-dannyh/'] \
        == {'url': 'https://stalmetural.ru/soglasie-na-obrabotku-personalnyh-dannyh/',
            'yandex': True, 'google': True}
    assert by_url['https://stalmetural.ru/polzovatelskoe-soglashenie/']['google'] is False


def test_parse_result_grouped_under_data_and_lists():
    """Тот же формат, но вложен в data и списками {url,index}."""
    result = {'data': {
        'y': [{'url': 'https://a.ru/', 'index': 'да'}],
        'g': [{'url': 'https://a.ru/', 'index': 'нет'}],
    }}
    rows = parse_result(result)
    assert rows[0] == {'url': 'https://a.ru/', 'yandex': True, 'google': False}
