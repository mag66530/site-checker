"""Тесты kp.py – сверка контактов сайта с КП."""
import pytest

from kp import (
    normalize_phone, split_phones, address_match, KPRow,
    check_against_kp, load_kp, _norm_host,
)


# ── Нормализация телефона ────────────────────────────────────────────


def test_normalize_phone_formats():
    for s in ['+7 (499) 130-60-28', '7 (499) 130-60-28', '8-499-130-60-28',
              '74991306028', '+7 499 130 60 28']:
        assert normalize_phone(s) == '4991306028', s


def test_split_phones_multiple():
    txt = 'Звоните: +7 (499) 130-60-28 или 8 (903) 130-36-69'
    assert set(split_phones(txt)) == {'4991306028', '9031303669'}


def test_normalize_phone_countries_and_float():
    # Узбекистан (+998 → 9 цифр), в т.ч. число из Excel с «.0»
    assert normalize_phone('998 (90) 006-84-48') == '900068448'
    assert normalize_phone('998900068448.0') == '900068448'
    # Беларусь (+375 → 9 цифр)
    assert normalize_phone('+375 (44) 588-81-48') == '445888148'
    # Казахстан (+7 → 10 цифр)
    assert normalize_phone('7 (727) 354-08-98') == '7273540898'
    assert normalize_phone('8-708-987-98-15') == '7089879815'


def test_split_phones_uzbek_tel_link():
    assert split_phones('tel:998900068448') == ['900068448']


# ── Мягкое сравнение адреса ──────────────────────────────────────────


def test_address_match_abbreviations():
    # «проспект» vs «пр.», латинская c вместо кириллической с
    assert address_match('Рязанский пр., 86/1c1', 'Рязанский проспект, 86/1с1')


def test_address_match_street_and_number():
    assert address_match('г. Москва, улица Люблинская, 151', 'улица Люблинская, 151')
    assert not address_match('улица Тверская, 5', 'улица Люблинская, 151')


def test_address_match_different_house():
    # та же улица, другой дом → не совпало
    assert not address_match('улица Люблинская, 99', 'улица Люблинская, 151')


# ── Приоритет телефона SEO → реклама → общий ─────────────────────────


def test_expected_phone_priority():
    r = KPRow(domain='x.ru', city='Тест',
              phone_seo='+7 (499) 111-11-11',
              phone_ad='+7 (499) 222-22-22',
              phone_common='+7 (499) 333-33-33')
    assert r.expected_phone() == ('4991111111', 'SEO')

    r2 = KPRow(domain='x.ru', city='Тест', phone_seo='',
               phone_ad='+7 (499) 222-22-22', phone_common='+7 (499) 333-33-33')
    assert r2.expected_phone() == ('4992222222', 'Реклама')

    r3 = KPRow(domain='x.ru', city='Тест', phone_common='+7 (499) 333-33-33')
    assert r3.expected_phone() == ('4993333333', 'Общий')

    r4 = KPRow(domain='x.ru', city='Тест')
    assert r4.expected_phone() == ('', 'critical')


# ── Сверка страницы с КП ─────────────────────────────────────────────


def _kp(**kw):
    row = KPRow(domain='x.ru', city='Москва', **kw)
    return {'x.ru': row}


HEAD_FOOT = (
    '<header><a href="tel:{ph}">{ph_disp}</a></header>'
    '<footer><a href="mailto:{em}">{em}</a><span>{addr}</span></footer>'
)


def _page(ph='+74991306028', ph_disp='+7 (499) 130-60-28',
          em='msk@x.ru', addr='Москва, улица Ленина, 5'):
    return HEAD_FOOT.format(ph=ph, ph_disp=ph_disp, em=em, addr=addr)


def test_all_match():
    kp = _kp(phone_seo='+7 (499) 130-60-28', email='msk@x.ru',
             address='улица Ленина, 5')
    res = check_against_kp(_page(), 'x.ru', kp)
    assert res.matched_kp and not res.has_issues


def test_phone_mismatch_is_bug_with_comment():
    # На сайте номер, которого нет среди номеров города в КП → баг
    kp = _kp(phone_seo='+7 (499) 999-99-99', all_phones='4999999999',
             email='msk@x.ru', address='улица Ленина, 5')
    res = check_against_kp(_page(), 'x.ru', kp)
    phone_issue = next(i for i in res.issues if i['field'] == 'Телефон')
    assert phone_issue['status'] == 'bug'
    assert 'нет в КП' in phone_issue['comment']


def test_phone_ok_when_matches_any_city_number():
    """Случай Воронежа: SEO пустой, сайт показывает «Общий» – он в КП → ОК."""
    kp = _kp(phone_seo='', phone_ad='+7 (962) 388-79-12',
             phone_common='+7 (499) 130-60-28',
             all_phones='4991306028;9623887912',
             email='msk@x.ru', address='улица Ленина, 5')
    res = check_against_kp(_page(), 'x.ru', kp)  # сайт показывает 499 130-60-28
    phone_issue = next(i for i in res.issues if i['field'] == 'Телефон')
    assert phone_issue['status'] == 'ok'


def test_phone_critical_when_kp_has_none():
    kp = _kp(email='msk@x.ru', address='улица Ленина, 5')  # без телефонов
    res = check_against_kp(_page(), 'x.ru', kp)
    phone_issue = next(i for i in res.issues if i['field'] == 'Телефон')
    assert phone_issue['status'] == 'critical'


def test_email_mismatch_is_bug():
    kp = _kp(phone_seo='+7 (499) 130-60-28', email='other@x.ru',
             address='улица Ленина, 5')
    res = check_against_kp(_page(), 'x.ru', kp)
    assert any(i['field'] == 'Почта' and i['status'] == 'bug' for i in res.issues)


def test_address_mismatch_is_bug():
    kp = _kp(phone_seo='+7 (499) 130-60-28', email='msk@x.ru',
             address='проспект Мира, 100')
    res = check_against_kp(_page(), 'x.ru', kp)
    assert any(i['field'] == 'Адрес' and i['status'] == 'bug' for i in res.issues)


def test_phone_branch_match_other_city_ok():
    """Филиальная модель: город показывает номер другого города из КП → ок."""
    kp = {
        'aktau.x.ru': KPRow(domain='aktau.x.ru', city='Актау',
                            phone_seo='8-708-987-98-15', all_phones='7089879815'),
        'almaty.x.ru': KPRow(domain='almaty.x.ru', city='Алматы',
                             phone_seo='7 (727) 354-08-98', all_phones='7273540898'),
    }
    # На сайте Актау – номер Алматы (обслуживающий филиал)
    page = HEAD_FOOT.format(ph='+77273540898', ph_disp='7 (727) 354-08-98',
                            em='aktau@x.ru', addr='Микрорайон 19А, 32/1')
    res = check_against_kp(page, 'aktau.x.ru', kp)
    phone = next(i for i in res.issues if i['field'] == 'Телефон')
    assert phone['status'] == 'ok'


def test_non_email_kp_value_not_compared():
    """Если в КП в поле почты не e-mail («надо заказывать») – с ним не сверяем
    (не баг). Но раз на сайте почта есть – показываем её как «есть» (info)."""
    kp = _kp(phone_seo='+7 (499) 130-60-28', all_phones='4991306028',
             email='надо заказывать', address='улица Ленина, 5')
    res = check_against_kp(_page(), 'x.ru', kp)
    mail = [i for i in res.issues if i['field'] == 'Почта']
    assert mail and mail[0]['status'] == 'info'   # не баг, не сверка с заметкой
    assert not res.has_issues


def test_email_present_on_site_but_absent_in_kp_is_info():
    """В КП почты для города нет, а на сайте она есть → статус «info» («есть»),
    не «–» и не баг. Не считается расхождением."""
    kp = _kp(phone_seo='+7 (499) 130-60-28', all_phones='4991306028',
             email='', address='улица Ленина, 5')
    res = check_against_kp(_page(em='msk@x.ru'), 'x.ru', kp)
    mail = next(i for i in res.issues if i['field'] == 'Почта')
    assert mail['status'] == 'info'
    assert not res.has_issues          # info – не расхождение


def test_email_absent_both_sides_no_issue():
    """Ни в КП, ни на сайте почты нет → по почте вообще нет строки (в отчёте «–»)."""
    kp = _kp(phone_seo='+7 (499) 130-60-28', all_phones='4991306028',
             email='', address='улица Ленина, 5')
    page = HEAD_FOOT.format(ph='+74991306028', ph_disp='+7 (499) 130-60-28',
                            em='', addr='улица Ленина, 5').replace(
        '<a href="mailto:"></a>', '')
    res = check_against_kp(page, 'x.ru', kp)
    assert all(i['field'] != 'Почта' for i in res.issues)


def test_unknown_domain_no_match():
    kp = _kp(phone_seo='+7 (499) 130-60-28')
    res = check_against_kp(_page(), 'unknown.ru', kp)
    assert not res.matched_kp and not res.has_issues


def test_norm_host_strips_www_and_scheme():
    assert _norm_host('https://www.spb.inmetprom.ru/') == 'spb.inmetprom.ru'
    assert _norm_host('mepen.ru') == 'mepen.ru'


# ── Реальные базы КП в репозитории (если уже сконвертированы) ─────────


@pytest.mark.parametrize('proj', ['smu', 'imp', 'mpe'])
def test_kp_csv_loads(proj):
    kp = load_kp(proj)
    if not kp:
        pytest.skip(f'{proj}-kp.csv ещё не сгенерирован')
    # У каждой записи есть домен и хотя бы город
    for dom, row in list(kp.items())[:5]:
        assert row.domain and '.' in row.domain


def test_contacts_page_addresses_vs_kp():
    """Сверка адресов всех городов на странице «Контакты» с КП: совпадение,
    расхождение и нормализация города ё=е."""
    from kp import check_contacts_addresses
    html = ('<div><b>Москва</b><br> улица Ленина, 5</div>'
            '<div><b>Уфа</b><br> проспект Мира, 10</div>'
            '<div><b>Орел</b><br> улица Победы, 1</div>')
    kp = {
        'msk':  KPRow(domain='msk', city='Москва', address='улица Ленина, 5'),
        'ufa':  KPRow(domain='ufa', city='Уфа', address='улица Гагарина, 99'),
        'orel': KPRow(domain='orel', city='Орёл', address='улица Победы, 1'),
    }
    res = check_contacts_addresses(html, kp)
    assert res['on_page'] == 3
    assert res['matched'] == 2          # Москва + Орёл (ё=е)
    assert len(res['mismatched']) == 1 and res['mismatched'][0]['city'] == 'Уфа'
    assert res['not_in_kp'] == []
