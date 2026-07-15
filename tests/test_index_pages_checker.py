"""Тесты index_pages_checker.py - 404 среди страниц в индексе.

Проверяем чистую логику (парсинг выборки, классификация кода ответа,
эвристика soft-404), пагинацию выборки из Вебмастера и агрегацию по хостам -
всё без сети (API и прозвон замоканы).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_pages_checker as m


# ── parse_samples ────────────────────────────────────────────────────

def test_parse_samples_shapes():
    """Берём url/page, пропускаем пустые и мусор, терпимы к схеме."""
    resp = {'count': 3, 'samples': [
        {'url': 'https://a/x', 'last_access': '2026-07-01', 'title': 'X'},
        {'url': '   '},                 # пустой после strip
        {'page': 'https://a/y'},        # альтернативное поле
        {},                             # ни url, ни page
        'https://a/z',                  # вдруг строкой
    ]}
    assert m.parse_samples(resp) == ['https://a/x', 'https://a/y', 'https://a/z']


def test_parse_samples_empty():
    assert m.parse_samples({}) == []
    assert m.parse_samples({'samples': None}) == []
    assert m.parse_samples(None) == []


# ── looks_soft_404 ───────────────────────────────────────────────────

def test_soft_404_positive():
    assert m.looks_soft_404('Страница не найдена | СМУ')
    assert m.looks_soft_404('ОШИБКА 404')
    assert m.looks_soft_404('Page Not Found')


def test_soft_404_false_positive_guard():
    """«404» в артикуле/названии товара - не soft-404."""
    assert not m.looks_soft_404('Балка 20Б1 длиной 404 мм купить')
    assert not m.looks_soft_404('Металлопрокат в наличии')
    assert not m.looks_soft_404('')


# ── classify_index_url ───────────────────────────────────────────────

@pytest.mark.parametrize('status,redirected,soft,error,verdict', [
    (404, False, False, None, 'dead'),
    (410, False, False, None, 'dead'),
    (200, False, True, None, 'soft'),
    (200, True, False, None, 'redirect'),
    (200, False, False, None, 'ok'),
    (301, False, False, None, 'redirect'),
    (503, False, False, None, 'server_error'),
    (403, False, False, None, 'client_error'),   # блокировка бота, НЕ dead
    (401, False, False, None, 'client_error'),
    (None, False, False, 'timeout', 'no_response'),
    (None, False, False, 'conn', 'no_response'),
])
def test_classify(status, redirected, soft, error, verdict):
    v, reason = m.classify_index_url(status, redirected, soft, error)
    assert v == verdict
    assert isinstance(reason, str)


def test_classify_403_not_dead():
    """403 - вероятная анти-бот блокировка, не «битая в индексе»."""
    assert m.classify_index_url(403, False, False, None)[0] != 'dead'


# ── fetch_indexed_sample: пагинация и лимит ─────────────────────────

def _fake_get_factory(total, page_size=100):
    """Фейковый _get: отдаёт count=total и по offset нарезает выборку."""
    def fake_get(token, path, proxy_url=None, params=None):
        params = params or {}
        offset = params.get('offset', 0)
        limit = params.get('limit', 100)
        remaining = max(0, min(total, page_size + offset) - offset)
        n = min(limit, remaining, total - offset)
        samples = [{'url': f'https://h/p{offset + i}'} for i in range(max(0, n))]
        return {'count': total, 'samples': samples}
    return fake_get


def test_fetch_sample_paginates_all(monkeypatch):
    """total=250 → собираем все 250 (3 страницы), total прокидывается."""
    monkeypatch.setattr(m, '_get', _fake_get_factory(250))
    urls, total = m.fetch_indexed_sample('tok', 1, 'hid', None, max_urls=300)
    assert total == 250
    assert len(urls) == 250
    assert urls[0] == 'https://h/p0'
    assert len(set(urls)) == 250          # без дублей между страницами


def test_fetch_sample_respects_max(monkeypatch):
    """max_urls=150 → берём ровно 150, не больше."""
    monkeypatch.setattr(m, '_get', _fake_get_factory(1000))
    urls, total = m.fetch_indexed_sample('tok', 1, 'hid', None, max_urls=150)
    assert total == 1000
    assert len(urls) == 150


def test_fetch_sample_empty(monkeypatch):
    """Пустая выборка (count=0) → пустой список, без зацикливания."""
    monkeypatch.setattr(m, '_get', _fake_get_factory(0))
    urls, total = m.fetch_indexed_sample('tok', 1, 'hid', None, max_urls=300)
    assert urls == []
    assert total == 0


# ── check_index_404: агрегация по хостам ─────────────────────────────

def test_check_index_404_no_token():
    res = m.check_index_404('smu', token='', log=None)
    assert res['available'] is False
    assert res['error']


def test_check_index_404_tallies(monkeypatch):
    """Мокаем разрешение хостов, выборку и прозвон - проверяем свод."""
    monkeypatch.setattr(m, '_resolve_hosts',
                        lambda tok, pid, proxy: (1, [('a.ru', 'hidA'),
                                                     ('b.ru', 'hidB')]))

    def fake_sample(token, uid, host_id, proxy, max_urls):
        if host_id == 'hidA':
            return (['https://a.ru/dead', 'https://a.ru/soft',
                     'https://a.ru/ok', 'https://a.ru/500'], 400)
        return (['https://b.ru/ok'], 20)
    monkeypatch.setattr(m, 'fetch_indexed_sample', fake_sample)

    async def fake_check_all(pairs, proxy):
        verdicts = {
            'https://a.ru/dead': 'dead', 'https://a.ru/soft': 'soft',
            'https://a.ru/ok': 'ok', 'https://a.ru/500': 'server_error',
            'https://b.ru/ok': 'ok',
        }
        out = {}
        for _, u in pairs:
            out[u] = {'url': u, 'status': 0, 'redirected': False,
                      'verdict': verdicts[u], 'reason': verdicts[u]}
        return out
    monkeypatch.setattr(m, '_check_all', fake_check_all)

    res = m.check_index_404('smu', token='tok', log=None)
    assert res['available'] is True
    assert res['total_checked'] == 5
    assert res['total_dead'] == 1
    assert res['total_soft'] == 1

    ha = next(h for h in res['hosts'] if h['host'] == 'a.ru')
    assert ha['in_index_total'] == 400
    assert len(ha['dead']) == 1 and ha['dead'][0]['url'] == 'https://a.ru/dead'
    assert len(ha['soft']) == 1
    assert len(ha['errors']) == 1        # 500 попал в errors
    assert ha['ok'] == 1

    hb = next(h for h in res['hosts'] if h['host'] == 'b.ru')
    assert hb['checked'] == 1 and hb['ok'] == 1 and not hb['dead']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
