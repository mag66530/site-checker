"""Тесты maps_compare.compare() - сверка карточки карты с КП.

Телефон/адрес сверяются ЧЕРЕЗ существующие kp.py-функции (phone_set,
address_match) - те же правила, что уже проверены на «Проверке КП» для
сайта. Здесь тестируем только логику сверки самой карты: что делает с
результатом available=False, отсутствием строки в КП, несовпадением по
каждому из трёх полей отдельно."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kp import KPRow
import maps_compare as mc

_KP_MOSCOW = KPRow(
    domain='stalmetural.ru', city='Москва',
    phone_common='+7 499 130-36-69', all_phones='4991303669',
    address='Люблинская ул., 151',
)

_CARD_MATCH = {
    'available': True, 'name': 'Стальметурал',
    'phone': '+7 (499) 130-36-69',
    'address': 'Москва, Люблинская улица, 151, 109341',
    'site': 'https://stalmetural.ru/',
}


def test_all_fields_match():
    r = mc.compare('yandex', 'Москва', 'https://yandex.ru/x', _CARD_MATCH, _KP_MOSCOW)
    assert r.is_ok
    assert r.phone_match and r.address_match and r.site_match
    assert not r.issues
    print('✓ все поля совпали - is_ok')


def test_card_unavailable_is_warning_not_error():
    """Карточка не прочиталась - это не «дефект сайта», отдельный статус."""
    card = {'available': False, 'error': 'таймаут'}
    r = mc.compare('yandex', 'Казань', 'https://yandex.ru/x', card, _KP_MOSCOW)
    assert r.is_warning
    assert not r.is_error
    assert r.error == 'таймаут'
    print('✓ недоступная карточка - warning, не error')


def test_phone_mismatch_is_error():
    card = dict(_CARD_MATCH, phone='+7 999 111-22-33')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    assert r.phone_match is False
    assert r.is_error
    assert any('телефон' in i for i in r.issues)
    print('✓ телефон не совпал → error с пояснением')


def test_address_mismatch_is_error():
    card = dict(_CARD_MATCH, address='Другая ул., 5, Казань')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    assert r.address_match is False
    assert r.is_error
    print('✓ адрес не совпал → error')


def test_site_mismatch_is_error():
    card = dict(_CARD_MATCH, site='https://konkurent.ru/')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    assert r.site_match is False
    assert r.is_error
    print('✓ сайт не совпал → error')


def test_site_subdomain_counts_as_match():
    """spb.stalmetural.ru на карте против stalmetural.ru в КП - не ошибка,
    это тот же бренд, просто городской поддомен."""
    card = dict(_CARD_MATCH, site='https://spb.stalmetural.ru/')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    assert r.site_match is True
    print('✓ поддомен того же домена - совпадение')


def test_site_root_domain_on_card_is_mismatch_when_kp_wants_subdomain():
    """Реальный случай (2ГИС, Севастополь/Ижевск): карточка показывает
    КОРНЕВОЙ домен stalmetural.ru, а КП для ЭТОГО города явно ждёт свой
    поддомен (sevastopol.stalmetural.ru/izhevsk.stalmetural.ru). Это НЕ тот
    же случай, что «карта = поддомен КП» (test_site_subdomain_counts_as_match) -
    здесь КП точно указывает нужный сайт для карточки, и корневой домен вместо
    него - реальная проблема (карточку не обновили под город), а не «тот же
    бренд». Подтверждено заказчиком на реальных карточках дважды."""
    row = KPRow(domain='sevastopol.stalmetural.ru', city='Севастополь',
               phone_common='+7 499 130-36-69', address='Хрусталёва, 74а')
    card = dict(_CARD_MATCH, site='stalmetural.ru')
    r = mc.compare('2gis', 'Севастополь', 'u', card, row)
    assert r.site_match is False
    assert r.is_error
    print('✓ карточка на корневой домен при поддомене в КП - расхождение, не совпадение')


def test_city_missing_from_kp_reports_but_does_not_crash():
    """Города нет в КП вообще (kp_row=None) - показываем данные карточки,
    явно помечаем «сверять не с чем», не притворяемся совпадением."""
    r = mc.compare('yandex', 'Неизвестный город', 'u', _CARD_MATCH, None)
    assert r.available
    assert r.phone_match is None and r.address_match is None
    assert any('нет в КП' in i for i in r.issues)
    print('✓ город без КП - не падает, явно помечен')


def test_no_phone_in_kp_is_not_compared():
    """В КП нет телефона - сверять нечего, phone_match остаётся None,
    а не ложным False."""
    kp_no_phone = KPRow(domain='stalmetural.ru', city='Тест', address='ул. Тест, 1')
    r = mc.compare('yandex', 'Тест', 'u', _CARD_MATCH, kp_no_phone)
    assert r.phone_match is None
    print('✓ нет телефона в КП → phone_match=None, не False')


# ── no_link: в КП просто нет ссылки на карту - норма, не ⚠ и не ✗ ──────────


def test_empty_url_is_no_link_not_warning():
    """Пустая ссылка (город без карточки на этом сервисе) - отдельная пометка
    no_link, is_warning/is_error оба False. Раньше это была обычная
    «карточка недоступна» (⚠) - неотличимо от реальной проблемы."""
    r = mc.compare('2gis', 'Брянск', '', {'available': False, 'error': 'ссылки нет'}, _KP_MOSCOW)
    assert r.no_link is True
    assert r.is_warning is False
    assert r.is_error is False
    assert r.is_ok is False
    print('✓ пустая ссылка → no_link=True, не warning и не error')


def test_broken_link_is_still_warning():
    """Ссылка ЕСТЬ, но карточка не прочиталась (сайт сломался/заблокировал) -
    это по-прежнему ⚠, no_link остаётся False."""
    card = {'available': False, 'error': 'таймаут'}
    r = mc.compare('yandex', 'Москва', 'https://yandex.ru/maps/org/x/1/', card, _KP_MOSCOW)
    assert r.no_link is False
    assert r.is_warning is True
    print('✓ ссылка есть, но не прочиталась → по-прежнему warning')


def test_details_populated_on_mismatch():
    """Комментарий-колонка в отчёте строится из details (kp/card по полю),
    а не из прозы issues - проверяем, что compare() их заполняет."""
    card = dict(_CARD_MATCH, phone='+7 000 000-00-00')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    assert len(r.details) == 1
    d = r.details[0]
    assert d['field'] == 'телефон'
    assert d['card'] == '+7 000 000-00-00'
    assert '4991303669' in d['kp']
    print('✓ details содержит структурированные КП/карточка для колонки отчёта')


# ── Телефон сверяем ТОЛЬКО с «Общий Город», не со всем набором города ───────
# (просьба заказчика: SEO/рекламный номер - для коллтрекинга на сайте, карта
# показывает публичный номер организации - это всегда Общий).


def test_phone_compares_only_common_not_seo_or_ad():
    """Номер совпадает с SEO/Рекламным слотом, но НЕ с Общим - раньше (через
    общий phone_set) засчиталось бы как ✓, теперь это ✗ - карта должна
    показывать именно Общий номер, а не любой номер города."""
    row = KPRow(domain='stalmetural.ru', city='Тест',
               phone_common='+7 495 111-11-11', phone_seo='+7 495 222-22-22',
               phone_ad='+7 495 333-33-33', all_phones='4951111111;4952222222;4953333333')
    card = dict(_CARD_MATCH, phone='+7 495 222-22-22')  # это SEO-номер, не Общий
    r = mc.compare('yandex', 'Тест', 'u', card, row)
    assert r.phone_match is False, 'SEO-номер на карте - не совпадение с Общим'
    print('✓ номер из SEO-слота на карте - расхождение, не ложное совпадение')


def test_phone_matches_when_equals_common():
    row = KPRow(domain='stalmetural.ru', city='Тест',
               phone_common='+7 495 111-11-11', phone_seo='+7 495 222-22-22',
               all_phones='4951111111;4952222222')
    card = dict(_CARD_MATCH, phone='+7 (495) 111-11-11')
    r = mc.compare('yandex', 'Тест', 'u', card, row)
    assert r.phone_match is True
    print('✓ номер совпал с Общим - совпадение')


def test_garbage_common_is_always_error_not_dash():
    """Общий Город - мусор, не номер (напр. тестовое '2') - это НЕ то же самое,
    что пустое поле: в КП есть инфа, и она не телефон → ВСЕГДА ✗, каким бы ни
    был номер на карточке. То же правило, что в check_variables для
    «Проверки КП» - мусор в КП не равно пустому полю (подтверждено 2026-07-30
    после того, как первая версия ошибочно давала «–» вместо ✗)."""
    row = KPRow(domain='stalmetural.ru', city='Тест', phone_common='2',
               phone_seo='+7 495 222-22-22', all_phones='4952222222')
    card = dict(_CARD_MATCH, phone='+7 (499) 130-36-69')
    r = mc.compare('yandex', 'Тест', 'u', card, row)
    assert r.phone_match is False
    assert r.is_error
    d = next(d for d in r.details if d['field'] == 'телефон')
    assert d['kp'] == '2'
    print('✓ мусор («2») в Общем → ✗, не «–»')


# ── «отсутствует» vs «не совпал» - одна и та же логика для телефона/адреса/
# сайта: на карточке ПУСТО (значения нет вовсе) - это не «другое значение»,
# формулировка должна отличаться от случая, когда карточка нашла ЧТО-ТО, но
# не то, что в КП.


def test_phone_absent_on_card_says_missing_not_mismatch():
    card = dict(_CARD_MATCH, phone='')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    assert r.phone_match is False
    issue = next(i for i in r.issues if 'телефон' in i)
    assert 'отсутствует' in issue
    assert 'не совпал' not in issue
    print('✓ телефона на карточке нет вовсе → «отсутствует», не «не совпал»')


def test_phone_different_value_on_card_says_mismatch_not_missing():
    card = dict(_CARD_MATCH, phone='+7 000 000-00-00')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    issue = next(i for i in r.issues if 'телефон' in i)
    assert 'не совпал' in issue
    assert 'отсутствует' not in issue
    print('✓ на карточке ДРУГОЙ телефон → «не совпал», не «отсутствует»')


def test_address_absent_on_card_says_missing():
    card = dict(_CARD_MATCH, address='')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    issue = next(i for i in r.issues if 'адрес' in i)
    assert 'отсутствует' in issue
    print('✓ адреса на карточке нет вовсе → «отсутствует»')


def test_site_absent_on_card_says_missing():
    card = dict(_CARD_MATCH, site='')
    r = mc.compare('yandex', 'Москва', 'u', card, _KP_MOSCOW)
    issue = next(i for i in r.issues if 'сайт' in i)
    assert 'отсутствует' in issue
    print('✓ сайта на карточке нет вовсе → «отсутствует»')


def test_genuinely_empty_common_is_dash_not_error():
    """А вот РОВНО пустое поле (не мусор, а пусто) - сверять действительно
    нечего: phone_match=None, не ✗. Отличие от предыдущего теста - здесь
    пустая строка, а не текст-не-номер."""
    row = KPRow(domain='stalmetural.ru', city='Тест', phone_common='',
               phone_seo='+7 495 222-22-22', all_phones='4952222222')
    card = dict(_CARD_MATCH, phone='+7 (499) 130-36-69')
    r = mc.compare('yandex', 'Тест', 'u', card, row)
    assert r.phone_match is None
    assert not any(d['field'] == 'телефон' for d in r.details)
    print('✓ по-настоящему пустое поле Общий → phone_match=None (нечего сравнивать)')
