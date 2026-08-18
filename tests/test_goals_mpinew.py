# -*- coding: utf-8 -*-
"""МПИ - новый прод в «Проверке целей»: каталог из Метрики + вход на закрытый сайт.

Стенд new.metpromintex.by закрыт nginx-паролем, и движок целей был ЧЕТВЁРТЫМ
независимым путём на сайт (после обхода чек-листа, глобального входа прогона и
движка форм) - без пароля он получал 401 и на страницы, и на догрузку JS, то
есть не находил ни одной привязки reachGoal.

Каталог собран из выгрузки страницы «Конверсии» Метрики (счётчик 99536626,
128 целей: 106 js + 22 автоцели).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import goals_tester as G

ROOT = Path(__file__).parent.parent
КАТАЛОГ = ROOT / 'catalogs' / 'goals-mpinew.json'


def _каталог() -> dict:
    return json.loads(КАТАЛОГ.read_text(encoding='utf-8'))


# ── Каталог целей ────────────────────────────────────────────────────


def test_каталог_на_месте_и_целый():
    c = _каталог()
    assert c['счётчик'] == 99536626
    assert c['домен'] == 'https://new.metpromintex.by'
    цели = c['цели']
    assert len(цели) == 128
    номера = [g['номер'] for g in цели]
    assert len(set(номера)) == len(номера), 'дубли номеров целей'
    assert all(g['название'] and g['условие'] for g in цели)


def test_js_цели_имеют_идентификатор():
    """js-цель без идентификатора проверить нечем - такой в каталоге быть не
    должно (иначе она молча уедет в «не сработала»)."""
    цели = _каталог()['цели']
    js = [g for g in цели if g['тип'] == 'js']
    assert len(js) == 106
    assert all(g['идентификаторы'] for g in js)
    assert all(not g['идентификаторы'] for g in цели if g['тип'] == 'auto')


def test_ym_вызов_в_поле_метрики_разобран():
    """В двух целях в поле «идентификатор содержит» лежит ВЕСЬ вызов
    ym(99536626,'reachGoal','addtocart') - кто-то вставил сниппет целиком.
    В каталог берём настоящий идентификатор, а условие оставляем как в
    Метрике: в отчёте видно, что цель заведена криво."""
    цели = {g['номер']: g for g in _каталог()['цели']}
    корзина = цели['358243946']
    assert корзина['идентификаторы'] == ['addtocart']
    assert корзина['содержит'] is True
    assert "ym(99536626,'reachGoal','addtocart')" in корзина['условие']
    assert цели['358244892']['идентификаторы'] == ['cartopen']


def test_последняя_цель_не_потеряна():
    """У последней цели в разметке Метрики свой класс (goals-list-item_last) -
    на нём ломался разбор, и её условие приклеивалось к предыдущей цели."""
    цели = {g['номер']: g for g in _каталог()['цели']}
    assert цели['595286526']['идентификаторы'] == [
        'listing-klik-na-kategoriyu-chasto-ishchut']
    assert цели['595286319']['идентификаторы'] == [
        'listing-klik-v-korzinu-populyarnye']


# ── План обхода ──────────────────────────────────────────────────────


def test_план_проекта_свой_а_не_смушный():
    """Без своей ветки в _план_страна проект уехал бы на план СМУ - чужие
    адреса (/catalog/truba-profilnaya/ и т.п.) и сплошные 404."""
    план = G._план_страна('mpinew', 'https://new.metpromintex.by')
    urls = [u for _n, u, _c in план['страницы']]
    assert 'https://new.metpromintex.by/' in urls
    assert 'https://new.metpromintex.by/cart' in urls
    assert any('/catalog/sortovoy-prokat' in u for u in urls)
    assert any('/product/691590/' in u for u in urls)
    # Корзину открываем ПОСЛЕ товара - иначе она пуста и блоков «Скачать КП»/
    # «Поделиться» на ней нет.
    названия = [n for n, _u, _c in план['страницы']]
    assert названия.index('Товар') < названия.index('Корзина')


def test_ожидаемые_цели_есть_в_каталоге():
    """«Ожидаемая» цель, которой нет в каталоге, - опечатка: она молча не
    сработает никогда и красный статус будет висеть на пустом месте."""
    план = G._план_страна('mpinew', 'https://new.metpromintex.by')
    в_каталоге = {i for g in _каталог()['цели'] for i in g['идентификаторы']}
    for gid in план['ожидаемые']:
        assert gid in в_каталоге, gid


# ── Вход на закрытый сайт ────────────────────────────────────────────


def test_вход_читается_из_окружения(monkeypatch):
    monkeypatch.setenv('SITE_BASIC_LOGIN', 'admin')
    monkeypatch.setenv('SITE_BASIC_PASSWORD', 'secret')
    assert G._basic_auth_from_env() == {'username': 'admin', 'password': 'secret'}


def test_без_логина_входа_нет(monkeypatch):
    """None, а не пустая пара: Playwright ждёт именно None, когда пароля нет."""
    monkeypatch.delenv('SITE_BASIC_LOGIN', raising=False)
    monkeypatch.delenv('SITE_BASIC_PASSWORD', raising=False)
    assert G._basic_auth_from_env() is None
    monkeypatch.setenv('SITE_BASIC_LOGIN', '   ')
    assert G._basic_auth_from_env() is None


def test_браузер_и_догрузка_js_получают_вход():
    """Оба канала движка: страницы (http_credentials контекста) и JS того же
    домена (auth= у requests). Пропустишь второй - привязки reachGoal не
    соберутся, и все js-цели станут «нет в коде»."""
    import inspect
    src = inspect.getsource(G.выполнить_прогон)
    assert 'http_credentials=_basic' in src
    assert 'auth=_ba_pair' in src


# ── Страница и прогон ────────────────────────────────────────────────


def test_проект_есть_на_странице_целей():
    src = (ROOT / 'checklists' / 'goals_check.py').read_text(encoding='utf-8')
    assert "'mpinew': 'МПИ - новый прод'" in src
    # Блок пароля рисуется и его результат уходит в окружение прогона.
    assert 'render_site_login' in src
    assert 'env.update(_site_env)' in src


def test_страна_проекта_беларусь():
    """id без суффикса страны раньше читался как «РФ / Россия» - и лист отчёта,
    и подпись в Telegram врали про страну стенда."""
    import goals_run as R
    assert R._метка('mpinew') == 'РБ'
    assert R._страна('mpinew') == 'Беларусь'
    assert R._ИМЕНА['mpinew'].startswith('МПИ')


def test_формные_цели_не_исчезают_без_прогона_форм(monkeypatch):
    """У стенда 36 js-целей на отправку форм. Формы внутри проверки целей мы
    для него не гоняем - и строка «Проверяется формами» пряталась как заглушка,
    унося из отчёта треть проекта. Для таких проектов план ставит
    «формы_не_гоняем» и строки остаются (у остальных поведение прежнее - см.
    tests/test_goals_missing_excluded.py)."""
    assert G._план_страна('mpinew', 'https://x')['формы_не_гоняем'] is True
    monkeypatch.setattr(G, '_результаты_форм', lambda pid: {})
    monkeypatch.setattr(G, '_формные_цели', lambda pid: set())
    monkeypatch.setattr(G, '_формные_url', lambda pid: set())
    k = _каталог()
    план = G._план_страна('mpinew', k['домен'])
    прогон = {'fired': set(), 'привязки': set(), 'код': '',
              'идентификаторы': set(),
              'страницы': [{'название': n, 'url': u, 'код': 200,
                            'счётчик': True, 'визит': True}
                           for n, u, _c in план['страницы']]}
    строки = G._классифицировать('mpinew', k, прогон)['строки']
    js_без_лида = [g for g in k['цели']
                   if g['тип'] == 'js' and not G._лид_цель(g)]
    assert len(строки) == len(js_без_лида) == 105
    names = {s['название'] for s in строки}
    assert 'Товар. Отправленная форма "Купить в 1 клик"' in names


def test_сквозной_заказ_для_стенда_пропускается():
    """url-целей у проекта нет - заказ подтверждать нечего, а формы стенда
    пока отвечают ошибкой."""
    import goals_run as R
    assert 'mpinew' in R.БЕЗ_ЗАКАЗА
    assert not [g for g in _каталог()['цели'] if g['тип'].startswith('url')]
