# -*- coding: utf-8 -*-
"""Адаптивные элементы, размер шрифта и доступность.

Замеры (контраст, тач-таргеты, мелкий шрифт, битые картинки) браузер снимал и
раньше, но в отчёт попадали только overflow и наложения - остальное терялось.
Здесь проверяем именно вывод + новые проверки (заголовки, меню по ширинам,
навигация с клавиатуры).
"""
from report_priorities import classify, _console_findings


def _страница(**mob):
    """Одна страница с замерами на трёх ширинах (значения по умолчанию - норма)."""
    def _vp(**kw):
        v = {'overflow': 0, 'overlaps': [], 'total': 100, 'small': 0,
             'small_examples': [], 'head_total': 5, 'head_small': 0,
             'head_examples': [], 'touch_total': 20, 'touch_small': 0,
             'touch_examples': [], 'menu_visible': True, 'menu_burger': False}
        v.update(kw)
        return v
    vps = {'1440': _vp(**mob.pop('w1440', {})),
           '768': _vp(**mob.pop('w768', {})),
           '390': _vp(**mob.pop('w390', {}))}
    страница = {'url': 'https://a.ru/x/', 'errors': [],
                'mobile': {'viewports': vps}, 'a11y': mob.pop('a11y', {})}
    return {'pages': [страница]}


def _тексты(check):
    return ' | '.join(f.problem for f in _console_findings(check))


def test_норма_молчит():
    assert _console_findings(_страница()) == []


# ── Шрифт ────────────────────────────────────────────────────────────


def test_мелкий_текст_только_когда_его_много():
    """Сноска или копирайт мелким шрифтом - не находка."""
    мало = _страница(w390={'small': 2, 'total': 100})
    assert 'мелкий' not in _тексты(мало)
    доля = _страница(w390={'small': 4, 'total': 100})     # 4% - мало
    assert 'мелкий' not in _тексты(доля)
    много = _страница(w390={'small': 30, 'total': 100,
                            'small_examples': ['Условия - 11px']})
    находки = [f for f in _console_findings(много) if 'мелкий' in f.problem]
    assert находки and 'на 390px текст слишком мелкий' in находки[0].problem
    # примеры из замера должны доехать до отчёта (ключ small_examples)
    assert 'Условия - 11px' in находки[0].detail


def test_мелкие_заголовки():
    check = _страница(w1440={'head_small': 3, 'head_total': 5,
                             'head_examples': ['h2 «Каталог» - 16px (нужно от 22)']})
    f = [x for x in _console_findings(check) if 'заголовки' in x.problem][0]
    assert f.level == 'Предупреждение'
    assert '1440px' in f.problem and 'нужно от 22' in f.detail
    assert classify(f)['task_group'] == 'font_small_head'


# ── Меню и тач-таргеты по ширинам ────────────────────────────────────


def test_меню_недоступно_на_ширине():
    """Типовая поломка планшета: меню спрятали, бургер не показали."""
    check = _страница(w768={'menu_visible': False, 'menu_burger': False})
    f = [x for x in _console_findings(check) if 'меню недоступно' in x.problem][0]
    assert f.level == 'Ошибка' and '768px' in f.problem
    assert classify(f)['task_group'] == 'adaptive_menu'


def test_бургер_вместо_меню_это_норма():
    check = _страница(w390={'menu_visible': False, 'menu_burger': True})
    assert 'меню недоступно' not in _тексты(check)


def test_мелкие_кнопки_под_палец():
    check = _страница(w390={'touch_small': 8, 'touch_total': 20,
                            'touch_examples': ['Купить (30x28)']})
    f = [x for x in _console_findings(check) if '44x44' in x.problem][0]
    assert classify(f)['task_group'] == 'touch_targets'
    # единичная мелкая кнопка - не находка
    assert '44x44' not in _тексты(_страница(w390={'touch_small': 2,
                                                  'touch_total': 20}))


# ── Доступность ──────────────────────────────────────────────────────


def test_низкий_контраст_выводится():
    check = _страница(a11y={'contrast_total': 120, 'contrast_low': 14,
                            'contrast_ex': ['Подробнее (2.1:1)']})
    f = [x for x in _console_findings(check) if 'контраст' in x.problem][0]
    assert f.level == 'Предупреждение'
    assert '14 из 120' in f.detail
    assert classify(f)['task_group'] == 'a11y_contrast'


def test_битые_и_растянутые_картинки():
    check = _страница(a11y={'img_broken': ['truba.jpg'],
                            'img_distorted': ['list.png']})
    находки = _console_findings(check)
    битая = [f for f in находки if 'не загрузилась' in f.problem][0]
    растянутая = [f for f in находки if 'растянута' in f.problem][0]
    assert битая.level == 'Ошибка' and битая.section == 'Изображения'
    assert растянутая.level == 'Предупреждение'
    assert classify(битая)['task_group'] == 'img_broken'
    assert classify(растянутая)['task_group'] == 'img_distorted'


def test_фокус_не_виден():
    check = _страница(a11y={'keyboard': {
        'steps': 10, 'interactive': 9, 'no_focus_style': 10,
        'examples': ['a «Каталог»']}})
    f = [x for x in _console_findings(check) if 'клавиатуры' in x.problem][0]
    assert f.level == 'Предупреждение'
    assert 'фокус не виден' in f.problem
    assert classify(f)['task_group'] == 'a11y_keyboard'


def test_tab_не_доходит_до_интерактива():
    check = _страница(a11y={'keyboard': {
        'steps': 6, 'interactive': 0, 'no_focus_style': 0, 'examples': []}})
    assert 'Tab не доходит' in _тексты(check)


def test_фокус_виден_хотя_бы_частично_молчим():
    """Часть элементов без стиля фокуса - это ещё не поломка навигации."""
    check = _страница(a11y={'keyboard': {
        'steps': 10, 'interactive': 9, 'no_focus_style': 4, 'examples': []}})
    assert 'клавиатуры' not in _тексты(check)


def test_клавиатура_не_проверялась_молчим():
    """Проба идёт не на всех страницах - отсутствие данных не находка."""
    assert _console_findings(_страница(a11y={'keyboard': None})) == []
    assert _console_findings(_страница(a11y={})) == []
