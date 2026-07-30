"""Тесты kp_sheets.kp_sheet_url() - приоритет источников после того, как
ссылку на КП-таблицу стало можно менять на странице «Настройки проекта» (БД).

Порядок тот же, что у остальных настроек проекта (см. proxy_config.py):
БД личного кабинета → env kp_sheet_url_<pid> → st.secrets → projects/<pid>.json.
Без подмены _db_kp_sheet_url тесты реально ходили бы в Supabase - тестируем
через monkeypatch, как test_proxy_config.py делает для _db_proxy."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kp_sheets as ks


def _clear_env(monkeypatch, pid=''):
    for k in (f'kp_sheet_url_{pid}', 'kp_sheet_url'):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def no_db(monkeypatch):
    """БД «не отвечает» (None) - для тестов, где важен только env/json-фоллбэк."""
    monkeypatch.setattr(ks, '_db_kp_sheet_url', lambda pid: None)


def test_db_wins_over_everything(monkeypatch):
    _clear_env(monkeypatch, 'smu')
    monkeypatch.setenv('kp_sheet_url_smu', 'https://docs.google.com/env')
    monkeypatch.setattr(ks, '_db_kp_sheet_url',
                        lambda pid: 'https://docs.google.com/db' if pid == 'smu' else None)
    assert ks.kp_sheet_url('smu') == 'https://docs.google.com/db'
    print('✓ БД (настройки проекта) побеждает env и всё остальное')


def test_env_wins_when_no_db(monkeypatch, no_db):
    _clear_env(monkeypatch, 'imp')
    monkeypatch.setenv('kp_sheet_url_imp', 'https://docs.google.com/env')
    assert ks.kp_sheet_url('imp') == 'https://docs.google.com/env'
    print('✓ без БД - побеждает env kp_sheet_url_<pid>')


def test_falls_back_to_projects_json(monkeypatch, no_db, tmp_path):
    _clear_env(monkeypatch, 'testproj')
    monkeypatch.setattr(ks, 'ROOT', tmp_path)
    (tmp_path / 'projects').mkdir()
    (tmp_path / 'projects' / 'testproj.json').write_text(
        json.dumps({'kp_sheet_url': 'https://docs.google.com/from-json'}),
        encoding='utf-8')
    assert ks.kp_sheet_url('testproj') == 'https://docs.google.com/from-json'
    print('✓ без БД и без env - берётся из projects/<pid>.json')


def test_empty_when_nothing_configured(monkeypatch, no_db, tmp_path):
    _clear_env(monkeypatch, 'noproj')
    monkeypatch.setattr(ks, 'ROOT', tmp_path)
    assert ks.kp_sheet_url('noproj') == ''
    print('✓ ничего не настроено - пустая строка, не падение')


def test_db_helper_empty_pid_returns_none_without_import():
    """Пустой project_id - None сразу, даже не пытаясь дойти до auth/БД."""
    assert ks._db_kp_sheet_url('') is None
    print('✓ пустой project_id → None, без похода в БД')


def test_db_helper_swallows_any_error(monkeypatch):
    """auth.project_setting падает (нет соединения с БД и т.п.) - _db_kp_sheet_url
    ловит это сама и отдаёт None, а не исключение (иначе kp_sheet_url() падал бы
    целиком, если Supabase временно недоступен)."""
    import types
    fake_auth = types.SimpleNamespace(
        project_setting=lambda pid, name: (_ for _ in ()).throw(RuntimeError('нет сети')))
    monkeypatch.setitem(sys.modules, 'auth', fake_auth)
    assert ks._db_kp_sheet_url('smu') is None
    print('✓ ошибка БД внутри _db_kp_sheet_url - тихо None, не исключение')
