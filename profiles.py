"""
profiles.py - пресеты настроек проверки.

Три профиля, числа согласованы с пользователем:

  Быстрая    - 3 города (Москва + 2),  3 кат, 3 фильтра, 2 товара → ~30 проверок
  Стандартная - 6 городов (Москва + 5), 5 кат, 5 фильтров, 3 товара → ~96 проверок
  Полная     - 11 городов (Москва + 10), 10 кат, 10 фильтров, 5 товаров → ~300 проверок

Каждый профиль - словарь, который можно передать в build_plan через **kwargs.
"""

PROFILES = {
    'quick': {
        'label': 'Быстрая',
        'description': 'Москва + 2 случайных города, по 3 категории и фильтра, 2 товара. ~1-2 минуты.',
        'random_subdomains_count': 2,
        'categories_per_subdomain': 3,
        'filters_per_subdomain': 3,
        'products_per_subdomain': 2,
        'cis_extra_subdomains': 0,   # быстрая: хватает обязательного smg.az
    },
    'standard': {
        'label': 'Стандартная',
        'description': 'Москва + 5 случайных городов, по 5 категорий и фильтров, 3 товара. ~3-5 минут.',
        'random_subdomains_count': 5,
        'categories_per_subdomain': 5,
        'filters_per_subdomain': 5,
        'products_per_subdomain': 3,
        'cis_extra_subdomains': 1,   # + 1 случайный СНГ помимо smg.az
    },
    'full': {
        'label': 'Полная',
        'description': 'Москва + 10 случайных городов, по 10 категорий и фильтров, 5 товаров. ~10-20 минут.',
        'random_subdomains_count': 10,
        'categories_per_subdomain': 10,
        'filters_per_subdomain': 10,
        'products_per_subdomain': 5,
        'cis_extra_subdomains': 1,   # + 1 случайный СНГ помимо smg.az
    },
}


def get_profile_kwargs(profile_id: str) -> dict:
    """Вернуть kwargs для build_plan, соответствующие профилю."""
    if profile_id not in PROFILES:
        raise ValueError(f'Неизвестный профиль: {profile_id}')
    p = PROFILES[profile_id]
    return {
        'random_subdomains_count': p['random_subdomains_count'],
        'categories_per_subdomain': p['categories_per_subdomain'],
        'filters_per_subdomain': p['filters_per_subdomain'],
        'products_per_subdomain': p['products_per_subdomain'],
        'cis_extra_subdomains': p.get('cis_extra_subdomains', 0),
    }
