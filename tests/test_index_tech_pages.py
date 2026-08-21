"""Тесты проверки «служебных страниц в индексе» (index_tech_pages.py).

Проверяем чистую логику классификации адреса, сбор кандидатов источниками
(выгрузка Вебмастера), слияние источников и живую перепроверку - всё без
сети (запросы замоканы).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_export_parser as ep
import index_reverify as rv
import index_tech_pages as m


# ── classify_tech_url ────────────────────────────────────────────────

def test_служебные_адреса_ловятся():
    случаи = {
        'https://site.ru/basket/': 'корзина',
        'https://site.ru/cart/': 'корзина',
        'https://site.ru/personal/orders/': 'личный кабинет',
        'https://site.ru/search/?q=труба': 'поиск по сайту',
        'https://site.ru/compare/': 'сравнение товаров',
        'https://site.ru/auth/?login=yes': 'авторизация',
        'https://site.ru/bitrix/admin/index.php': 'админ. панель',
        'https://site.ru/ajax/getprice/': 'AJAX-обработчик',
        'https://site.ru/local/templates/main/': 'служебный каталог /local/',
        'https://site.ru/auth.php': 'авторизация',
    }
    for url, метка in случаи.items():
        hit = m.classify_tech_url(url)
        assert hit and hit['label'] == метка, url
    print('✓ корзина/поиск/ЛК/админка/обработчики распознаются')


def test_обычные_страницы_не_трогаем():
    """Сравнение по СЕГМЕНТУ пути, а не подстрокой: иначе товар с «cart» в
    слаге и категория «personalnye-podarki» уехали бы в находки."""
    чистые = [
        'https://site.ru/catalog/cartridge-hp-05a/',
        'https://site.ru/catalog/personalnye-podarki/',
        'https://site.ru/company/vacancy/',
        'https://site.ru/',
        'https://site.ru/catalog/truba/filter/gost-is-3262/apply/',
        # параметрические дубли - ДРУГАЯ проверка (robots), не эта
        'https://site.ru/catalog/?sort=price',
        'https://site.ru/catalog/?utm_source=ya',
    ]
    for url in чистые:
        assert m.classify_tech_url(url) is None, url
    print('✓ каталог, фильтры и параметрические адреса не считаются служебными')


def test_оформление_заказа_ловим_по_всем_слагам_проектов():
    """Слаги собраны по живым сайтам: СМУ /order → /basket/, ИМП /checkout/ →
    /cart/, МПК /onepagecheckout/, МПЭ /personal/order/."""
    for url in ('https://site.ru/checkout/cart',
                'https://site.ru/onepagecheckout/',
                'https://site.ru/personal/order/',
                'https://site.ru/oformlenie/'):
        hit = m.classify_tech_url(url)
        assert hit, url
    print('✓ все реальные адреса оформления заказа распознаются')


def test_order_метка_мягкая_а_остальные_жёсткие():
    """/order/ бывает и информационной «Как заказать» - такую метку
    подтверждает живая страница, а /checkout/ и /basket/ однозначны."""
    assert m.classify_tech_url('https://site.ru/order/')['soft'] is True
    assert m.classify_tech_url('https://site.ru/checkout/')['soft'] is False
    assert m.classify_tech_url('https://site.ru/basket/')['soft'] is False
    print('✓ мягкая метка только у /order/')


def test_разметка_оформления_отличается_от_рассказа_о_заказе():
    чекаут = '<form id="bx-soa-order"><input name="ORDER_PROP_1"></form>'
    инфо = ('<h1>Как оформить заказ</h1><p>Чтобы оформить заказ, позвоните '
            'нам или напишите на почту.</p>')

    assert m.looks_like_checkout(чекаут)
    assert not m.looks_like_checkout(инфо)
    print('✓ судим по разметке, а не по фразе «оформить заказ»')


def test_мягкая_метка_без_чекаута_не_находка(monkeypatch):
    async def _fake(urls, proxy):
        return {
            'https://site.ru/order/': ('finding', 200, False),   # «Как заказать»
            'https://site.ru/basket/': ('finding', 200, False),  # корзина, метка жёсткая
        }
    monkeypatch.setattr(m, '_check_all', _fake)

    check = {'available': True, 'hosts': [
        {'host': 'site.ru', 'dead': [], 'soft': [], 'errors': [],
         'tech': [{'url': 'https://site.ru/order/', 'label': 'оформление заказа',
                   'source': 'Яндекс', 'soft': True},
                  {'url': 'https://site.ru/basket/', 'label': 'корзина',
                   'source': 'Яндекс', 'soft': False}],
         'checked': 5, 'ok': 5, 'redirects': 0, 'in_index_total': 5}]}

    res = m.reverify_tech_pages(check)

    assert [e['url'] for e in res['hosts'][0]['tech']] == \
        ['https://site.ru/basket/']
    print('✓ информационная «Как заказать» в находки не идёт, корзина идёт')


def test_свои_тех_страницы_проекта_не_обвиняем():
    """«Поиск по товару» /search/ у ИМП - обычная страница проекта, она
    заведена в его списке тех. страниц и в индексе законна."""
    assert m.classify_tech_url('https://inmetprom.ru/search/') is not None
    assert m.classify_tech_url('https://inmetprom.ru/search/', 'imp') is None
    # У чужого проекта тот же адрес по-прежнему служебный.
    assert m.classify_tech_url('https://site.ru/search/', 'smu') is not None
    print('✓ страницы из списка проекта служебными не считаются')


def test_потолок_на_хост():
    hb = {}
    for i in range(m.MAX_TECH_PER_HOST + 10):
        m.add_tech(hb, f'https://site.ru/basket/?id={i}', 'Яндекс')
    assert len(hb['tech']) == m.MAX_TECH_PER_HOST
    print('✓ отчёт не превращается в простыню: потолок кандидатов на хост')


# ── сбор кандидатов из выгрузки Вебмастера ───────────────────────────

def _csv(rows) -> bytes:
    head = 'updateDate,url,httpCode,status,target,lastAccess,title,event\n'
    body = ''.join(f',{u},{code},{st},,,,\n' for u, code, st in rows)
    return (head + body).encode('utf-8')


def test_из_выгрузки_берём_только_страницы_в_поиске():
    """SEARCHABLE = адрес в выдаче. LOW_DEMAND/UNKNOWN_URL - робот знает
    адрес, но в поиске его нет, предъявлять нечего."""
    data = _csv([
        ('https://site.ru/basket/', 200, 'SEARCHABLE'),
        ('https://site.ru/compare/', 200, 'LOW_DEMAND'),
        ('https://site.ru/catalog/truba/', 200, 'SEARCHABLE'),
        ('https://site.ru/personal/', 404, 'HTTP_ERROR'),
    ])
    res = ep.analyze_exports([('searchable.csv', data)])

    host = res['hosts'][0]
    assert [e['url'] for e in host['tech']] == ['https://site.ru/basket/']
    assert res['total_tech'] == 1
    print('✓ в кандидаты идут только служебные адреса со статусом SEARCHABLE')


def test_слияние_источников_дедупит_адреса():
    """Один и тот же /basket/ приходит и от Яндекса, и от Google - в отчёте
    он должен быть одной строкой."""
    я = {'available': True, 'source': 'yandex_export', 'hosts': [
        {'host': 'site.ru', 'dead': [], 'soft': [], 'errors': [],
         'tech': [{'url': 'https://site.ru/basket/', 'source': 'Яндекс',
                   'label': 'корзина'}],
         'checked': 5, 'ok': 4, 'redirects': 0, 'in_index_total': 5}]}
    g = {'available': True, 'source': 'gsc', 'hosts': [
        {'host': 'site.ru', 'dead': [], 'soft': [], 'errors': [],
         'tech': [{'url': 'https://site.ru/basket/', 'source': 'Google (API)',
                   'label': 'корзина'},
                  {'url': 'https://site.ru/search/', 'source': 'Google (API)',
                   'label': 'поиск по сайту'}],
         'checked': 3, 'ok': 3, 'redirects': 0, 'in_index_total': 3}]}

    res = ep.merge_index_404(я, g)

    tech = res['hosts'][0]['tech']
    assert [e['url'] for e in tech] == ['https://site.ru/basket/',
                                        'https://site.ru/search/']
    assert res['total_tech'] == 2
    print('✓ адрес из двух источников - одна строка в отчёте')


# ── перепроверка 404 не должна терять служебные адреса ───────────────

def test_перепроверка_404_сохраняет_tech(monkeypatch):
    """У reverify_index_404 своя задача (подтвердить 404). Хост без битых, но
    со служебными адресами раньше выпадал целиком - вместе с находками."""
    async def _fake(urls, proxy):
        return {u: ('ok', 200) for u in urls}
    monkeypatch.setattr(rv, '_check_all', _fake)

    check = {'available': True, 'hosts': [
        {'host': 'site.ru',
         'dead': [{'url': 'https://site.ru/old/', 'source': 'Яндекс'}],
         'soft': [], 'errors': [],
         'tech': [{'url': 'https://site.ru/basket/', 'source': 'Яндекс',
                   'label': 'корзина'}],
         'checked': 9, 'ok': 8, 'redirects': 0, 'in_index_total': 9}]}

    res = rv.reverify_index_404(check)

    assert res['total_dead'] == 0          # 404 не подтвердился - убран
    assert res['hosts'], 'хост выкинули вместе со служебными адресами'
    assert [e['url'] for e in res['hosts'][0]['tech']] == \
        ['https://site.ru/basket/']
    print('✓ хост со служебными адресами переживает перепроверку 404')


# ── живая перепроверка служебных адресов ─────────────────────────────

def test_вердикт_по_живому_ответу():
    assert m.tech_verdict(200, False) == 'finding'
    assert m.tech_verdict(200, True) == 'noindex'
    assert m.tech_verdict(301, False) == 'gone'
    assert m.tech_verdict(404, False) == 'gone'
    assert m.tech_verdict(None, False) == 'gone'
    print('✓ находка только у живой страницы 200 без noindex')


def test_перепроверка_убирает_неподтверждённые(monkeypatch):
    async def _fake(urls, proxy):
        return {
            'https://site.ru/basket/': ('finding', 200, True),   # правда открыта
            'https://site.ru/search/': ('noindex', 200, False),  # уже закрыта noindex
            'https://site.ru/auth/': ('gone', 404, False),       # страницы нет
        }
    monkeypatch.setattr(m, '_check_all', _fake)

    check = {'available': True, 'hosts': [
        {'host': 'site.ru', 'dead': [], 'soft': [], 'errors': [],
         'tech': [{'url': 'https://site.ru/basket/', 'label': 'корзина',
                   'source': 'Яндекс'},
                  {'url': 'https://site.ru/search/', 'label': 'поиск по сайту',
                   'source': 'Яндекс'},
                  {'url': 'https://site.ru/auth/', 'label': 'авторизация',
                   'source': 'Google (API)'}],
         'checked': 30, 'ok': 30, 'redirects': 0, 'in_index_total': 30}]}

    res = m.reverify_tech_pages(check)

    assert [e['url'] for e in res['hosts'][0]['tech']] == \
        ['https://site.ru/basket/']
    assert res['total_tech'] == 1
    print('✓ noindex и удалённые страницы в отчёт не идут')


# ── находки в «Проблемы» ─────────────────────────────────────────────

def test_находки_попадают_в_проблемы():
    import report_priorities as rp

    check = {'available': True, 'hosts': [
        {'host': 'site.ru', 'dead': [], 'soft': [], 'errors': [],
         'tech': [{'url': 'https://site.ru/basket/', 'label': 'корзина',
                   'source': 'Яндекс', 'status': 200}],
         'checked': 10, 'ok': 10, 'redirects': 0, 'in_index_total': 10}]}

    finds = rp.collect_findings([], index_404_check=check)
    свои = [f for f in finds if f.section == rp.SEC_INDEX_TECH]

    assert len(свои) == 1
    f = свои[0]
    assert f.level == 'Ошибка'
    assert 'корзина' in f.problem
    assert f.url == 'https://site.ru/basket/'
    # У находки должен быть приоритет и ответственный из таксономии, а не дефолт.
    правило = rp.classify(f)
    assert правило['task_group'] == 'index_tech_pages'
    assert правило['priority'] == 2
    print('✓ служебная страница в индексе - строка «Проблем» со своей задачей')
