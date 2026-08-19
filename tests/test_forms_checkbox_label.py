# -*- coding: utf-8 -*-
"""Согласие: клик по подписи, но НЕ по уже отмеченной галочке.

Поймано на met-trans.am (React): input чекбокса лежит внутри label с opacity:0,
программное checked=true форма не видит и продолжает требовать согласие - все
шесть форм не отправлялись. Поэтому сначала кликаем по label, как человек.

Обратная сторона: клик по label ПЕРЕКЛЮЧАЕТ галочку. Часть вызовов приходит
сюда без проверки (обязательные чекбоксы), а на некоторых сайтах согласие
предустановлено - для них клик снял бы галочку и сломал отправку. Значит
уже отмеченный чекбокс не трогаем вовсе.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'forms_tester'))

import test_all as T


class _Бокс:
    """Заглушка Playwright-локатора чекбокса.

    сценарий:
      'react'  - галочка ставится ТОЛЬКО кликом по label (как на met-trans.am);
      'обычный'- работает штатный check();
      'готов'  - галочка уже отмечена.
    """

    def __init__(self, сценарий, есть_ссылка=False):
        self.сценарий = сценарий
        self.есть_ссылка = есть_ссылка
        self.checked = (сценарий == 'готов')
        self.действия = []

    def is_checked(self):
        return self.checked

    def scroll_into_view_if_needed(self):
        pass

    def check(self, **kw):
        self.действия.append('check')
        if self.сценарий == 'react':
            raise RuntimeError('element is not visible')
        self.checked = True

    def click(self, **kw):
        self.действия.append('click')
        if self.сценарий == 'react':
            raise RuntimeError('element is not visible')
        self.checked = True

    def get_attribute(self, name):
        return None

    def evaluate(self, js):
        # клик по label вокруг input
        if 'closest(' in js and 'lab.click()' in js:
            self.действия.append('label')
            if self.есть_ссылка:
                return False
            self.checked = not self.checked      # label ПЕРЕКЛЮЧАЕТ галочку
            return True
        # программная установка checked
        if 'el.checked = true' in js:
            self.действия.append('js')
            if self.сценарий == 'react':
                return None          # форма такого не видит - галочка не «дошла»
            self.checked = True
            return None
        return None


def test_реактовый_чекбокс_ставится_кликом_по_подписи():
    box = _Бокс('react')
    T._click_checkbox_via_label_or_js(box, page=None)
    assert box.checked is True
    assert box.действия[0] == 'label', box.действия


def test_уже_отмеченный_чекбокс_не_трогаем():
    """Иначе клик по label снял бы согласие и форма перестала отправляться."""
    box = _Бокс('готов')
    T._click_checkbox_via_label_or_js(box, page=None)
    assert box.checked is True
    assert box.действия == [], box.действия


def test_обычный_чекбокс_как_раньше():
    """Где штатный check() работает, поведение не меняется - лишних кликов нет."""
    box = _Бокс('обычный')
    T._click_checkbox_via_label_or_js(box, page=None)
    assert box.checked is True
    assert 'label' in box.действия or 'check' in box.действия


def test_label_со_ссылкой_на_политику_не_кликаем():
    """В подписи бывает ссылка на политику - клик по ней увёл бы со страницы."""
    box = _Бокс('обычный', есть_ссылка=True)
    T._click_checkbox_via_label_or_js(box, page=None)
    assert box.checked is True
    assert box.действия[0] == 'label'      # попытка была
    assert 'check' in box.действия         # но галочку поставил штатный путь
