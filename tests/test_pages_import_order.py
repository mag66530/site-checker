"""Страницы Streamlit не должны использовать имя раньше его импорта.

Файл страницы выполняется сверху вниз при каждом заходе, поэтому импорт,
опущенный в середину файла, работает только для кода НИЖЕ него. Так и упала
«Проверка целей»: `from checklists import ui_widgets as _ui` стоял на 393-й
строке, а `_ui.estimate_badge(...)` - на 230-й, и страница валилась NameError,
как только у прогноза времени появлялись данные (то есть при выбранном сайте -
на пустой странице ошибки не было).

Проверяем только код ВЕРХНЕГО уровня: внутри функций имя достаточно определить
до вызова, а не до определения.
"""
import ast
import builtins
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
СТРАНИЦЫ = sorted((КОРЕНЬ / 'checklists').glob('*.py'))

ВСТРОЕННЫЕ = set(dir(builtins)) | {'__file__', '__name__', '__doc__'}


def _определяемые(узел):
    """Имена, которые этот оператор верхнего уровня делает доступными."""
    имена = set()
    if isinstance(узел, (ast.Import, ast.ImportFrom)):
        for a in узел.names:
            имена.add((a.asname or a.name).split('.')[0])
    elif isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        имена.add(узел.name)
    else:
        for вложенный in ast.walk(узел):
            if isinstance(вложенный, ast.Name) and isinstance(вложенный.ctx, ast.Store):
                имена.add(вложенный.id)
            elif isinstance(вложенный, (ast.Import, ast.ImportFrom)):
                for a in вложенный.names:
                    имена.add((a.asname or a.name).split('.')[0])
            elif isinstance(вложенный, (ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.ClassDef)):
                имена.add(вложенный.name)
            elif isinstance(вложенный, ast.ExceptHandler) and вложенный.name:
                имена.add(вложенный.name)      # except … as e
    return имена


def _чтения_вне_функций(узел):
    """Имена, читаемые ПРЯМО в этом операторе, минуя тела функций/классов и
    comprehension-переменные (их область своя)."""
    if isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []                       # тело выполнится позже, при вызове
    найдено, стек = [], [узел]
    局部 = set()
    while стек:
        n = стек.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                          ast.Lambda)):
            continue
        if isinstance(n, ast.comprehension):
            for цель in ast.walk(n.target):
                if isinstance(цель, ast.Name):
                    局部.add(цель.id)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            найдено.append((n.id, n.lineno))
        стек.extend(ast.iter_child_nodes(n))
    return [(имя, стр) for имя, стр in найдено if имя not in 局部]


def _поздние_имена(исходник: str, файл: str = '<код>') -> dict:
    """{имя: строка} - что читается в теле модуля раньше своего объявления."""
    дерево = ast.parse(исходник, filename=файл)
    известные = set(ВСТРОЕННЫЕ)
    поздние = {}
    for узел in дерево.body:
        # Сначала объявления САМОГО оператора: внутри одного `for`/`if`/`with`
        # имя может и определяться, и читаться (цикл по городам, импорт внутри
        # ветки) - это законно и к нашей ошибке отношения не имеет. Ловим только
        # чтение из оператора, который стоит ВЫШЕ объявления.
        известные |= _определяемые(узел)
        for имя, стр in _чтения_вне_функций(узел):
            if имя not in известные:
                поздние.setdefault(имя, стр)
    return поздние


@pytest.mark.parametrize('путь', СТРАНИЦЫ, ids=lambda p: p.name)
def test_имена_определены_до_использования(путь):
    поздние = _поздние_имена(путь.read_text(encoding='utf-8'), str(путь))
    if поздние:
        детали = '; '.join(f'{имя} читается на строке {стр}, а определяется ниже'
                           for имя, стр in sorted(поздние.items(),
                                                  key=lambda kv: kv[1]))
        pytest.fail(f'{путь.name}: {детали}')
    print(f'✓ {путь.name}: порядок объявлений в теле модуля корректен')


def test_ловит_реальную_поломку_страницы_целей():
    """Тот самый случай: использование выше, импорт ниже."""
    поздние = _поздние_имена(
        'import streamlit as st\n'
        'if _оценка:\n'
        '    _ui.estimate_badge(*_оценка)\n'
        '_оценка = None\n'
        'from checklists import ui_widgets as _ui\n')

    assert '_ui' in поздние and поздние['_ui'] == 3, поздние
    print('✓ проверка ловит импорт, опущенный ниже места использования')


def test_законные_случаи_не_ложатся():
    """Цикл, импорт внутри ветки, except … as e - не ошибки."""
    assert _поздние_имена(
        'for город in ("Москва",):\n'
        '    print(город)\n'
        'if True:\n'
        '    import json as _js\n'
        '    print(_js.dumps({}))\n'
        'try:\n'
        '    pass\n'
        'except Exception as e:\n'
        '    print(e)\n'
        '[x for x in range(3)]\n') == {}
    print('✓ ложных срабатываний на обычном коде страниц нет')
