# -*- coding: utf-8 -*-
"""Секундомер прогона: «идёт 4:32» и «заняло 12:07».

Счётчик был только на «Проверке форм», и то лишь для своей сессии - после
перезагрузки страницы он показывал «…». Время старта берём из pid-файла:
прогон живёт отдельным процессом и виден из любой сессии."""
import time
from pathlib import Path

import pytest

from checklists import ui_widgets as ui


@pytest.fixture
def страница(monkeypatch):
    """Ловим то, что страница выводит через st.caption."""
    выведено = []
    monkeypatch.setattr(ui.st, 'caption', lambda t, **kw: выведено.append(t))
    return выведено


def _файлы(tmp_path, возраст_сек=0.0):
    pid = tmp_path / 'run.pid'
    log = tmp_path / 'run.log'
    pid.write_text('123')
    log.write_text('строка лога')
    if возраст_сек:
        т = time.time() - возраст_сек
        import os
        os.utime(pid, (т, т))
    return pid, log


def test_идущий_прогон_показывает_время(tmp_path, страница):
    pid, log = _файлы(tmp_path, возраст_сек=272)      # 4:32
    ui.elapsed_caption(pid, log, running=True)
    assert страница and 'идёт' in страница[0]
    assert '4:3' in страница[0]                       # 4:32 (±секунда)


def test_остаток_по_прогнозу(tmp_path, страница):
    pid, log = _файлы(tmp_path, возраст_сек=60)
    ui.elapsed_caption(pid, log, running=True,
                       estimate_low=300, estimate_high=600)
    assert 'осталось' in страница[0]


def test_вышли_за_прогноз(tmp_path, страница):
    pid, log = _файлы(tmp_path, возраст_сек=900)
    ui.elapsed_caption(pid, log, running=True,
                       estimate_low=300, estimate_high=600)
    assert 'дольше прогноза' in страница[0]


def test_завершённый_прогон_показывает_итог(tmp_path, страница):
    import os
    pid, log = _файлы(tmp_path)
    старт = time.time() - 727                          # 12:07
    os.utime(pid, (старт, старт))
    ui.elapsed_caption(pid, log, running=False)
    assert страница and 'заняло' in страница[0]
    assert '12:0' in страница[0]


def test_сверка_с_прогнозом_после_завершения(tmp_path, страница):
    import os
    pid, log = _файлы(tmp_path)
    старт = time.time() - 400
    os.utime(pid, (старт, старт))
    ui.elapsed_caption(pid, log, running=False,
                       estimate_low=300, estimate_high=600)
    assert 'уложился в прогноз' in страница[0]


def test_без_pid_файла_молчим(tmp_path, страница):
    """Прогон не запускался - показывать нечего."""
    ui.elapsed_caption(tmp_path / 'нет.pid', tmp_path / 'нет.log', running=True)
    assert страница == []


def test_старт_виден_после_перезагрузки_страницы(tmp_path):
    """Главное отличие от прежней схемы: не зависим от session_state."""
    pid, log = _файлы(tmp_path, возраст_сек=100)
    assert ui.run_started_at(pid, log) is not None


def test_формат_времени():
    assert ui._мм_сс(0) == '0:00'
    assert ui._мм_сс(59) == '0:59'
    assert ui._мм_сс(60) == '1:00'
    assert ui._мм_сс(727) == '12:07'
    assert ui._мм_сс(-5) == '0:00'          # отрицательное не показываем


def test_счётчик_есть_на_нужных_страницах():
    """Чек-лист, цели и КП - да; автокликеры и скорость страниц - нет."""
    корень = Path(__file__).resolve().parent.parent / 'checklists'
    for файл in ('checklist_30min.py', 'goals_check.py', 'variables_check.py'):
        текст = (корень / файл).read_text(encoding='utf-8')
        assert 'elapsed_caption' in текст or 'run_started_at' in текст, файл
