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

# Фиксированная надбавка на весь прогон, от объёма не зависит.
FIXED_SEC = {
    # Браузерные поездки - дорого и почти не зависит от числа страниц.
    'check_index_404': 180,
    'check_gsc_pages': 150,
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
    'check_yabusiness': 60,
    'check_404': 30,
    'check_traffic': 30,
    'check_link_profile': 30,
    'check_anomaly': 30,
    'check_trust': 25,
    'check_static': 20,
    'fetch_notifications': 40,
    'fetch_metrika_404': 30,
}

# Во что превращаем итог. Диапазон несимметричный: прогон чаще затягивается
# (ретраи, тормозящий сайт, лимиты сервисов), чем идёт быстрее ожидаемого.
_LOW_FACTOR = 0.7
_HIGH_FACTOR = 1.45


def estimate_run_seconds(pages: int, cities: int, checks: dict) -> tuple[int, int]:
    """
    Примерная длительность прогона в секундах: (минимум, максимум).

    pages  - сколько страниц проверяем всего (города × страниц на город)
    cities - сколько городов (поддоменов) в выборке
    checks - {'check_console': True, ...}; ключи как в flags прогона,
             неизвестные игнорируем (не падаем на новых галочках)
    """
    pages = max(0, int(pages))
    cities = max(0, int(cities))

    total = OVERHEAD_SEC + BASE_PER_PAGE_SEC * pages

    for key, on in (checks or {}).items():
        if not on:
            continue
        total += PER_PAGE_SEC.get(key, 0.0) * pages
        total += PER_CITY_SEC.get(key, 0.0) * cities
        total += FIXED_SEC.get(key, 0)

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
