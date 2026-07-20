"""Тесты webmaster_metrics (Блок B: аномалии Вебмастера) - чистая логика:
разбор истории обхода (2xx/4xx/5xx), проблемы из summary, падение
страниц/ИКС от эталона, best-effort эталон в кэше."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webmaster_metrics as WM
from webmaster_metrics import analyze_crawl, analyze_summary, append_baseline


def _p(vals):
    return [{'date': f'2026-{i + 1:02d}-01', 'value': v} for i, v in enumerate(vals)]


def test_crawl_2xx_ignored():
    # Число обойдённых 2xx-страниц за период само гуляет - это шум, не аномалия.
    ind = {'HTTP_2XX': _p([1000, 1000, 1000, 200])}
    assert not any(a['metric'] == 'Обход: страницы 2xx' for a in analyze_crawl(ind))
    print('✓ обход: просадка 2xx больше НЕ считается (шум)')


def test_crawl_4xx_5xx_spike():
    ind = {'HTTP_4XX': _p([2, 3, 50]), 'HTTP_5XX': _p([0, 12])}
    an = analyze_crawl(ind)
    mets = {a['metric']: a['severity'] for a in an}
    assert mets.get('Обход: ошибки 404 (4xx)') == 'critical'
    assert mets.get('Обход: ошибки сервера (5xx)') == 'fatal'
    print('✓ обход: всплеск 4xx/5xx - аномалии (5xx = фатально)')


def test_crawl_small_errors_not_flagged():
    # Мелочь гасится порогами: 404 до 15, 5xx до 10.
    assert not any(a['metric'] == 'Обход: ошибки 404 (4xx)'
                   for a in analyze_crawl({'HTTP_4XX': _p([1, 2, 11])}))
    assert not any(a['metric'] == 'Обход: ошибки сервера (5xx)'
                   for a in analyze_crawl({'HTTP_5XX': _p([1, 2, 8])}))
    print('✓ обход: мелкие 404 (до 15) и 5xx (до 10) не аномалия')


def test_crawl_stable_no_anomaly():
    ind = {'HTTP_2XX': _p([500, 505, 500, 505]), 'HTTP_4XX': _p([1])}
    assert analyze_crawl(ind) == []
    print('✓ обход стабилен - аномалий нет')


def test_summary_problems_no_baseline():
    summ = {'sqi': 120, 'searchable_pages_count': 800, 'excluded_pages_count': 300,
            'site_problems': {'FATAL': 1, 'CRITICAL': 2, 'POSSIBLE_PROBLEM': 5}}
    an, snap = analyze_summary(summ)          # без эталона
    mets = [a['metric'] for a in an]
    assert 'Фатальные проблемы' in mets           # фатальные - тащим
    assert 'Критические проблемы' not in mets     # критические - шум, не тащим
    assert 'Страницы в поиске' not in mets and 'ИКС (SQI)' not in mets
    assert snap['searchable'] == 800 and snap['sqi'] == 120
    print('✓ summary: только фатальные (крит - шум); страницы/ИКС - с эталоном')


def test_summary_pages_and_sqi_drop():
    summ = {'sqi': 120, 'searchable_pages_count': 800, 'excluded_pages_count': 300,
            'site_problems': {}}
    an, _ = analyze_summary(summ, base_searchable=1000, base_sqi=140)
    mets = {a['metric']: a['delta_pct'] for a in an}
    assert mets.get('Страницы в поиске') == -20     # 1000→800
    assert mets.get('ИКС (SQI)') == -14             # 140→120
    print('✓ summary: падение страниц −20% и ИКС −14% от эталона помечены')


def test_summary_small_drop_ignored():
    # Падение страниц 1000→900 = −10% (< порога 15%) - не шумим.
    summ = {'searchable_pages_count': 900, 'site_problems': {}}
    an, _ = analyze_summary(summ, base_searchable=1000)
    assert not any(a['metric'] == 'Страницы в поиске' for a in an)
    print('✓ summary: падение меньше порога не помечается')


def test_append_baseline_returns_prior_medians():
    pid = '__test_wm_metrics__'
    path = WM._baseline_path(pid)
    try:
        if path.exists():
            path.unlink()
        # первый прогон - эталона нет
        p1 = append_baseline(pid, 'x.ru', {'sqi': 100, 'searchable': 500, 'excluded': 10},
                             today=date(2026, 7, 1))
        assert p1 == (None, None)
        # второй прогон - медиана по одному прошлому = 500 / 100
        p2 = append_baseline(pid, 'x.ru', {'sqi': 90, 'searchable': 400, 'excluded': 12},
                             today=date(2026, 7, 2))
        assert p2 == (500, 100)
        print('✓ эталон: первый прогон пуст, дальше отдаёт медиану прошлых')
    finally:
        if path.exists():
            path.unlink()


if __name__ == '__main__':
    test_crawl_2xx_ignored()
    test_crawl_4xx_5xx_spike()
    test_crawl_small_errors_not_flagged()
    test_crawl_stable_no_anomaly()
    test_summary_problems_no_baseline()
    test_summary_pages_and_sqi_drop()
    test_summary_small_drop_ignored()
    test_append_baseline_returns_prior_medians()
    print('Все тесты webmaster_metrics пройдены.')
