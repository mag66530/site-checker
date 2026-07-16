"""Тесты источника Sitemap и слияния источников для «404 в индексе» (без сети)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_sitemap_checker as sm
from index_export_parser import merge_index_404


def test_window_rotation_covers_all():
    """Окно по дате покрывает весь список за n_windows дней и зацикливается."""
    urls = [f'u{i}' for i in range(25)]
    w0, tot = sm._window(urls, 10, 0)
    w1, _ = sm._window(urls, 10, 1)
    w2, _ = sm._window(urls, 10, 2)
    w3, _ = sm._window(urls, 10, 3)
    assert tot == 25
    assert w0 == [f'u{i}' for i in range(0, 10)]
    assert w1 == [f'u{i}' for i in range(10, 20)]
    assert w2 == [f'u{i}' for i in range(20, 25)]
    assert w3 == w0                         # 3 окна → день 3 == день 0
    # объединение первых 3 окон = весь список
    assert set(w0) | set(w1) | set(w2) == set(urls)


def test_window_small_sitemap():
    """Если список меньше окна - берём целиком, без ротации."""
    urls = ['a', 'b', 'c']
    w, tot = sm._window(urls, 100, 7)
    assert w == urls and tot == 3


def test_host_of():
    assert sm._host_of('https://www.stalmetural.ru/catalog/x/') == 'stalmetural.ru'
    assert sm._host_of('https://smg.az/y') == 'smg.az'


def test_merge_dedup_and_sources():
    """Слияние источников: total суммируется, доступность — хоть один,
    источники доступных перечислены."""
    ya = {'available': True, 'source': 'yandex_export', 'total_checked': 100,
          'total_dead': 1, 'total_soft': 0, 'hosts': [
              {'host': 'a.ru', 'checked': 100, 'ok': 99, 'redirects': 0,
               'in_index_total': 80, 'soft': [],
               'dead': [{'url': 'https://a.ru/x', 'status': '404',
                         'source': 'Яндекс'}], 'errors': []}]}
    smr = {'available': True, 'source': 'sitemap', 'total_checked': 50,
           'total_dead': 1, 'total_soft': 0, 'hosts': [
               {'host': 'a.ru', 'checked': 50, 'ok': 49, 'redirects': 0,
                'in_index_total': 0, 'soft': [],
                'dead': [{'url': 'https://a.ru/y', 'status': '410',
                          'source': 'Sitemap'}], 'errors': []}]}
    m = merge_index_404(ya, smr)
    assert m['available'] is True
    assert m['total_checked'] == 150
    assert m['total_dead'] == 2
    assert m['sources'] == ['Яндекс', 'Sitemap']
    assert len(m['hosts']) == 1 and m['hosts'][0]['host'] == 'a.ru'
    assert len(m['hosts'][0]['dead']) == 2      # объединены


def test_merge_skips_failed_source_in_sources_list():
    """Недоступный источник (нет сессии) не попадает в список источников,
    но общий результат доступен за счёт второго."""
    failed = {'available': False, 'source': 'yandex_export',
              'error': 'нет сессии', 'hosts': []}
    smr = {'available': True, 'source': 'sitemap', 'total_checked': 10,
           'total_dead': 0, 'total_soft': 0, 'hosts': [
               {'host': 'a.ru', 'checked': 10, 'ok': 10, 'redirects': 0,
                'in_index_total': 0, 'soft': [], 'dead': [], 'errors': []}]}
    m = merge_index_404(failed, smr)
    assert m['available'] is True
    assert m['sources'] == ['Sitemap']
    assert m['error'] is None            # есть доступный источник → не ошибка


def test_merge_all_none():
    assert merge_index_404(None, None) is None


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
