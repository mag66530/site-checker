# -*- coding: utf-8 -*-
"""Проверка меню (п.1.11): прямые ссылки, ссылка на категории, запасная зона.

Два дефекта, из-за которых это переписывалось (оба поймали живьём):
  • ссылка на каталог искалась строкой «/catalog» - у АПС такого раздела нет
    вовсе (категории в корне), и предупреждение выписывалось на каждой странице;
  • вся проверка была под «если есть <header>/<nav>» - у Метпромко таких тегов
    нет, и проверка молча не работала, а в отчёте это читалось как «всё хорошо».
"""
import layout_checker as lc
from report_priorities import classify, _layout_findings


def _menu(warnings):
    return [w for w in warnings if 'меню' in w]


# ── Указатель путей категорий ────────────────────────────────────────


def test_указатель_включает_родителей():
    """В шапке обычно ссылка на РАЗДЕЛ, а в выгрузке - его подкатегории."""
    idx = lc.menu_category_index(['/list/list-riflenyj/',
                                  '/catalog/truba/profil/'])
    assert 'list' in idx and 'list/list-riflenyj' in idx
    assert 'catalog' in idx and 'catalog/truba' in idx
    assert 'catalog/truba/profil' in idx


def test_указатель_нормализует():
    idx = lc.menu_category_index(['/Catalog/Truba/', 'catalog/truba'])
    assert idx == {'catalog', 'catalog/truba'}
    assert lc.menu_category_index([]) == set()
    assert lc.menu_category_index(None) == set()


# ── Ссылка на категории: проекты с /catalog и без него ───────────────


_HTML_HEADER = ('<html><body><header><nav>'
                '<a href="/about/">О компании</a>'
                '<a href="{cat}">Каталог</a>'
                '</nav></header><main>текст</main></body></html>')


def test_категории_в_корне_не_дают_ложной_находки():
    """Случай АПС: категории вида /chernyi-prokat, раздела /catalog нет."""
    html = _HTML_HEADER.format(cat='/chernyi-prokat')
    idx = lc.menu_category_index(['/chernyi-prokat', '/cvetnoi-prokat'])
    r = lc.check_layout(html, [], base_url='https://aviastal.ru/',
                        menu_category_paths=idx)
    assert _menu(r['warnings']) == []
    assert r['menu_catalog'] is True
    assert r['menu_checked'] is True


def test_нет_ссылки_на_категории_находка_с_верным_текстом():
    html = _HTML_HEADER.format(cat='/contacts/')
    idx = lc.menu_category_index(['/chernyi-prokat'])
    r = lc.check_layout(html, [], base_url='https://aviastal.ru/',
                        menu_category_paths=idx)
    т = ' | '.join(_menu(r['warnings']))
    assert 'нет ссылок на категории каталога' in т
    # старого текста про «/catalog…» быть не должно: у сайта нет такого раздела
    assert '/catalog…' not in т
    assert r['menu_catalog'] is False


def test_ссылка_на_подкатегорию_тоже_считается():
    html = _HTML_HEADER.format(cat='/catalog/truba/profil/')
    idx = lc.menu_category_index(['/catalog/truba/profil/'])
    r = lc.check_layout(html, [], base_url='https://a.ru/',
                        menu_category_paths=idx)
    assert r['menu_catalog'] is True and _menu(r['warnings']) == []


def test_абсолютная_ссылка_в_меню_считается():
    html = _HTML_HEADER.format(cat='https://a.ru/catalog/truba/')
    idx = lc.menu_category_index(['/catalog/truba/'])
    r = lc.check_layout(html, [], base_url='https://a.ru/',
                        menu_category_paths=idx)
    assert r['menu_catalog'] is True


def test_без_указателя_работает_как_раньше():
    """Совместимость: не передали пути - старый признак «/catalog» и старый
    текст. Иначе сломались бы вызовы, которые указатель не передают."""
    r_ok = lc.check_layout(_HTML_HEADER.format(cat='/catalog/truba/'), [],
                           base_url='https://a.ru/')
    assert r_ok['menu_catalog'] is True and _menu(r_ok['warnings']) == []
    r_bad = lc.check_layout(_HTML_HEADER.format(cat='/contacts/'), [],
                            base_url='https://a.ru/')
    assert 'прямой ссылки на каталог (/catalog…)' in ' '.join(_menu(r_bad['warnings']))


# ── Пустышки в меню ──────────────────────────────────────────────────


def test_пустышки_в_меню():
    html = ('<html><body><header>'
            '<a href="#">раз</a><a href="javascript:void(0)">два</a>'
            '<a href="/catalog/truba/">каталог</a>'
            '</header></body></html>')
    r = lc.check_layout(html, [], base_url='https://a.ru/')
    assert r['menu_dummy'] == 2
    assert 'не прямыми ссылками' in ' '.join(_menu(r['warnings']))


def test_одна_пустышка_не_находка():
    """Порог - две: одна кнопка-переключатель на href="#" бывает у всех."""
    html = ('<html><body><header><a href="#">меню</a>'
            '<a href="/catalog/truba/">каталог</a></header></body></html>')
    r = lc.check_layout(html, [], base_url='https://a.ru/')
    assert r['menu_dummy'] == 1
    assert 'не прямыми ссылками' not in ' '.join(_menu(r['warnings']))


# ── Запасная зона и «не проверено» ───────────────────────────────────


def test_без_header_берём_контейнер_по_классу():
    """Случай Метпромко: <header>/<nav> нет, меню - div с class."""
    html = ('<html><body><div class="main-menu">'
            '<a href="/list/">Лист</a><a href="#">раз</a><a href="#">два</a>'
            '</div><main>текст</main></body></html>')
    idx = lc.menu_category_index(['/list/list-riflenyj/'])
    r = lc.check_layout(html, [], base_url='https://metpromko.ru/',
                        menu_category_paths=idx)
    assert r['menu_checked'] is True and r['menu_zone'] == 'class/id'
    assert r['menu_catalog'] is True          # /list/ - раздел каталога
    assert 'не прямыми ссылками' in ' '.join(_menu(r['warnings']))


def test_меню_не_найдено_говорим_прямо():
    """Тишина в отчёте читалась бы как «меню в порядке» - так нельзя."""
    html = '<html><body><div class="wrap"><a href="/x/">ссылка</a></div></body></html>'
    r = lc.check_layout(html, [], base_url='https://a.ru/',
                        menu_category_paths={'catalog'})
    assert r['menu_checked'] is False and r['menu_zone'] == ''
    т = ' '.join(_menu(r['warnings']))
    assert 'меню не проверено' in т
    # и НЕ выписываем при этом «нет ссылок на категории»: мы просто не смотрели
    assert 'нет ссылок на категории' not in т
    assert r['menu_catalog'] is False


def test_закомментированная_ссылка_не_считается():
    """По выключенному из меню пункту посетитель перейти не может. Так же
    поступает прозвон меню (extract_menu_links) - иначе проверки расходятся."""
    html = ('<html><body><header>'
            '<!-- <a href="/catalog/truba/">каталог</a> -->'
            '<a href="/about/">О компании</a>'
            '</header></body></html>')
    idx = lc.menu_category_index(['/catalog/truba/'])
    r = lc.check_layout(html, [], base_url='https://a.ru/',
                        menu_category_paths=idx)
    assert r['menu_checked'] is True          # зона есть
    assert r['menu_catalog'] is False         # но ссылка выключена
    assert 'нет ссылок на категории каталога' in ' '.join(_menu(r['warnings']))


def test_закомментированные_пустышки_не_считаются():
    html = ('<html><body><header><!-- <a href="#">1</a><a href="#">2</a> -->'
            '<a href="/catalog/truba/">каталог</a></header></body></html>')
    r = lc.check_layout(html, [], base_url='https://a.ru/')
    assert r['menu_dummy'] == 0
    assert _menu(r['warnings']) == []


def test_header_приоритетнее_запасной_зоны():
    html = ('<html><body><header><a href="/catalog/truba/">каталог</a></header>'
            '<div class="menu"><a href="#">1</a><a href="#">2</a></div>'
            '</body></html>')
    r = lc.check_layout(html, [], base_url='https://a.ru/')
    assert r['menu_zone'] == 'header/nav'
    # пустышки из div.menu не учитываем - смотрели только шапку
    assert r['menu_dummy'] == 0


# ── Вывод в отчёт ────────────────────────────────────────────────────


def test_находки_меню_идут_в_вёрстку_со_своими_задачами():
    html = ('<html><body><header><a href="#">1</a><a href="#">2</a>'
            '</header></body></html>')
    r = lc.check_layout(html, [], base_url='https://a.ru/',
                        menu_category_paths={'catalog'})
    находки = [f for f in _layout_findings(r, city='Москва',
                                          page_type='Главная',
                                          url='https://a.ru/')
               if 'меню' in f.problem]
    группы = {classify(f)['task_group'] for f in находки}
    assert 'menu_direct_links' in группы
    assert 'menu_catalog_link' in группы
    assert all(f.section == 'Вёрстка' for f in находки)
    assert all(f.level == 'Предупреждение' for f in находки)


def test_задача_для_непроверенного_меню():
    html = '<html><body><p>без меню</p></body></html>'
    r = lc.check_layout(html, [], base_url='https://a.ru/')
    находки = [f for f in _layout_findings(r, city='Москва', page_type='Главная',
                                          url='https://a.ru/')
               if 'меню не проверено' in f.problem]
    assert len(находки) == 1
    assert classify(находки[0])['task_group'] == 'menu_unchecked'
