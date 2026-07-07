"""Тесты ротации и профилей."""
import sys
import random
import tempfile
import os
import json
from pathlib import Path
sys.path.insert(0, '/home/claude/site-checker-py')

from history import weighted_sample, load_history, save_history, RECENT_WEIGHT
from profiles import PROFILES, get_profile_kwargs
from sources import load_project_config, load_sources, build_plan


# ── Тесты weighted_sample ──────────────────────────────────────────


def test_weighted_sample_no_history():
    """Без истории - обычная выборка (все равновероятны)."""
    pool = [f'/cat-{i}/' for i in range(100)]
    rng = random.Random(42)
    sample = weighted_sample(pool, 10, set(), rng)
    assert len(sample) == 10
    assert len(set(sample)) == 10  # без дубликатов
    print('✓ weighted_sample без истории: 10 уникальных')


def test_weighted_sample_with_history():
    """С историей - недавно проверенные реже попадают."""
    pool = [f'/cat-{i}/' for i in range(100)]
    # Первые 50 - "недавно проверены"
    recent = set(pool[:50])

    # 10 прогонов по 20 урлов, считаем сколько раз попали "свежие" vs "недавние"
    fresh_count = 0
    recent_count = 0
    for trial in range(10):
        rng = random.Random(trial)
        sample = weighted_sample(pool, 20, recent, rng)
        for s in sample:
            if s in recent:
                recent_count += 1
            else:
                fresh_count += 1

    # Свежих должно быть значительно больше (вес 1.0 против 0.3)
    # Ожидание: ~70% свежих, ~30% недавних
    total = fresh_count + recent_count
    fresh_ratio = fresh_count / total
    print(f'  Свежих: {fresh_count} ({fresh_ratio*100:.0f}%), недавних: {recent_count} ({(1-fresh_ratio)*100:.0f}%)')
    assert fresh_ratio > 0.55, f'Свежих должно быть больше 55%, получили {fresh_ratio:.0%}'
    print('✓ weighted_sample с историей: свежие в приоритете')


def test_weighted_sample_no_duplicates():
    """Выборка без замены - каждый элемент максимум 1 раз."""
    pool = [f'/cat-{i}/' for i in range(20)]
    recent = set(pool[:10])
    rng = random.Random(42)
    sample = weighted_sample(pool, 15, recent, rng)
    assert len(sample) == 15
    assert len(set(sample)) == 15
    print('✓ weighted_sample: без дубликатов')


def test_weighted_sample_n_larger_than_pool():
    """Если n >= len(pool) - возвращаем весь pool."""
    pool = [f'/cat-{i}/' for i in range(5)]
    rng = random.Random(42)
    sample = weighted_sample(pool, 100, set(), rng)
    assert len(sample) == 5
    print('✓ weighted_sample: n > pool возвращает всё')


# ── Тесты history (load/save) ──────────────────────────────────────


def test_history_save_and_load():
    """Сохранение и загрузка истории."""
    # Подменим CACHE_DIR на временную
    import history
    original = history.CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        history.CACHE_DIR = Path(tmp)
        save_history('test_proj', ['/url1/', '/url2/', '/url3/'])

        loaded = load_history('test_proj')
        assert set(loaded.keys()) == {'/url1/', '/url2/', '/url3/'}
        # timestamp ~ сейчас
        import time
        now = time.time() * 1000
        for url, ts in loaded.items():
            assert abs(now - ts) < 5000, f"timestamp {ts} далеко от {now}"
    history.CACHE_DIR = original
    print('✓ history: save и load работают')


def test_history_ttl_cleanup():
    """Старые записи (>7 дней) автоматически отсекаются."""
    import history
    original = history.CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        history.CACHE_DIR = Path(tmp)

        # Создаём файл вручную с двумя записями: свежей и просроченной
        import time
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 8 * 24 * 3600 * 1000  # 8 дней назад
        data = {'/fresh/': now_ms, '/old/': old_ms}
        with open(Path(tmp) / 'history-test.json', 'w', encoding='utf-8') as f:
            json.dump(data, f)

        loaded = load_history('test')
        assert '/fresh/' in loaded
        assert '/old/' not in loaded, 'Старая запись должна отсечься'
    history.CACHE_DIR = original
    print('✓ history: TTL 7 дней работает')


# ── Тесты profiles ──────────────────────────────────────────────────


def test_profiles_exist():
    assert set(PROFILES.keys()) == {'quick', 'standard', 'full'}
    for pid, p in PROFILES.items():
        assert 'label' in p and 'description' in p
        assert 'random_subdomains_count' in p
    print(f'✓ Профили: {", ".join(p["label"] for p in PROFILES.values())}')


def test_profile_quick_smaller_than_full():
    quick = get_profile_kwargs('quick')
    full = get_profile_kwargs('full')
    assert quick['random_subdomains_count'] < full['random_subdomains_count']
    assert quick['categories_per_subdomain'] < full['categories_per_subdomain']
    print('✓ Быстрая < Полная')


def test_profile_applied_to_build_plan():
    """Профиль можно скормить в build_plan через **kwargs."""
    cfg = load_project_config('smu')
    src = load_sources(cfg)
    
    # Быстрая: cis_extra_subdomains=0 -> Москва + 2 случайных = 3 города,
    # по 1 main + 1 catalog + 3 cat + 3 filter + 0 products = 8 проверок × 3 = 24
    quick_plan = build_plan(src, **get_profile_kwargs('quick'), seed=42)
    assert len(quick_plan.selected_subdomains) == 3
    assert len(quick_plan.tasks) == 3 * 8

    # Полная: cis_extra_subdomains=1 -> Москва + 1 СНГ-домен + 10 случайных = 12
    # городов, по 1+1+10+10+0 = 22 проверки × 12 = 264
    full_plan = build_plan(src, **get_profile_kwargs('full'), seed=42)
    assert len(full_plan.selected_subdomains) == 12
    assert len(full_plan.tasks) == 12 * 22
    
    print(f'✓ Профили работают: quick={len(quick_plan.tasks)}, full={len(full_plan.tasks)}')


def test_smu_cis_selection_rule():
    """СМУ: Москва + smg.az всегда; стандарт/полная (cis_extra=1) добавляют ещё
    хотя бы 1 СНГ-домен помимо smg.az."""
    cfg = load_project_config('smu')
    src = load_sources(cfg)
    by_host = {s.host: s for s in src.subdomains}
    for seed in range(5):
        # быстрая: smg.az обязателен, доп. СНГ не требуется
        q = build_plan(src, random_subdomains_count=2, mandatory_city='Москва',
                       mandatory_hosts=['smg.az'], cis_extra_subdomains=0, seed=seed)
        hq = [s.host for s in q.selected_subdomains]
        assert 'stalmetural.ru' in hq and 'smg.az' in hq
        # стандарт/полная: smg.az + ещё минимум 1 СНГ-домен
        f = build_plan(src, random_subdomains_count=5, mandatory_city='Москва',
                       mandatory_hosts=['smg.az'], cis_extra_subdomains=1, seed=seed)
        hf = [s.host for s in f.selected_subdomains]
        assert 'stalmetural.ru' in hf and 'smg.az' in hf
        cis = [h for h in hf if by_host[h].country and by_host[h].country != 'Россия']
        assert len(cis) >= 2, f'seed={seed}: ожидалось >=2 СНГ, получили {cis}'
    print('✓ СМУ: правило выборки (Москва + smg.az + СНГ) работает')


# ── Тесты ротации в build_plan ─────────────────────────────────────


def test_build_plan_with_rotation():
    """С передачей rotation_history выборка склоняется к новым URL."""
    cfg = load_project_config('smu')
    src = load_sources(cfg)

    # "Недавно проверены" - первая половина категорий.
    # Именно половина ОТ ФАКТИЧЕСКОГО размера каталога: раньше тут стояло
    # фиксированное 1500, а в каталоге СМУ категорий меньше - в recent
    # попадало всё, и «свежих» не оставалось вовсе.
    recent = set(src.categories[:len(src.categories) // 2])

    # Прогоняем 5 раз с разными seed-ами и считаем
    fresh_total = 0
    recent_total = 0
    for trial in range(5):
        plan = build_plan(
            src,
            random_subdomains_count=0,         # только Москва
            categories_per_subdomain=10,
            filters_per_subdomain=0,
            products_per_subdomain=0,
            check_categories=True, check_filters=False, check_products=False, check_main=False, check_catalog=False,
            seed=trial,
            rotation_history=recent,
        )
        for t in plan.tasks:
            # из URL вырезаем pathname
            from urllib.parse import urlparse
            p = urlparse(t.url).path
            if p in recent:
                recent_total += 1
            else:
                fresh_total += 1

    total = fresh_total + recent_total
    fresh_ratio = fresh_total / total
    print(f'  С ротацией: свежих {fresh_total} ({fresh_ratio*100:.0f}%), недавних {recent_total}')
    # У нас половина пула в recent. Без ротации было бы 50/50.
    # С весом 0.3 для недавних - свежих должно быть значительно больше 50%.
    assert fresh_ratio > 0.55, f'Ожидали >55% свежих, получили {fresh_ratio:.0%}'
    print('✓ build_plan с ротацией: свежие URL в приоритете')


if __name__ == '__main__':
    test_weighted_sample_no_history()
    test_weighted_sample_with_history()
    test_weighted_sample_no_duplicates()
    test_weighted_sample_n_larger_than_pool()
    test_history_save_and_load()
    test_history_ttl_cleanup()
    test_profiles_exist()
    test_profile_quick_smaller_than_full()
    test_profile_applied_to_build_plan()
    test_build_plan_with_rotation()
    print('\n✅ Все тесты ротации и профилей прошли')
