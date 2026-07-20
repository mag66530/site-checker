"""Тесты «главной» картинки страницы (уникальность, п.1.15): распознавание
og:image / первой картинки после h1, нормализация resize_cache, заглушки,
поиск дублей между категориями одного поддомена и между товарами (с отсевом
«тот же товар в другой категории» по slug)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_checker import (category_image, category_image_dups, _img_key,
                           product_image, product_image_dups, product_slug,
                           product_category)


BASE = 'https://x.ru/catalog/truby/'


def test_og_image_wins():
    html = ('<meta property="og:image" content="/upload/iblock/ab12/truby.jpg">'
            '<h1>Трубы</h1><img src="/upload/iblock/cd34/other.jpg" alt="x">')
    ci = category_image(html, BASE)
    assert ci and ci['source'] == 'og:image'
    assert ci['key'] == '/upload/iblock/ab12/truby.jpg'
    assert ci['name'] == 'truby.jpg'
    assert not ci['placeholder']
    print('✓ og:image приоритетнее картинки после h1')


def test_og_image_logo_falls_back_to_content():
    html = ('<meta property="og:image" content="/img/logo.png">'
            '<h1>Трубы</h1><img src="/upload/iblock/cd34/truby-banner.jpg">')
    ci = category_image(html, BASE)
    assert ci and ci['source'] == 'после h1'
    assert ci['name'] == 'truby-banner.jpg'
    print('✓ og:image-логотип пропущен, взята картинка после h1')


def test_first_img_after_h1_skips_svg_and_logo():
    html = ('<img src="/img/logo.png"><h1>Трубы</h1>'
            '<img src="/img/icon-arrow.svg"><img src="/img/deco.svg">'
            '<img src="/upload/iblock/ee55/hero.webp" alt="Трубы">')
    ci = category_image(html, BASE)
    assert ci and ci['name'] == 'hero.webp'
    print('✓ svg/логотипы после h1 пропущены, логотип до h1 не взят')


def test_placeholder_flagged():
    html = '<h1>Раздел</h1><img src="/img/no-photo.png">'
    ci = category_image(html, BASE)
    assert ci and ci['placeholder']
    print('✓ заглушка no-photo распознана с пометкой')


def test_not_recognized_returns_none():
    assert category_image('<h1>Пусто</h1><p>текст</p>', BASE) is None
    assert category_image('', BASE) is None
    print('✓ нет картинки - None (пропуск, не находка)')


def test_resize_cache_collapsed():
    a = _img_key('/upload/resize_cache/iblock/ab12/300_200_1/truby.jpg', BASE)
    b = _img_key('/upload/iblock/ab12/truby.jpg', BASE)
    assert a == b == '/upload/iblock/ab12/truby.jpg'
    c = _img_key('/upload/iblock/ab12/truby.jpg?v=2', BASE)
    assert c == b
    print('✓ resize_cache и query схлопнуты в один ключ')


def test_dups_same_subdomain_only():
    img = {'key': '/upload/iblock/ab/x.jpg', 'name': 'x.jpg',
           'source': 'og:image', 'placeholder': False}
    other = {'key': '/upload/iblock/cd/y.jpg', 'name': 'y.jpg',
             'source': 'og:image', 'placeholder': False}
    cats = [
        ('msk', 'https://msk.x.ru/catalog/a/', img),
        ('msk', 'https://msk.x.ru/catalog/b/', img),      # дубль в msk
        ('spb', 'https://spb.x.ru/catalog/a/', img),      # зеркало - не дубль
        ('msk', 'https://msk.x.ru/catalog/c/', other),    # своя - ок
        ('msk', 'https://msk.x.ru/catalog/d/', None),     # не распознана
    ]
    dups = category_image_dups(cats)
    assert list(dups) == [('msk', '/upload/iblock/ab/x.jpg')]
    assert sorted(dups[('msk', '/upload/iblock/ab/x.jpg')]) == [
        'https://msk.x.ru/catalog/a/', 'https://msk.x.ru/catalog/b/']
    print('✓ дубль ловится в рамках поддомена, зеркала городов не шумят')


def test_placeholders_not_counted_as_dups():
    plh = {'key': '/img/no-photo.png', 'name': 'no-photo.png',
           'source': 'после h1', 'placeholder': True}
    cats = [('msk', 'https://msk.x.ru/catalog/a/', plh),
            ('msk', 'https://msk.x.ru/catalog/b/', plh)]
    assert category_image_dups(cats) == {}
    print('✓ заглушки в дубли не считаются (у них своё предупреждение)')


# ── Фото товаров (изображения товаров в разных категориях не дублируются) ──

PBASE = 'https://x.ru/catalog/sladosti/malina/'


def test_product_image_same_extractor_as_category():
    html = ('<meta property="og:image" content="/upload/iblock/ab/malina.jpg">'
            '<h1>Малиновое варенье</h1>')
    pi = product_image(html, PBASE)
    assert pi and pi['source'] == 'og:image'
    assert pi['key'] == '/upload/iblock/ab/malina.jpg'
    assert not pi['placeholder']
    print('✓ фото товара распознаётся тем же движком, что и картинка категории')


def test_product_slug_last_segment():
    assert product_slug('/catalog/sladosti/malina/') == 'malina'
    assert product_slug('/catalog/podarki/malina/') == 'malina'
    assert product_slug('https://x.ru/karbid-bora-f80/') == 'karbid-bora-f80'
    assert product_slug('/catalog/armatura/2938017-armatura-10-mm/') \
        == '2938017-armatura-10-mm'
    print('✓ slug товара - последний сегмент пути')


def test_product_category_from_url():
    """Категория из URL - родительский путь (у СМУ/металлопроката это и есть
    категория листинга)."""
    assert product_category(
        '/catalog/armatura-a4-a600/2938017-armatura-10-mm/') \
        == '/catalog/armatura-a4-a600'
    assert product_category('https://msk.x.ru/catalog/truby/5000-truba-57/') \
        == '/catalog/truby'
    print('✓ категория товара по URL - родительский путь')


def test_metalloprokat_same_category_reuse_not_dup():
    """Металлопрокат: арматура 10 мм и 12 мм - разные товары одной категории с
    одним фото. Внутри категории общее фото - норма, НЕ дубль."""
    armat = {'key': '/upload/iblock/ab/armatura.jpg', 'name': 'armatura.jpg',
             'source': 'og:image', 'placeholder': False}
    prods = [
        ('msk', 'https://msk.x.ru/catalog/armatura/2938017-armatura-10-mm/',
         armat),
        ('msk', 'https://msk.x.ru/catalog/armatura/2938018-armatura-12-mm/',
         armat),
        ('msk', 'https://msk.x.ru/catalog/armatura/2938019-armatura-14-mm/',
         armat),
    ]
    assert product_image_dups(prods) == {}
    print('✓ металлопрокат: одно фото внутри категории - норма, не дубль')


def test_same_product_different_categories_not_dup():
    """«Малиновое варенье» в «Сладостях» и «Подарках» - один товар (тот же
    slug), одно фото. Это норма CMS, не дубль (скриншот из ТЗ)."""
    img = {'key': '/upload/iblock/ab/malina.jpg', 'name': 'malina.jpg',
           'source': 'og:image', 'placeholder': False}
    prods = [
        ('msk', 'https://msk.x.ru/catalog/sladosti/malina/', img),
        ('msk', 'https://msk.x.ru/catalog/podarki/malina/', img),
    ]
    assert product_image_dups(prods) == {}
    print('✓ один товар в разных категориях (тот же slug) - не дубль')


def test_photo_across_categories_is_dup():
    """Одно фото у РАЗНЫХ товаров из РАЗНЫХ категорий (арматура и труба) -
    реальный дубль. Зеркало города и своё фото не в счёт."""
    stock = {'key': '/upload/iblock/ab/stock.jpg', 'name': 'stock.jpg',
             'source': 'og:image', 'placeholder': False}
    own = {'key': '/upload/iblock/cd/own.jpg', 'name': 'own.jpg',
           'source': 'og:image', 'placeholder': False}
    prods = [
        ('msk', 'https://msk.x.ru/catalog/armatura/2938017-armatura-10-mm/',
         stock),
        ('msk', 'https://msk.x.ru/catalog/truby/5000-truba-57mm/', stock),
        ('spb', 'https://spb.x.ru/catalog/armatura/2938017-armatura-10-mm/',
         stock),  # зеркало города - не дубль
        ('msk', 'https://msk.x.ru/catalog/truby/5001-truba-89mm/', own),
    ]
    dups = product_image_dups(prods)
    assert list(dups) == [('msk', '/upload/iblock/ab/stock.jpg')]
    assert sorted(dups[('msk', '/upload/iblock/ab/stock.jpg')]) == [
        'https://msk.x.ru/catalog/armatura/2938017-armatura-10-mm/',
        'https://msk.x.ru/catalog/truby/5000-truba-57mm/']
    print('✓ одно фото у товаров из разных категорий - дубль; зеркало/своё ок')


def test_category_of_callback_for_hidden_categories():
    """У МПЭ/ИМП категория из URL товара не видна (/catalog/tovar/<slug>/) -
    реальную категорию передаём через category_of. Без него URL-родитель у
    обоих одинаковый и дубль бы не нашёлся."""
    stock = {'key': '/upload/iblock/ab/stock.jpg', 'name': 'stock.jpg',
             'source': 'og:image', 'placeholder': False}
    prods = [
        ('msk', 'https://msk.x.ru/catalog/tovar/list-a/', stock),
        ('msk', 'https://msk.x.ru/catalog/tovar/truba-b/', stock),
    ]
    # Без category_of: у обоих родитель /catalog/tovar - одна категория, не дубль
    assert product_image_dups(prods) == {}
    # С реальными категориями из базы листингов - дубль между категориями
    cat_map = {'/catalog/tovar/list-a/': '/catalog/listy/',
               '/catalog/tovar/truba-b/': '/catalog/truby/'}
    from urllib.parse import urlsplit
    cat_of = lambda u: cat_map.get(urlsplit(u).path, '')
    dups = product_image_dups(prods, category_of=cat_of)
    assert list(dups) == [('msk', '/upload/iblock/ab/stock.jpg')]
    print('✓ скрытые в URL категории берутся из базы через category_of')


def test_product_placeholder_not_counted_as_dup():
    plh = {'key': '/img/no-photo.png', 'name': 'no-photo.png',
           'source': 'после h1', 'placeholder': True}
    prods = [('msk', 'https://msk.x.ru/catalog/a/item1/', plh),
             ('msk', 'https://msk.x.ru/catalog/b/item2/', plh)]
    assert product_image_dups(prods) == {}
    print('✓ заглушки товаров в дубли не считаются (у них своё предупреждение)')


if __name__ == '__main__':
    test_og_image_wins()
    test_og_image_logo_falls_back_to_content()
    test_first_img_after_h1_skips_svg_and_logo()
    test_placeholder_flagged()
    test_not_recognized_returns_none()
    test_resize_cache_collapsed()
    test_dups_same_subdomain_only()
    test_placeholders_not_counted_as_dups()
    test_product_image_same_extractor_as_category()
    test_product_slug_last_segment()
    test_product_category_from_url()
    test_metalloprokat_same_category_reuse_not_dup()
    test_same_product_different_categories_not_dup()
    test_photo_across_categories_is_dup()
    test_category_of_callback_for_hidden_categories()
    test_product_placeholder_not_counted_as_dup()
    print('Все тесты картинок категорий и товаров пройдены.')
