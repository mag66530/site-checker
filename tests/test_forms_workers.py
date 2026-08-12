"""Сколько браузеров форм-тестер держит в памяти одновременно.

У каждого потока свой Chromium, поэтому число потоков = число браузеров.
В контейнере Streamlit Cloud три штуки выбирают лимит памяти, контейнер
убивают, и приложение падает целиком вместе с фоновым прогоном.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / 'forms_tester'))

import test_all as t


def test_локально_три_потока(monkeypatch):
    monkeypatch.delenv('FORMS_MAX_WORKERS', raising=False)
    monkeypatch.setattr(t, 'в_контейнере', lambda: False)

    assert t.параллельных_форм() == 3
    print('✓ на рабочей машине - три формы разом')


def test_в_облаке_одна(monkeypatch):
    monkeypatch.delenv('FORMS_MAX_WORKERS', raising=False)
    monkeypatch.setattr(t, 'в_контейнере', lambda: True)

    assert t.параллельных_форм() == 1
    print('✓ в контейнере - строго по одной, один Chromium в памяти')


def test_ручное_переопределение_сильнее(monkeypatch):
    monkeypatch.setenv('FORMS_MAX_WORKERS', '2')
    monkeypatch.setattr(t, 'в_контейнере', lambda: True)

    assert t.параллельных_форм() == 2
    print('✓ FORMS_MAX_WORKERS перебивает автоопределение')


def test_мусор_в_переменной_не_ломает(monkeypatch):
    monkeypatch.setenv('FORMS_MAX_WORKERS', 'три')
    monkeypatch.setattr(t, 'в_контейнере', lambda: False)

    assert t.параллельных_форм() == 3

    monkeypatch.setenv('FORMS_MAX_WORKERS', '0')
    assert t.параллельных_форм() == 1, 'ноль потоков - это не прогон'
    print('✓ битое значение переменной не роняет прогон')


def test_контейнер_определяется_по_mount_src(monkeypatch):
    """Streamlit Cloud монтирует репозиторий в /mount/src - это и есть признак."""
    monkeypatch.setattr(t.os.path, 'isdir', lambda p: p == '/mount/src')
    assert t.в_контейнере() is True

    monkeypatch.setattr(t.os.path, 'isdir', lambda p: False)
    assert t.в_контейнере() is False
    print('✓ облако узнаётся по /mount/src')
