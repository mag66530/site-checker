"""Тесты webmaster_metrics (Блок B: аномалии Вебмастера) - чистая логика:
разбор истории обхода (2xx/4xx/5xx), проблемы из summary, падение
страниц/ИКС от эталона, best-effort эталон в кэше."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webmaster_metrics as WM
from webmaster_metrics import analyze_crawl, analyze_summary, append_baseline


def test_crawl_2xx_drop():
    ind = {'HTTP_2XX': [{'date': '2026-05-01', 'value': 1000},
                        {'date': '2026-06-01', 'value': 980},
                        {'date': '2026-07-01', 'value': 700}]}
    an = analyze_crawl(ind)
    assert any(a['metric'] == 'Обход: страницы 2xx' and a['delta_pct'] == -30
               for a in an), an
    print('✓ обход: просадка 2xx (−30%) - аномалия')


def test_crawl_4xx_5xx_spike():
    ind = {'HTTP_4XX': [{'date': '2026-05-01', 'value': 2},
                        {'date': '2026-06-01', 'value': 3},
                        {'date': '2026-07-01', 'value': 50}],
           'HTTP_5XX': [{'date': '2026-06-01', 'value': 0},
                        {'date': '2026-07-01', 'value': 12}]}
    an = analyze_crawl(ind)
    mets = {a['metric']: a['severity'] for a in an}
    assert mets.get('Обход: ошибки 404 (4xx)') == 'critical'
    assert mets.get('Обход: ошибки сервера (5xx)') == 'fatal'
    print('✓ обход: всплеск 4xx/5xx - аномалии (5xx = фатально)')


def test_crawl_stable_no_anomaly():
    ind = {'HTTP_2XX': [{'date': '2026-06-01', 'value': 500},
                        {'date': '2026-07-01', 'value': 505}],
           'HTTP_4XX': [{'date': '2026-07-01', 'value': 1}]}
    assert analyze_crawl(ind) == []
    print('✓ обход стабилен - аномалий нет')


def test_summary_problems_no_baseline():
    summ = {'sqi': 120, 'searchable_pages_count': 800, 'excluded_pages_count': 300,
            'site_problems': {'FATAL': 1, 'CRITICAL': 2, 'POSSIBLE_PROBLEM': 5}}
    an, snap = analyze_summary(summ)          # без эталона
    mets = [a['metric'] for a in an]
    assert 'Фатальные проблемы' in mets and 'Критические проблемы' in mets
    assert 'Страницы в поиске' not in mets and 'ИКС (SQI)' not in mets
    assert snap['searchable'] == 800 and snap['sqi'] == 120
    print('✓ summary: фатал/крит без эталона; страницы/ИКС - только с эталоном')


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
    test_crawl_2xx_drop()
    test_crawl_4xx_5xx_spike()
    test_crawl_stable_no_anomaly()
    test_summary_problems_no_baseline()
    test_summary_pages_and_sqi_drop()
    test_summary_small_drop_ignored()
    test_append_baseline_returns_prior_medians()
    print('Все тесты webmaster_metrics пройдены.')
