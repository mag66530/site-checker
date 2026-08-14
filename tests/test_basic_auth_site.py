"""Вход по паролю на закрытый сайт (новый прод МПИ за nginx-паролем).

Главное требование: пароль уходит ТОЛЬКО на домен проекта. Проверка битых
ссылок звонит и на чужие адреса - session-level auth в aiohttp утёк бы вместе
с каждым таким запросом.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import aiohttp

import http_checker as HC
from sources import load_project_config, load_sources


def test_заголовок_собирается_верно():
    assert HC._basic_header('admin', 'secret') == 'Basic YWRtaW46c2VjcmV0'
    print('✓ Basic-заголовок кодируется как надо')


def test_без_пароля_обычная_сессия():
    import asyncio

    async def _проверить():
        s = HC._сделать_сессию({}, aiohttp.TCPConnector(), None)
        try:
            assert type(s) is aiohttp.ClientSession
        finally:
            await s.close()

    asyncio.run(_проверить())
    print('✓ обычным проектам достаётся прежняя сессия')


def test_с_паролем_особая_сессия():
    import asyncio

    async def _проверить():
        s = HC._сделать_сессию({}, aiohttp.TCPConnector(),
                               {'host': 'new.example.by',
                                'login': 'admin', 'password': 'p'})
        try:
            assert isinstance(s, HC._СессияСПаролем)
            assert s._auth_host == 'new.example.by'
            assert s._auth_header.startswith('Basic ')
        finally:
            await s.close()

    asyncio.run(_проверить())
    print('✓ закрытому сайту - сессия с паролем')


def test_пароль_только_своему_хосту():
    """Ключевая проверка: чужие домены заголовка не получают."""
    свои = ('https://new.example.by/page', 'https://a.new.example.by/x')
    чужие = ('https://example.com/', 'https://newexample.by/',
             'https://evil-new.example.by.attacker.tld/')
    хост_проекта = 'new.example.by'

    def _подпадает(url):
        хост = (HC.urlsplit(url).hostname or '').lower()
        return хост == хост_проекта or хост.endswith('.' + хост_проекта)

    assert all(_подпадает(u) for u in свои)
    assert not any(_подпадает(u) for u in чужие)
    print('✓ пароль уходит своему домену и его поддоменам, чужим - нет')


def test_проект_нового_прода_заведён():
    cfg = load_project_config('mpinew')

    assert cfg['name'] == 'МПИ - новый прод'
    assert cfg['basic_auth'] is True
    assert cfg['root_domain'] == 'new.metpromintex.by'
    # Каталог переиспользуется от старого МПИ - структура адресов та же.
    assert cfg['catalog_csv'] == 'catalogs/mpi-catalog.csv'
    print('✓ карточка проекта на месте')


def test_план_прогона_строится():
    cfg = load_project_config('mpinew')
    src = load_sources(cfg)

    assert len(src.subdomains) == 1
    assert src.categories and src.filters
    print(f'✓ план строится: {len(src.categories)} категорий, '
          f'{len(src.filters)} фильтров')


def test_другие_проекты_без_входа():
    """Флага basic_auth нет ни у кого, кроме нового прода."""
    from sources import list_projects

    с_входом = [p['id'] for p in list_projects()
                if (load_project_config(p['id']) or {}).get('basic_auth')]

    assert с_входом == ['mpinew'], с_входом
    print('✓ у остальных проектов обход идёт как раньше')
