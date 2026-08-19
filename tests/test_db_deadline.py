# -*- coding: utf-8 -*-
"""Обращение к базе не должно вешать интерфейс.

Живой случай: в настройках проекта лежит сессия браузера (~15 КБ). На канале,
который режет крупные пакеты, сервер запрос выполнял, а ответ до клиента не
доходил - страница «Автокликеры» грузилась бесконечно. connect_timeout тут не
помогает (соединение уже установлено), serverный statement_timeout тоже
(сервер отработал быстро) - нужен клиентский дедлайн."""
import time

import pytest

from auth import db


def test_быстрая_операция_проходит_как_обычно():
    assert db._with_deadline(lambda: 42) == 42


def test_аргументы_передаются():
    assert db._with_deadline(lambda a, b=0: a + b, 1, b=2) == 3


def test_зависшая_операция_обрывается_дедлайном(monkeypatch):
    monkeypatch.setattr(db, 'CLIENT_DEADLINE_SEC', 1)
    t = time.time()
    with pytest.raises(TimeoutError) as e:
        db._with_deadline(lambda: time.sleep(30))
    assert time.time() - t < 5, 'дедлайн не сработал'
    # сообщение объясняет, что чинить
    assert 'MTU' in str(e.value) or 'канал' in str(e.value)


def test_ошибка_операции_доходит_как_есть():
    class Своя(RuntimeError):
        pass

    def _падает():
        raise Своя('оригинал')

    with pytest.raises(Своя, match='оригинал'):
        db._with_deadline(_падает)


def test_дедлайн_не_держит_процесс():
    """Поток должен быть демоном - иначе интерпретатор не выйдет."""
    import threading
    before = {t.name for t in threading.enumerate()}
    db._with_deadline(lambda: None)
    new = [t for t in threading.enumerate()
           if t.name not in before and t.name == 'db-deadline']
    assert all(t.daemon for t in new)


def test_настройки_проекта_под_дедлайном(monkeypatch):
    """get_project_settings обязана быть обёрнута - именно она тянет сессию."""
    monkeypatch.setattr(db, 'CLIENT_DEADLINE_SEC', 1)
    monkeypatch.setattr(db, '_get_project_settings_raw',
                        lambda pk: time.sleep(30))
    with pytest.raises(TimeoutError):
        db.get_project_settings('mpi')


def test_недоступность_базы_кэшируется_как_пусто(monkeypatch):
    """Иначе каждый рендер страницы заново ждёт дедлайн."""
    import auth.ui as ui

    monkeypatch.setattr(ui.db, 'get_project_settings',
                        lambda pk: (_ for _ in ()).throw(TimeoutError('канал')))
    ui._c_proj_settings.clear()
    assert ui._c_proj_settings('mpi') == {}
    assert 'канал' in ui.settings_db_error()
    ui._c_proj_settings.clear()
