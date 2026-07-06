"""
region_checker.py - региональные проверки страниц поддоменов.

Пункт 1.4.1 чек-листа - «верные переменные в текстовых блоках»:
  на странице города X не должно быть подстановок ДРУГОГО города:
    • чужой город проекта в title / meta description / H1
      (это шаблонные зоны - там всегда живёт подстановка «в {городе}»);
    • телефон другого города из КП в видимом тексте страницы;
    • почта другого города из КП в видимом тексте страницы.
  Сверяем только со СПРАВОЧНИКАМИ (список городов проекта + КП), никаких
  «угадываний» - поэтому ложных срабатываний практически нет.

Пункт 1.6 чек-листа - «чистота СНГ-доменов»:
  на сайте страны Y (не Россия) не должно быть:
    • упоминаний «РФ», «Россия/России/российск…»;
    • аббревиатуры «СНГ»;
    • названий ДРУГИХ стран (на казахском сайте - только Казахстан и т.д.)
  Проверяем title, meta description, H1 и весь видимый текст (включая
  контактные данные - они тоже видимый текст).

Обе проверки - чистые regex по уже скачанному HTML: сеть не трогают,
на скорость прогона не влияют.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from text_checker import strip_non_visible
from kp import normalize_phone

MAX_ISSUES_PER_KIND = 8      # не заваливаем отчёт повторами
_CTX_CHARS = 45              # символов контекста вокруг находки


# ── Извлечение зон страницы ──────────────────────────────────────────

_RE_TITLE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
_RE_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']'
    r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
    re.I)
_RE_H1 = re.compile(r'<h1[^>]*>(.*?)</h1>', re.I | re.S)
_RE_TAG = re.compile(r'<[^>]+>')


def _plain(s: str) -> str:
    s = _RE_TAG.sub(' ', s or '')
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&')
    return re.sub(r'\s+', ' ', s).strip()


def извлечь_зоны(html: str) -> dict:
    """title / description / h1 / видимый текст страницы (без скриптов и атрибутов)."""
    title = _plain(m.group(1)) if (m := _RE_TITLE.search(html)) else ''
    desc = ''
    if (m := _RE_DESC.search(html)):
        desc = _plain(m.group(1) or m.group(2) or '')
    h1 = ' | '.join(_plain(x) for x in _RE_H1.findall(html)[:3])
    text = _plain(strip_non_visible(html))
    return {'title': title, 'description': desc, 'h1': h1, 'текст': text}


def _контекст(text: str, start: int, end: int) -> str:
    a, b = max(0, start - _CTX_CHARS), min(len(text), end + _CTX_CHARS)
    return ('…' if a > 0 else '') + text[a:b].strip() + ('…' if b < len(text) else '')


# ── Контекст проекта (справочники) ───────────────────────────────────

# Страна → корни слов, по которым узнаём её упоминание (без своей страны).
COUNTRY_STEMS = {
    'Россия':      ['росси', 'российск', 'рф'],
    'Казахстан':   ['казахстан', 'казахстанск'],
    'Беларусь':    ['беларус', 'белорус', 'белоруссi', 'белорусси'],
    'Кыргызстан':  ['кыргызстан', 'киргиз'],
    'Узбекистан':  ['узбекистан', 'узбекск'],
    'Азербайджан': ['азербайджан'],
    'Армения':     ['армени', 'армянск'],
}
# Слова из 2-3 букв матчим только как отдельное слово (РФ, СНГ).
_SHORT = {'рф', 'снг'}


def _stem_word(w: str) -> str:
    """Грубая основа русского слова: отрезаем окончание-гласную/«ь»/«й»."""
    w = w.strip().lower()
    if len(w) > 4 and w[-1] in 'аяоеиыуюьй':
        return w[:-1]
    return w


def _city_regex(city: str):
    """Regex упоминания города с падежными окончаниями («в Казани», «Казанью»)."""
    words = [w for w in re.split(r'[\s-]+', city.strip()) if w]
    if not words:
        return None
    # Города из коротких слов (Ош и т.п.) пропускаем - слишком много омонимов.
    if max(len(w) for w in words) < 4:
        return None
    parts = [re.escape(_stem_word(w)) + r'[а-яё]{0,3}' for w in words]
    return re.compile(r'(?<![а-яё])' + r'[\s-]+'.join(parts) + r'(?![а-яё])', re.I)


@dataclass
class RegionContext:
    """Справочники для региональных проверок (строится один раз на прогон)."""
    host_city: dict = field(default_factory=dict)      # host → город
    host_country: dict = field(default_factory=dict)   # host → страна
    city_regex: dict = field(default_factory=dict)     # город → compiled regex
    phone_cities: dict = field(default_factory=dict)   # номер (норм.) → set(город)
    email_cities: dict = field(default_factory=dict)   # почта (lower) → set(город)


def build_region_context(kp_map: dict | None, subdomains: list) -> RegionContext:
    """Собирает контекст из справочника поддоменов и КП. kp_map может быть None -
    тогда проверяются только города (title/h1/description) и СНГ-чистота."""
    ctx = RegionContext()
    for s in subdomains or []:
        host = getattr(s, 'host', '') or ''
        city = (getattr(s, 'city', '') or '').strip()
        if not host or not city:
            continue
        ctx.host_city[host] = city
        ctx.host_country[host] = (getattr(s, 'country', '') or '').strip()
        rx = _city_regex(city)
        if rx is not None:
            ctx.city_regex[city] = rx
    for row in (kp_map or {}).values():
        city = (getattr(row, 'city', '') or '').strip()
        if not city:
            continue
        for num in row.phone_set():
            ctx.phone_cities.setdefault(num, set()).add(city)
        mail = (getattr(row, 'email', '') or '').strip().lower()
        if mail:
            ctx.email_cities.setdefault(mail, set()).add(city)
    return ctx


# ── 1.4.1: верные переменные (город / телефон / почта) ───────────────

_RE_PHONE_LIKE = re.compile(r'\+?\d[\d\s\-(). ]{8,18}\d')
_RE_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]{2,}', re.I)


def check_region_vars(html: str, host: str, ctx: RegionContext) -> dict | None:
    """Проверка «верных переменных» страницы поддомена (пункт 1.4.1).
    Возвращает {'город': ..., 'issues': [...]} или None, если город хоста не известен."""
    свой_город = ctx.host_city.get(host, '')
    if not свой_город:
        return None
    зоны = извлечь_зоны(html)
    issues: list[dict] = []

    # 1) Чужой город проекта в шаблонных зонах (title / description / h1).
    for зона in ('title', 'description', 'h1'):
        t = зоны[зона]
        if not t:
            continue
        for город, rx in ctx.city_regex.items():
            if город == свой_город:
                continue
            # города-«вложения» («Новгород» в «Нижний Новгород») не сравниваем
            if город in свой_город or свой_город in город:
                continue
            m = rx.search(t)
            if m:
                issues.append({
                    'тип': 'город', 'зона': зона,
                    'найдено': m.group(0),
                    'контекст': _контекст(t, m.start(), m.end()),
                    'пояснение': f'город «{город}» на странице «{свой_город}»',
                })
                if sum(1 for i in issues if i['тип'] == 'город') >= MAX_ISSUES_PER_KIND:
                    break

    # 2) Телефон другого города (по КП) в видимом тексте.
    if ctx.phone_cities:
        свои_номера = {n for n, cs in ctx.phone_cities.items() if свой_город in cs}
        seen: set[str] = set()
        for m in _RE_PHONE_LIKE.finditer(зоны['текст']):
            num = normalize_phone(m.group(0))
            if not num or num in seen:
                continue
            seen.add(num)
            города = ctx.phone_cities.get(num)
            if города and свой_город not in города and num not in свои_номера:
                issues.append({
                    'тип': 'телефон', 'зона': 'текст',
                    'найдено': m.group(0).strip(),
                    'контекст': _контекст(зоны['текст'], m.start(), m.end()),
                    'пояснение': f'это номер города: {", ".join(sorted(города))} (по КП)',
                })
                if sum(1 for i in issues if i['тип'] == 'телефон') >= MAX_ISSUES_PER_KIND:
                    break

    # 3) Почта другого города (по КП) в видимом тексте.
    if ctx.email_cities:
        seen_mail: set[str] = set()
        for m in _RE_EMAIL.finditer(зоны['текст']):
            mail = m.group(0).lower().rstrip('.')
            if mail in seen_mail:
                continue
            seen_mail.add(mail)
            города = ctx.email_cities.get(mail)
            if города and свой_город not in города:
                issues.append({
                    'тип': 'почта', 'зона': 'текст',
                    'найдено': mail,
                    'контекст': _контекст(зоны['текст'], m.start(), m.end()),
                    'пояснение': f'это почта города: {", ".join(sorted(города))} (по КП)',
                })
                if sum(1 for i in issues if i['тип'] == 'почта') >= MAX_ISSUES_PER_KIND:
                    break

    return {'город': свой_город, 'issues': issues}


# ── 1.6: СНГ-домены без РФ/СНГ и чужих стран ─────────────────────────

def check_cis_mentions(html: str, host: str, ctx: RegionContext) -> dict | None:
    """Для доменов НЕ-России: ищем упоминания РФ / СНГ / чужих стран в
    title, description, h1 и видимом тексте. None - домен РФ или страна неизвестна."""
    страна = ctx.host_country.get(host, '')
    if not страна or страна == 'Россия':
        return None

    # Запрещённые основы: все страны, кроме своей, + «СНГ».
    запрет: list[tuple[str, str]] = [('СНГ', 'снг')]
    for c, stems in COUNTRY_STEMS.items():
        if c == страна:
            continue
        for s in stems:
            запрет.append((c, s))

    зоны = извлечь_зоны(html)
    issues: list[dict] = []
    for зона in ('title', 'description', 'h1', 'текст'):
        t = зоны[зона]
        if not t:
            continue
        for страна_имя, stem in запрет:
            if stem in _SHORT or len(stem) <= 3:
                rx = re.compile(r'(?<![а-яёa-z])' + re.escape(stem) + r'(?![а-яёa-z])', re.I)
            else:
                rx = re.compile(r'(?<![а-яё])' + re.escape(stem) + r'[а-яё]{0,6}', re.I)
            for m in rx.finditer(t):
                issues.append({
                    'тип': 'страна', 'зона': зона,
                    'найдено': m.group(0),
                    'контекст': _контекст(t, m.start(), m.end()),
                    'пояснение': (f'упоминание «{страна_имя}» на сайте страны '
                                  f'«{страна}»') if страна_имя != 'СНГ'
                                 else f'аббревиатура «СНГ» на сайте страны «{страна}»',
                })
                break   # по одной находке на (зона, основа) - без повторов
        if sum(1 for i in issues) >= MAX_ISSUES_PER_KIND * 2:
            break

    return {'страна': страна, 'issues': issues}
