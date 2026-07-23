"""Тесты calltracking_browser (уровень 2, браузерная проверка замены
рекламного номера) - чистая логика без браузера: нормализация номера,
извлечение номеров из .ct_phone и вердикт check_city через стаб-страницу."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calltracking_browser import (_nat, _read_ct_numbers, check_city,
                                   check_city_seo, AD_PARAM, SEO_REFERER)


def test_nat():
    assert _nat('+7 (499) 130-07-86') == '4991300786'
    assert _nat('8 499 130 07 86') == '4991300786'
    assert _nat('') == ''
    print('✓ нормализация номера')


class _StubPage:
    """Мини-стаб Playwright-страницы: eval_on_selector_all возвращает
    заранее заданные тексты .ct_phone; goto/wait_for_timeout - заглушки."""
    def __init__(self, ct_texts):
        self._ct = ct_texts
        self.last_url = None

    def goto(self, url, **kw):
        self.last_url = url
        self.last_referer = kw.get('referer')

    def wait_for_timeout(self, ms):
        pass

    def eval_on_selector_all(self, selector, js):
        return list(self._ct)


def test_read_ct_numbers():
    pg = _StubPage(['+7 (499) 130-07-86|tel:+74991300786',
                    'звоните: 8 (499) 130-60-28'])
    nums = _read_ct_numbers(pg)
    assert nums == {'4991300786', '4991306028'}, nums
    print('✓ извлечение номеров из .ct_phone (текст + tel:)')


def test_check_city_replaced_ok():
    # На странице показан рекламный номер → подмена сработала.
    pg = _StubPage(['+7 (499) 130-07-86'])
    r = check_city(pg, 'https://x.ru/', _nat('7 (499) 130-07-86'))
    assert r['status'] == 'replaced_ok', r
    assert AD_PARAM in pg.last_url          # открыли с рекламной меткой
    print('✓ рекламный номер на странице → replaced_ok, метка добавлена в URL')


def test_check_city_not_replaced():
    # Показан SEO-номер, рекламного нет → подмена НЕ сработала.
    pg = _StubPage(['+7 (499) 130-60-28'])
    r = check_city(pg, 'https://x.ru/?a=1', _nat('7 (499) 130-07-86'))
    assert r['status'] == 'not_replaced', r
    assert '4991306028' in r['shown']
    assert pg.last_url.count('?') == 1 and AD_PARAM in pg.last_url  # метка через &
    print('✓ остался SEO-номер → not_replaced; метка добавлена через &')


def test_check_city_no_element():
    pg = _StubPage([])                       # .ct_phone нет
    r = check_city(pg, 'https://x.ru/', _nat('7 (499) 130-07-86'))
    assert r['status'] == 'no_element', r
    print('✓ нет .ct_phone → no_element')


def test_check_city_seo_replaced_ok():
    # SEO-подмена: открываем БЕЗ метки, но с реферрером органики; на странице
    # показан поисковый номер → сработала.
    pg = _StubPage(['+7 (499) 130-60-28'])
    r = check_city_seo(pg, 'https://x.ru/', _nat('7 (499) 130-60-28'))
    assert r['status'] == 'replaced_ok', r
    assert pg.last_referer == SEO_REFERER            # передан реферрер поиска
    assert AD_PARAM not in (pg.last_url or '')       # рекламной метки НЕТ
    print('✓ SEO: поисковый номер + реферрер органики → replaced_ok, без метки')


def test_check_city_seo_not_replaced():
    # Показан общий номер, поискового нет → SEO-подмена не сработала.
    pg = _StubPage(['+7 (499) 130-36-69'])
    r = check_city_seo(pg, 'https://x.ru/', _nat('7 (499) 130-60-28'))
    assert r['status'] == 'not_replaced', r
    assert '4991303669' in r['shown']
    print('✓ SEO: остался общий номер → not_replaced')


if __name__ == '__main__':
    test_nat()
    test_read_ct_numbers()
    test_check_city_replaced_ok()
    test_check_city_not_replaced()
    test_check_city_no_element()
    test_check_city_seo_replaced_ok()
    test_check_city_seo_not_replaced()
    print('Все тесты calltracking_browser пройдены.')
