# -*- coding: utf-8 -*-
"""Невидимая капча - не дефект формы.

На met-trans.am стоит SmartCaptcha: автотест она молча не пропускает - форма
заполняется, поля очищаются, Метрика фиксирует отправку, но запрос к API сайта
не уходит и подтверждения нет. Руками формы работают (заказчик подтвердил
19.08.2026), поэтому вердикт «форма не показала успех» был бы ложным обвинением
сайта: показываем «НЕ ПРОВЕРИТЬ АВТОТЕСТОМ (капча)» и отправляем на ручную
проверку.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'forms_tester'))

import test_all as T


def test_видим_капчу_по_виджету():
    for html in (
        '<script src="https://smartcaptcha.yandexcloud.net/captcha.js"></script>',
        '<div class="smart-captcha" data-sitekey="ysc1_abc"></div>',
        '<script src="https://www.google.com/recaptcha/api.js"></script>',
        '<div class="g-recaptcha" data-sitekey="6Lc"></div>',
        '<script src="https://hcaptcha.com/1/api.js"></script>',
        '<div id="cf"><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></div>',
    ):
        assert T.разметка_под_капчей(html), html[:40]


def test_обычная_страница_не_считается_капчей():
    """Слово «captcha» в аналитике или в тексте не должно обвинять капчу."""
    for html in (
        '<html><body><form><input name="phone"></form></body></html>',
        '<script>var stat = {captchaShown: false};</script>',
        '<p>Мы не используем капчу на этой форме</p>',
        '',
    ):
        assert not T.разметка_под_капчей(html), html[:40]


def test_статус_капчи_попадает_в_матрицу():
    """Без регистрации статуса строка не становилась столбцом - форма исчезала
    из матрицы целиком (на met-trans.am это все шесть форм)."""
    assert any('не проверить автотестом' in s for s in T._МАТРИЦА_SUBMIT_ST)


def test_капча_это_предупреждение_а_не_крест():
    """И правило должно стоять ВЫШЕ общего «ошибк»/«нет подтверждения»."""
    правила = T._МАТРИЦА_ПРАВИЛА['Статус']
    паттерны = [p for p, _s, _c in правила]
    символы = {p: s for p, s, _c in правила}
    assert 'не проверить автотестом' in паттерны
    assert символы['не проверить автотестом'] == '⚠'
    assert (паттерны.index('не проверить автотестом')
            < паттерны.index('нет подтверждения'))
    assert (паттерны.index('не проверить автотестом')
            < паттерны.index('ошибк'))


def test_конфиг_армении_помнит_про_капчу():
    """Чтобы через месяц не начать чинить «неработающие формы» заново."""
    from pathlib import Path
    p = (Path(__file__).parent.parent / 'forms_tester' / 'projects'
         / 'mtt_am' / 'config.py')
    текст = p.read_text(encoding='utf-8')
    assert 'SmartCaptcha' in текст
    assert 'вручную формы' in текст.lower() or 'вручную' in текст
