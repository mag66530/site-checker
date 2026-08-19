"""Счётчик «проверено форм» и секундомер на странице проверки форм.

На прогоне МПЭ по 10 городам страница обещала 60 форм, а счётчик доходил до 91
и продолжал расти: в запасном пути он считал СТРОКИ листа «Логи», куда кроме
форм попадают мобильная вёрстка, cookie-проверка и файл-проба.

Секундомер при этом пропадал: время старта брали из session_state, а он пуст
после перезагрузки страницы, в чужой сессии и после обновления приложения в
облаке.
"""
import sys
import types
from pathlib import Path

import pytest
from openpyxl import Workbook

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))


@pytest.fixture
def модуль():
    """Нужные функции страницы без запуска Streamlit-кода: шапка файла плюс
    _forms_done_live, который объявлен ниже, среди кода страницы."""
    import re

    исходник = (КОРЕНЬ / 'checklists' / 'forms_check.py').read_text(
        encoding='utf-8')
    голова = исходник[:исходник.index('# ── Заголовок + подсказка')]
    хвост = re.search(r'\ndef _forms_done_live\(.*?\n    return n\n',
                      исходник, re.DOTALL)
    assert хвост, 'не нашли _forms_done_live - тест устарел'

    m = types.ModuleType('fc_head')
    m.__dict__['__file__'] = str(КОРЕНЬ / 'checklists' / 'forms_check.py')
    exec(compile(голова + хвост.group(0), 'forms_check.py', 'exec'),
         m.__dict__)
    return m


def _лог(xlsx: Path, названия):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Логи'
    ws.append(['Дата', 'Город', 'Название', 'Статус'])
    for н in названия:
        ws.append(['13.08.2026', 'Москва', н, 'Успешно'])
    wb.save(xlsx)


def test_служебные_строки_не_считаются_формами(модуль, tmp_path):
    xlsx = tmp_path / 'log_forms.xlsx'
    _лог(xlsx, [
        'Заказать звонок', 'Обратная связь',          # формы
        'нет горизонтального скролла',                 # мобильная вёрстка
        'тач-размер кнопок/полей', 'без поломок',
        'Cookie-уведомление новым пользователям (2.12)',
        'Ссылка на политику в cookie-уведомлении',
        'Проба загрузки файла (безопасность): Экспресс заявка',
    ])

    assert модуль._rows_done(xlsx) == 2
    print('✓ в запасном счётчике остаются только формы')


def test_живой_лог_главнее_excel(модуль):
    """Строк «▶ Форма» ровно столько, сколько форм - им и верим."""
    лог = '▶ Форма 1: Заказать звонок\n▶ Форма 2: Обратная связь\n'

    assert модуль._forms_done_live(лог) == 2
    print('✓ живой лог считает формы точно')


def test_счётчик_не_перерастает_ожидание(модуль, tmp_path):
    """Сценарий МПЭ: 6 форм на город, лог знает 6, а в Excel строк больше."""
    xlsx = tmp_path / 'log_forms.xlsx'
    _лог(xlsx, [f'Форма {i}' for i in range(6)]
         + ['нет горизонтального скролла'] * 9)
    лог = ''.join(f'▶ Форма {i}: Форма {i}\n' for i in range(1, 7))

    живой = модуль._forms_done_live(лог)
    запасной = модуль._rows_done(xlsx) if not живой else 0

    assert живой == 6 and запасной == 0
    print('✓ пока лог жив, строки Excel в счёт не идут')


def test_секундомер_переживает_потерю_сессии(tmp_path):
    """Время старта читается из pid-файла, а не из session_state."""
    sys.path.insert(0, str(КОРЕНЬ / 'checklists'))
    from checklists import ui_widgets

    pid = tmp_path / 'run.pid'
    pid.write_text('12345', encoding='utf-8')

    старт = ui_widgets.run_started_at(pid)

    assert старт and abs(старт - pid.stat().st_mtime) < 0.01
    assert ui_widgets.run_started_at(tmp_path / 'нет.pid') is None
    print('✓ старт берётся из pid-файла, сессия не нужна')
