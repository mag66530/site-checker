# -*- coding: utf-8 -*-
"""Подпись «кто запустил» - у всех прогонов, а не только у чек-листа.

Фоновые проверки (формы, цели, КП, скорость) шлют отчёт через одну функцию
telegram_notify.send_report_from_env, поэтому подпись добавляется там - и
появляется у всех сразу.
"""
from pathlib import Path

import telegram_notify as tn
from telegram_notify import SIGN_PREFIX, with_started_by


# ── Чистая функция подписи ───────────────────────────────────────────


def test_подпись_дописывается_в_конец():
    assert with_started_by('Найдено 3 проблемы', 'Иван Петров') == \
        f'Найдено 3 проблемы\n\n{SIGN_PREFIX}Иван Петров'


def test_без_имени_текст_не_меняется():
    assert with_started_by('текст', '') == 'текст'
    assert with_started_by('текст', None) == 'текст'
    assert with_started_by('текст', '   ') == 'текст'


def test_подпись_не_двоится():
    """Чек-лист ставит подпись сам; второй раз добавлять нельзя - две подписи
    подряд читаются как ошибка отчёта."""
    один = with_started_by('текст', 'Иван Петров')
    assert with_started_by(один, 'Иван Петров') == один
    # и с другим именем тоже не добавляем: подпись в сообщении одна
    assert with_started_by(один, 'Пётр Иванов') == один


# ── Проброс через окружение ──────────────────────────────────────────


def _перехват(monkeypatch):
    """Подменяем отправку: возвращаем то, с чем её позвали."""
    поймано = {}

    def send(**kw):
        поймано.update(kw)
        return {'sent': 1, 'failed': 0}

    monkeypatch.setattr(tn, 'send_run_notification', send)
    return поймано


def test_подпись_из_окружения_доходит_до_сообщения(monkeypatch):
    поймано = _перехват(monkeypatch)
    monkeypatch.setenv('TG_BOT_TOKEN', 'токен')
    monkeypatch.setenv('TG_RECIPIENTS', '111')
    monkeypatch.setenv('TG_STARTED_BY', 'Иван Петров')
    tn.send_report_from_env('СМУ', 'Проверка форм: 2 дефекта', None)
    assert поймано['summary_text'].endswith(f'{SIGN_PREFIX}Иван Петров')


def test_без_переменной_подписи_нет(monkeypatch):
    поймано = _перехват(monkeypatch)
    monkeypatch.setenv('TG_BOT_TOKEN', 'токен')
    monkeypatch.setenv('TG_RECIPIENTS', '111')
    monkeypatch.delenv('TG_STARTED_BY', raising=False)
    tn.send_report_from_env('СМУ', 'Проверка форм: 2 дефекта', None)
    assert поймано['summary_text'] == 'Проверка форм: 2 дефекта'
    assert SIGN_PREFIX not in поймано['summary_text']


def test_подпись_не_ломает_отправку_без_кредов(monkeypatch):
    monkeypatch.delenv('TG_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TG_RECIPIENTS', raising=False)
    monkeypatch.setenv('TG_STARTED_BY', 'Иван Петров')
    assert tn.send_report_from_env('СМУ', 'текст', None)['skipped'] is True


# ── runner_env кладёт подпись в окружение фонового прогона ───────────


def test_runner_env_кладёт_подпись(monkeypatch):
    import tg_report
    monkeypatch.setattr(tg_report, '_secret',
                        lambda k: 'токен' if k == 'telegram_bot_token' else '')
    monkeypatch.setattr(tg_report, '_получатели', lambda pid: '111')
    monkeypatch.setattr(tg_report, 'drive_env', lambda pid, name='': {})
    monkeypatch.setattr(tg_report, 'кто_запустил', lambda: 'Иван Петров')
    env = tg_report.runner_env('smu', 'СМУ')
    assert env['TG_STARTED_BY'] == 'Иван Петров'
    assert env['TG_BOT_TOKEN'] == 'токен'


def test_runner_env_без_пользователя_не_кладёт_пустую_подпись(monkeypatch):
    import tg_report
    monkeypatch.setattr(tg_report, '_secret',
                        lambda k: 'токен' if k == 'telegram_bot_token' else '')
    monkeypatch.setattr(tg_report, '_получатели', lambda pid: '111')
    monkeypatch.setattr(tg_report, 'drive_env', lambda pid, name='': {})
    monkeypatch.setattr(tg_report, 'кто_запустил', lambda: '')
    assert 'TG_STARTED_BY' not in tg_report.runner_env('smu', 'СМУ')


def test_runner_env_без_telegram_подписи_нет(monkeypatch):
    """Telegram не настроен - в окружение не кладём ничего, включая подпись."""
    import tg_report
    monkeypatch.setattr(tg_report, '_secret', lambda k: '')
    monkeypatch.setattr(tg_report, '_получатели', lambda pid: '')
    monkeypatch.setattr(tg_report, 'drive_env', lambda pid, name='': {})
    monkeypatch.setattr(tg_report, 'кто_запустил', lambda: 'Иван Петров')
    assert tg_report.runner_env('smu', 'СМУ') == {}


def test_кто_запустил_склеивает_имя(monkeypatch):
    import tg_report
    import types
    фейк = types.SimpleNamespace(
        current_user=lambda: {'first_name': 'Иван', 'last_name': 'Петров',
                              'email': 'i@p.ru'})
    monkeypatch.setitem(__import__('sys').modules, 'auth', фейк)
    assert tg_report.кто_запустил() == 'Иван Петров'
    # нет имени - откатываемся на почту
    фейк.current_user = lambda: {'email': 'i@p.ru'}
    assert tg_report.кто_запустил() == 'i@p.ru'
    # пользователя нет вовсе
    фейк.current_user = lambda: None
    assert tg_report.кто_запустил() == ''
