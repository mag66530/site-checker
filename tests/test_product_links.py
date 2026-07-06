"""Тесты product_links - сбор товарных ссылок с листингов."""
import json

from product_links import (
    extract_product_paths, save_product_links, load_product_links,
)


PAGE_URL = 'https://stalmetural.ru/catalog/armatura/'
KNOWN_CATS = {'/catalog/armatura/', '/catalog/armatura/at800/'}
KNOWN_FILTERS = {'/catalog/armatura/gost-5781/'}


def test_extracts_product_links():
    # Товары берутся из карточек (контейнер catalog-product-card-item).
    html = (
        '<div class="catalog-product-card-item">'
        '<a href="/catalog/armatura/armatura-10-at800/">Арматура 10 АТ800</a></div>'
        '<div class="catalog-product-card-item">'
        '<a href="https://stalmetural.ru/catalog/armatura/armatura-12-a500/">12 А500</a></div>'
    )
    paths = extract_product_paths(html, PAGE_URL, KNOWN_CATS, KNOWN_FILTERS)
    assert '/catalog/armatura/armatura-10-at800/' in paths
    assert '/catalog/armatura/armatura-12-a500/' in paths


def test_extracts_imp_root_level_product_from_card():
    """ИМП: товар - КОРНЕВОЙ slug (/list-…/), карточка card-product. Берём его,
    пропуская иконку-ассет; URL-фильтр (категория/характеристика/значение) - нет."""
    html = (
        '<div class="listing__cards_col card-product">'
        '<a href="/catalog/view/theme/default/sprite.svg">иконка</a>'
        '<a class="card-product__title" href="/list-otsinkovannyj-0-25h1250-mm-rulon-nlmk/">Лист</a>'
        '</div>'
    )
    paths = extract_product_paths(html, 'https://inmetprom.ru/catalog/listovoj-prokat/list-otsinkovannyj/',
                                  set(), set())
    assert paths == ['/list-otsinkovannyj-0-25h1250-mm-rulon-nlmk/']


def test_excludes_template_junk_hrefs():
    """Битый JS-шаблон в href (${ product.href }) - не ссылка, пропускаем."""
    html = (
        '<div class="card-product">'
        '<a href="${ product.href }">шаблон</a>'
        '<a href="/list-stalnoj-goryachekatanyj-3-mm-gost/">реальный</a>'
        '</div>'
    )
    paths = extract_product_paths(html, 'https://inmetprom.ru/catalog/x/', set(), set())
    assert paths == ['/list-stalnoj-goryachekatanyj-3-mm-gost/']


def test_excludes_imp_facet_listing_in_fallback():
    """Без карточек: ссылка-фильтр <категория>/<характеристика>/<значение>/ -
    это листинг, не товар (так устроен ИМП)."""
    known_cats = {'/catalog/listovoj-prokat/list-otsinkovannyj/'}
    html = (
        '<a href="/catalog/listovoj-prokat/list-otsinkovannyj/tolschina-mm/0-5/">фильтр</a>'
        '<a href="/catalog/listovoj-prokat/list-otsinkovannyj/marka/st3/">фильтр</a>'
    )
    paths = extract_product_paths(html, 'https://inmetprom.ru/catalog/listovoj-prokat/list-otsinkovannyj/',
                                  known_cats, set())
    assert paths == []


def test_excludes_categories_filters_and_short_paths():
    html = (
        '<a href="/catalog/armatura/">категория (сама страница)</a>'
        '<a href="/catalog/armatura/at800/">подкатегория из каталога</a>'
        '<a href="/catalog/armatura/gost-5781/">тег из каталога</a>'
        '<a href="/catalog/armatura/filter/diametr-10/">фильтр</a>'
        '<a href="/catalog/balka/">категория, 2 сегмента</a>'
        '<a href="/about/">не каталог</a>'
    )
    paths = extract_product_paths(html, PAGE_URL, KNOWN_CATS, KNOWN_FILTERS)
    assert paths == []


def test_excludes_foreign_hosts_and_dedups():
    html = (
        '<a href="https://other-site.ru/catalog/armatura/tovar-x/">чужой хост</a>'
        '<a href="/catalog/armatura/tovar-y/">наш товар</a>'
        '<a href="/catalog/armatura/tovar-y/">дубль</a>'
        '<a href="mailto:a@b.ru">почта</a>'
        '<a href="tel:+74951234567">телефон</a>'
    )
    paths = extract_product_paths(html, PAGE_URL, KNOWN_CATS, KNOWN_FILTERS)
    assert paths == ['/catalog/armatura/tovar-y/']


def test_path_without_trailing_slash_normalized():
    html = '<a href="/catalog/armatura/tovar-z">без слеша</a>'
    paths = extract_product_paths(html, PAGE_URL, KNOWN_CATS, KNOWN_FILTERS)
    assert paths == ['/catalog/armatura/tovar-z/']


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    import product_links as pl
    monkeypatch.setattr(pl, 'CATALOGS_DIR', tmp_path)

    collected = {
        'links': [
            {'url': '/catalog/armatura/tovar-1/', 'category': '/catalog/armatura/'},
            {'url': '/catalog/balka/tovar-2/', 'category': '/catalog/balka/'},
        ],
        'categories_total': 10,
        'categories_ok': 9,
        'categories_failed': 1,
    }
    save_product_links('testproj', collected)

    loaded = load_product_links('testproj')
    assert loaded is not None
    assert loaded['pathnames'] == ['/catalog/armatura/tovar-1/', '/catalog/balka/tovar-2/']
    assert loaded['categories_total'] == 10
    assert loaded['categories_ok'] == 9
    assert loaded['is_stale'] is False, 'только что собранная база - свежая'


def test_load_missing_returns_none(tmp_path, monkeypatch):
    import product_links as pl
    monkeypatch.setattr(pl, 'CATALOGS_DIR', tmp_path)
    assert load_product_links('no-such-project') is None


def test_stale_after_31_days(tmp_path, monkeypatch):
    import time
    import product_links as pl
    monkeypatch.setattr(pl, 'CATALOGS_DIR', tmp_path)

    save_product_links('testproj', {
        'links': [{'url': '/catalog/c/t/', 'category': '/catalog/c/'}],
        'categories_total': 1, 'categories_ok': 1, 'categories_failed': 0,
    })
    # Сдвигаем дату сбора на 31 день назад
    meta_file = tmp_path / 'testproj-products-meta.json'
    meta = json.loads(meta_file.read_text(encoding='utf-8'))
    meta['collected_at'] = int(time.time() * 1000) - 31 * 24 * 3600 * 1000
    meta_file.write_text(json.dumps(meta), encoding='utf-8')

    loaded = load_product_links('testproj')
    assert loaded['is_stale'] is True
