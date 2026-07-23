"""Тесты calltracking_checker - статическая проверка замены рекламного
номера (коллтрекинг Sipuni): разбор пула номеров из init-конфига и сверка
рекламного номера с phone_ad из КП."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calltracking_checker import parse_config, check_ad_number, _nat

# Реальный init-вызов Sipuni с главной СМУ (Москва): рекламный номер
# 74991300786 = phone_ad «7 (499) 130-07-86».
SMU_SNIPPET = '''
<script src="/local/templates/uralmetall/assets/js/sipuni-calltracking.js"></script>
<span class="ct_phone">+7 (499) 130-60-28</span>
<script>
    sipuniCalltracking({
        sources: {
            'yadirect': {'utm_source': 'yandex'},
            'googleads': {'utm_source': 'google'}
        },
        phones: [
            {'src': 'yadirect', 'phone': ['74991300786']},
            {'src': 'googleads', 'phone': ['74991300786']}
        ],
        pattern: '+# (###) ###-##-##'
    }, window);
</script>
'''


def test_nat_normalizes():
    assert _nat('7 (499) 130-07-86') == '4991300786'
    assert _nat('84991300786') == '4991300786'
    assert _nat('+7 499 130 07 86') == '4991300786'
    # СНГ: Киргизия (+996) и Азербайджан (+994) → 9 цифр (как +375/+998)
    assert _nat('+996 221 31 88 82') == '221318882'
    assert _nat('+994 12 345 67 89') == '123456789'
    print('✓ нормализация номера к 10 цифрам (СНГ - к 9)')


def test_parse_real_sipuni_config():
    cfg = parse_config(SMU_SNIPPET)
    assert cfg['has_script'] is True
    assert cfg['has_init'] is True
    assert cfg['ad_numbers'] == {'4991300786'}
    print('✓ разбор реального конфига Sipuni: скрипт, init, пул номеров')


def test_ad_number_matches_kp():
    res = check_ad_number(SMU_SNIPPET, '7 (499) 130-07-86')
    assert res['status'] == 'ok', res
    assert res['kp'] == '4991300786'
    print('✓ рекламный номер в конфиге совпал с phone_ad из КП → ok')


def test_ad_number_mismatch_is_bug():
    # В КП другой рекламный номер, чем настроен на сайте.
    res = check_ad_number(SMU_SNIPPET, '7 (499) 999-99-99')
    assert res['status'] == 'bug', res
    assert '4991300786' in res['configured']
    print('✓ номер в конфиге ≠ phone_ad из КП → bug')


def test_no_calltracking_is_na():
    html = '<html><body><span>+7 (499) 130-60-28</span></body></html>'
    res = check_ad_number(html, '7 (499) 130-07-86')
    assert res['status'] == 'na', res
    assert 'не обнаружена' in res['comment']
    print('✓ подмены нет на странице → na (нейтрально, не баг)')


def test_no_kp_ad_returns_none():
    assert check_ad_number(SMU_SNIPPET, '') is None
    assert check_ad_number(SMU_SNIPPET, None) is None
    print('✓ в КП нет рекламного номера → None (сверять не с чем)')


def test_script_present_pool_unparsed_is_na():
    # Скрипт есть, но пул номеров грузится иначе (не разобрали) - мягко.
    html = ('<script src="/js/sipuni-calltracking.js"></script>'
            '<script>sipuniCalltracking(window.CFG, window);</script>')
    res = check_ad_number(html, '7 (499) 130-07-86')
    assert res['status'] == 'na', res
    print('✓ скрипт есть, пул не разобран → na (проверить вручную)')


if __name__ == '__main__':
    test_nat_normalizes()
    test_parse_real_sipuni_config()
    test_ad_number_matches_kp()
    test_ad_number_mismatch_is_bug()
    test_no_calltracking_is_na()
    test_no_kp_ad_returns_none()
    test_script_present_pool_unparsed_is_na()
    print('Все тесты calltracking_checker пройдены.')
