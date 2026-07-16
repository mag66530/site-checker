# -*- coding: utf-8 -*-
"""Тесты чистых функций admin_settings_check (без сети/браузера)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from admin_settings_check import _mk_check, load_admin_creds, summarize


def test_mk_check_basic():
    c = _mk_check('login', 'Вход', True, 'ок')
    assert c == {'code': 'login', 'title': 'Вход', 'ok': True,
                 'detail': 'ок', 'warnings': []}


def test_mk_check_roundtrip_kept():
    rt = {'field': 'SORT', 'orig': '100', 'saved': True, 'reverted': True}
    c = _mk_check('categories', 'Категории', True, roundtrip=rt)
    assert c['roundtrip'] is rt


def test_summarize_ok():
    assert summarize([_mk_check('a', 'A', True)]) == 'ok'


def test_summarize_warn():
    assert summarize([_mk_check('a', 'A', True, warnings=['w'])]) == 'warn'


def test_summarize_fail_beats_warn():
    checks = [_mk_check('a', 'A', True, warnings=['w']),
              _mk_check('b', 'B', False)]
    assert summarize(checks) == 'fail'


def _write(tmp_path, name, data):
    (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False),
                                 encoding='utf-8')


def test_creds_missing(tmp_path):
    assert load_admin_creds(tmp_path) is None


def test_creds_prod_and_test_are_separate_files(tmp_path):
    _write(tmp_path, 'admin.local.json',
           {'login': 'prod', 'password': 'p1'})
    _write(tmp_path, 'admin.test.local.json',
           {'domain': 'https://t.example.ru', 'login': 'test',
            'password': 'p2', 'basic_login': 'b', 'basic_password': 'bp'})
    prod = load_admin_creds(tmp_path)
    test = load_admin_creds(tmp_path, test=True)
    assert prod == {'login': 'prod', 'password': 'p1'}
    assert test == {'domain': 'https://t.example.ru', 'login': 'test',
                    'password': 'p2', 'basic_login': 'b',
                    'basic_password': 'bp'}


def test_creds_template_ignored(tmp_path):
    _write(tmp_path, 'admin.local.json',
           {'login': 'ВПИШИ_СЮДА_ЛОГИН', 'password': 'x'})
    assert load_admin_creds(tmp_path) is None


def test_creds_broken_json(tmp_path):
    (tmp_path / 'admin.local.json').write_text('{oops', encoding='utf-8')
    assert load_admin_creds(tmp_path) is None
