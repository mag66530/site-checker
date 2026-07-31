"""Тесты google_map_check.extract(): разбор карточки Google Maps.

Текст полей (адрес/телефон/сайт) у Google лежит в ОДИНАКОВОМ классе
"Io6YTe fontBodyMedium kR99db fdkmkc" - различать поля по классу нельзя,
только по data-item-id на кнопке/ссылке-обёртке (address / phone: / authority) -
это и проверяем: три поля с одинаковым классом текста разбираются верно."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import google_map_check as gm

_HEAD = '<html><head><meta content="Стальметурал" property="og:title"></head><body>'
_TAIL = '</body></html>'

# Тюмень (СМУ) - реальные значения полей, присланные пользователем; обёртка
# data-item-id - стандартная для Google Maps разметка (кнопка на адрес/телефон,
# ссылка на сайт), текстовый класс - тот же у всех трёх, как в реальной карточке.
_TYUMEN_PANEL = """
<button data-item-id="address" aria-label="Адрес: Коммунистическая ул., 16 а, Тюмень, Тюменская обл., 625000">
  <div class="Io6YTe fontBodyMedium kR99db fdkmkc ">Коммунистическая ул., 16 а, Тюмень, Тюменская обл., 625000</div>
</button>
<button data-item-id="phone:tel:+73452531698" aria-label="Телефон: 8 (345) 253-16-98">
  <div class="Io6YTe fontBodyMedium kR99db fdkmkc ">8 (345) 253-16-98</div>
</button>
<a data-item-id="authority" href="http://tyumen.stalmetural.ru/" aria-label="Сайт: tyumen.stalmetural.ru">
  <div class="Io6YTe fontBodyMedium kR99db fdkmkc ">tyumen.stalmetural.ru</div>
</a>
"""


def _html(panel):
    return _HEAD + panel + _TAIL


def test_address_found_by_item_id_not_by_shared_class():
    data = gm.extract(_html(_TYUMEN_PANEL))
    assert data['address'] == 'Коммунистическая ул., 16 а, Тюмень, Тюменская обл., 625000'
    print('✓ адрес разобран через data-item-id, не через общий класс текста')


def test_phone_found_by_item_id_prefix():
    data = gm.extract(_html(_TYUMEN_PANEL))
    assert data['phone'] == '8 (345) 253-16-98'
    print('✓ телефон разобран (значение - из видимого текста, не из атрибута)')


def test_site_found_by_authority_item_id():
    data = gm.extract(_html(_TYUMEN_PANEL))
    assert data['site'] == 'tyumen.stalmetural.ru'
    print('✓ сайт разобран через data-item-id="authority"')


def test_name_from_og_title():
    data = gm.extract(_html(_TYUMEN_PANEL))
    assert data['name'] == 'Стальметурал'
    print('✓ имя организации - из og:title')


def test_all_three_fields_do_not_get_mixed_up():
    """Раз класс текста ОДИНАКОВЫЙ у всех трёх полей - главный риск: перепутать
    их местами (напр. телефон попадёт в site). Проверяем разом, что каждое
    поле - именно своё значение."""
    data = gm.extract(_html(_TYUMEN_PANEL))
    assert data['address'] != data['phone'] != data['site']
    assert 'Коммунистическая' in data['address']
    assert '253-16-98' in data['phone']
    assert 'tyumen' in data['site']
    print('✓ три поля с одинаковым классом текста не перепутаны местами')


def test_site_with_sibling_icon_glyph_is_not_polluted():
    """Баг: сайт на карте не совпадал с КП, хотя внешне текст был одинаковый -
    в элементе с data-item-id="authority", помимо div.Io6YTe с самим доменом,
    была ещё соседняя иконка («открыть в новом окне») - лигатура иконочного
    шрифта, при извлечении текста без самого шрифта превращающаяся в
    мусорный символ. get_text() по ВСЕМУ элементу цеплял и его. Берём текст
    строго из div.Io6YTe - иконка мимо."""
    icon_glyph = ''   # лигатура Material Icons ("open_in_new") как текст
    panel = f"""
    <a data-item-id="authority" href="http://perm.stalmetural.ru/">
      <span class="icon-wrap">{icon_glyph}</span>
      <div class="Io6YTe fontBodyMedium kR99db fdkmkc ">perm.stalmetural.ru</div>
    </a>
    """
    data = gm.extract(_HEAD + panel + _TAIL)
    assert data['site'] == 'perm.stalmetural.ru'
    assert icon_glyph not in data['site']
    print('✓ иконка-лигатура рядом со значением не портит текст сайта')


def test_zero_width_space_in_value_is_stripped():
    """Невидимый zero-width space внутри значения (как у 2ГИС-карточек) не
    должен оставаться в результате - иначе визуально одинаковый текст не
    совпадает с КП побайтово."""
    zwsp = '​'   # zero-width space - невидимый, но реально в HTML присутствует
    panel = (
        '<a data-item-id="authority" href="http://perm.stalmetural.ru/">'
        f'<div class="Io6YTe fontBodyMedium kR99db fdkmkc ">{zwsp}perm.stalmetural.ru</div>'
        '</a>'
    )
    data = gm.extract(_HEAD + panel + _TAIL)
    assert data['site'] == 'perm.stalmetural.ru'
    print('✓ невидимый zero-width space вычищен из значения')


def test_available_false_without_any_item_id():
    data = gm.extract('<html><head></head><body><div>ничего нет</div></body></html>')
    assert data['available'] is False
    print('✓ без data-item-id и без og:title - available=False, штатный исход')


def test_partial_card_only_address_present():
    """Только адрес есть на карточке (телефон/сайт не указаны организацией) -
    остальные поля просто пустые, available всё равно True (адрес - тоже сигнал)."""
    panel = """
    <button data-item-id="address">
      <div class="Io6YTe fontBodyMedium kR99db fdkmkc ">ул. Тестовая, 1</div>
    </button>
    """
    data = gm.extract(_HEAD + panel + _TAIL)
    assert data['address'] == 'ул. Тестовая, 1'
    assert data['phone'] == '' and data['site'] == ''
    assert data['available'] is True
    print('✓ частичная карточка (только адрес) - остальное пусто, available=True')
