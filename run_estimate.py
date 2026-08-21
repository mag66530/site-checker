"""
run_estimate.py - примерное время прогона чек-листа по выбранным галочкам.

Считаем ДО запуска, чтобы человек понимал, на сколько он подписывается:
поставил «ошибки JS в консоли» - браузер откроет каждую страницу, и вместо
10 минут прогон станет часовым.

Модель простая, на константах: базовая цена страницы плюс надбавки за
галочки. Часть проверок стоит «за страницу» (идут по каждому URL прогона),
часть - «за город» (по одной на поддомен), часть - фиксированно (один
запрос к API или одна поездка браузера на весь прогон).

Константы - оценка порядка величины, а не замер. Реальное время зависит от
скорости сайта, прокси и лимитов внешних сервисов (W3C, Вебмастер, GSC),
поэтому наружу отдаём ДИАПАЗОН, а не точное число.

Без Streamlit - чистые функции, чтобы можно было проверить тестами.
"""

# Базовая цена одной страницы: HTTP-запрос, парсинг, разбор контента.
# Прогон идёт с concurrency=6, поэтому это уже «эффективная» цена с учётом
# параллельности, а не время одного запроса.
BASE_PER_PAGE_SEC = 2.5

# Постоянные накладные на прогон: старт процесса, загрузка sitemap/каталога,
# сборка xlsx-отчёта в конце.
OVERHEAD_SEC = 45

# Надбавка за КАЖДУЮ страницу прогона.
PER_PAGE_SEC = {
    # Браузер открывает каждую страницу - самая дорогая галочка в чек-листе.
    'check_console': 8.0,
    # Прозвон внутренних ссылок страницы (лимит 2500 на прогон).
    'check_links': 4.0,
    # Ниже - проверки поверх уже скачанного HTML, поэтому копейки.
    'check_images': 0.5,
    'check_layout': 0.6,
    'check_indexing': 0.4,
    'check_meta': 0.3,
    'check_markup': 0.3,
    'check_text': 0.2,
    'check_region': 0.1,
    'check_cis': 0.1,
}

# Надбавка за КАЖДЫЙ город (поддомен) в выборке.
PER_CITY_SEC = {
    # Несколько вариантов адреса главной на город (www, http, слэш, index.php).
    'check_home_dupes': 8.0,
    # Заголовки безопасности - один запрос на город.
    'check_security': 3.0,
}

# Надбавка за КАЖДЫЙ ХОСТ ПРОЕКТА (не за город выборки!). Эти проверки ходят
# в API по ВСЕМ сайтам проекта, сколько бы городов ни выбрали для страниц.
# Замеры с боевых прогонов:
#   ссылочный профиль - СМУ 84 хоста 3:49 (2.7 с/хост), ИМП 242 хоста 6:53
#                       (1.7 с/хост); берём 2.0 с усреднённо;
#   траст (ИКС)       - СМУ 84 хоста 1:50 (1.3), ИМП 242 хоста 3:09 (0.8);
#   аномалии          - СМУ 84 хоста 4:08 (3.0).
# Именно из-за них оценка систематически не сходилась: раньше они считались
# фиксированными 25-45 секундами, а на проекте с 242 хостами это МИНУТЫ.
PER_HOST_SEC = {
    'check_link_profile': 2.0,
    'check_anomaly': 3.0,
    'check_trust': 1.1,
    # Регион-проверки идут по всем городам КП, а не по выборке страниц.
    # Время зависит не от числа городов, а от числа НОМЕРОВ в КП: СМУ 81
    # город / 186 номеров - 6:11, МПЭ 158 городов / 85 номеров - 1:38.
    # Номера в интерфейсе неизвестны, поэтому берём нижнюю оценку по городам.
    'check_region': 0.6,
}

# Фиксированная надбавка на весь прогон, от объёма не зависит.
# Замеры с прогона СМУ (4 города, 46 проверок, всего 55 мин):
#   404 в индексе      - 8 мин (перепроверка 1137 кандидатов вживую);
#   регион-проверки    - 6 мин (81 город × номера КП);
#   страницы в ГСК     - 5 мин (прозвон 200 URL через API с паузами).
FIXED_SEC = {
    # Браузерные поездки и живые перепроверки - самое дорогое в прогоне.
    # 480 - перепроверка 404-кандидатов вживую; +60 - служебные адреса из
    # индекса (до 150 запросов с чтением тела ради noindex).
    'check_index_404': 540,
    'check_gsc_pages': 300,   # было 150: прозвон URL через API с паузами
    'check_filter_fn': 120,
    'check_calltracking': 120,
    'check_stress': 120,
    'check_uniqueness': 90,
    # Внешние сервисы и API.
    'check_w3c': 45,          # выборка ~3 страниц через W3C Nu + CSS
    'check_ps_filters': 40,
    'check_review_priority': 45,
    'check_anomalies': 45,
    'check_arsenkin': 60,
    # Одна задача Вордстата на весь прогон: ~30-60 с на счёт + опрос.
    'check_title_key_order': 60,
    'check_yabusiness': 60,
    'check_404': 30,
    'check_traffic': 30,
    'check_static': 20,
    'fetch_notifications': 40,
    'fetch_metrika_404': 30,
}

# Во что превращаем итог. Диапазон несимметричный: прогон чаще затягивается
# (ретраи, тормозящий сайт, лимиты сервисов), чем идёт быстрее ожидаемого.
# Нижняя граница опущена с 0.7 до 0.6: на лёгких прогонах (пара галочек,
# быстрый сайт) факт оказывался НИЖЕ прогноза - оценка выглядела завышенной
# на каждом коротком запуске. Сверено с боевыми прогонами СМУ/МПЭ/ИМП.
_LOW_FACTOR = 0.6
_HIGH_FACTOR = 1.45


def estimate_run_seconds(pages: int, cities: int, checks: dict,
                         hosts: int = 0) -> tuple[int, int]:
    """
    Примерная длительность прогона в секундах: (минимум, максимум).

    pages  - сколько страниц проверяем всего (города × страниц на город)
    cities - сколько городов (поддоменов) в выборке
    checks - {'check_console': True, ...}; ключи как в flags прогона,
             неизвестные игнорируем (не падаем на новых галочках)
    hosts  - сколько ВСЕГО хостов у проекта. Часть проверок (ссылочный
             профиль, ИКС, аномалии) ходит по всем сайтам проекта независимо
             от того, сколько городов выбрано для страниц: у проекта на 242
             хоста это минуты, а не секунды. Не задан - считаем по городам,
             как раньше.
    """
    pages = max(0, int(pages))
    cities = max(0, int(cities))
    hosts = max(0, int(hosts)) or cities

    total = OVERHEAD_SEC + BASE_PER_PAGE_SEC * pages

    for key, on in (checks or {}).items():
        if not on:
            continue
        total += PER_PAGE_SEC.get(key, 0.0) * pages
        total += PER_CITY_SEC.get(key, 0.0) * cities
        total += PER_HOST_SEC.get(key, 0.0) * hosts
        total += FIXED_SEC.get(key, 0)

    return int(total * _LOW_FACTOR), int(total * _HIGH_FACTOR)


# ── Оценки для остальных проверок ────────────────────────────────────
# Константы сняты с реальных прогонов, а не выдуманы:
#   формы  - SHOPMET 7 форм = 8 мин 18 с, ИМП 12 форм = 11 мин 34 с
#            (≈60-70 с на форму при одном городе);
#   КП     - SHOPMET 21 город = ~25 с без карт (≈1 с на город).
FORMS_PER_FORM_SEC = 62          # проверка одной формы: заполнение + пробы
FORMS_OVERHEAD_SEC = 60          # старт браузера, сборка отчёта
FORMS_ADMIN_SEC = 90             # сверка заявок в админке (вход + список)

GOALS_PER_PAGE_SEC = 18          # страница плана: загрузка + клики
GOALS_OVERHEAD_SEC = 60
GOALS_ORDERS_SEC = 210           # прогон сквозного заказа внутри целей

# Замер: SHOPMET, 21 город без карт - 25 с на весь прогон, включая обновление
# КП из Google-таблицы. Страницы качаются параллельно, поэтому цена города мала.
KP_PER_CITY_SEC = 1.0            # страница города: HTTP + сверка с КП
KP_OVERHEAD_SEC = 12
KP_MAP_PER_CITY_SEC = 28         # карта (Яндекс/2ГИС/Google) - браузер

PS_PER_URL_SEC = 32              # один замер PageSpeed API (мобайл)
PS_OVERHEAD_SEC = 20


def estimate_forms_seconds(forms: int, cities: int = 1, *,
                           admin: bool = False) -> tuple[int, int]:
    """Проверка форм: (минимум, максимум) секунд.
    forms - сколько форм выбрано, cities - сколько доменов/городов."""
    forms = max(0, int(forms))
    cities = max(1, int(cities))
    total = FORMS_OVERHEAD_SEC + FORMS_PER_FORM_SEC * forms * cities
    if admin:
        total += FORMS_ADMIN_SEC
    return int(total * _LOW_FACTOR), int(total * _HIGH_FACTOR)


def estimate_goals_seconds(pages: int, sites: int = 1, *,
                           with_forms: bool = False,
                           forms_count: int = 0,
                           run_orders: bool = True) -> tuple[int, int]:
    """Проверка целей: (минимум, максимум) секунд.

    pages - страниц в плане обхода на один сайт, sites - сколько сайтов/стран.
    Внутри целей гоняются формы: по умолчанию только сквозной заказ
    (run_orders), с галочкой «все формы» - полный прогон (forms_count форм).
    """
    pages = max(0, int(pages))
    sites = max(1, int(sites))
    total = GOALS_OVERHEAD_SEC + GOALS_PER_PAGE_SEC * pages * sites
    if with_forms:
        total += FORMS_OVERHEAD_SEC + FORMS_PER_FORM_SEC * max(0, int(forms_count))
    elif run_orders:
        total += GOALS_ORDERS_SEC
    return int(total * _LOW_FACTOR), int(total * _HIGH_FACTOR)


def estimate_kp_seconds(cities: int, *, check_site: bool = True,
                        maps: int = 0) -> tuple[int, int]:
    """Проверка КП: (минимум, максимум) секунд.
    maps - сколько источников карт включено (Яндекс/2ГИС/Google)."""
    cities = max(0, int(cities))
    total = KP_OVERHEAD_SEC
    if check_site:
        total += KP_PER_CITY_SEC * cities
    total += KP_MAP_PER_CITY_SEC * cities * max(0, int(maps))
    return int(total * _LOW_FACTOR), int(total * _HIGH_FACTOR)


def estimate_pagespeed_seconds(urls: int) -> tuple[int, int]:
    """Скорость страниц: (минимум, максимум) секунд. Замеры идут через API
    Google, поэтому время почти линейно числу адресов."""
    urls = max(0, int(urls))
    total = PS_OVERHEAD_SEC + PS_PER_URL_SEC * urls
    return int(total * _LOW_FACTOR), int(total * _HIGH_FACTOR)


def format_estimate(low_sec: int, high_sec: int) -> str:
    """
    Диапазон в человеческий вид: «≈ 12–20 мин».

    Округляем «наружу» (низ вниз, верх вверх) - обещать точность, которой у
    модели нет, хуже, чем показать честно широкий диапазон.
    """
    def _round(sec: int, up: bool) -> int:
        if sec < 600:                       # до 10 минут - шаг 1 мин
            step = 60
        elif sec < 3600:                    # до часа - шаг 5 мин
            step = 300
        else:                               # дальше - шаг 15 мин
            step = 900
        n = (sec + step - 1) // step if up else sec // step
        # Не опускаем границы до нуля: «0 мин» выглядит как «мгновенно»,
        # хотя даже пустой прогон стоит накладных.
        return max(60, n * step)

    low = _round(low_sec, up=False)
    high = _round(high_sec, up=True)
    if high <= low:
        high = low + 60

    def _txt(sec: int) -> str:
        if sec < 3600:
            return f'{sec // 60} мин'
        h, m = sec // 3600, (sec % 3600) // 60
        return f'{h} ч {m} мин' if m else f'{h} ч'

    # Низ без единиц, если единицы совпадают: «12–20 мин», а не «12 мин – 20 мин».
    if low >= 60 and low < 3600 and high < 3600:
        return f'≈ {low // 60}–{high // 60} мин'
    return f'≈ {_txt(low)} – {_txt(high)}'
