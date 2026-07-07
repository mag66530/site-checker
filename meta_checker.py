"""
meta_checker.py - проверка метаданных и дублей (пункт 1.8 чек-листа)
+ единственность ключевых SEO-тегов (пункт 1.3.1, см. низ файла).

Что проверяем (по уже скачанному HTML, без доп. запросов):
  • title / meta description / H1 - есть и не пустые;
  • город поддомена встречается в title и description (стем-сравнение,
    чтобы «в Москве» совпало с городом «Москва») - ловит незамененные
    шаблоны и чужой город;
  • длины: title ~10-70, description ~50-160 - выход за рамки =
    предупреждение (не баг).

Дубли (пост-обработка всех результатов прогона):
  • внутри одного города (поддомена) одинаковый title/description/H1
    у разных страниц - баг;
  • между городами баг только при ПОЛНОМ совпадении title/description
    (значит, город не подставился в шаблон); тех. страницы исключаем -
    политики/доставка легитимно одинаковы на всех городах.

Дубли УРЛОВ (лёгкие доп. запросы, только главная и каталог поддомена):
  • варианты адреса - http://, без завершающего слэша, с www - должны
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
_DESC_RE2 = re.compile(  # content до name - тоже валидный порядок атрибутов
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
    """Варианты стемов по каждому слову города: [[варианты слова 1], …].

    «Нижний Новгород» → [['нижн'], ['новгор']]. Отрезаем окончание (до
    2 букв), чтобы «в Москве» совпало с «Москва». Нормализуем ё→е («Орёл»
    и «в Орле» - разные буквы). Плюс вариант с БЕГЛОЙ гласной: «Орёл» →
    «Орла/Орлу» - гласная выпадает, поэтому добавляем стем без неё
    («орл»)."""
    stems = []
    for w in re.split(r'[\s-]+', (city or '').strip().lower()):
        if len(w) < 3:
            continue
        w = w.replace('ё', 'е')
        variants = [w[:len(w) - 1] if len(w) <= 4 else w[:len(w) - 2]]
        # Беглая гласная в последнем слоге: орел→орл, посёлок→поселк.
        if len(w) >= 4 and w[-2] in 'еео' and w[-1] not in 'аеиоуыэюя':
            variants.append(w[:-2] + w[-1])
        stems.append(variants)
    return stems


def city_in_text(city: str, text: str) -> Optional[bool]:
    """Есть ли город в тексте: каждое слово названия найдено хотя бы одним
    вариантом стема (терпимо к склонениям, ё/е и беглым гласным).
    None - город не задан/слишком короткий (проверить нельзя)."""
    stems = _city_stems(city)
    if not stems or not text:
        return None if not stems else False
    low = text.lower().replace('ё', 'е')
    return all(any(v in low for v in variants) for variants in stems)


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
        # проверке для остальных) - здесь фиксируем для полноты картины.
        issues.append('нет заголовка H1')

    # Город в title/description - только для SEO-типов.
    # Тексты замечаний - БЕЗ подстановок (города/длины): по одинаковому
    # тексту отчёт группирует страницы в одну строку-проблему.
    if type_code in _CITY_TYPES and city:
        if title and city_in_text(city, title) is False:
            issues.append('в title нет города')
        if desc and city_in_text(city, desc) is False:
            issues.append('в description нет города')

    # Длины - мягкие пороги, предупреждения
    if title:
        if len(title) < TITLE_MIN:
            warnings.append('title короткий')
        elif len(title) > TITLE_MAX:
            warnings.append('title длинный')
    if desc:
        if len(desc) < DESC_MIN:
            warnings.append('description короткий')
        elif len(desc) > DESC_MAX:
            warnings.append('description длинный')

    return {'title': title, 'description': desc, 'h1': h1,
            'issues': issues, 'warnings': warnings}


# ── Дубли title/description/H1 по результатам прогона ───────────────


def _norm_val(v: Optional[str]) -> Optional[str]:
    v = (v or '').strip().lower()
    return re.sub(r'\s+', ' ', v) or None


def find_duplicates(results) -> dict:
    """Найти дубли метаданных среди результатов прогона.

    Возвращает {'same_city': [...], 'cross_city': [...]} - списки групп:
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
    # 3. www. - только для корневого домена (www.spb.… обычно без DNS)
    if not host.startswith('www.') and host.count('.') == 1:
        variants.append(('www', f'{sp.scheme}://www.{host}{path}'))
    return variants


async def _probe_variant(session, url, proxy_url, timeout_ms=20000):
    """Код первого ответа варианта БЕЗ следования редиректам.
    3xx - ок (редиректит), 200 - дубль, 4xx/сеть - дубля нет."""
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


# ═════════════════════════════════════════════════════════════════════
# П.1.3.1 чек-листа: единственность ключевых SEO-тегов.
#
# На странице ключевые теги должны быть в ЕДИНСТВЕННОМ экземпляре:
#   • <title>              - ровно 1 непустой (0 → нет тега; ≥2 → дубли);
#   • <meta name=descr…>   - ровно 1 непустой (0 → нет; ≥2 → дубли; 1 пустой → пустой);
#   • <h1>                 - не больше 1 (≥2 → несколько H1). Отсутствие H1 не
#                            дублируем - его по типу страницы уже ловит
#                            структурная проверка (лист «Структура страниц»).
# Плюс дубли H2: два и более <h2> с ОДИНАКОВЫМ текстом - шаблонная ошибка.
# (Несколько РАЗНЫХ H2 - норма, не баг: их «текстовость» проверяет п.1.3.2.)
#
# Считаем по «структурному» HTML: вырезаем <svg> (там бывают свои <title>) и
# <template> (неактивный контент), чтобы не завышать счётчики.

from collections import Counter

MAX_SHOWN = 3        # сколько примеров текста показать в отчёте

_RE_SVG = re.compile(r'<svg\b[^>]*>.*?</svg>', re.I | re.S)
_RE_TEMPLATE = re.compile(r'<template\b[^>]*>.*?</template>', re.I | re.S)
_RE_TAGS = re.compile(r'<[^>]+>')
_RE_META = re.compile(r'<meta\b[^>]*>', re.I)


def _clean_struct(html: str) -> str:
    html = _RE_SVG.sub(' ', html or '')
    html = _RE_TEMPLATE.sub(' ', html)
    return html


def _txt(inner: str) -> str:
    s = _RE_TAGS.sub(' ', inner or '')
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&')
    return re.sub(r'\s+', ' ', s).strip()


def _tag_texts(html: str, tag: str) -> list[str]:
    return [_txt(m) for m in re.findall(rf'<{tag}\b[^>]*>(.*?)</{tag}>', html, re.I | re.S)]


def _meta_descriptions(html: str) -> list[str]:
    """Содержимое всех <meta name="description"> (в т.ч. пустых). og:description
    и прочие НЕ считаем - только name="description"."""
    out = []
    for tag in _RE_META.findall(html):
        if not re.search(r'''name\s*=\s*["']?\s*description\b''', tag, re.I):
            continue
        m = (re.search(r'content\s*=\s*"([^"]*)"', tag, re.I)
             or re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
             or re.search(r'content\s*=\s*([^\s"\'>]+)', tag, re.I))
        out.append(m.group(1).strip() if m else '')
    return out


def _short(s: str, n: int = 60) -> str:
    s = (s or '').strip()
    return s if len(s) <= n else s[:n - 1] + '…'


def check_meta_uniqueness(html: str, url: str = '', type_code: str = '') -> dict:
    """Проверка единственности title/description/H1 (+ дубли H2).
    Возвращает {'issues': [...], 'counts': {...}}."""
    h = _clean_struct(html or '')
    titles = [t for t in _tag_texts(h, 'title') if t]
    h1s = [t for t in _tag_texts(h, 'h1') if t]
    h2s = [t for t in _tag_texts(h, 'h2') if t]
    descs = _meta_descriptions(h)
    descs_ne = [d for d in descs if d]

    issues: list[dict] = []

    # ── title ──
    if len(titles) == 0:
        issues.append({'тип': 'title', 'найдено': '-',
                       'пояснение': 'нет тега <title> на странице'})
    elif len(titles) >= 2:
        issues.append({'тип': 'title', 'найдено': f'{len(titles)} тегов',
                       'пояснение': 'на странице несколько <title>: '
                                    + ' | '.join(_short(t, 45) for t in titles[:MAX_SHOWN])})

    # ── meta description ──
    if len(descs) == 0:
        issues.append({'тип': 'description', 'найдено': '-',
                       'пояснение': 'нет meta description'})
    elif len(descs) >= 2:
        issues.append({'тип': 'description', 'найдено': f'{len(descs)} тегов',
                       'пояснение': 'несколько meta description: '
                                    + ' | '.join(_short(d, 45) for d in descs[:MAX_SHOWN])})
    elif not descs_ne:
        issues.append({'тип': 'description', 'найдено': 'пустой',
                       'пояснение': 'meta description есть, но пустой'})

    # ── H1 (несколько) ──
    if len(h1s) >= 2:
        issues.append({'тип': 'h1', 'найдено': f'{len(h1s)} шт.',
                       'пояснение': 'на странице несколько H1: '
                                    + ' | '.join(_short(t, 40) for t in h1s[:MAX_SHOWN])})

    # ── H2 дубли (одинаковый текст) ──
    norm = Counter(re.sub(r'\s+', ' ', t.strip().lower()) for t in h2s)
    dups = [(t, n) for t, n in norm.items() if n >= 2]
    for t, n in dups[:MAX_SHOWN]:
        issues.append({'тип': 'h2', 'найдено': f'×{n}',
                       'пояснение': f'H2 «{_short(t, 45)}» повторяется {n} раза(раз)'})

    return {
        'issues': issues,
        'counts': {'title': len(titles), 'description': len(descs),
                   'h1': len(h1s), 'h2': len(h2s), 'h2_dups': len(dups)},
    }


# ═════════════════════════════════════════════════════════════════════
# П.1.3.2 чек-листа: заголовки h2-h6 используются только в тексте.
#
# Заголовок - элемент СТРУКТУРЫ ТЕКСТА, а не вёрстки. h2-h6 в шапке,
# подвале, меню или сайдбаре (семантические зоны <header>/<footer>/
# <nav>/<aside>) - ошибка шаблона: их видят роботы и ломается иерархия
# заголовков страницы.

_PLACEMENT_ZONES = (
    ('header', 'шапка'),
    ('footer', 'подвал'),
    ('nav', 'меню'),
    ('aside', 'сайдбар'),
)

_MAX_PLACEMENT_ISSUES = 20   # кап находок на страницу (шаблонная ошибка повторяется)


def check_headings_placement(html: str) -> dict:
    """Проверка «текстовости» заголовков (п.1.3.2): h2-h6 не должны
    встречаться в служебных зонах страницы (<header>/<footer>/<nav>/<aside>).

    Возвращает {'issues': [{'тип','найдено','пояснение'}, …]} - тот же
    формат, что у check_meta_uniqueness (попадает в тот же лист отчёта)."""
    h = _clean_struct(html or '')
    issues: list[dict] = []
    seen: set = set()
    for tag, label in _PLACEMENT_ZONES:
        for zone in re.findall(rf'<{tag}\b[^>]*>.*?</{tag}>', h, re.I | re.S):
            for m in re.finditer(r'<(h[2-6])\b[^>]*>(.*?)</\1\s*>', zone,
                                 re.I | re.S):
                htag = m.group(1).lower()
                txt = _txt(m.group(2))
                key = (htag, re.sub(r'\s+', ' ', txt.strip().lower()))
                # вложенные зоны (nav внутри header) дают повтор - дедуп
                if key in seen:
                    continue
                seen.add(key)
                issues.append({
                    'тип': htag, 'найдено': f'в <{tag}>',
                    'пояснение': f'{htag.upper()} «{_short(txt, 45)}» в зоне '
                                 f'«{label}» (<{tag}>) - заголовки h2-h6 '
                                 f'должны быть только в тексте',
                })
                if len(issues) >= _MAX_PLACEMENT_ISSUES:
                    return {'issues': issues}
    return {'issues': issues}


def check_tags(html: str, url: str = '', type_code: str = '') -> dict:
    """Объединённая проверка тегов для пункта 1.8: единственность
    title/description/H1 + дубли H2 (п.1.3.1) и «текстовость» заголовков
    (п.1.3.2). Один dict для CheckResult.meta_unique."""
    out = check_meta_uniqueness(html, url, type_code)
    try:
        out['issues'] = list(out.get('issues') or []) + \
            (check_headings_placement(html).get('issues') or [])
    except Exception:
        pass
    return out
