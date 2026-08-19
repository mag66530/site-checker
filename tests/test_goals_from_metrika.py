# -*- coding: utf-8 -*-
"""Каталог целей собирается из API Метрики, а не из сохранённой страницы кабинета.

Раньше цели переносили руками: человек сохранял страницу «Конверсии» и разбор
доставал их из вёрстки. Источник истины - management-API (тот же OAuth-токен из
«Настроек проекта», что у остальных обращений к Метрике), а каталог в
репозитории остаётся снапшотом на случай «нет токена/сети».
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import goals_tester as G

ROOT = Path(__file__).parent.parent


# ── Разбор одной цели ────────────────────────────────────────────────


def test_js_цель_из_действия():
    g = {'id': 31524874, 'name': 'Заказать звонок', 'type': 'action',
         'conditions': [{'type': 'exact', 'url': 'call-to-back'}]}
    assert G.цель_из_метрики(g) == {
        'номер': '31524874', 'название': 'Заказать звонок',
        'условие': 'exact: call-to-back', 'тип': 'js',
        'идентификаторы': ['call-to-back'], 'содержит': False, 'url_часть': ''}


def test_условие_содержит_переносится():
    """«содержит» меняет сравнение в прогоне (подстрока вместо равенства)."""
    g = {'id': 1, 'name': 'Формы', 'type': 'action',
         'conditions': [{'type': 'contain', 'url': 'forma'},
                        {'type': 'exact', 'url': 'zakaz'}]}
    c = G.цель_из_метрики(g)
    assert c['идентификаторы'] == ['forma', 'zakaz']
    assert c['содержит'] is True


def test_url_цель_и_регэксп():
    """regexp Метрики - это регулярка по адресу: у нас для этого тип url_re,
    иначе прогон искал бы «success» подстрокой и врал на любой странице,
    где это слово встречается в пути."""
    обычная = G.цель_из_метрики(
        {'id': 2, 'name': 'Контакты', 'type': 'url',
         'conditions': [{'type': 'contain', 'url': 'contacts'}]})
    assert обычная['тип'] == 'url' and обычная['url_часть'] == 'contacts'

    регэксп = G.цель_из_метрики(
        {'id': 3, 'name': 'Заказ оформлен', 'type': 'url',
         'conditions': [{'type': 'regexp', 'url': 'success'}]})
    assert регэксп['тип'] == 'url_re' and регэксп['url_часть'] == 'success'


def test_автоцели_метрики_помечены_auto():
    """Клики по телефону/почте/мессенджерам, отправка формы, глубина, воронка,
    электронная торговля - Метрика считает их сама на сервере, извне сигнала
    нет. Такие цели прогон не выводит - значит и тип у них 'auto'."""
    for t in ('phone', 'email', 'messenger', 'form', 'file', 'search',
              'contact_data', 'contact_data_sent', 'number', 'step',
              'visit_duration', 'a_purchase', 'a_create_order'):
        c = G.цель_из_метрики({'id': 9, 'name': t, 'type': t, 'conditions': []})
        assert c['тип'] == 'auto', t


def test_цель_без_условия_не_становится_проверяемой():
    """action без идентификатора и url без адреса проверять нечем - иначе
    строка уехала бы в «не сработала» на пустом месте."""
    assert G.цель_из_метрики(
        {'id': 4, 'name': 'x', 'type': 'action', 'conditions': []})['тип'] == 'auto'
    assert G.цель_из_метрики(
        {'id': 5, 'name': 'y', 'type': 'url',
         'conditions': [{'type': 'contain', 'url': ''}]})['тип'] == 'auto'


# ── Обновление каталога ──────────────────────────────────────────────


def test_обновление_не_ломает_каталог_без_токена(tmp_path, monkeypatch):
    """Нет токена - каталог НЕ трогаем: прогон должен пойти по снапшоту, а не
    остаться без целей вовсе."""
    ok, msg = G.каталог_из_метрики('mpk', '', counter=1)
    assert ok is False and 'токен' in msg.lower()


def test_обновление_переживает_недоступный_api(monkeypatch):
    import metrika_api
    monkeypatch.setattr(metrika_api, 'counter_goals',
                        lambda *a, **k: None)          # сеть/права не дали ответ
    ok, msg = G.каталог_из_метрики('mpk', 'токен', counter=1)
    assert ok is False and 'API' in msg


def test_ручные_пометки_переживают_обновление(monkeypatch, tmp_path):
    """Тип «jivo» (СМУ/АПС) и флаг «форма» (SHOPMET) из API не выводятся, а
    теряются - меняют вердикт: чат стал бы обычной js-целью и краснел бы каждый
    прогон, а цель-конструктор Метрики требовала бы reachGoal, которого нет."""
    import metrika_api
    каталоги = tmp_path / 'catalogs'
    каталоги.mkdir()
    monkeypatch.setattr(G, 'CATALOGS', каталоги)
    monkeypatch.setattr(G, '_счётчик_про_этот_сайт', lambda *a, **k: None)
    (каталоги / 'goals-test.json').write_text(json.dumps({
        'проект': 'Тест', 'счётчик': 1, 'домен': 'https://x.ru', 'цели': [
            {'номер': '10', 'название': 'Чат', 'условие': '', 'тип': 'jivo',
             'идентификаторы': ['jivo_chat'], 'содержит': False, 'url_часть': ''},
            {'номер': '20', 'название': 'Отправка формы', 'условие': 'форма',
             'тип': 'auto', 'идентификаторы': [], 'содержит': False,
             'url_часть': '', 'форма': True}]}, ensure_ascii=False),
        encoding='utf-8')
    monkeypatch.setattr(metrika_api, 'counter_goals', lambda *a, **k: [
        {'id': 10, 'name': 'Чат', 'type': 'action',
         'conditions': [{'type': 'exact', 'url': 'jivo_chat'}]},
        {'id': 20, 'name': 'Отправка формы', 'type': 'form', 'conditions': []},
    ])

    ok, msg = G.каталог_из_метрики('test', 'токен')
    assert ok, msg
    цели = {g['номер']: g for g in json.loads(
        (каталоги / 'goals-test.json').read_text(encoding='utf-8'))['цели']}
    assert цели['10']['тип'] == 'jivo'      # не стал обычной js-целью
    assert цели['20'].get('форма') is True
    assert 'ручных пометок' in msg


def test_чужой_счётчик_не_подменяет_каталог(monkeypatch, tmp_path):
    """Номер счётчика лежит в снимке и мог устареть. Если по нему отвечает другой
    сайт - каталог не трогаем, иначе цели проекта молча заменятся чужими."""
    import metrika_api
    каталоги = tmp_path / 'catalogs'
    каталоги.mkdir()
    monkeypatch.setattr(G, 'CATALOGS', каталоги)
    (каталоги / 'goals-test.json').write_text(json.dumps({
        'проект': 'Тест', 'счётчик': 1, 'домен': 'https://x.ru',
        'цели': [{'номер': '1', 'название': 'Своя', 'условие': '', 'тип': 'js',
                  'идентификаторы': ['a'], 'содержит': False, 'url_часть': ''}]},
        ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(G, '_счётчик_про_этот_сайт', lambda *a, **k: False)
    monkeypatch.setattr(metrika_api, 'counter_goals',
                        lambda *a, **k: [{'id': 99, 'name': 'Чужая',
                                          'type': 'action', 'conditions': []}])

    ok, msg = G.каталог_из_метрики('test', 'токен')
    assert ok is False and 'другому сайту' in msg
    цели = json.loads((каталоги / 'goals-test.json').read_text(
        encoding='utf-8'))['цели']
    assert [g['название'] for g in цели] == ['Своя']


def test_исключённые_цели_переживают_обновление(monkeypatch, tmp_path):
    """Решение «эту цель не проверяем» принято человеком (у МПИ так убраны две
    старые цели) и не должно возвращаться при каждом обновлении из API."""
    import metrika_api
    каталоги = tmp_path / 'catalogs'
    каталоги.mkdir()
    monkeypatch.setattr(G, 'CATALOGS', каталоги)
    (каталоги / 'goals-test.json').write_text(json.dumps({
        'проект': 'Тест', 'счётчик': 1, 'домен': 'https://x.ru',
        'цели': [], 'исключены': [{'номер': '777', 'название': 'старая',
                                   'почему': 'не проверяем'}]},
        ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(metrika_api, 'counter_goals', lambda *a, **k: [
        {'id': 777, 'name': 'старая', 'type': 'action',
         'conditions': [{'type': 'exact', 'url': 'old'}]},
        {'id': 888, 'name': 'живая', 'type': 'action',
         'conditions': [{'type': 'exact', 'url': 'new'}]}])

    ok, _ = G.каталог_из_метрики('test', 'токен')
    assert ok
    c = json.loads((каталоги / 'goals-test.json').read_text(encoding='utf-8'))
    assert [g['номер'] for g in c['цели']] == ['888']
    assert c['исключены'][0]['номер'] == '777'


# ── МПК на странице «Проверка целей» ─────────────────────────────────


def test_каталоги_мпк_собраны_из_api():
    for pid, счётчик in (('mpk', 21630337), ('mpk-kz', 21636463)):
        c = json.loads((ROOT / 'catalogs' / f'goals-{pid}.json')
                       .read_text(encoding='utf-8'))
        assert c['счётчик'] == счётчик
        assert 'API Метрики' in c['источник']
        assert any(g['тип'].startswith(('js', 'url')) for g in c['цели'])


def test_план_мпк_свой():
    """Без своей ветки проект уехал бы на план СМУ: /catalog/… которого у
    Magento-сайта нет вовсе."""
    план = G._план_страна('mpk', 'https://metpromko.ru')
    urls = [u for _n, u, _c in план['страницы']]
    assert 'https://metpromko.ru/' in urls
    assert any('/asbestovye-materialy' in u for u in urls)
    assert any('/prod-' in u for u in urls)
    assert any('/contacts' in u for u in urls)
    assert not [u for u in urls if '/catalog/' in u]
    # Ожидаемые цели должны существовать в каталоге - иначе красный статус
    # повиснет на опечатке.
    c = json.loads((ROOT / 'catalogs' / 'goals-mpk.json').read_text(encoding='utf-8'))
    в_каталоге = {i for g in c['цели'] for i in g['идентификаторы']}
    for gid in план['ожидаемые']:
        assert gid in в_каталоге, gid


def test_формы_мпк_ищутся_под_своим_именем():
    """id проекта в целях - «mpk», а конфиг и результаты форм лежат под
    «metpromko»: без карты цели не увидят ни формных целей, ни отчёта форм."""
    assert G._форм_проект('mpk') == 'metpromko'
    assert (ROOT / 'forms_tester' / 'projects' / 'metpromko' / 'config.py').is_file()


def test_страница_передаёт_токен_метрики():
    src = (ROOT / 'checklists' / 'goals_check.py').read_text(encoding='utf-8')
    assert "'mpk': 'МПК - Метпромко'" in src
    assert "env['METRIKA_OAUTH']" in src
    run = (ROOT / 'goals_run.py').read_text(encoding='utf-8')
    assert 'каталог_из_метрики' in run
