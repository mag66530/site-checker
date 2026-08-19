"""Откуда «Проверка скорости» берёт ключ PageSpeed.

Ключ, сохранённый на странице «Настройки проекта», страница не видела: свой
локальный _secret() читал только st.secrets и окружение, а базу личного
кабинета - нет. Человек вводил ключ руками «на сессию», и это выглядело как
разовый сбой. Порядок источников теперь тот же, что у остальных проверок.
"""
import importlib
import sys
import types
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))


@pytest.fixture
def модуль(monkeypatch):
    """pagespeed_check - страница Streamlit: импортировать её целиком нельзя
    (выполнится весь UI). Берём только нужные функции, выполнив шапку файла до
    объявления _api_key."""
    исходник = (КОРЕНЬ / 'checklists' / 'pagespeed_check.py').read_text(
        encoding='utf-8')
    граница = исходник.index('# ── Управление фоновым процессом')
    m = types.ModuleType('ps_head')
    m.__dict__['__file__'] = str(КОРЕНЬ / 'checklists' / 'pagespeed_check.py')
    exec(compile(исходник[:граница], 'pagespeed_check.py', 'exec'), m.__dict__)
    return m


def _подменить_auth(monkeypatch, значение):
    фейк = types.ModuleType('auth')
    фейк.project_setting = lambda pid, name: значение
    monkeypatch.setitem(sys.modules, 'auth', фейк)


def test_настройки_проекта_главнее_секретов(модуль, monkeypatch):
    _подменить_auth(monkeypatch, 'ключ-из-кабинета')
    monkeypatch.setattr(модуль, '_secret',
                        lambda k, d='': 'ключ-из-секрета')
    monkeypatch.setenv('PAGESPEED_API_KEY', 'ключ-из-окружения')

    assert модуль._api_key('sm') == 'ключ-из-кабинета'
    print('✓ ключ из «Настроек проекта» побеждает секреты и окружение')


def test_без_кабинета_работают_секреты(модуль, monkeypatch):
    _подменить_auth(monkeypatch, None)
    monkeypatch.setattr(модуль, '_secret',
                        lambda k, d='': 'ключ-из-секрета'
                        if k == 'pagespeed_api_key_sm' else '')

    assert модуль._api_key('sm') == 'ключ-из-секрета'
    print('✓ без настройки в БД берётся пер-проектный секрет')


def test_ошибка_базы_не_роняет_страницу(модуль, monkeypatch):
    фейк = types.ModuleType('auth')

    def _падает(pid, name):
        raise RuntimeError('нет соединения с БД')

    фейк.project_setting = _падает
    monkeypatch.setitem(sys.modules, 'auth', фейк)
    monkeypatch.setattr(модуль, '_secret',
                        lambda k, d='': 'запасной-ключ'
                        if k == 'pagespeed_api_key' else '')

    assert модуль._api_key('sm') == 'запасной-ключ'
    print('✓ недоступная БД не ломает поиск ключа')


def test_ручной_ввод_остаётся_последним(модуль, monkeypatch):
    _подменить_auth(monkeypatch, None)
    monkeypatch.setattr(модуль, '_secret', lambda k, d='': '')
    monkeypatch.delenv('PAGESPEED_API_KEY', raising=False)
    модуль.st.session_state['ps_key_sm'] = 'ключ-на-сессию'

    assert модуль._api_key('sm') == 'ключ-на-сессию'
    print('✓ ручной ввод по-прежнему работает, когда больше негде взять')
