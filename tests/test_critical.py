"""Тесты critical – выделение критических ошибок прогона (п.4.3)."""
import types

from critical import analyze, is_availability_down
from telegram_notify import format_critical_alert, format_critical_block


def _r(**kw):
    d = dict(city='Москва', url='https://x.ru/p/', status='ok', is_ok=True,
             is_error=False, type_code='product', content=None,
             kp_result=None, has_text_issues=False, text_issues=[])
    d.update(kw)
    return types.SimpleNamespace(**d)


def _content(is_soft_404=False, page_kind='', bug_keys=()):
    bugs = [types.SimpleNamespace(key=k) for k in bug_keys]
    return types.SimpleNamespace(is_soft_404=is_soft_404, page_kind=page_kind, bugs=bugs)


def test_availability_server_down():
    for st in ('server_error', 'timeout', 'network_error'):
        r = _r(status=st, is_ok=False, is_error=True, type_code='product')
        assert is_availability_down(r)
        assert len(analyze([r]).availability) == 1


def test_availability_main_any_error():
    r = _r(status='not_found', is_ok=False, is_error=True, type_code='main')
    s = analyze([r])
    assert len(s.availability) == 1
    assert s.availability[0].page == 'Главная'
    assert s.availability[0].detail == '404 не найдена'


def test_404_not_main_goes_to_not_found_not_availability():
    r = _r(status='not_found', is_ok=False, is_error=True, type_code='product')
    s = analyze([r])
    assert not s.availability
    assert len(s.others['not_found']) == 1 and s.others['not_found'][0].detail == '404 не найдена'


def test_soft_404():
    r = _r(content=_content(is_soft_404=True))
    s = analyze([r])
    assert len(s.others['not_found']) == 1 and 'заглушка' in s.others['not_found'][0].detail


def test_kp_mismatch_only_bug_fields():
    r = _r(kp_result={'has_issues': True, 'issues': [
        {'field': 'Телефон', 'status': 'bug'},
        {'field': 'Адрес', 'status': 'ok'}]})
    s = analyze([r])
    assert len(s.others['kp']) == 1
    assert 'Телефон' in s.others['kp'][0].detail and 'Адрес' not in s.others['kp'][0].detail


def test_cannot_buy_empty_section():
    s = analyze([_r(content=_content(page_kind='empty'))])
    assert any('пустой' in it.detail for it in s.others['cannot_buy'])


def test_cannot_buy_no_price_or_button():
    s = analyze([_r(content=_content(bug_keys=('price', 'btn_order')))])
    assert len(s.others['cannot_buy']) == 1
    d = s.others['cannot_buy'][0].detail
    assert 'цены' in d and 'кнопки' in d


def test_text_issues():
    s = analyze([_r(has_text_issues=True, text_issues=[1, 2, 3])])
    assert len(s.others['text']) == 1 and '3' in s.others['text'][0].detail


def test_cancelled_skipped():
    assert analyze([_r(status='cancelled', is_ok=False, is_error=False)]).total == 0


def test_total_and_formatters():
    rs = [
        _r(status='server_error', is_ok=False, is_error=True, type_code='main', city='Москва'),
        _r(status='timeout', is_ok=False, is_error=True, type_code='product', city='Москва',
           url='https://msk.x.ru/catalog/a/t/'),
        _r(status='not_found', is_ok=False, is_error=True, type_code='product', city='Уфа'),
        _r(has_text_issues=True, text_issues=[1], city='Омск'),
    ]
    s = analyze(rs)
    assert s.total == 4 and s.has_availability
    alert = format_critical_alert('СМУ – Сталметурал', s.availability)
    # без эмодзи и длинных тире, группировка по городу, конкретная ошибка
    assert '🔴' not in alert and '—' not in alert
    assert 'Упала доступность' in alert and 'СМУ' in alert
    assert '<b>Москва</b>' in alert and 'Главная: сервер не отвечает (5xx)' in alert
    block = format_critical_block(s)
    assert '🔴' not in block and '—' not in block
    # группировка по городу, каждая проблема на своей строке
    assert 'Критические (4)' in block and '<b>Москва</b>' in block
    assert 'Главная: сервер не отвечает (5xx)' in block


def test_no_critical_empty_block():
    s = analyze([_r()])   # обычная рабочая страница
    assert s.total == 0 and not s.has_any
    assert format_critical_block(s) == ''
