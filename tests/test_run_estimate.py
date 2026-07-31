"""Тесты run_estimate.py - прогноз времени прогона по выбранным галочкам."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_estimate import estimate_run_seconds, format_estimate


def test_range_is_ordered():
    low, high = estimate_run_seconds(100, 5, {})
    assert low < high, f'Нижняя граница должна быть меньше верхней: {low} / {high}'
    print(f'✓ диапазон упорядочен: {low}-{high} сек')


def test_grows_with_pages():
    small, _ = estimate_run_seconds(10, 1, {})
    big, _ = estimate_run_seconds(500, 1, {})
    assert big > small, f'Больше страниц - дольше прогон, получили {small} и {big}'
    print(f'✓ растёт с объёмом: 10 стр = {small} сек, 500 стр = {big} сек')


def test_heavy_check_costs_more_than_light():
    """Браузерная проверка консоли должна быть заметно дороже разбора метатегов."""
    pages, cities = 200, 5
    light, _ = estimate_run_seconds(pages, cities, {'check_meta': True})
    heavy, _ = estimate_run_seconds(pages, cities, {'check_console': True})
    assert heavy > light * 2, (
        f'Консоль (браузер) должна быть кратно дороже метатегов: {heavy} vs {light}')
    print(f'✓ тяжёлая дороже лёгкой: метатеги {light} сек, консоль {heavy} сек')


def test_check_off_is_free():
    on, _ = estimate_run_seconds(100, 3, {'check_console': True})
    off, _ = estimate_run_seconds(100, 3, {'check_console': False})
    base, _ = estimate_run_seconds(100, 3, {})
    assert off == base, f'Снятая галочка не должна ничего стоить: {off} vs {base}'
    assert on > off
    print(f'✓ снятая галочка бесплатна: {off} сек = базовые {base} сек')


def test_fixed_cost_independent_of_pages():
    """Фиксированные надбавки не должны расти вместе с числом страниц."""
    d10 = (estimate_run_seconds(10, 1, {'check_index_404': True})[0]
           - estimate_run_seconds(10, 1, {})[0])
    d500 = (estimate_run_seconds(500, 1, {'check_index_404': True})[0]
            - estimate_run_seconds(500, 1, {})[0])
    assert abs(d10 - d500) <= 1, (
        f'«404 в индексе» - фиксированная цена, а выросла: {d10} → {d500}')
    print(f'✓ фиксированная надбавка стабильна: {d10} сек при любом объёме')


def test_per_city_cost_grows_with_cities():
    few, _ = estimate_run_seconds(100, 1, {'check_home_dupes': True})
    many, _ = estimate_run_seconds(100, 20, {'check_home_dupes': True})
    assert many > few, f'Дубли главной считаются по городам: {few} vs {many}'
    print(f'✓ по-городская надбавка растёт: 1 город {few} сек, 20 городов {many} сек')


def test_unknown_check_is_ignored():
    """Новая галочка без цены не должна ронять прогноз."""
    base = estimate_run_seconds(50, 2, {})
    with_new = estimate_run_seconds(50, 2, {'check_something_new': True})
    assert base == with_new, 'Неизвестный ключ должен игнорироваться, а не падать'
    print('✓ неизвестная галочка игнорируется')


def test_empty_run_does_not_crash():
    low, high = estimate_run_seconds(0, 0, {})
    assert low > 0 and high > low, f'Пустой прогон всё равно стоит накладных: {low}/{high}'
    print(f'✓ пустой прогон не падает: {low}-{high} сек')


def test_none_checks_allowed():
    low, high = estimate_run_seconds(10, 1, None)
    assert low > 0 and high > low
    print('✓ checks=None не роняет')


def test_format_minutes_range():
    txt = format_estimate(12 * 60, 20 * 60)
    assert '–' in txt and 'мин' in txt, f'Ожидали диапазон в минутах, получили {txt!r}'
    assert txt.count('мин') == 1, f'Единицы не должны дублироваться: {txt!r}'
    print(f'✓ формат минут: {txt}')


def test_format_hours_for_long_runs():
    txt = format_estimate(3 * 3600, 5 * 3600)
    assert 'ч' in txt, f'Длинный прогон должен показываться в часах: {txt!r}'
    print(f'✓ формат часов: {txt}')


def test_format_never_shows_zero():
    txt = format_estimate(5, 20)
    assert '0 мин' not in txt, f'«0 мин» вводит в заблуждение: {txt!r}'
    print(f'✓ ноль не показывается: {txt}')


def test_realistic_run_is_plausible():
    """Боевой сценарий: 6 городов × 20 страниц, стандартный набор галочек."""
    checks = {
        'check_main': True, 'check_catalog': True, 'check_categories': True,
        'check_products': True, 'check_text': True, 'check_indexing': True,
        'check_meta': True, 'check_markup': True, 'check_home_dupes': True,
        'check_security': True, 'check_404': True, 'check_static': True,
    }
    low, high = estimate_run_seconds(120, 6, checks)
    assert 120 < low < 3600, f'Ожидали единицы-десятки минут, получили {low} сек'
    print(f'✓ боевой сценарий: {format_estimate(low, high)}')


def test_console_makes_run_much_longer():
    """Смысл всей затеи: видно, что галочка консоли превращает прогон в долгий."""
    base = estimate_run_seconds(120, 6, {'check_meta': True})
    with_console = estimate_run_seconds(120, 6, {'check_meta': True, 'check_console': True})
    assert with_console[0] > base[0] * 2
    print(f'✓ консоль удорожает прогон: {format_estimate(*base)} → '
          f'{format_estimate(*with_console)}')
