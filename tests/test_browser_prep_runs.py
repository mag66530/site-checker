"""Фоновые прогоны обязаны готовить браузер перед Playwright.

В облаке Chromium не предустановлен: библиотека приезжает из requirements.txt,
а сам браузер доустанавливается в рантайме (browser_setup). Фильтр-тест этого
не делал и падал «Executable doesn't exist … chrome-headless-shell», а в отчёт
писал «ok 0 из 0» - будто проверять было нечего.
"""
import re
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import browser_setup

# Точки входа, которые чек-лист запускает отдельным процессом и которые в итоге
# поднимают Playwright (сами или через импортируемый модуль проверки).
ПРОГОНЫ = ['filters_run.py', 'console_run.py', 'index404_run.py',
           'index_gsc_run.py', 'admin_settings_run.py', 'yabusiness_run.py',
           'review_priority_run.py']


@pytest.mark.parametrize('имя', ПРОГОНЫ)
def test_прогон_готовит_браузер(имя):
    текст = (КОРЕНЬ / имя).read_text(encoding='utf-8')

    assert 'ensure_for_run' in текст or 'ensure_browser' in текст, (
        f'{имя}: перед запуском браузера нет browser_setup - в облаке прогон '
        f'упадёт на «Executable doesn\'t exist»')
    print(f'✓ {имя} готовит браузер')


def test_помощник_молчит_на_готовом_браузере(monkeypatch):
    monkeypatch.setattr(browser_setup, 'ensure_browser',
                        lambda: (True, 'браузер готов'))
    записи = []

    assert browser_setup.ensure_for_run(записи.append, 'Фильтр-тест') is True
    assert записи == [], записи
    print('✓ готовый браузер не засоряет лог')


def test_помощник_сообщает_про_установку(monkeypatch):
    monkeypatch.setattr(browser_setup, 'ensure_browser',
                        lambda: (True, 'браузер установлен'))
    записи = []

    browser_setup.ensure_for_run(записи.append, 'Фильтр-тест')

    assert записи == ['Фильтр-тест: браузер установлен']
    print('✓ доустановку в облаке видно в логе')


def test_помощник_объясняет_отказ(monkeypatch):
    monkeypatch.setattr(browser_setup, 'ensure_browser',
                        lambda: (False, 'нет библиотеки playwright'))
    записи = []

    assert browser_setup.ensure_for_run(записи.append, 'Фильтр-тест') is False
    assert записи and 'нет библиотеки playwright' in записи[0]
    print('✓ причина отказа попадает в лог, а не теряется')


def test_исключение_не_роняет_прогон(monkeypatch):
    def _падает():
        raise RuntimeError('диск переполнен')

    monkeypatch.setattr(browser_setup, 'ensure_browser', _падает)
    записи = []

    assert browser_setup.ensure_for_run(записи.append) is False
    assert записи and 'диск переполнен' in записи[0]
    print('✓ сбой подготовки не бросает исключение в прогон')
