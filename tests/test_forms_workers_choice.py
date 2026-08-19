"""Выбор числа потоков в проверке форм.

Потоков столько же, сколько браузеров: в облаке они делят контейнер с самим
приложением, и на трёх контейнер выходил за лимит памяти - падало ВСЁ
приложение вместе с прогоном. Поэтому число задаётся на странице (1..5),
по умолчанию 2.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / 'forms_tester'))

import test_all as t


def test_по_умолчанию_два(monkeypatch):
    monkeypatch.delenv('FORMS_MAX_WORKERS', raising=False)

    assert t.параллельных_форм() == 2
    assert t.ПОТОКОВ_ПО_УМОЛЧАНИЮ == 2
    print('✓ без указания - два потока')


def test_явное_значение_главнее(monkeypatch):
    monkeypatch.setenv('FORMS_MAX_WORKERS', '5')

    assert t.параллельных_форм(1) == 1, 'аргумент со страницы сильнее env'
    assert t.параллельных_форм() == 5
    print('✓ значение со страницы важнее переменной окружения')


def test_границы_1_5(monkeypatch):
    monkeypatch.delenv('FORMS_MAX_WORKERS', raising=False)

    assert t.параллельных_форм(0) == 1
    assert t.параллельных_форм(-3) == 1
    assert t.параллельных_форм(99) == 5
    assert t.ПОТОКОВ_МАКСИМУМ == 5
    print('✓ выходящие за 1..5 значения подрезаются')


def test_мусор_не_ломает(monkeypatch):
    monkeypatch.delenv('FORMS_MAX_WORKERS', raising=False)

    assert t.параллельных_форм('три') == 2
    assert t.параллельных_форм('') == 2
    monkeypatch.setenv('FORMS_MAX_WORKERS', 'много')
    assert t.параллельных_форм() == 2
    print('✓ битое значение откатывается к умолчанию')


def test_страница_передаёт_выбор_в_прогон():
    """На странице есть поле, и оно уходит в forms_run аргументом --workers."""
    стр = (КОРЕНЬ / 'checklists' / 'forms_check.py').read_text(encoding='utf-8')
    прогон = (КОРЕНЬ / 'forms_run.py').read_text(encoding='utf-8')

    assert "'Форм одновременно'" in стр
    assert "max_value=5" in стр and "value=2" in стр
    assert "'--workers'" in стр and "--workers" in прогон
    assert 'потоков=a.workers' in прогон
    print('✓ поле на странице → --workers → run_test')
