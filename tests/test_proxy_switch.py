# -*- coding: utf-8 -*-
"""Галочка «Прокси» должна ВЫКЛЮЧАТЬ прокси, а не просто не передавать адрес.

Поймано на АПС: у проекта use_proxy=true, галочку выключили, прогон КП всё
равно пошёл через прокси и получил 4xx (через прокси aviastal.ru отдаёт 401,
напрямую 200). Причина: страница при выключенной галочке просто не клала
proxy_url в окружение, а прогон доставал адрес сам.
"""
import proxy_config as pc


def _чисто(monkeypatch):
    """Убираем все источники адреса, кроме общего секрета-заглушки."""
    for k in ('USE_PROXY', 'proxy_url', 'HTTP_PROXY', 'http_proxy',
              'proxy_url_avia'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pc, '_secret', lambda k: None)
    monkeypatch.setattr(pc, '_db_proxy', lambda pid: None)


# ── Чтение решения из окружения ──────────────────────────────────────


def test_env_use_proxy_читает_разные_написания(monkeypatch):
    for v in ('0', 'false', 'FALSE', 'no', 'off'):
        monkeypatch.setenv('USE_PROXY', v)
        assert pc.env_use_proxy() is False, v
    for v in ('1', 'true', 'TRUE', 'yes', 'on'):
        monkeypatch.setenv('USE_PROXY', v)
        assert pc.env_use_proxy() is True, v
    monkeypatch.setenv('USE_PROXY', '')
    assert pc.env_use_proxy() is None
    monkeypatch.delenv('USE_PROXY', raising=False)
    assert pc.env_use_proxy() is None


def test_proxy_env_кладёт_и_адрес_и_решение():
    assert pc.proxy_env('http://u:p@host:8080') == {
        'proxy_url': 'http://u:p@host:8080', 'USE_PROXY': '1'}


def test_выключенная_галочка_гасит_системный_прокси():
    """requests по умолчанию читает HTTP_PROXY/HTTPS_PROXY сам и идёт через
    прокси, даже если мы передали «без прокси». На машине разработчика такие
    переменные выставлены - и прогон «без прокси» всё равно шёл через него."""
    env = pc.proxy_env(None)
    assert env['USE_PROXY'] == '0'
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
              'proxy_url'):
        assert env[k] == '', k
    assert env['NO_PROXY'] == '*' and env['no_proxy'] == '*'
    assert pc.proxy_env('') == env


# ── proxy_for_project ────────────────────────────────────────────────


def test_выключенная_галочка_сильнее_use_proxy_проекта(monkeypatch):
    """Главный случай АПС: use_proxy=true, галочка выключена → без прокси."""
    _чисто(monkeypatch)
    monkeypatch.setattr(pc, '_secret',
                        lambda k: 'http://u:p@host:8080' if k == 'proxy_url' else None)
    monkeypatch.setattr(pc, 'project_use_proxy', lambda pid: True)
    # без решения - прокси берётся (прежнее поведение)
    assert pc.proxy_for_project('avia') == 'http://u:p@host:8080'
    # галочка выключена - прокси нет
    monkeypatch.setenv('USE_PROXY', '0')
    assert pc.proxy_for_project('avia') is None


def test_включённая_галочка_включает_прокси_проекту_без_use_proxy(monkeypatch):
    """Обратный случай: у проекта use_proxy=false, но человек включил галочку."""
    _чисто(monkeypatch)
    monkeypatch.setattr(pc, '_secret',
                        lambda k: 'http://u:p@host:8080' if k == 'proxy_url' else None)
    monkeypatch.setattr(pc, 'project_use_proxy', lambda pid: False)
    assert pc.proxy_for_project('smu') is None          # без решения
    monkeypatch.setenv('USE_PROXY', '1')
    assert pc.proxy_for_project('smu') == 'http://u:p@host:8080'


def test_решение_не_задано_работает_как_раньше(monkeypatch):
    _чисто(monkeypatch)
    monkeypatch.setattr(pc, '_secret',
                        lambda k: 'http://u:p@host:8080' if k == 'proxy_url' else None)
    monkeypatch.setattr(pc, 'project_use_proxy', lambda pid: True)
    assert pc.proxy_for_project('imp') == 'http://u:p@host:8080'
    monkeypatch.setattr(pc, 'project_use_proxy', lambda pid: False)
    assert pc.proxy_for_project('smu') is None


def test_галочка_включена_но_адреса_нет(monkeypatch):
    _чисто(monkeypatch)
    monkeypatch.setenv('USE_PROXY', '1')
    monkeypatch.setattr(pc, 'project_use_proxy', lambda pid: False)
    assert pc.proxy_for_project('smu') is None


# ── Чек-лист: решение едет в creds ───────────────────────────────────


def test_runner_уважает_выключенную_галочку():
    """runner_30min берёт решение из creds['use_proxy_choice']: у чек-листа
    прогон идёт в том же процессе, окружения там нет."""
    import inspect
    import runner_30min
    src = inspect.getsource(runner_30min.run_check)
    assert "creds.get('use_proxy_choice')" in src
    # и при False прокси обнуляется до всех прочих источников
    i_choice = src.index("use_proxy_choice")
    i_none = src.index('proxy_url = None', i_choice)
    i_fallback = src.index('proxy_for_project(pid)', i_choice)
    assert i_none < i_fallback


def test_страница_кп_передаёт_решение():
    """variables_check кладёт в окружение прогона и адрес, и решение."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / 'checklists'
           / 'variables_check.py').read_text(encoding='utf-8')
    assert 'proxy_env(_effective_proxy)' in src


def test_кп_прогон_уважает_выключатель():
    from pathlib import Path
    src = (Path(__file__).parent.parent / 'variables_run.py').read_text(
        encoding='utf-8')
    assert 'env_use_proxy()' in src
    assert 'Прокси выключен галочкой на странице' in src
