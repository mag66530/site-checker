# -*- coding: utf-8 -*-
"""Ящики проекта настраиваются в личном кабинете, а не только в секретах.

Раньше почты для раздела «Уведомления» жили ТОЛЬКО в секретах приложения
(metrika_<pid>_email, gsc_<pid>_email): руководитель без доступа к секретам не
мог подключить новый проект, а сам проект требовал правки кода."""
import sys
import types
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
if str(КОРЕНЬ) not in sys.path:
    sys.path.insert(0, str(КОРЕНЬ))


@pytest.fixture
def мод(monkeypatch):
    """checklist_30min без Streamlit-страницы: берём только его функции.

    Файл страницы при импорте рисует UI, поэтому грузим его как обычный
    модуль нельзя - вытаскиваем нужные функции из исходника."""
    import auth
    import webmaster_notify as W

    src = (КОРЕНЬ / 'checklists' / 'checklist_30min.py').read_text(encoding='utf-8')
    начало = src.index('def _secret(key)')
    конец = src.index('def _gdrive_refresh')
    m = types.ModuleType('c30_creds')
    m.__dict__.update({
        'st': types.SimpleNamespace(secrets={}),
        'MAILBOX_CONFIG': __import__('metrika_404').MAILBOX_CONFIG,
        'GSC_GMAIL_CONFIG': W.GSC_GMAIL_CONFIG,
        'YABUSINESS_YANDEX_CONFIG': W.YABUSINESS_YANDEX_CONFIG,
        'TWOGIS_YANDEX_CONFIG': W.TWOGIS_YANDEX_CONFIG,
        'GOOGLE_ACCOUNTS_CONFIG': W.GOOGLE_ACCOUNTS_CONFIG,
        'GOOGLE_FOLDER_YANDEX_CONFIG': W.GOOGLE_FOLDER_YANDEX_CONFIG,
        'DEFAULT_FOLDERS': W.DEFAULT_FOLDERS,
        'auth': auth,
    })
    exec(compile(src[начало:конец], 'c30_creds', 'exec'), m.__dict__)
    return m


def _настройки(monkeypatch, значения: dict):
    """Подменяем личный кабинет: project_setting отдаёт заданные поля."""
    import auth
    monkeypatch.setattr(auth, 'project_setting',
                        lambda pid, key: значения.get((pid, key)), raising=False)


def test_почта_из_кабинета_важнее_секретов(мод, monkeypatch):
    мод.st.secrets = {'metrika_smu_email': 'старая@ya.ru',
                      'metrika_smu_password': 'старый'}
    _настройки(monkeypatch, {('smu', 'mail_yandex_login'): 'новая@ya.ru',
                             ('smu', 'mail_yandex_password'): 'новый'})
    assert мод.get_metrika_credentials('smu') == ('новая@ya.ru', 'новый')


def test_без_настроек_работают_секреты(мод, monkeypatch):
    """Уже настроенные проекты не должны сломаться."""
    мод.st.secrets = {'metrika_smu_email': 'старая@ya.ru',
                      'metrika_smu_password': 'старый'}
    _настройки(monkeypatch, {})
    assert мод.get_metrika_credentials('smu') == ('старая@ya.ru', 'старый')


def test_новый_проект_без_правки_кода(мод, monkeypatch):
    """Проекта нет ни в одном словаре конфигов - хватает кабинета."""
    мод.st.secrets = {}
    _настройки(monkeypatch, {('новый', 'mail_yandex_login'): 'p@ya.ru',
                             ('новый', 'mail_yandex_password'): 'пароль',
                             ('новый', 'mail_google_login'): 'p@gmail.com',
                             ('новый', 'mail_google_password'): 'пароль2'})
    assert мод.get_metrika_credentials('новый') == ('p@ya.ru', 'пароль')
    assert мод.get_gsc_credentials('новый') == ('p@gmail.com', 'пароль2')
    # папки берутся по умолчанию - у всех проектов они называются одинаково
    assert мод.get_yabusiness_credentials('новый') == ('p@ya.ru', 'пароль', 'Я.Бизнес')
    assert мод.get_twogis_credentials('новый') == ('p@ya.ru', 'пароль', '2ГИС')
    assert мод.get_google_folder_credentials('новый') == ('p@ya.ru', 'пароль', 'Гугл')


def test_gmail_отдельно_от_яндекса(мод, monkeypatch):
    """Письма GSC читаются из Gmail, остальное - из Яндекс-почты."""
    мод.st.secrets = {}
    _настройки(monkeypatch, {('sm', 'mail_yandex_login'): 'y@ya.ru',
                             ('sm', 'mail_yandex_password'): 'п1',
                             ('sm', 'mail_google_login'): 'g@gmail.com',
                             ('sm', 'mail_google_password'): 'п2'})
    assert мод.get_gsc_credentials('sm') == ('g@gmail.com', 'п2')
    assert мод.get_google_accounts_credentials('sm') == ('g@gmail.com', 'п2')
    assert мод.get_yabusiness_credentials('sm')[0] == 'y@ya.ru'


def test_ничего_не_задано_пусто(мод, monkeypatch):
    мод.st.secrets = {}
    _настройки(monkeypatch, {})
    assert мод.get_metrika_credentials('никакой') == (None, None)
    assert мод.get_gsc_credentials('никакой') == (None, None)


def test_поля_есть_в_настройках_проекта():
    from auth.ui import PROJECT_SETTING_FIELDS
    ключи = {k for k, _label, _type in PROJECT_SETTING_FIELDS}
    for поле in ('mail_yandex_login', 'mail_yandex_password',
                 'mail_google_login', 'mail_google_password'):
        assert поле in ключи, f'нет поля {поле} в настройках проекта'
    # пароли не должны показываться открытым текстом
    типы = {k: t for k, _l, t in PROJECT_SETTING_FIELDS}
    assert типы['mail_yandex_password'] == 'password'
    assert типы['mail_google_password'] == 'password'
