"""
sources.py – загрузка каталогов и построение плана проверки.

Работает с CSV-файлами (быстрее xlsx в десятки раз).

Структура CSV:
    {proj}-subdomains.csv  → колонки: url, city
    {proj}-catalog.csv     → колонки: url, type (категория | тег)
    {proj}-categories.csv  → колонка: url (опционально, заменяет категории из каталога)

Главная функция:
    load_sources(project: dict) -> Sources

Сборка плана:
    build_plan(sources, options) -> Plan
"""
import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).parent


@dataclass
class Subdomain:
    """Один поддомен: URL + город + хост."""
    url: str
    city: str
    host: str
    country: str = ''        # «Россия» / «Казахстан» / … (для правила выборки СНГ)


@dataclass
class Sources:
    """Загруженные данные проекта."""
    subdomains: list[Subdomain]
    categories: list[str]          # pathname'ы категорий
    filters: list[str]             # pathname'ы фильтров
    products: list[str] = field(default_factory=list)  # из sitemap, добавится потом

    @property
    def stats(self) -> dict:
        return {
            'subdomains_count': len(self.subdomains),
            'categories_count': len(self.categories),
            'filters_count': len(self.filters),
            'products_count': len(self.products),
            'has_filters': len(self.filters) > 0,
        }


@dataclass
class CheckTask:
    """Одна задача проверки (URL для запроса + контекст)."""
    url: str
    city: str
    subdomain: str
    type_code: str       # 'main', 'catalog', 'category', 'filter', 'product', 'custom'
    type_label: str      # 'Главная', 'Каталог', ...


@dataclass
class Plan:
    """План проверки."""
    tasks: list[CheckTask]
    selected_subdomains: list[Subdomain]


# ── Парсеры CSV ─────────────────────────────────────────────────────


def _read_csv(path: Path) -> list[dict]:
    """Прочесть CSV как список словарей."""
    with open(path, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def parse_subdomains(csv_path: Path) -> list[Subdomain]:
    """Загрузить поддомены из CSV."""
    rows = _read_csv(csv_path)
    result = []
    seen = set()
    for r in rows:
        url = (r.get('url') or '').strip()
        city = (r.get('city') or '').strip() or '(без названия)'
        country = (r.get('country') or r.get('Страна') or '').strip()
        if not url.startswith('http'):
            continue
        try:
            host = urlparse(url).hostname
        except ValueError:
            continue
        if not host or host in seen:
            continue
        seen.add(host)
        result.append(Subdomain(url=url, city=city, host=host, country=country))
    return result


def parse_catalog(csv_path: Path) -> tuple[list[str], list[str]]:
    """
    Загрузить каталог. Возвращает (категории, фильтры) – pathname'ы.
    Тип 'категория' → categories, тип 'тег' → filters.
    """
    rows = _read_csv(csv_path)
    categories = []
    filters = []
    for r in rows:
        url = (r.get('url') or '').strip()
        typ = (r.get('type') or '').strip()
        if not url.startswith('http'):
            continue
        try:
            pathname = urlparse(url).path
        except ValueError:
            continue
        if typ == 'категория':
            categories.append(pathname)
        elif typ == 'тег':
            filters.append(pathname)
    return categories, filters


def parse_categories_file(csv_path: Path) -> list[str]:
    """Отдельный файл актуальных категорий (для СМУ)."""
    rows = _read_csv(csv_path)
    result = []
    for r in rows:
        url = (r.get('url') or '').strip()
        if not url.startswith('http'):
            continue
        try:
            result.append(urlparse(url).path)
        except ValueError:
            continue
    return result


# ── Главная функция ─────────────────────────────────────────────────


def load_project_config(project_id: str) -> dict:
    """Прочитать JSON конфига проекта."""
    config_path = PROJECT_ROOT / 'projects' / f'{project_id}.json'
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_sources(project: dict) -> Sources:
    """Загрузить всё, что нужно для построения плана проверки."""
    sub_path = PROJECT_ROOT / project['subdomains_csv']
    cat_path = PROJECT_ROOT / project['catalog_csv']

    subdomains = parse_subdomains(sub_path)
    categories, filters = parse_catalog(cat_path)

    # Если есть отдельный файл актуальных категорий – он замещает
    if project.get('categories_csv'):
        categories_extra_path = PROJECT_ROOT / project['categories_csv']
        if categories_extra_path.exists():
            categories = parse_categories_file(categories_extra_path)

    return Sources(
        subdomains=subdomains,
        categories=categories,
        filters=filters,
    )


def list_projects() -> list[dict]:
    """Список всех доступных проектов."""
    projects_dir = PROJECT_ROOT / 'projects'
    result = []
    for f in sorted(projects_dir.glob('*.json')):
        with open(f, 'r', encoding='utf-8') as fp:
            cfg = json.load(fp)
        result.append({
            'id': cfg['id'],
            'name': cfg['name'],
            'root_domain': cfg.get('root_domain'),
        })
    return result


# ── Построение плана ────────────────────────────────────────────────


# Метки для типов задач – на русском, для отчёта
TYPE_LABELS = {
    'main': 'Главная',
    'catalog': 'Каталог',
    'category': 'Категория',
    'filter': 'Фильтр',
    'product': 'Товар',
    'custom': 'URL',
    'tech': 'Тех. страница',
}


# Технические/служебные страницы (на главном домене проекта). У каждого проекта
# свои slug'и. Проверяем в первую очередь доступность (открывается / 404).
_TECH_PATHS_SMU = [
    '/about/',
    '/payment/',
    '/delivery/',
    '/contacts/',
    '/vacancy/',
    '/sitemap/',
    '/soglasie-na-obrabotku-personalnyh-dannyh/',
    '/soglasie-na-poluchenie-reklamnoy-informacii/',
    '/polzovatelskoe-soglashenie/',
    '/polzovatelskoe-soglashenie-ob-ispolzovanii-cookie-faylov/',
]
_TECH_PATHS_IMP = [
    '/o-kompanii/', '/faq/', '/vakansii/', '/nashi-postavshiki/', '/rekvizity/',
    '/contact/', '/oplata/', '/dostavka/', '/pravila-otgruzki/', '/kontrol-kachestva/',
    '/komplektaciya-zakaza/', '/upakovka-zakaza/', '/vozvrat-tovara/', '/garantii/',
    '/price-list/', '/kak-sdelat-pokupku/', '/uslugi-metalloobrabotki/',
    '/proizvodstvo-metalloprokata/', '/specials/', '/postavshhikam/',
    '/polzovatelskoe-soglashenie/', '/politika-konfidentsialnosti/', '/sitemap/', '/search/',
]
TECH_PAGE_PATHS = {
    'smu': _TECH_PATHS_SMU,
    'smu-test': _TECH_PATHS_SMU,   # тест-стенд СМУ – те же страницы
    'imp': _TECH_PATHS_IMP,
    'mpe': [
        '/about/', '/catalog/spetstekhnika/', '/payment_delivery/',
        '/contacts/', '/rekvizity/', '/sitemap/',
    ],
}


def get_tech_paths(project_id: str) -> list[str]:
    """Тех. страницы проекта. Нет в карте → пусто (не проверяем, чтобы не ловить
    ложные 404 от чужих slug'ов)."""
    return TECH_PAGE_PATHS.get(project_id, [])


# Человеческие названия тех. страниц для отчёта (ключ – путь). Чтобы в таблице
# было «Оплата», а не «/oplata/». Подписи короткие – помещаются в первый столбец.
TECH_PAGE_LABELS = {
    # общие
    '/about/': 'О компании',
    '/o-kompanii/': 'О компании',
    '/contacts/': 'Контакты',
    '/contact/': 'Контакты',
    '/sitemap/': 'Карта сайта',
    '/rekvizity/': 'Реквизиты',
    '/vacancy/': 'Вакансии',
    '/vakansii/': 'Вакансии',
    '/polzovatelskoe-soglashenie/': 'Пользовательское соглашение',
    '/politika-konfidentsialnosti/': 'Политика конфиденциальности',
    # СМУ
    '/payment/': 'Оплата',
    '/delivery/': 'Доставка',
    '/soglasie-na-obrabotku-personalnyh-dannyh/': 'Согласие на обработку ПД',
    '/soglasie-na-poluchenie-reklamnoy-informacii/': 'Согласие на рекламу',
    '/polzovatelskoe-soglashenie-ob-ispolzovanii-cookie-faylov/': 'Соглашение по cookie',
    # ИМП
    '/faq/': 'Вопрос-ответ',
    '/nashi-postavshiki/': 'Наши поставщики',
    '/oplata/': 'Оплата',
    '/dostavka/': 'Доставка',
    '/pravila-otgruzki/': 'Правила отгрузки',
    '/kontrol-kachestva/': 'Контроль качества',
    '/komplektaciya-zakaza/': 'Комплектация заказа',
    '/upakovka-zakaza/': 'Упаковка заказа',
    '/vozvrat-tovara/': 'Возврат товара',
    '/garantii/': 'Гарантии',
    '/price-list/': 'Прайс-лист',
    '/kak-sdelat-pokupku/': 'Как сделать покупку',
    '/catalog/': 'Каталог',
    '/uslugi-metalloobrabotki/': 'Услуги металлообработки',
    '/proizvodstvo-metalloprokata/': 'Наше производство',
    '/specials/': 'Спецпредложения',
    '/postavshhikam/': 'Поставщикам',
    '/search/': 'Поиск по товару',
    # МПЭ
    '/catalog/spetstekhnika/': 'Спецтехника',
    '/payment_delivery/': 'Оплата и доставка',
}


def tech_page_label(path: str) -> str:
    """Человеческое имя тех. страницы по пути; нет в карте → сам путь."""
    if not path:
        return path or ''
    p = path if path.endswith('/') else path + '/'
    return TECH_PAGE_LABELS.get(p, path)


def _pick_random(items: list, n: int, rng: random.Random) -> list:
    """Выбрать n случайных элементов."""
    if n >= len(items):
        return list(items)
    return rng.sample(items, n)


def build_plan(
    sources: Sources,
    *,
    random_subdomains_count: int = 5,
    categories_per_subdomain: int = 5,
    filters_per_subdomain: int = 5,
    products_per_subdomain: int = 3,
    check_main: bool = True,
    check_catalog: bool = True,
    check_categories: bool = True,
    check_filters: bool = True,
    check_products: bool = True,
    mandatory_city: str = 'Москва',
    mandatory_hosts: Optional[list[str]] = None,  # домены, всегда в выборке (напр. smg.az)
    cis_extra_subdomains: int = 0,      # сколько доп. СНГ-доменов помимо mandatory_hosts
    seed: Optional[int] = None,
    rotation_history: Optional[set[str]] = None,  # pathname'ы проверенные за 7 дней
) -> Plan:
    """
    Построить план проверки: Москва + обязательные домены + доп. СНГ + N случайных.

    mandatory_hosts – хосты, которые включаем ВСЕГДА (напр. ['smg.az'] для СМУ).
    cis_extra_subdomains – сколько ещё СНГ-доменов (country != «Россия») гарантированно
        добавить помимо mandatory_hosts (0 для быстрой, 1 для стандартной/полной).

    Если передана rotation_history – pathname'ы из неё получают меньший
    вес (в 3 раза реже попадают в выборку), но не исключаются полностью.
    """
    rng = random.Random(seed)

    # 1) Главный город (Москва) – всегда.
    mandatory = next((s for s in sources.subdomains if s.city == mandatory_city), None)
    selected = [mandatory] if mandatory else []
    chosen_hosts = {s.host for s in selected}

    # 2) Обязательные домены (напр. smg.az) – всегда, помимо города.
    for h in (mandatory_hosts or []):
        if h in chosen_hosts:
            continue
        sub = next((s for s in sources.subdomains if s.host == h), None)
        if sub:
            selected.append(sub); chosen_hosts.add(sub.host)

    # 3) Доп. СНГ-домены (country != «Россия»), помимо уже выбранных.
    if cis_extra_subdomains > 0:
        cis_pool = [s for s in sources.subdomains
                    if s.host not in chosen_hosts
                    and s.country and s.country.strip().lower() != 'россия']
        for s in _pick_random(cis_pool, cis_extra_subdomains, rng):
            selected.append(s); chosen_hosts.add(s.host)

    # 4) Остальные – случайные из оставшихся (steelgroup.az и пр. могут попасть сюда).
    rest = [s for s in sources.subdomains if s.host not in chosen_hosts]
    selected += _pick_random(rest, random_subdomains_count, rng)

    # Если есть история ротации – используем weighted_sample вместо обычного _pick_random
    from history import weighted_sample
    recent = rotation_history or set()

    def pick(items: list[str], n: int) -> list[str]:
        if not recent:
            return _pick_random(items, n, rng)
        return weighted_sample(items, n, recent, rng)

    tasks = []
    for sub in selected:
        base = sub.url.rstrip('/')

        if check_main:
            tasks.append(CheckTask(
                url=sub.url, city=sub.city, subdomain=sub.host,
                type_code='main', type_label=TYPE_LABELS['main'],
            ))
        if check_catalog:
            tasks.append(CheckTask(
                url=f'{base}/catalog/', city=sub.city, subdomain=sub.host,
                type_code='catalog', type_label=TYPE_LABELS['catalog'],
            ))
        if check_categories and sources.categories:
            for path in pick(sources.categories, categories_per_subdomain):
                tasks.append(CheckTask(
                    url=f'{base}{path}', city=sub.city, subdomain=sub.host,
                    type_code='category', type_label=TYPE_LABELS['category'],
                ))
        if check_filters and sources.filters:
            for path in pick(sources.filters, filters_per_subdomain):
                tasks.append(CheckTask(
                    url=f'{base}{path}', city=sub.city, subdomain=sub.host,
                    type_code='filter', type_label=TYPE_LABELS['filter'],
                ))
        if check_products and sources.products:
            for path in pick(sources.products, products_per_subdomain):
                tasks.append(CheckTask(
                    url=f'{base}{path}', city=sub.city, subdomain=sub.host,
                    type_code='product', type_label=TYPE_LABELS['product'],
                ))

    return Plan(tasks=tasks, selected_subdomains=selected)


def build_custom_tasks_typed(
    urls: list[str],
    sources: 'Sources | None' = None,
) -> list[CheckTask]:
    """
    Задачи для своего списка URL **в контексте проекта**: тип определяется
    по адресу. Сначала точное совпадение со списками проекта (категории /
    фильтры), затем правила пути:
      /                    → Главная
      /catalog/            → Каталог
      …/filter/…           → Фильтр
      /catalog/x/          → Категория
      /catalog/x/y(/…)     → Товар
      прочее               → URL (общая проверка)
    """
    known_cats = set(sources.categories) if sources else set()
    known_filters = set(sources.filters) if sources else set()
    city_by_host = {s.host: s.city for s in (sources.subdomains if sources else [])}

    tasks = []
    seen = set()
    for raw in urls:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url or url.startswith('#'):
            continue
        if '#' in url:
            url = url.split('#', 1)[0].strip()
        if not url:
            continue
        if not url.startswith(('http://', 'https://')):
            if '.' in url:
                url = 'https://' + url
            else:
                continue
        if url in seen:
            continue
        seen.add(url)

        try:
            parsed = urlparse(url)
            host = parsed.hostname or ''
        except ValueError:
            continue

        path = parsed.path or '/'
        if not path.endswith('/'):
            path += '/'

        # ── Тип по адресу ──
        if path in known_filters or '/filter/' in path:
            tcode = 'filter'
        elif path in known_cats:
            tcode = 'category'
        elif path == '/':
            tcode = 'main'
        elif path == '/catalog/':
            tcode = 'catalog'
        elif path.startswith('/catalog/'):
            segments = [s for s in path.split('/') if s]
            # /catalog/x/ → категория; /catalog/x/y/ и глубже → товар
            tcode = 'category' if len(segments) == 2 else 'product'
        else:
            tcode = 'custom'

        tasks.append(CheckTask(
            url=url,
            city=city_by_host.get(host, ''),
            subdomain=host,
            type_code=tcode,
            type_label=TYPE_LABELS[tcode],
        ))

    return tasks


def build_custom_plan(urls: list[str]) -> Plan:
    """План для произвольного списка URL (custom-режим)."""
    tasks = []
    seen = set()
    for raw in urls:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url or url.startswith('#'):
            continue
        # Отрезаем комментарий после #
        if '#' in url:
            url = url.split('#', 1)[0].strip()
        if not url:
            continue
        # Добавляем https:// если нет протокола
        if not url.startswith(('http://', 'https://')):
            if '.' in url:
                url = 'https://' + url
            else:
                continue
        if url in seen:
            continue
        seen.add(url)

        try:
            host = urlparse(url).hostname or ''
        except ValueError:
            continue

        tasks.append(CheckTask(
            url=url, city='', subdomain=host,
            type_code='custom', type_label=TYPE_LABELS['custom'],
        ))

    return Plan(tasks=tasks, selected_subdomains=[])
