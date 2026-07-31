"""Тесты пакетного сохранения настроек проекта (auth/db.set_project_settings).

Смысл: форма шлёт все поля разом, и раньше на каждое уходил свой SQL-запрос -
на удалённой базе это десяток round-trip'ов и заметная задержка «Сохранить».
Проверяем, что запросов ровно два (upsert + delete) и параметры не перепутаны.
Реальная БД не нужна - подменяем соединение.
"""
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeCursor:
    def __init__(self, calls):
        self._calls = calls

    def execute(self, sql, params=None):
        self._calls.append((' '.join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def cursor(self, *a, **kw):
        return _FakeCursor(self._calls)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def db_calls(monkeypatch):
    """Подменяем соединение и шифрование; возвращаем список SQL-вызовов."""
    from auth import db

    calls = []

    @contextmanager
    def _fake_conn():
        yield _FakeConn(calls)

    monkeypatch.setattr(db, '_conn', _fake_conn)
    monkeypatch.setattr(db, '_PROJ_SETTINGS_READY', True)
    monkeypatch.setattr(db.security, 'encrypt_secret', lambda v: f'enc({v})')
    return db, calls


def test_all_values_saved_in_one_query(db_calls):
    db, calls = db_calls
    db.set_project_settings('smu', {
        'proxy_url': 'http://p:1', 'textru_key': 'abc', 'arsenkin_token': 'xyz',
    })
    assert len(calls) == 1, f'Ожидали ОДИН пакетный upsert, получили {len(calls)}'
    sql, params = calls[0]
    assert sql.count('(%s, %s, %s)') == 3, f'Три строки в VALUES, а в SQL: {sql}'
    assert params == ['smu', 'proxy_url', 'enc(http://p:1)',
                      'smu', 'textru_key', 'enc(abc)',
                      'smu', 'arsenkin_token', 'enc(xyz)']
    print('✓ три поля = один запрос, параметры в правильном порядке')


def test_empty_values_deleted_in_one_query(db_calls):
    db, calls = db_calls
    db.set_project_settings('imp', {'proxy_url': '', 'textru_key': '   '})
    assert len(calls) == 1, f'Ожидали ОДИН пакетный delete, получили {len(calls)}'
    sql, params = calls[0]
    assert 'DELETE' in sql and 'ANY(%s)' in sql
    assert params == ('imp', ['proxy_url', 'textru_key'])
    print('✓ пустые поля удаляются одним запросом')


def test_mixed_is_two_queries(db_calls):
    """Боевой случай: часть полей заполнена, часть очищена."""
    db, calls = db_calls
    db.set_project_settings('mpe', {
        'proxy_url': 'http://p:1', 'textru_key': '', 'arsenkin_token': 'xyz',
    })
    assert len(calls) == 2, f'Ожидали upsert + delete = 2 запроса, получили {len(calls)}'
    assert 'INSERT' in calls[0][0]
    assert 'DELETE' in calls[1][0]
    print('✓ смешанный случай = ровно два запроса')


def test_eleven_fields_still_two_queries(db_calls):
    """Столько полей реально в форме - раньше это было 11 обращений к базе."""
    db, calls = db_calls
    vals = {f'key_{i}': (f'v{i}' if i % 2 else '') for i in range(11)}
    db.set_project_settings('smu', vals)
    assert len(calls) == 2, f'11 полей должны уложиться в 2 запроса, вышло {len(calls)}'
    print(f'✓ 11 полей формы = {len(calls)} запроса вместо 11')


def test_nothing_to_do_makes_no_queries(db_calls):
    db, calls = db_calls
    db.set_project_settings('smu', {})
    assert calls == [], 'Пустой словарь не должен ходить в базу'
    print('✓ пустой ввод не дёргает базу')


def test_values_are_encrypted(db_calls):
    """Секреты не должны уезжать в базу открытым текстом."""
    db, calls = db_calls
    db.set_project_settings('smu', {'proxy_url': 'http://user:pass@host'})
    _, params = calls[0]
    assert 'enc(' in params[2], 'Значение должно шифроваться перед записью'
    assert params[2] != 'http://user:pass@host'
    print('✓ значение шифруется')
