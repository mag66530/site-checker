"""Тесты картинки категории (уникальность, п.1.15): распознавание
og:image / первой картинки после h1, нормализация resize_cache,
заглушки, поиск дублей между категориями одного поддомена."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_checker import category_image, category_image_dups, _img_key


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


if __name__ == '__main__':
    test_og_image_wins()
    test_og_image_logo_falls_back_to_content()
    test_first_img_after_h1_skips_svg_and_logo()
    test_placeholder_flagged()
    test_not_recognized_returns_none()
    test_resize_cache_collapsed()
    test_dups_same_subdomain_only()
    test_placeholders_not_counted_as_dups()
    print('Все тесты картинок категорий пройдены.')
