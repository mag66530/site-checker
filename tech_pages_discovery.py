"""
tech_pages_discovery.py - поиск технических (служебных информационных) страниц
проекта по самому сайту.

Зачем: списки тех. страниц заведены руками в sources.TECH_PAGE_PATHS, и для
проектов, где список не завели (МПК, МТТ, STB, стенд МПИ), весь блок служебных
страниц просто выпадал из отчёта - «О компании», «Доставка», «Оплата»,
«Политика конфиденциальности» никто не проверял. Заводить руками каждый новый
проект - гарантированная дыра: список забывают.

Как ищем: берём главную страницу и собираем ссылки из её разметки (шапка и
подвал - там служебные страницы и живут), отсеиваем каталог, товары и
служебные адреса движка, а оставшихся кандидатов прозваниваем. В список
попадают только те, что реально отвечают 200 и не редиректят на главную.
Подпись берём из текста самой ссылки - в отчёте будет «Оплата и доставка»,
как называет её сайт, а не голый путь.

Ручной список ВСЕГДА главнее: если у проекта он есть, автопоиск не
запускается (у СМУ/ИМП/МПЭ/МПИ/SHOPMET/АПС списки собраны и выверены вживую,
там же учтены нюансы вроде адресов без завершающего слеша).

Результат кешируется в cache/techpaths-<pid>.json на неделю: набор служебных
страниц меняется редко, а лишний обход главной в каждом прогоне не нужен.

Служебные адреса ДВИЖКА (корзина, поиск, ЛК, админка) сюда намеренно не идут:
им в индексе и в отчёте не место, их отдельно ловит index_tech_pages.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / 'cache'

# Сколько кандидатов прозваниваем и сколько страниц оставляем в итоге.
MAX_CANDIDATES = 40
MAX_PAGES = 25
CACHE_TTL_DAYS = 7
_TIMEOUT_S = 20
_CONCURRENCY = 8

_RE_A = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.I | re.S)
_RE_HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_TAG = re.compile(r'<[^>]+>')
_RE_TITLE = re.compile(r'<title\b[^>]*>(.*?)</title>', re.I | re.S)

# Файлы: ссылка на прайс.pdf или картинку - не страница.
_SKIP_EXT = ('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.zip', '.rar',
             '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico',
             '.xml', '.txt', '.mp4', '.mp3')

# Каталог, товары, теги: это НЕ служебные страницы, их проверяет своя часть
# прогона (категории/фильтры/товары из каталога проекта).
_SKIP_PARTS = ('/catalog', '/filter/', '/product', '/tovar', '/tag/',
               '/brand', '/collection')

# Разделы, где служебная страница законно лежит на втором уровне
# (/company/about/, /info/delivery/). В остальных случаях берём только первый
# уровень: /news/kak-my-otgruzili-trubu/ - это статья, а не служебная страница,
# и такие адреса в блок тех. страниц не нужны.
_ALLOW_DEEP_ROOTS = ('company', 'info', 'about', 'pages', 'o-kompanii',
                     'kompaniya')


def clean_label(raw: str) -> str:
    """Текст ссылки → подпись страницы. Внутри <a> бывают <span>/<svg>, а сам
    текст - с переносами. ЧИСТАЯ функция - есть юнит-тест."""
    text = _RE_TAG.sub(' ', raw or '')
    text = re.sub(r'&nbsp;?', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:60]


# Типовые слаги служебных страниц → подпись для отчёта. Нужны потому, что
# текст ссылки не всегда название страницы: у STB ссылка на политику стоит
# внутри чекбокса согласия и читается как «политикой обработки персональных
# данных». Слаг вида /privacy тут надёжнее.
_SLUG_LABELS = (
    ('o-kompanii', 'О компании'), ('about', 'О компании'),
    ('company', 'О компании'),
    ('contact', 'Контакты'), ('kontakt', 'Контакты'),
    ('dostavka', 'Доставка'), ('deliver', 'Доставка'), ('shipping', 'Доставка'),
    ('oplat', 'Оплата'), ('payment', 'Оплата'),
    ('privacy', 'Политика конфиденциальности'),
    ('politika', 'Политика конфиденциальности'),
    ('policy', 'Политика конфиденциальности'),
    ('personal-data', 'Политика конфиденциальности'),
    ('cookie', 'Соглашение по cookie'),
    ('soglashenie', 'Пользовательское соглашение'),
    ('agreement', 'Пользовательское соглашение'),
    ('soglasie', 'Согласие на обработку ПД'),
    ('oferta', 'Оферта'), ('offer', 'Оферта'),
    ('rekvizit', 'Реквизиты'), ('requisites', 'Реквизиты'),
    ('vakans', 'Вакансии'), ('vacanc', 'Вакансии'), ('career', 'Вакансии'),
    ('postavshik', 'Поставщики'), ('postavshhik', 'Поставщики'),
    ('provider', 'Поставщики'), ('partner', 'Партнёры'),
    ('garanti', 'Гарантии'), ('warranty', 'Гарантии'),
    ('vozvrat', 'Возврат товара'), ('return', 'Возврат товара'),
    ('faq', 'Вопрос-ответ'), ('vopros', 'Вопрос-ответ'),
    ('price', 'Прайс-лист'), ('prays', 'Прайс-лист'),
    ('sitemap', 'Карта сайта'), ('otzyv', 'Отзывы'), ('review', 'Отзывы'),
    ('news', 'Новости'), ('blog', 'Блог'), ('sertifikat', 'Сертификаты'),
    ('certificate', 'Сертификаты'), ('uslug', 'Услуги'),
    ('proizvodstv', 'Производство'), ('otgruzk', 'Отгрузки'),
)


def label_for(path: str, anchor_text: str = '') -> str:
    """Подпись служебной страницы для отчёта.

    Сначала типовой слаг адреса (устойчиво), потом текст ссылки, и только
    затем сам путь. Текст ссылки берём, лишь если он похож на название -
    короткий и с заглавной буквы: фразы вроде «соглашаюсь на обработку»
    заголовком строки в отчёте быть не должны.
    ЧИСТАЯ функция - есть юнит-тест."""
    p = (path or '').lower()
    for slug, label in _SLUG_LABELS:
        if slug in p:
            return label
    t = (anchor_text or '').strip()
    if 2 <= len(t) <= 40 and t[:1].isupper():
        return t
    return path or ''


def is_tech_candidate(path: str) -> bool:
    """Похож ли путь на служебную информационную страницу сайта.

    Отсекаем: главную, файлы, каталог/товары, служебные адреса движка
    (корзина/поиск/ЛК/админка - у них своя проверка) и глубокую вложенность
    (статьи новостей/блога). ЧИСТАЯ функция - есть юнит-тест."""
    p = (path or '').strip().lower()
    if not p.startswith('/') or p in ('/', ''):
        return False
    if p.endswith(_SKIP_EXT):
        return False
    if any(part in p for part in _SKIP_PARTS):
        return False
    # Служебные адреса движка сюда не тащим (см. модуль index_tech_pages).
    try:
        from index_tech_pages import classify_tech_url
        if classify_tech_url(f'https://x{p}'):
            return False
    except Exception:
        pass
    segs = [s for s in p.split('/') if s]
    if len(segs) == 1:
        return True
    if len(segs) == 2 and segs[0] in _ALLOW_DEEP_ROOTS:
        return True
    return False


def norm_path(path: str) -> str:
    """Путь для сравнения: без завершающего слеша и регистра."""
    return (path or '').rstrip('/').lower() or '/'


def known_catalog_paths(project_id: str) -> set:
    """Пути каталога проекта (категории/фильтры) из catalogs/<pid>-catalog.csv
    и <pid>-categories.csv.

    Нужны, чтобы не принять категорию за служебную страницу: у части проектов
    (МТТ, STB) каталог лежит прямо в корне - /armatura, /truby - и по виду
    адреса такая ссылка неотличима от /dostavka. Категории проверяются своей
    частью прогона, в блоке тех. страниц им не место."""
    out = set()
    for name in (f'{project_id}-catalog.csv', f'{project_id}-categories.csv'):
        f = ROOT / 'catalogs' / name
        if not f.is_file():
            continue
        try:
            import csv
            with open(f, encoding='utf-8-sig', newline='') as fh:
                for row in csv.DictReader(fh):
                    u = (row.get('url') or row.get('URL') or '').strip()
                    if not u:
                        continue
                    p = urlsplit(u).path if u.startswith('http') else u
                    if p and p != '/':
                        out.add(norm_path(p))
        except Exception:
            continue
    return out


def candidate_links(html: str, host: str, known_paths: set = None) -> list:
    """Ссылки главной → [{'path', 'label'}] кандидатов в тех. страницы.

    Только СВОЙ хост (ссылка на другой город - это другой сайт со своим
    прогоном), без query и якорей, дедуп по пути; порядок сохраняем -
    в шапке и подвале служебные страницы идут раньше прочего. known_paths -
    пути каталога проекта, их отсеиваем (см. known_catalog_paths).
    ЧИСТАЯ функция - есть юнит-тест."""
    host = (host or '').lower().removeprefix('www.')
    known = known_paths or set()
    out, seen = [], set()
    for m in _RE_A.finditer((html or '')[:800_000]):
        hm = _RE_HREF.search(m.group(1))
        if not hm:
            continue
        href = hm.group(1).strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:',
                                        'data:')):
            continue
        sp = urlsplit(urljoin(f'https://{host}/', href))
        h = (sp.netloc or '').lower().removeprefix('www.')
        if h and h != host:
            continue
        path = sp.path or '/'
        if not is_tech_candidate(path):
            continue
        key = norm_path(path)
        if key in seen or key in known:
            continue
        seen.add(key)
        out.append({'path': path, 'label': clean_label(m.group(2))})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


# ── Прозвон кандидатов ───────────────────────────────────────────────

async def _fetch(session, url, proxy_url):
    """(status, финальный путь, html). С редиректами: сайт сам приводит адрес
    к канонической форме (со слешем или без) - берём то, что он отдал."""
    to = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    try:
        async with session.get(url, timeout=to, proxy=proxy_url,
                               allow_redirects=True) as r:
            html = await r.text(errors='replace') if r.status == 200 else ''
            return r.status, (r.url.path or '/'), html
    except Exception:
        return None, '', ''


# Признаки товарного листинга/карточки: цена в микроразметке, тип Product,
# кнопки заказа. У служебных страниц («О компании», «Оплата», оферта) их нет -
# проверено на живых сайтах: там 0, на категории - десятки.
_RE_PRICE_PROP = re.compile(r'itemprop\s*=\s*["\']price', re.I)
_RE_PRODUCT_TYPE = re.compile(r'itemtype\s*=\s*["\'][^"\']*product', re.I)
_RE_CART = re.compile(r'add-to-cart|tocart|в\s+корзину', re.I)


def looks_like_listing(html: str) -> bool:
    """Товарная страница (категория/листинг/карточка), а не служебная.

    Нужно для проектов, где каталог лежит прямо в корне (/armatura, /truby):
    по адресу такую страницу от /dostavka не отличить, а по разметке - легко.
    Каталог наш прогон проверяет отдельно, дублировать его в блоке служебных
    страниц незачем. ЧИСТАЯ функция - есть юнит-тест."""
    h = (html or '')[:600_000]
    if len(_RE_PRICE_PROP.findall(h)) >= 2:
        return True
    if _RE_PRODUCT_TYPE.search(h):
        return True
    return len(_RE_CART.findall(h)) >= 2


def page_alive(status, final_path: str, html: str) -> bool:
    """Живая ли служебная страница: 200, не редирект на главную и не
    заглушка «страница не найдена» (soft-404 у части движков отдаёт 200).
    ЧИСТАЯ функция - есть юнит-тест."""
    if status != 200:
        return False
    if not (final_path or '/').strip('/'):
        return False            # увели на главную - значит страницы нет
    m = _RE_TITLE.search(html or '')
    if m:
        from index_pages_checker import looks_soft_404
        if looks_soft_404(re.sub(r'\s+', ' ', m.group(1)).strip()):
            return False
    return True


async def _discover(host: str, proxy_url, log, known_paths: set = None) -> list:
    from http_checker import make_browser_headers
    home = f'https://{host}/'
    async with aiohttp.ClientSession(
            headers=make_browser_headers(url=home)) as session:
        st, _fp, html = await _fetch(session, home, proxy_url)
        if st != 200 or not html:
            log(f'  главная {host} не открылась (код {st}) - искать негде')
            return []
        cands = candidate_links(html, host, known_paths)
        log(f'  кандидатов со ссылок главной: {len(cands)}')
        if not cands:
            return []

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _one(c):
            async with sem:
                st_, fp, h = await _fetch(session, f'https://{host}{c["path"]}',
                                          proxy_url)
            if not page_alive(st_, fp, h):
                return None
            # Куда привёл редирект, заранее не известно: ссылка «Корзина» с
            # нетиповым слагом уводит на /checkout/cart. Финальный адрес
            # проверяем теми же правилами, что и кандидата.
            if not is_tech_candidate(fp):
                return None
            if looks_like_listing(h):
                return None     # это каталог, его проверяет своя часть прогона
            return {'path': fp, 'label': label_for(fp, c['label'])}

        found = await asyncio.gather(*[_one(c) for c in cands])

    out, seen = [], set()
    for p in found:
        if not p:
            continue
        key = p['path'].rstrip('/').lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= MAX_PAGES:
            break
    return out


# ── Кеш ──────────────────────────────────────────────────────────────

def cache_path(project_id: str) -> Path:
    return CACHE_DIR / f'techpaths-{project_id}.json'


def cache_fresh(data: dict, host: str, *, ttl_days: int = CACHE_TTL_DAYS,
                now: datetime = None) -> bool:
    """Годен ли кеш: тот же хост и обновлён не позже ttl_days назад. Набор
    служебных страниц меняется редко, обходить главную каждый прогон незачем.
    ЧИСТАЯ функция - есть юнит-тест."""
    if not data or not data.get('pages'):
        return False
    if (data.get('host') or '') != host:
        return False
    try:
        upd = datetime.fromisoformat(str(data.get('updated')))
    except (TypeError, ValueError):
        return False
    return (now or datetime.now()) - upd <= timedelta(days=ttl_days)


def _read_cache(project_id: str) -> dict:
    f = cache_path(project_id)
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _write_cache(project_id: str, host: str, pages: list) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_path(project_id).write_text(json.dumps(
            {'host': host, 'updated': datetime.now().isoformat(timespec='seconds'),
             'pages': pages}, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass                    # кеш - удобство, а не условие работы


def tech_paths_for_project(project_id: str, host: str, *, proxy_url=None,
                           ttl_days: int = CACHE_TTL_DAYS, log=None) -> list:
    """Тех. страницы проекта: [{'path', 'label'}].

    Порядок источников:
      1. ручной список sources.TECH_PAGE_PATHS - он выверен вживую, автопоиск
         его не трогает;
      2. свежий кеш cache/techpaths-<pid>.json;
      3. живой обход главной.
    Сеть недоступна и кеша нет → пустой список (прогон не падает, просто без
    блока служебных страниц - как было до этого модуля)."""
    def _log(m):
        if not log:
            return
        try:
            log('info', m)
        except TypeError:
            log(m)

    try:
        import sources
        manual = sources.get_tech_paths(project_id)
    except Exception:
        manual = []
    if manual:
        return [{'path': p, 'label': _manual_label(p)} for p in manual]

    cached = _read_cache(project_id)
    if cache_fresh(cached, host, ttl_days=ttl_days):
        _log(f'Тех. страницы: беру из кеша ({len(cached["pages"])} шт., '
             f'обновлён {cached.get("updated", "?")})')
        return cached['pages']

    _log(f'Тех. страницы: списка у проекта нет - ищу по ссылкам главной {host}…')
    known = known_catalog_paths(project_id)
    try:
        pages = asyncio.run(_discover(host, proxy_url, _log, known))
    except Exception as e:
        _log(f'⚠ Тех. страницы: автопоиск не удался ({e})')
        pages = cached.get('pages') or []      # протухший кеш лучше пустоты
        return pages
    if pages:
        _write_cache(project_id, host, pages)
        _log('Тех. страницы найдены: '
             + ', '.join(f'{p["label"]} ({p["path"]})' for p in pages))
    else:
        _log('Тех. страницы: по ссылкам главной ничего не подтвердилось')
    return pages


def _manual_label(path: str) -> str:
    try:
        import sources
        return sources.tech_page_label(path)
    except Exception:
        return path


def _main():
    import argparse
    ap = argparse.ArgumentParser(
        description='Поиск тех. страниц проекта по ссылкам главной')
    ap.add_argument('project')
    ap.add_argument('--host', help='хост (по умолчанию - из конфига проекта)')
    ap.add_argument('--proxy')
    ap.add_argument('--fresh', action='store_true', help='игнорировать кеш')
    a = ap.parse_args()

    host = a.host
    if not host:
        from sources import load_project_config
        cfg = load_project_config(a.project)
        host = (cfg.get('main_url') or cfg.get('root_domain') or '')
        host = host.replace('https://', '').replace('http://', '').strip('/')
    pages = tech_paths_for_project(a.project, host, proxy_url=a.proxy,
                                   ttl_days=0 if a.fresh else CACHE_TTL_DAYS,
                                   log=lambda m: print(m, flush=True))
    print(f'\nНайдено {len(pages)}:')
    for p in pages:
        print(f'   {p["path"]:<45} {p["label"]}')


if __name__ == '__main__':
    _main()
