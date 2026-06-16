"""Тесты sources.py — проверяем что данные парсятся корректно."""
import sys
sys.path.insert(0, '/home/claude/site-checker-py')

from sources import (
    load_project_config, load_sources, build_plan, build_custom_plan,
    list_projects,
)


def test_list_projects():
    projects = list_projects()
    ids = {p['id'] for p in projects}
    # Базовые проекты должны быть; могут быть и доп. (напр. smu-test — тестовый стенд)
    assert {'smu', 'imp', 'mpe'} <= ids, f"Нет базовых проектов, получили {ids}"
    print(f'✓ list_projects: {len(projects)} проектов ({", ".join(sorted(ids))})')


def test_smu_sources():
    cfg = load_project_config('smu')
    src = load_sources(cfg)
    assert len(src.subdomains) == 34, f"СМУ: ожидалось 34 поддомена, получили {len(src.subdomains)}"
    assert len(src.categories) > 1000, f"СМУ: мало категорий ({len(src.categories)})"
    assert len(src.filters) > 13000, f"СМУ: мало фильтров ({len(src.filters)})"
    moscow = next((s for s in src.subdomains if s.city == 'Москва'), None)
    assert moscow is not None and moscow.host == 'stalmetural.ru'
    print(f'✓ СМУ: {len(src.subdomains)} городов, '
          f'{len(src.categories)} категорий, {len(src.filters)} фильтров')


def test_imp_sources():
    cfg = load_project_config('imp')
    src = load_sources(cfg)
    assert len(src.subdomains) == 239, f"ИМП: ожидалось 239, получили {len(src.subdomains)}"
    assert len(src.categories) > 1400, f"ИМП: мало категорий"
    assert len(src.filters) > 14000, f"ИМП: мало фильтров"
    print(f'✓ ИМП: {len(src.subdomains)} городов, '
          f'{len(src.categories)} категорий, {len(src.filters)} фильтров')


def test_mpe_sources():
    cfg = load_project_config('mpe')
    src = load_sources(cfg)
    assert len(src.subdomains) == 159, f"МПЭ: ожидалось 159, получили {len(src.subdomains)}"
    assert len(src.categories) > 1000, f"МПЭ: мало категорий"
    assert len(src.filters) == 0, f"МПЭ: должно быть 0 фильтров, получили {len(src.filters)}"
    print(f'✓ МПЭ: {len(src.subdomains)} городов, '
          f'{len(src.categories)} категорий, {len(src.filters)} фильтров')


def test_build_plan_smu():
    cfg = load_project_config('smu')
    src = load_sources(cfg)
    plan = build_plan(
        src,
        random_subdomains_count=5,
        categories_per_subdomain=5,
        filters_per_subdomain=5,
        products_per_subdomain=0,
        seed=42,
    )
    # Москва + 5 случайных = 6 городов
    assert len(plan.selected_subdomains) == 6
    # На каждом: 1 main + 1 catalog + 5 категорий + 5 фильтров = 12
    assert len(plan.tasks) == 6 * 12
    # Москва первая
    assert plan.selected_subdomains[0].city == 'Москва'
    # Проверим что все URL правильные
    for t in plan.tasks[:3]:
        assert t.url.startswith('http')
        assert t.city == 'Москва'
    print(f'✓ build_plan СМУ: {len(plan.tasks)} задач, '
          f'{len(plan.selected_subdomains)} городов')


def test_build_plan_mpe_no_filters():
    """МПЭ — нет фильтров, поле filters_per_subdomain должно игнорироваться."""
    cfg = load_project_config('mpe')
    src = load_sources(cfg)
    plan = build_plan(
        src,
        random_subdomains_count=2,
        categories_per_subdomain=3,
        filters_per_subdomain=5,    # не должно создавать задач
        products_per_subdomain=0,
        seed=42,
    )
    # 3 города × (1 main + 1 catalog + 3 категории) = 15
    assert len(plan.tasks) == 3 * 5, f"Ожидалось 15 задач, получили {len(plan.tasks)}"
    # Никаких filter в задачах
    filter_tasks = [t for t in plan.tasks if t.type_code == 'filter']
    assert len(filter_tasks) == 0
    print(f'✓ build_plan МПЭ: {len(plan.tasks)} задач (без фильтров)')


def test_build_custom_plan():
    raw = [
        'https://example.com/page1',
        'https://example.com/page2',
        'example.com/page3',            # без https://
        '   ',                          # пустая
        '# comment',                    # только комментарий
        'https://example.com/p4 # хвост',
        'https://example.com/page1',    # дубликат
        'not-a-url',                    # невалид
    ]
    plan = build_custom_plan(raw)
    urls = [t.url for t in plan.tasks]
    assert len(urls) == 4, f"Ожидалось 4 URL, получили {len(urls)}: {urls}"
    assert all(u.startswith('https://') for u in urls)
    # Без дубликатов
    assert len(set(urls)) == len(urls)
    print(f'✓ build_custom_plan: {len(urls)} URL после очистки')


def test_seed_reproducibility():
    """Один и тот же seed даёт одинаковую выборку."""
    cfg = load_project_config('smu')
    src = load_sources(cfg)
    plan1 = build_plan(src, random_subdomains_count=3, categories_per_subdomain=2,
                       filters_per_subdomain=0, products_per_subdomain=0, seed=123)
    plan2 = build_plan(src, random_subdomains_count=3, categories_per_subdomain=2,
                       filters_per_subdomain=0, products_per_subdomain=0, seed=123)
    urls1 = [t.url for t in plan1.tasks]
    urls2 = [t.url for t in plan2.tasks]
    assert urls1 == urls2
    print('✓ Воспроизводимость seed: одинаковая выборка')


if __name__ == '__main__':
    test_list_projects()
    test_smu_sources()
    test_imp_sources()
    test_mpe_sources()
    test_build_plan_smu()
    test_build_plan_mpe_no_filters()
    test_build_custom_plan()
    test_seed_reproducibility()
    print('\n✅ Все тесты sources.py прошли')
