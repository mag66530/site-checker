"""Тест живой перепроверки «404 в индексе» (без сети — _check_all замокан)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_reverify as rv


def _check(monkeyverdicts):
    async def fake(urls, proxy):
        return {u: monkeyverdicts[u] for u in urls}
    return fake


def test_reverify_drops_false_positives(monkeypatch):
    """404 остаётся, 200 (уже работает) и таймаут — убираются; 5xx остаётся."""
    monkeypatch.setattr(rv, '_check_all', _check({
        'https://s/a': ('dead', 404),      # реально битая
        'https://s/b': ('ok', 200),        # уже починили → убрать
        'https://s/c': ('server', 500),    # реально ошибка сервера
        'https://s/d': ('timeout', None),  # медленная → не подтвердилось
    }))
    check = {'available': True, 'source': 'combo', 'sources': ['Яндекс'],
             'total_checked': 100, 'total_dead': 2, 'total_soft': 0, 'error': None,
             'hosts': [{'host': 's', 'checked': 100, 'ok': 96, 'redirects': 0,
                        'in_index_total': 50, 'soft': [],
                        'dead': [{'url': 'https://s/a', 'status': '404', 'source': 'Яндекс'},
                                 {'url': 'https://s/b', 'status': '404', 'source': 'Яндекс'}],
                        'errors': [{'url': 'https://s/c', 'status': '500', 'source': 'Яндекс'},
                                   {'url': 'https://s/d', 'status': None, 'source': 'Sitemap'}]}]}
    out = rv.reverify_index_404(check, log=None)
    assert out['reverified'] is True
    h = out['hosts'][0]
    assert [e['url'] for e in h['dead']] == ['https://s/a']
    assert [e['url'] for e in h['errors']] == ['https://s/c']
    assert out['total_dead'] == 1
    # живой код проставлен
    assert h['dead'][0]['status'] == '404'
    assert h['errors'][0]['status'] == '500'


def test_reverify_all_fixed_gives_empty(monkeypatch):
    """Если все кандидаты уже работают (200) — хостов не остаётся."""
    monkeypatch.setattr(rv, '_check_all', _check({
        'https://s/a': ('ok', 200), 'https://s/b': ('ok', 200)}))
    check = {'available': True, 'source': 'combo', 'sources': ['Яндекс'],
             'total_checked': 10, 'total_dead': 2, 'total_soft': 0, 'error': None,
             'hosts': [{'host': 's', 'checked': 10, 'ok': 8, 'redirects': 0,
                        'in_index_total': 5, 'soft': [],
                        'dead': [{'url': 'https://s/a', 'status': '404', 'source': 'Яндекс'},
                                 {'url': 'https://s/b', 'status': '404', 'source': 'Яндекс'}],
                        'errors': []}]}
    out = rv.reverify_index_404(check, log=None)
    assert out['hosts'] == []
    assert out['total_dead'] == 0


def test_reverify_noop_on_empty():
    assert rv.reverify_index_404({'available': False}) == {'available': False}
    assert rv.reverify_index_404(None) is None


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
