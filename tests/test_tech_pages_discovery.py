"""Тесты автопоиска тех. страниц (tech_pages_discovery.py).

Проверяем чистую логику: какие ссылки главной идут в кандидаты, как читается
подпись, когда страница считается живой и когда годен кеш. Сети нет.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources as S
import tech_pages_discovery as m


# ── отбор кандидатов ─────────────────────────────────────────────────

def test_служебные_информационные_страницы_берём():
    for p in ('/about', '/dostavka/', '/oplata/', '/rekvizity/',
              '/politika-konfidentsialnosti/', '/company/about/'):
        assert m.is_tech_candidate(p), p
    print('✓ «О компании», «Доставка», «Оплата», политики - кандидаты')


def test_каталог_товары_и_файлы_не_берём():
    for p in ('/', '/catalog/', '/catalog/truba/', '/catalog/truba/1234-truba/',
              '/upload/price.pdf', '/tag/truba/', '/filter/gost-is-3262/'):
        assert not m.is_tech_candidate(p), p
    print('✓ каталог, товары, теги и файлы мимо')


def test_служебные_адреса_движка_не_берём():
    """Корзина/поиск/ЛК/админка - это ДРУГАЯ проверка (index_tech_pages),
    в блок тех. страниц отчёта они не идут."""
    for p in ('/basket/', '/cart/', '/search/', '/personal/', '/auth/',
              '/bitrix/admin/'):
        assert not m.is_tech_candidate(p), p
    print('✓ корзина/поиск/ЛК/админка не попадают в тех. страницы')


def test_статьи_новостей_не_берём():
    """Второй уровень разрешён только у /company/ и подобных: иначе в
    служебные страницы уехали бы новости и статьи блога."""
    assert not m.is_tech_candidate('/news/kak-my-otgruzili-trubu/')
    assert not m.is_tech_candidate('/blog/stati/vybor-truby/')
    assert m.is_tech_candidate('/news/')
    print('✓ раздел новостей - да, отдельная статья - нет')


def test_ссылки_главной_свой_хост_и_подписи():
    html = '''
    <header>
      <a href="/">Главная</a>
      <a href="/about/"><span>О</span> компании</a>
      <a href="https://site.ru/dostavka/">Доставка
         и оплата</a>
      <a href="https://spb.site.ru/about/">Питер</a>
      <a href="/catalog/truba/">Трубы</a>
      <a href="/basket/">Корзина</a>
      <a href="/about/?utm_source=x">О компании ещё раз</a>
      <a href="mailto:a@b.ru">Почта</a>
    </header>'''

    got = m.candidate_links(html, 'site.ru')

    assert [c['path'] for c in got] == ['/about/', '/dostavka/']
    assert got[0]['label'] == 'О компании'          # теги внутри <a> срезаны
    assert got[1]['label'] == 'Доставка и оплата'   # перенос строки схлопнут
    print('✓ свой хост, дедуп по пути, подпись из текста ссылки')


def test_подпись_подрезается_и_чистится():
    assert m.clean_label('  <i class="ico"></i> Оплата\n и доставка ') == \
        'Оплата и доставка'
    assert len(m.clean_label('о' * 200)) == 60
    print('✓ подпись без тегов, без переносов, не длиннее 60 символов')


# ── живая ли страница ────────────────────────────────────────────────

def test_каталог_в_корне_отсекается_по_разметке():
    """У МТТ и STB категории лежат в корне (/armatura): по адресу от
    /dostavka не отличить, по разметке - да."""
    категория = ('<div itemtype="https://schema.org/Product">'
                 '<span itemprop="price">1200</span>'
                 '<span itemprop="price">1300</span></div>')
    листинг = '<a class="add-to-cart">В корзину</a><a class="add-to-cart">В корзину</a>'
    служебная = '<h1>Доставка и оплата</h1><p>Возим по России, цена доставки — по запросу.</p>'

    assert m.looks_like_listing(категория)
    assert m.looks_like_listing(листинг)
    assert not m.looks_like_listing(служебная)
    print('✓ товарные страницы в блок служебных не попадают')


def test_подпись_по_слагу_главнее_текста_ссылки():
    """У STB ссылка на политику стоит внутри чекбокса согласия и читается
    как «политикой обработки персональных данных» - в отчёте это не название."""
    assert m.label_for('/privacy', 'политикой обработки персональных данных') == \
        'Политика конфиденциальности'
    assert m.label_for('/agreement', 'соглашаюсь на обработку') == \
        'Пользовательское соглашение'
    # Нетипового слага нет - берём текст ссылки, если он похож на название.
    assert m.label_for('/sprav', 'Справочники') == 'Справочники'
    assert m.label_for('/xyz', 'нажимая кнопку вы соглашаетесь с условиями') == '/xyz'
    print('✓ подпись устойчива: слаг → текст ссылки → путь')


def test_каталог_проекта_не_считаем_служебными():
    html = ('<a href="/armatura">Арматура</a>'
            '<a href="/dostavka">Доставка</a>')

    got = m.candidate_links(html, 'site.ru', known_paths={'/armatura'})

    assert [c['path'] for c in got] == ['/dostavka']
    print('✓ пути из каталога проекта отсеиваются до прозвона')


def test_живой_считаем_только_настоящую_страницу():
    assert m.page_alive(200, '/dostavka/', '<title>Доставка</title>')
    assert not m.page_alive(404, '/dostavka/', '')
    assert not m.page_alive(200, '/', '<title>Главная</title>')   # увели на главную
    assert not m.page_alive(200, '/dostavka/',
                            '<title>Страница не найдена</title>')  # soft-404
    print('✓ 404, редирект на главную и заглушка «не найдена» отсеиваются')


# ── кеш ──────────────────────────────────────────────────────────────

def test_кеш_годен_неделю_и_только_для_своего_хоста():
    свежий = {'host': 'site.ru', 'updated': datetime.now().isoformat(),
              'pages': [{'path': '/about/', 'label': 'О компании'}]}
    assert m.cache_fresh(свежий, 'site.ru')
    assert not m.cache_fresh(свежий, 'other.ru'), 'чужой хост'

    старый = dict(свежий,
                  updated=(datetime.now() - timedelta(days=8)).isoformat())
    assert not m.cache_fresh(старый, 'site.ru')

    assert not m.cache_fresh({'host': 'site.ru', 'updated': 'кривая дата',
                              'pages': [1]}, 'site.ru')
    assert not m.cache_fresh({}, 'site.ru')
    print('✓ кеш живёт неделю, привязан к хосту, кривой не ломает прогон')


# ── ручной список главнее автопоиска ─────────────────────────────────

def test_ручной_список_не_трогаем(monkeypatch):
    """У СМУ/ИМП/АПС списки выверены вживую (включая адреса без слеша) -
    автопоиск для них не запускается."""
    def _взорвись(*a, **kw):
        raise AssertionError('автопоиск не должен запускаться')
    monkeypatch.setattr(m, '_discover', _взорвись)

    got = m.tech_paths_for_project('avia', 'aviastal.ru')

    assert [p['path'] for p in got] == S.get_tech_paths('avia')
    assert got[0]['label'] == 'О компании'      # подпись из ручной карты
    print('✓ ручной список побеждает, подписи из карты')


# ── подписи найденных страниц доходят до отчёта ──────────────────────

def test_найденные_страницы_попадают_в_подписи_и_в_карту():
    pid = '_тест_проект'
    try:
        S.register_tech_pages(pid, [{'path': '/dostavka-i-oplata/',
                                     'label': 'Доставка и оплата'}])

        assert S.get_tech_paths(pid) == ['/dostavka-i-oplata/']
        assert S.tech_page_label('/dostavka-i-oplata/') == 'Доставка и оплата'
        # Путь без завершающего слеша ищется в той же карте.
        assert S.tech_page_label('/dostavka-i-oplata') == 'Доставка и оплата'
    finally:
        S.TECH_PAGE_PATHS.pop(pid, None)
        S._RUNTIME_TECH_LABELS.pop('/dostavka-i-oplata/', None)
    print('✓ автопоиск даёт и пути для прогона, и человеческие подписи')
