# -*- coding: utf-8 -*-
"""Прогноз времени должен накрывать реальные прогоны.

Раньше факт систематически превышал ВЕРХНЮЮ границу: тяжёлые проверки
(ссылочный профиль, ИКС, аномалии, регион) считались фиксированными
секундами, хотя ходят по ВСЕМ хостам проекта - на 242 хостах это минуты.
Замеры ниже сняты с боевых логов, а не выдуманы."""
import pytest

from run_estimate import PER_HOST_SEC, estimate_run_seconds

_ПОЛНЫЙ = {k: True for k in (
    'check_main', 'check_catalog', 'check_categories', 'check_filters',
    'check_products', 'check_text', 'check_indexing', 'check_meta',
    'check_markup', 'check_images', 'check_layout', 'check_security',
    'check_console', 'check_links', 'check_region', 'check_cis',
    'check_home_dupes', 'check_404', 'check_w3c', 'check_static',
    'check_index_404', 'check_gsc_pages', 'check_stress', 'check_ps_filters',
    'check_link_profile', 'check_trust', 'check_anomaly', 'check_arsenkin',
    'check_uniqueness', 'check_traffic', 'fetch_notifications',
    'fetch_metrika_404')}
_МАЛЫЙ = {'check_main': True, 'check_catalog': True, 'check_categories': True,
          'check_indexing': True, 'check_meta': True, 'check_region': True}
_ССЫЛКИ = {'check_link_profile': True, 'check_trust': True}

# (описание, страниц, городов, хостов, галочки, факт в секундах)
ЗАМЕРЫ = [
    ('СМУ полный прогон', 45, 4, 84, _ПОЛНЫЙ, 55 * 60 + 29),
    ('МПЭ лёгкий прогон', 20, 3, 160, _МАЛЫЙ, 2 * 60 + 12),
    ('ИМП ссылки + ИКС', 10, 1, 242, _ССЫЛКИ, 10 * 60 + 2),
]


@pytest.mark.parametrize('описание,страниц,городов,хостов,галочки,факт', ЗАМЕРЫ,
                         ids=[з[0] for з in ЗАМЕРЫ])
def test_факт_попадает_в_прогноз(описание, страниц, городов, хостов, галочки,
                                 факт):
    lo, hi = estimate_run_seconds(страниц, городов, галочки, hosts=хостов)
    assert lo <= факт <= hi, (
        f'{описание}: прогноз {lo // 60}:{lo % 60:02d}-{hi // 60}:{hi % 60:02d}, '
        f'факт {факт // 60}:{факт % 60:02d}')


def test_число_хостов_влияет_на_прогноз():
    """Ссылочный профиль ходит по всем сайтам проекта, а не по выборке."""
    мало = estimate_run_seconds(20, 2, {'check_link_profile': True}, hosts=10)
    много = estimate_run_seconds(20, 2, {'check_link_profile': True}, hosts=242)
    assert много[1] > мало[1] * 2, (мало, много)


def test_без_числа_хостов_считаем_по_городам():
    """Старые вызовы без hosts не должны падать и обязаны давать оценку."""
    lo, hi = estimate_run_seconds(20, 5, {'check_trust': True})
    assert 0 < lo < hi


def test_тяжёлые_проверки_зависят_от_хостов():
    """Именно из-за них прогноз systematically не сходился."""
    for ключ in ('check_link_profile', 'check_trust', 'check_anomaly',
                 'check_region'):
        assert PER_HOST_SEC.get(ключ), f'{ключ} снова считается фиксированно'


def test_пустой_прогон_не_обещает_часы():
    lo, hi = estimate_run_seconds(0, 0, {}, hosts=0)
    assert hi < 120
