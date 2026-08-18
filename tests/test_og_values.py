# -*- coding: utf-8 -*-
"""OpenGraph: проверка ЗНАЧЕНИЙ, а не только наличия тегов.

Раньше читалось лишь имя свойства, поэтому пустой
«<meta property="og:title" content="">» засчитывался как заполненный - на
шаблонных сайтах это типовая поломка (тег в вёрстке есть, переменная не
подставилась).
"""
import schema_checker as sc
from report_priorities import classify, _markup_findings


ПОЛНЫЙ = '''<html><head>
<meta property="og:url" content="https://a.ru/catalog/truba/">
<meta property="og:title" content="Труба профильная">
<meta property="og:description" content="Труба профильная в Москве">
<meta property="og:image" content="https://a.ru/img/truba.jpg">
<meta property="og:type" content="website">
</head><body></body></html>'''


def _markup(html, url='https://a.ru/catalog/truba/', type_code='category'):
    return sc.check_markup(html, type_code, url)


# ── Разбор ───────────────────────────────────────────────────────────


def test_значения_читаются():
    og = sc.parse_og(ПОЛНЫЙ)
    assert og['url'] == 'https://a.ru/catalog/truba/'
    assert og['title'] == 'Труба профильная'
    assert og['type'] == 'website'


def test_повторный_тег_берём_первое_непустое():
    """og:image часто дублируют для разных размеров; пустой дубль не должен
    затирать заполненный."""
    html = ('<meta property="og:image" content="">'
            '<meta property="og:image" content="https://a.ru/1.jpg">')
    assert sc.parse_og(html)['image'] == 'https://a.ru/1.jpg'
    html2 = ('<meta property="og:image" content="https://a.ru/1.jpg">'
             '<meta property="og:image" content="https://a.ru/2.jpg">')
    assert sc.parse_og(html2)['image'] == 'https://a.ru/1.jpg'


def test_тег_без_content():
    assert sc.parse_og('<meta property="og:title">') == {'title': ''}


# ── Пустые значения ──────────────────────────────────────────────────


def test_пустой_тег_это_ошибка():
    html = ПОЛНЫЙ.replace('content="Труба профильная"', 'content=""')
    r = _markup(html)
    assert any('og:title пустой' in i for i in r['issues'])
    # и это НЕ считается «тега нет» - находка одна, не две
    assert not any('нет OpenGraph-тега og:title' in i for i in r['issues'])


def test_отсутствие_тега_по_прежнему_ошибка():
    html = ПОЛНЫЙ.replace(
        '<meta property="og:type" content="website">', '')
    r = _markup(html)
    assert 'type' in r['og_missing']
    assert any('нет OpenGraph-тега og:type' in i for i in r['issues'])
    # пустым его не называем - тега нет вовсе
    assert not any('og:type пустой' in i for i in r['issues'])


def test_полный_набор_молчит():
    r = _markup(ПОЛНЫЙ)
    og_находки = [t for t in r['issues'] + r['warnings'] if 'og:' in t.lower()]
    assert og_находки == [], og_находки


# ── og:url ───────────────────────────────────────────────────────────


def test_относительный_og_url_ошибка():
    i, w = sc.check_og({'url': '/catalog/truba/'}, 'https://a.ru/catalog/truba/')
    assert any('относительный адрес' in x for x in i)


def test_og_url_на_другую_страницу_предупреждение():
    i, w = sc.check_og({'url': 'https://a.ru/other/'},
                       'https://a.ru/catalog/truba/')
    assert i == []
    assert any('ведёт не на эту страницу' in x for x in w)


def test_og_url_совпал_с_точностью_до_www_и_слеша():
    """Разный слеш на конце и www - тот же адрес, ругаться не на что."""
    i, w = sc.check_og({'url': 'https://www.a.ru/catalog/truba'},
                       'https://a.ru/catalog/truba/')
    assert i == [] and w == []


# ── og:image ─────────────────────────────────────────────────────────


def test_data_картинка_ошибка():
    i, _w = sc.check_og({'image': 'data:image/png;base64,iVBORw0KGgo='})
    assert any('data:' in x for x in i)


def test_относительная_картинка_ошибка():
    i, _w = sc.check_og({'image': '/img/truba.jpg'})
    assert any('относительный адрес' in x for x in i)


def test_протокол_относительная_картинка_годится():
    """«//host/img.jpg» - валидный абсолютный адрес."""
    i, w = sc.check_og({'image': '//a.ru/img/truba.jpg'})
    assert i == [] and w == []


# ── og:description и og:type ─────────────────────────────────────────


def test_длинное_описание_предупреждение():
    i, w = sc.check_og({'description': 'а' * (sc.OG_DESC_MAX + 1)})
    assert i == [] and any('длиннее 300' in x for x in w)
    # ровно на пределе - не находка
    _i2, w2 = sc.check_og({'description': 'а' * sc.OG_DESC_MAX})
    assert w2 == []


def test_незнакомый_тип_предупреждение():
    _i, w = sc.check_og({'type': 'страница'})
    assert any('незнакомое значение' in x for x in w)
    for ok in ('website', 'article', 'product', 'video.movie', 'WEBSITE'):
        assert sc.check_og({'type': ok})[1] == [], ok


# ── Вывод в отчёт ────────────────────────────────────────────────────


def test_находки_идут_в_разметку_со_своими_задачами():
    html = (ПОЛНЫЙ
            .replace('content="Труба профильная"', 'content=""')
            .replace('https://a.ru/img/truba.jpg', '/img/truba.jpg')
            .replace('content="website"', 'content="страница"'))
    r = _markup(html)
    находки = _markup_findings(r, city='Москва', page_type='Категория',
                               url='https://a.ru/catalog/truba/')
    группы = {classify(f)['task_group'] for f in находки}
    assert 'og_empty' in группы
    assert 'og_image' in группы
    assert 'og_type' in группы
    assert all(f.section == 'Разметка' for f in находки)


def test_старое_поведение_не_сломано():
    """Прежние ключи ответа на месте - на них смотрит отчёт."""
    r = _markup('<html><head></head><body></body></html>')
    assert set(r['og_missing']) == set(sc.OG_REQUIRED)
    assert r['og'] == {}
    assert 'micro_types' in r and 'ld_types' in r
