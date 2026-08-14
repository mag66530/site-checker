"""Список проектов страницы «Проверка форм» и список запускалки не должны
расходиться.

Живой случай: проект добавили на страницу, но забыли в forms_run.PROJECT_NAMES -
кнопка «Запустить» отработала, а прогон умер сразу на разборе аргументов
(«argument --project: invalid choice»), и в логе не было ни одной формы.
"""
import re
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import forms_run


def _проекты_страницы() -> dict:
    """PROJECTS из checklists/forms_check.py БЕЗ импорта страницы (она вся
    выполняется сверху вниз и требует Streamlit): выдёргиваем блок и исполняем
    отдельно - это обычный словарь-литерал."""
    текст = (КОРЕНЬ / 'checklists' / 'forms_check.py').read_text(encoding='utf-8')
    кусок = re.search(r'^PROJECTS = \{.*?^\}', текст, re.S | re.M)
    assert кусок, 'не нашёл PROJECTS в forms_check.py'
    место = {}
    exec(compile(кусок.group(0), 'forms_check_PROJECTS', 'exec'), место)
    return место['PROJECTS']


def test_каждый_проект_страницы_можно_запустить():
    страница = set(_проекты_страницы())
    запускалка = set(forms_run.PROJECT_NAMES)

    забыли = страница - запускалка
    assert not забыли, ('на странице есть, а запустить нельзя (argparse отвергнет): '
                        + ', '.join(sorted(забыли)))
    print(f'✓ все {len(страница)} проектов страницы принимает forms_run')


def test_названия_проектов_совпадают():
    """Название идёт в отчёт и в Telegram - разнобой путал бы получателей."""
    for pid, данные in _проекты_страницы().items():
        assert forms_run.PROJECT_NAMES[pid] == данные['name'], pid
    print('✓ названия проектов на странице и в запускалке одинаковые')


def test_у_каждого_проекта_есть_конфиг_форм():
    """Без forms_tester/projects/<id>/config.py прогон падает уже после старта."""
    for pid in _проекты_страницы():
        конфиг = КОРЕНЬ / 'forms_tester' / 'projects' / pid / 'config.py'
        assert конфиг.is_file(), f'нет конфига форм: {конфиг}'
    print('✓ у каждого проекта страницы есть конфиг форм')
