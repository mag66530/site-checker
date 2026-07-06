"""
meta_checker.py – проверка метаданных и дублей (пункт 1.8 чек-листа).

Что проверяем (по уже скачанному HTML, без доп. запросов):
  • title / meta description / H1 – есть и не пустые;
  • город поддомена встречается в title и description (стем-сравнение,
    чтобы «в Москве» совпало с городом «Москва») – ловит незамененные
    шаблоны и чужой город;
  • длины: title ~10–70, description ~50–160 – выход за рамки =
    предупреждение (не баг).

Дубли (пост-обработка всех результатов прогона):
  • внутри одного города (поддомена) одинаковый title/description/H1
    у разных страниц – баг;
  • между городами баг только при ПОЛНОМ совпадении title/description
    (значит, город не подставился в шаблон); тех. страницы исключаем –
    политики/доставка легитимно одинаковы на всех городах.

Дубли УРЛОВ (лёгкие доп. запросы, только главная и каталог поддомена):
  • варианты адреса – http://, без завершающего слэша, с www – должны
    301-редиректить на канонический вид; ответ 200 без редиректа = дубль.
"""
import asyncio
import re
from typing import Optional

# Мягкие пороги длин (предупреждение, не баг)
TITLE_MIN, TITLE_MAX = 10, 70
DESC_MIN, DESC_MAX = 50, 160


# ── Извлечение title / description / H1 ─────────────────────────────


_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
_DESC_RE = re.compile(
    r'<meta\b[^>]*name\s*=\s*["\']description["\'][^>]*>', re.I)
_DESC_RE2 = re.compile(  # content до name – тоже валидный порядок атрибутов
    r'<meta\b[^>]*content\s*=\s*["\']([^"\']*)["\'][^>]*name\s*=\s*["\']description["\']', re.I)
_CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.I)
_H1_RE = re.compile(r'<h1\b[^>]*>(.*?)</h1>', re.I | re.S)
_TAG_RE = re.compile(r'<[^>]+>')


def _clean(text: str) -> str:
    """Убрать теги и схлопнуть пробелы."""
    text = _TAG_RE.sub(' ', text or '')
    # Частые html-сущности (полноценный html.unescape не нужен для сравнения)
    import html as _html
    text = _html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def extract_meta(html: str) -> dict:
    """{'title': str|None, 'description': str|None, 'h1': str|None}."""
    head = html[:300_000]
    title = None
    m = _TITLE_RE.search(head)
    if m:
        title = _clean(m.group(1)) or None
    description = None
    m = _DESC_RE.search(head)
    if m:
        cm = _CONTENT_RE.search(m.group(0))
        if cm:
            description = _clean(cm.group(1)) or None
    if description is None:
        m = _DESC_RE2.search(head)
        if m:
            description = _clean(m.group(1)) or None
    h1 = None
    m = _H1_RE.search(html)
    if m:
        h1 = _clean(m.group(1)) or None
    return {'title': title, 'description': description, 'h1': h1}


# ── Город в тексте (стем-сравнение, терпимо к склонениям) ────────────


def _city_stems(city: str) -> list:
    """Стемы слов города: «Нижний Новгород» → ['нижн', 'новгород'].
    Отрезаем окончание (до 2 букв), чтобы «в Москве» совпало с «Москва»."""
    stems = []
    for w in re.split(r'[\s-]+', (city or '').strip().lower()):
        if len(w) < 3:
            continue
        stems.append(w[:len(w) - 1] if len(w) <= 4 else w[:len(w) - 2])
    return stems


def city_in_text(city: str, text: str) -> Optional[bool]:
    """Есть ли город в тексте (по стемам всех слов названия).
    None – город не задан/слишком короткий (проверить нельзя)."""
    stems = _city_stems(city)
    if not stems or not text:
        return None if not stems else False
    low = text.lower()
    return all(s in low for s in stems)


# ── Проверка метаданных одной страницы ──────────────────────────────

# Типы, где город обязан быть в title/description (SEO-шаблоны с городом).
_CITY_TYPES = {'main', 'catalog', 'category', 'filter', 'product'}


def check_meta(meta: dict, city: str, type_code: str) -> dict:
    """Проверить метаданные страницы. Возвращает dict для CheckResult.meta:
    {'title','description','h1','issues':[...],'warnings':[...]}"""
    title = meta.get('title')
    desc = meta.get('description')
    h1 = meta.get('h1')
    issues, warnings = [], []

    if not title:
        issues.append('нет title')
    if not desc:
        issues.append('нет meta description')
    if not h1 and type_code != 'tech':
        # На тех. страницах H1 не обязателен (и он уже есть в структурной
        # проверке для остальных) – здесь фиксируем для полноты картины.
        issues.append('нет заголовка H1')

    # Город в title/description – только для SEO-типов
    if type_code in _CITY_TYPES and city:
        if title and city_in_text(city, title) is False:
            issues.append(f'в title нет города «{city}»')
        if desc and city_in_text(city, desc) is False:
            issues.append(f'в description нет города «{city}»')

    # Длины – мягкие пороги, предупреждения
    if title:
        if len(title) < TITLE_MIN:
            warnings.append(f'title слишком короткий ({len(title)} символов)')
        elif len(title) > TITLE_MAX:
            warnings.append(f'title длинный ({len(title)} симв., рек. до {TITLE_MAX})')
    if desc:
        if len(desc) < DESC_MIN:
            warnings.append(f'description короткий ({len(desc)} символов)')
        elif len(desc) > DESC_MAX:
            warnings.append(f'description длинный ({len(desc)} симв., рек. до {DESC_MAX})')

    return {'title': title, 'description': desc, 'h1': h1,
            'issues': issues, 'warnings': warnings}


# ── Дубли title/description/H1 по результатам прогона ───────────────


def _norm_val(v: Optional[str]) -> Optional[str]:
    v = (v or '').strip().lower()
    return re.sub(r'\s+', ' ', v) or None


def find_duplicates(results) -> dict:
    """Найти дубли метаданных среди результатов прогона.

    Возвращает {'same_city': [...], 'cross_city': [...]} – списки групп:
    {'field': 'title'|'description'|'h1', 'value': str,
     'scope': subdomain|None, 'pages': [{'city','url','type_label'}]}"""
    same_city, cross_city = [], []
    ok = [r for r in results
          if getattr(r, 'meta', None) and r.is_ok]

    for field in ('title', 'description', 'h1'):
        # Внутри поддомена: любые повторы = дубль
        per_sub: dict = {}
        for r in ok:
            val = _norm_val(r.meta.get(field))
            if not val:
                continue
            per_sub.setdefault((r.subdomain, val), []).append(r)
        for (sub, val), rs in per_sub.items():
            # Разные URL с одним значением (одна страница могла попасть дважды)
            urls = {r.url for r in rs}
            if len(urls) > 1:
                same_city.append({
                    'field': field, 'value': rs[0].meta.get(field), 'scope': sub,
                    'pages': [{'city': r.city, 'url': r.url,
                               'type_label': r.type_label} for r in rs],
                })

        # Между городами: полное совпадение = город не подставился.
        # Тех. страницы исключаем (политики одинаковы легитимно).
        by_val: dict = {}
        for r in ok:
            if r.type_code == 'tech':
                continue
            val = _norm_val(r.meta.get(field))
            if not val:
                continue
            by_val.setdefault(val, []).append(r)
        for val, rs in by_val.items():
            subs = {r.subdomain for r in rs}
            if len(subs) > 1:
                cross_city.append({
                    'field': field, 'value': rs[0].meta.get(field), 'scope': None,
                    'pages': [{'city': r.city, 'url': r.url,
                               'type_label': r.type_label} for r in rs],
                })

    return {'same_city': same_city, 'cross_city': cross_city}


# ── Дубли УРЛОВ: варианты адреса должны редиректить ─────────────────


def _url_variants(url: str) -> list:
    """Варианты адреса, которые обязаны 301-редиректить на канонический вид."""
    from urllib.parse import urlsplit
    sp = urlsplit(url)
    host, path = sp.netloc, sp.path or '/'
    variants = []
    # 1. http:// вместо https://
    if sp.scheme == 'https':
        variants.append(('http', f'http://{host}{path}'))
    # 2. Без завершающего слэша (для главной «/» не бывает варианта)
    if path.endswith('/') and path != '/':
        variants.append(('без слэша', f'{sp.scheme}://{host}{path.rstrip("/")}'))
    elif not path.endswith('/') and path != '':
        variants.append(('со слэшем', f'{sp.scheme}://{host}{path}/'))
    # 3. www. – только для корневого домена (www.spb.… обычно без DNS)
    if not host.startswith('www.') and host.count('.') == 1:
        variants.append(('www', f'{sp.scheme}://www.{host}{path}'))
    return variants


async def _probe_variant(session, url, proxy_url, timeout_ms=20000):
    """Код первого ответа варианта БЕЗ следования редиректам.
    3xx – ок (редиректит), 200 – дубль, 4xx/сеть – дубля нет."""
    import aiohttp
    to = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    try:
        async with session.get(url, timeout=to, allow_redirects=False,
                               proxy=proxy_url) as r:
            loc = r.headers.get('Location')
            return r.status, loc
    except Exception:
        return None, None


async def check_url_duplicates(urls: list, *, proxy_url=None,
                               concurrency: int = 6) -> list:
    """Прозвонить варианты адресов (http/слэш/www) для списка канонических
    URL (обычно главная и каталог каждого поддомена).

    Возвращает список багов:
    [{'canonical': url, 'variant': url, 'kind': str, 'code': int}]"""
    import aiohttp
    from http_checker import make_browser_headers
    sem = asyncio.Semaphore(concurrency)
    out = []

    async def probe(canonical, kind, variant, session):
        async with sem:
            code, loc = await _probe_variant(session, variant, proxy_url)
        if code is not None and 200 <= code < 300:
            out.append({'canonical': canonical, 'variant': variant,
                        'kind': kind, 'code': code})

    tasks = []
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
    async with aiohttp.ClientSession(headers=make_browser_headers(),
                                     connector=connector) as session:
        seen = set()
        for u in urls:
            for kind, variant in _url_variants(u):
                if variant in seen:
                    continue
                seen.add(variant)
                tasks.append(probe(u, kind, variant, session))
        if tasks:
            await asyncio.gather(*tasks)
    return out
