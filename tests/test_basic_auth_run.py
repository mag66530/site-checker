"""Вход на закрытый сайт действует на ВЕСЬ прогон чек-листа.

Основной обход - только часть прогона: robots, sitemap, 404-страница, дубли
главной, поиск и нагрузка ходят своими клиентами. Раньше пароль знал только
обход, и по этим проверкам закрытый стенд отвечал 401 - отчёт состоял из
ошибок доступа.

Главное требование прежнее: пароль уходит ТОЛЬКО своему домену.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import http_checker as HC


def _включить(host='new.example.by'):
    HC.установить_вход_прогона({'host': host, 'login': 'admin',
                                'password': 'секрет'})


def _выключить():
    HC.установить_вход_прогона(None)


def test_без_входа_заголовков_нет():
    _выключить()
    try:
        assert HC.заголовок_входа('https://new.example.by/') == {}
        # Обычные проекты получают ровно те же заголовки, что и раньше.
        assert 'Authorization' not in HC.make_browser_headers(
            url='https://new.example.by/')
    finally:
        _выключить()
    print('✓ без входа ничего не меняется')


def test_вход_уходит_только_своему_домену():
    _включить()
    try:
        свои = ('https://new.example.by/', 'https://a.new.example.by/x',
                'https://new.example.by/sitemap.xml')
        чужие = ('https://example.com/', 'https://newexample.by/',
                 'https://new.example.by.attacker.tld/')
        assert all(HC.заголовок_входа(u) for u in свои)
        assert not any(HC.заголовок_входа(u) for u in чужие)
    finally:
        _выключить()
    print('✓ пароль знает свой домен и поддомены, чужим не уходит')


def test_заголовки_браузера_получают_вход():
    _включить()
    try:
        свои = HC.make_browser_headers(url='https://new.example.by/page')
        чужие = HC.make_browser_headers(url='https://example.com/page')
        без_адреса = HC.make_browser_headers()

        assert свои['Authorization'].startswith('Basic ')
        assert 'Authorization' not in чужие
        assert 'Authorization' not in без_адреса   # адрес не назвали - не гадаем
        # Остальной набор не пострадал: анти-бот проверки смотрят на него.
        assert свои['User-Agent'] and свои['Sec-Fetch-Mode'] == 'navigate'
    finally:
        _выключить()
    print('✓ make_browser_headers подмешивает вход только своему адресу')


def test_заголовки_sitemap_получают_вход():
    from sitemap import _sitemap_headers

    _включить()
    try:
        свои = _sitemap_headers(url='https://new.example.by/sitemap.xml')
        чужие = _sitemap_headers(url='https://example.com/sitemap.xml')

        assert свои['Authorization'].startswith('Basic ')
        assert 'Authorization' not in чужие
        assert свои['Accept'].startswith('application/xml')
    finally:
        _выключить()
    print('✓ карты сайта закрытого стенда качаются с паролем')


def test_вход_собирается_из_карточки_и_доступов():
    cfg = {'basic_auth': True, 'root_domain': 'New.Example.BY'}

    вход = HC.basic_auth_for(cfg, {'site_basic_login': 'admin',
                                   'site_basic_password': 'p'})

    assert вход == {'host': 'new.example.by', 'login': 'admin', 'password': 'p'}
    # Обычный проект - без входа, даже если логин зачем-то передали.
    assert HC.basic_auth_for({'root_domain': 'x.ru'},
                             {'site_basic_login': 'admin'}) is None
    # Флаг есть, а логина нет - тоже None (иначе слали бы пустой Basic).
    assert HC.basic_auth_for(cfg, {}) is None
    print('✓ вход берётся из карточки проекта и доступов прогона')


def test_прогон_ставит_вход_до_первых_запросов():
    """runner_30min должен включить вход СРАЗУ: часть проверок (sitemap,
    товары) идёт раньше основного обхода."""
    текст = (КОРЕНЬ / 'runner_30min.py').read_text(encoding='utf-8')

    assert 'установить_вход_прогона' in текст
    assert 'basic_auth=basic_auth' in текст, 'обход не получает вход'
    место_входа = текст.index('_установить_вход(basic_auth)')
    место_обхода = текст.index('results = asyncio.run(run_batch(')
    assert место_входа < место_обхода, 'вход ставится позже первых запросов'
    print('✓ вход включается до первых запросов прогона')


def test_проверки_чек_листа_знают_про_вход():
    """Модули, которые ходят на сайт своим клиентом, должны просить заголовки
    С АДРЕСОМ - иначе на закрытом стенде они получат 401."""
    for файл in ('page404_checker.py', 'home_dupes_checker.py',
                 'indexing_checker.py', 'search_check.py', 'meta_checker.py',
                 'stress_checker.py', 'index_pages_checker.py',
                 'index_reverify.py'):
        текст = (КОРЕНЬ / файл).read_text(encoding='utf-8')
        assert 'make_browser_headers(url=' in текст, f'{файл}: заголовки без адреса'
    for файл in ('sitemap_audit.py', 'sitemap_sampling.py'):
        текст = (КОРЕНЬ / файл).read_text(encoding='utf-8')
        assert '_sitemap_headers(url=' in текст, f'{файл}: заголовки без адреса'
    print('✓ отдельные проверки чек-листа тоже входят по паролю')
