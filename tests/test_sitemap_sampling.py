"""Тесты чистой логики sitemap_sampling.py (группировка по видам, выбор
файла на вид) - без сети."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sitemap_sampling import kind_of, group_by_kind, pick_one_per_kind


def test_kind_of_убирает_цифры_и_разделители():
    assert kind_of('https://a.ru/sitemap-products-1.xml') == 'sitemapproducts'
    assert kind_of('https://a.ru/sitemap-products-2.xml') == 'sitemapproducts'
    assert kind_of('https://a.ru/sitemap-category.xml') == 'sitemapcategory'
    assert kind_of('https://a.ru/sitemap-filters-1.xml') == 'sitemapfilters'


def test_kind_of_не_завязан_на_словарь_слов():
    # Любое слово, не только «продукты/товары» - алгоритм не знает языков.
    assert kind_of('https://a.ru/catalog_export_3.xml') == kind_of(
        'https://a.ru/catalog_export_9.xml')
    assert kind_of('https://a.ru/blah-blah-42.xml') == 'blahblah'


def test_group_by_kind_группирует_и_сохраняет_порядок():
    urls = [
        'https://a.ru/sitemap-products-1.xml',
        'https://a.ru/sitemap-category.xml',
        'https://a.ru/sitemap-products-2.xml',
        'https://a.ru/sitemap-filters-1.xml',
        'https://a.ru/sitemap-products-3.xml',
    ]
    groups = group_by_kind(urls)
    assert list(groups.keys()) == ['sitemapproducts', 'sitemapcategory', 'sitemapfilters']
    assert groups['sitemapproducts'] == [
        'https://a.ru/sitemap-products-1.xml',
        'https://a.ru/sitemap-products-2.xml',
        'https://a.ru/sitemap-products-3.xml',
    ]
    assert groups['sitemapcategory'] == ['https://a.ru/sitemap-category.xml']


def test_карта_без_продолжений_тоже_берётся():
    groups = group_by_kind(['https://a.ru/sitemap-category.xml'])
    chosen = pick_one_per_kind(groups, excluded=set())
    assert chosen == ['https://a.ru/sitemap-category.xml']


def test_pick_one_per_kind_один_файл_на_каждый_вид():
    groups = {
        'products': [f'https://a.ru/p{i}.xml' for i in range(10)],
        'filters': [f'https://a.ru/f{i}.xml' for i in range(3)],
        'category': ['https://a.ru/c.xml'],
    }
    chosen = pick_one_per_kind(groups, excluded=set(), rng=random.Random(1))
    assert len(chosen) == 3
    assert sum(u.startswith('https://a.ru/p') for u in chosen) == 1
    assert sum(u.startswith('https://a.ru/f') for u in chosen) == 1
    assert 'https://a.ru/c.xml' in chosen


def test_исключённый_файл_не_выбирается():
    groups = {'category': ['https://a.ru/c1.xml', 'https://a.ru/c2.xml']}
    chosen = pick_one_per_kind(groups, excluded={'https://a.ru/c1.xml'},
                               rng=random.Random(1))
    assert chosen == ['https://a.ru/c2.xml']


def test_вид_пропускается_если_все_файлы_исключены():
    groups = {
        'category': ['https://a.ru/c.xml'],
        'filters': ['https://a.ru/f.xml'],
    }
    chosen = pick_one_per_kind(groups, excluded={'https://a.ru/f.xml'})
    assert chosen == ['https://a.ru/c.xml']


def test_pick_one_per_kind_детерминирован_с_seed():
    groups = {'products': [f'https://a.ru/p{i}.xml' for i in range(20)]}
    a = pick_one_per_kind(groups, excluded=set(), rng=random.Random(42))
    b = pick_one_per_kind(groups, excluded=set(), rng=random.Random(42))
    assert a == b


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
