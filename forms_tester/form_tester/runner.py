"""Оркестрация прогона. Реализация пока в `test_all`; импорт ленивый, чтобы избежать циклов."""


def run_test(ОЧИСТИТЬ_EXCEL=True, stop_flag=None, headless=True):
    from test_all import run_test as _run_test

    return _run_test(ОЧИСТИТЬ_EXCEL=ОЧИСТИТЬ_EXCEL, stop_flag=stop_flag, headless=headless)
