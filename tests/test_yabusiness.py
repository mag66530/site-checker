"""Я.Бизнес: извлечение города карточки и сверка с поддоменами.

Регресс: у карточки в SSR-вёрстке между "kind":"locality" и её "name" бывают
вложенные объекты ("translated_name":{...}), а на странице встречаются чужие
блоки (филиалы сети/похожие организации). Старый regex [^}]*? обрывался на
вложенности и хватал ЧУЖОЙ город → карточки Москвы/СПб/… уезжали в «нет
карточки». Проверяем, что город берётся правильно (locality подтверждается
собственным адресом, есть фолбэк по адресу) и что реальные карточки
сопоставляются с поддоменами."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yabusiness_check as Y


def _city(html):
    return Y._org_card_from_html(html, '1')['city']


def test_clean_locality():
    assert _city('{"kind":"locality","name":{"value":"Екатеринбург"}}') \
        == 'Екатеринбург'


def test_nested_locality_confirmed_by_address():
    """Вложенный translated_name между kind и name - старый regex ломался."""
    html = ('{"kind":"locality","translated_name":{"en":{"value":"S"}},'
            '"name":{"value":"Санкт-Петербург"}}'
            '"formatted":{"value":"Санкт-Петербург, Воронежская, 96"}')
    assert _city(html) == 'Санкт-Петербург'


def test_foreign_locality_first_is_rejected_by_address():
    """Чужой город идёт ПЕРВЫМ в вёрстке, но собственный адрес карточки -
    Москва: подтверждение адресом отсекает чужой locality."""
    html = ('{"kind":"locality","name":{"value":"Абакан"}} ...прочее... '
            '{"kind":"locality","translated_name":{"en":{"value":"M"}},'
            '"name":{"value":"Москва"}} '
            '"formatted":{"value":"Москва, улица Люблинская, 151"}')
    assert _city(html) == 'Москва'


def test_city_from_address_strips_country_and_region():
    assert _city('"formatted":{"value":"Россия, Новосибирская область, '
                 'Новосибирск, Горького, 79"}') == 'Новосибирск'


def test_street_only_address_gives_no_false_city():
    assert _city('"formatted":{"value":"улица Ленина, 10"}') is None


def test_reverse_key_order():
    assert _city('{"name":{"value":"Казань"},"kind":"locality"}') == 'Казань'


def test_subdomain_matching_no_false_positive(monkeypatch):
    """Реальные карточки сопоставляются; город без карточки остаётся missing."""
    def card(loc_html, addr=''):
        h = loc_html + (f'"formatted":{{"value":"{addr}"}}' if addr else '')
        return Y._org_card_from_html(h, '100')

    companies = [
        card('{"kind":"locality","name":{"value":"Екатеринбург"}}',
             'Екатеринбург, Зверева, 31'),
        card('{"kind":"locality","name":{"value":"Абакан"}} '          # чужой
             '{"kind":"locality","translated_name":{"en":{"value":"M"}},'
             '"name":{"value":"Москва"}}', 'Москва, Люблинская, 151'),
        card('{"kind":"locality","translated_name":{"en":{"value":"S"}},'
             '"name":{"value":"Санкт-Петербург"}}',
             'Санкт-Петербург, Воронежская, 96'),
    ]
    monkeypatch.setattr(Y, '_subdomains', lambda pid: [
        ('https://stalmetural.ru/', 'Москва', 'Россия'),
        ('https://spb.stalmetural.ru/', 'Санкт-Петербург', 'Россия'),
        ('https://ekaterinburg.stalmetural.ru/', 'Екатеринбург', 'Россия'),
        ('https://kazan.stalmetural.ru/', 'Казань', 'Россия'),   # карточки нет
    ])
    res = Y.check_subdomain_regions(companies, 'smu')
    assert sorted(m['city'] for m in res['matched']) == \
        ['Екатеринбург', 'Москва', 'Санкт-Петербург']
    assert [m['city'] for m in res['missing']] == ['Казань']   # честный missing
    assert not res['orphan_orgs']                              # чужих городов нет
