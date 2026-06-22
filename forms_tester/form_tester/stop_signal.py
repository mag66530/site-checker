"""Единая проверка остановки: callable (GUI) или файл (сервер / CLI)."""

from __future__ import annotations

import os
from collections.abc import Callable


def make_stop_check(
    stop_flag: Callable[[], bool] | None = None,
    stop_file: str | None = None,
) -> Callable[[], bool]:
    """Возвращает функцию без аргументов: True = нужно остановить прогон."""

    def check() -> bool:
        if stop_flag is not None:
            try:
                if stop_flag():
                    return True
            except Exception:
                return True
        if stop_file and os.path.isfile(stop_file):
            return True
        return False

    return check
