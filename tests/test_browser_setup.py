"""Тесты browser_setup.ensure_browser(): кэшируем ТОЛЬКО успех, не неудачу.

Баг: раньше @functools.lru_cache кэшировал ЛЮБОЙ результат навсегда (плюс
ещё один слой st.cache_resource поверх в forms_check.py/goals_check.py) -
если Chromium в момент первой проверки был не готов (например, самый первый
запуск контейнера), страница «Браузер ещё не готов» показывалась бы вечно,
даже если браузер уже прекрасно работает - до перезапуска всего приложения.
Пользователь словил это локально (playwright реально работал во всех наших
тестах в этой же сессии, но forms_check.py настаивал на «не готов»)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_setup


def _reset():
    browser_setup._УСПЕХ = None


def test_failure_is_not_cached_retries_next_call(monkeypatch):
    """Первый вызов - неудача (браузер "не готов"), второй - без ошибок:
    должен вернуть УСПЕХ, а не повторить закэшированную неудачу."""
    _reset()
    calls = {'n': 0}

    def _fake_на_месте():
        # ensure_browser() зовёт эту проверку ДВАЖДЫ за один неудачный вызов
        # (до попытки install и после) - оба раза False в первом вызове,
        # True начиная со второго вызова ensure_browser().
        calls['n'] += 1
        return calls['n'] > 2

    monkeypatch.setattr(browser_setup, '_браузер_на_месте', _fake_на_месте)
    monkeypatch.setattr(browser_setup.subprocess, 'run', lambda *a, **kw: None)

    ok1, _ = browser_setup.ensure_browser()
    assert ok1 is False, 'первый вызов - браузер ещё не готов'

    ok2, msg2 = browser_setup.ensure_browser()
    assert ok2 is True, 'неудача не должна кэшироваться - второй вызов обязан перепроверить'
    assert msg2 == 'браузер готов'
    print('✓ неудача не закэширована - следующий вызов видит, что браузер уже готов')


def test_success_is_cached_no_repeat_check(monkeypatch):
    """Успех - кэшируется: повторный вызов не должен снова дёргать проверку."""
    _reset()
    calls = {'n': 0}

    def _fake_на_месте():
        calls['n'] += 1
        return True

    monkeypatch.setattr(browser_setup, '_браузер_на_месте', _fake_на_месте)

    ok1, _ = browser_setup.ensure_browser()
    ok2, _ = browser_setup.ensure_browser()
    assert ok1 is True and ok2 is True
    assert calls['n'] == 1, 'успех должен кэшироваться - повторной проверки быть не должно'
    print('✓ успех закэширован - повторный вызов не перепроверяет заново')


def test_no_playwright_library_is_not_cached_as_failure(monkeypatch):
    """Библиотеки playwright нет вовсе - тоже неудача, тоже не должна залипать
    (если её потом установят и перезапустят worker в том же процессе)."""
    _reset()
    import builtins
    _orig_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == 'playwright':
            raise ImportError('no playwright')
        return _orig_import(name, *a, **kw)

    monkeypatch.setattr(builtins, '__import__', _fake_import)
    ok, msg = browser_setup.ensure_browser()
    assert ok is False
    assert 'playwright' in msg
    print('✓ отсутствие библиотеки playwright - тоже не кэшируется как успех (очевидно)')


def test_real_exception_text_surfaces_in_message(monkeypatch):
    """Раньше при сбое _браузер_на_месте() пользователь видел только общую
    заглушку «проверьте packages.txt» без единого намёка на РЕАЛЬНУЮ причину -
    это и не давало продвинуться в диагностике живого бага. Теперь настоящий
    текст исключения должен быть виден в итоговом сообщении."""
    _reset()

    async def _boom():
        raise RuntimeError('какая-то конкретная причина падения')

    monkeypatch.setattr(browser_setup, '_браузер_на_месте_async', _boom)
    monkeypatch.setattr(browser_setup.subprocess, 'run', lambda *a, **kw: None)

    ok, msg = browser_setup.ensure_browser()
    assert ok is False
    assert 'какая-то конкретная причина падения' in msg
    assert 'RuntimeError' in msg
    print('✓ реальный текст исключения виден в сообщении, не только общая заглушка')


def teardown_module():
    _reset()
