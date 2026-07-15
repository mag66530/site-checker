"""Тесты link_profile: разбор ответов Яндекс.Вебмастера (samples/history)
в формате из доков, эвристика спам-доноров, вердикты обвал/всплеск,
сборка профиля хоста."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link_profile import (
    looks_spam_host, analyze_samples, analyze_history, build_host_profile,
    _host_of, DROP_PCT, SPIKE_FACTOR,
)


def test_host_of_strips_www_scheme():
    assert _host_of('https://www.donor.ru/page/') == 'donor.ru'
    assert _host_of('http://sub.donor.com/a?b=1') == 'sub.donor.com'
    print('✓ хост донора без схемы/www')


def test_spam_host_heuristic():
    assert looks_spam_host('cheap-casino.xyz')       # зона + слово
    assert looks_spam_host('normal-name.loan')       # мусорная зона
    assert looks_spam_host('best-viagra-shop.ru')    # ключевое слово
    assert not looks_spam_host('metalloprokat.ru')   # нормальный
    assert not looks_spam_host('')
    print('✓ спам-доноры: зоны и ключевые слова, чистые не ловятся')


def test_analyze_samples_counts_and_spam():
    # Ответ в формате /links/external/samples из доков.
    resp = {
        'count': 1200,
        'links': [
            {'source_url': 'https://a.ru/p', 'destination_url': 'https://x.ru/'},
            {'source_url': 'https://a.ru/q', 'destination_url': 'https://x.ru/'},
            {'source_url': 'https://good.com/', 'destination_url': 'https://x.ru/'},
            {'source_url': 'https://casino.xyz/', 'destination_url': 'https://x.ru/'},
        ],
    }
    s = analyze_samples(resp['count'], resp['links'])
    assert s['total'] == 1200
    assert s['sample_size'] == 4
    assert s['distinct_hosts'] == 3          # a.ru дедуплицирован
    assert s['spam_hosts'] == ['casino.xyz']
    print('✓ samples: всего/доноры/дедуп/спам')


def test_analyze_samples_empty():
    s = analyze_samples(0, [])
    assert s['total'] == 0 and s['distinct_hosts'] == 0 and s['spam_hosts'] == []
    print('✓ samples пустые - нули без падения')


def test_history_stable():
    ind = {'LINKS_TOTAL_COUNT': [
        {'date': '2026-01-01T00:00:00,000+0300', 'value': 100},
        {'date': '2026-02-01T00:00:00,000+0300', 'value': 105},
        {'date': '2026-03-01T00:00:00,000+0300', 'value': 110},
    ]}
    h = analyze_history(ind)
    assert h['latest'] == 110 and h['peak'] == 110 and h['first'] == 100
    assert not h['dropped'] and not h['spiked']
    print('✓ history стабильная - без обвала/всплеска')


def test_history_drop_flagged():
    # Пик 200, упало до 120 = -40% (>30 порога).
    ind = {'LINKS_TOTAL_COUNT': [
        {'date': '2026-01-01', 'value': 150},
        {'date': '2026-02-01', 'value': 200},
        {'date': '2026-03-01', 'value': 120},
    ]}
    h = analyze_history(ind)
    assert h['dropped'] and h['drop_pct'] == 40
    assert not h['spiked']
    print(f'✓ history: обвал -{h["drop_pct"]}% помечен (порог {DROP_PCT}%)')


def test_history_spike_flagged():
    # С 20 до 80 = ×4 (>×3 порога) - возможный спам.
    ind = {'LINKS_TOTAL_COUNT': [
        {'date': '2026-01-01', 'value': 20},
        {'date': '2026-02-01', 'value': 80},
    ]}
    h = analyze_history(ind)
    assert h['spiked'] and h['spike_factor'] == 4.0
    print(f'✓ history: всплеск ×{h["spike_factor"]} помечен (порог ×{SPIKE_FACTOR})')


def test_history_ignores_tiny_moves():
    # Мелкие абсолютные изменения (пик-последнее < 5) не шумят, даже если %.
    ind = {'LINKS_TOTAL_COUNT': [
        {'date': '2026-01-01', 'value': 3},
        {'date': '2026-02-01', 'value': 10},
        {'date': '2026-03-01', 'value': 8},
    ]}
    h = analyze_history(ind)
    assert not h['dropped']          # пик10-послед8=2 (<5) - тихо
    print('✓ history: мелкие абсолютные сдвиги не шумят')


def test_build_host_profile_no_profile():
    prof = build_host_profile('young.ru', 'https://wm/', {'count': 0, 'links': []},
                              {'indicators': {'LINKS_TOTAL_COUNT': []}})
    assert prof['total'] == 0
    assert not prof['warnings']
    assert any('профиля пока нет' in i for i in prof['infos'])
    print('✓ профиль: нулевые ссылки - инфо, не предупреждение')


def test_build_host_profile_warnings():
    samples = {'count': 500, 'links': [
        {'source_url': 'https://spam-casino.xyz/'},
        {'source_url': 'https://normal.ru/'},
    ]}
    history = {'indicators': {'LINKS_TOTAL_COUNT': [
        {'date': '2026-01-01', 'value': 300},
        {'date': '2026-02-01', 'value': 500},
        {'date': '2026-03-01', 'value': 200},   # обвал от пика 500 = -60%
    ]}}
    prof = build_host_profile('x.ru', 'https://wm/', samples, history)
    txt = ' | '.join(prof['warnings'])
    assert 'просела' in txt          # обвал
    assert 'доноры' in txt           # спам
    assert prof['spam_count'] == 1
    print('✓ профиль: обвал + спам-доноры в предупреждениях')


if __name__ == '__main__':
    test_host_of_strips_www_scheme()
    test_spam_host_heuristic()
    test_analyze_samples_counts_and_spam()
    test_analyze_samples_empty()
    test_history_stable()
    test_history_drop_flagged()
    test_history_spike_flagged()
    test_history_ignores_tiny_moves()
    test_build_host_profile_no_profile()
    test_build_host_profile_warnings()
    print('Все тесты link_profile пройдены.')
