# -*- coding: utf-8 -*-
"""Страницы проверок не должны падать из-за устаревшего run_estimate в памяти.

Живой случай: после обновления кода облако перечитало файлы страниц, но модуль
run_estimate остался в sys.modules старой версии. Строка
`from run_estimate import estimate_forms_seconds` не нашла новую функцию и
уронила ImportError'ом сразу четыре страницы (формы, цели, КП, скорость).
Лечится перечитыванием модуля, а не голым импортом."""
import importlib
import re
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
СТРАНИЦЫ = sorted((КОРЕНЬ / 'checklists').glob('*.py'))


@pytest.mark.parametrize('файл', СТРАНИЦЫ, ids=lambda p: p.name)
def test_страница_не_импортирует_run_estimate_напрямую(файл):
    текст = файл.read_text(encoding='utf-8')
    if 'run_estimate' not in текст:
        pytest.skip('страница не показывает время прогона')
    голый = re.findall(r'^\s*from run_estimate import .*$', текст, re.M)
    assert not голый, (
        f'{файл.name}: голый импорт из run_estimate ломает страницу, если в '
        f'памяти осталась старая версия модуля. Нужно `import run_estimate` + '
        f'importlib.reload при отсутствии функции. Строки: {голый}')
    assert 'importlib.reload' in текст, (
        f'{файл.name}: нет перечитывания run_estimate')


def test_перечитывание_поднимает_новые_функции():
    """Приём должен работать: из старой копии модуля видно новую функцию."""
    import run_estimate

    старая = type(sys)('run_estimate')          # копия без новых функций
    старая.__file__ = run_estimate.__file__
    старая.__spec__ = run_estimate.__spec__
    старая.estimate_run_seconds = run_estimate.estimate_run_seconds
    sys.modules['run_estimate'] = старая
    try:
        assert not hasattr(старая, 'estimate_forms_seconds')
        свежая = importlib.reload(старая)
        assert hasattr(свежая, 'estimate_forms_seconds')
        assert hasattr(свежая, 'format_estimate')
    finally:
        sys.modules['run_estimate'] = run_estimate
