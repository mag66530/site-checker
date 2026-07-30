"""Тесты yandex_map_check.extract() - разбор карточки Яндекс.Карт.

Фрагмент HTML ниже - вырезка из РЕАЛЬНОЙ карточки (yandex.ru/maps/org/
stalmetural/128446144797/), полученной вживую 2026-07-30: то же название,
телефон, адрес, сайт, что и в catalogs/smu-kp.csv для Москвы. itemprop-атрибуты
- это microdata schema.org, она устойчивее CSS-классов (см. docstring модуля)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yandex_map_check as y

# Минимальный реальный фрагмент разметки (сокращён, но структура и атрибуты -
# как на живой странице, включая лишний itemprop="name" у отзывов - именно
# из-за него extract() обязан брать ПЕРВОЕ совпадение, а не любое).
_REAL_FRAGMENT = '''
<div class="orgpage-header-view__header">
  <h1 itemprop="name">Стальметурал</h1>
  <a class="orgpage-header-view__address" title="Москва, Люблинская улица, 151">
    <span>Люблинская ул., 151, Москва</span>
  </a>
</div>
<meta itemprop="address" content="Москва, Люблинская улица, 151, 109341">
<div class="card-phones-view__phone-number" dir="ltr">
  <span dir="ltr" itemprop="telephone">+7 (499) 130-36-69</span>
</div>
<div class="business-urls-view__url">
  <a itemprop="url" class="business-urls-view__link"
     href="https://stalmetural.ru/" target="_blank">stalmetural.ru</a>
</div>
<div class="business-reviews-view__review">
  <span itemprop="name" dir="auto">Владимир Чернов</span>
</div>
'''


def test_extract_real_fragment():
    r = y.extract(_REAL_FRAGMENT)
    assert r['name'] == 'Стальметурал', 'должно взять ПЕРВОЕ itemprop=name (org), не отзыв'
    assert r['phone'] == '+7 (499) 130-36-69'
    assert r['address'] == 'Москва, Люблинская улица, 151, 109341'
    assert r['site'] == 'https://stalmetural.ru/'
    assert r['available'] is True
    print('✓ реальный фрагмент разобран целиком, имя - организации, не отзыва')


def test_extract_empty_page_is_unavailable():
    """Заглушка/капча/не та страница - available=False, не падение и не мусор."""
    r = y.extract('<html><body>Доступ ограничен</body></html>')
    assert r['available'] is False
    assert r['name'] == r['phone'] == r['address'] == r['site'] == ''
    print('✓ пустая страница → available=False, без падения')


def test_extract_partial_data_still_available():
    """Хватает хотя бы одного поля, чтобы считать карточку прочитанной."""
    r = y.extract('<span itemprop="telephone">+7 900 000-00-00</span>')
    assert r['available'] is True
    assert r['phone'] == '+7 900 000-00-00'
    assert r['name'] == ''
    print('✓ частичные данные всё равно available=True')


def test_afetch_without_url_is_graceful():
    """Пустая ссылка (город не проверяется по карте) - не сетевой вызов, не падение."""
    import asyncio
    r = asyncio.run(y.afetch(None, None, ''))
    assert r['available'] is False
    assert r['error'] == 'ссылки нет'
    print('✓ пустая ссылка обработана без похода в сеть')
