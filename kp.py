"""
kp.py — сверка контактов на сайте с «Картой присутствия» (КП).

Что делает:
  • парсит КП-файлы проектов в единую таблицу по поддоменам:
        домен → {город, телефоны (SEO/реклама/общий), почта, адрес};
  • достаёт фактические контакты из шапки/подвала страницы;
  • сравнивает и выдаёт результат по правилам заказчика.

Правило для телефона (по согласованию):
  ожидаемый = «SEO Город» → если пусто → «Реклама Город» → если пусто →
  «Общий Город» → если и его нет в КП → критическая ошибка (КП неполная).
  Если на сайте номер есть, но не совпадает с ожидаемым по городу → баг
  с комментарием «номер есть, но не совпадает с КП».
  (У МПЭ в КП нет SEO/Реклама/Общий — там «Телефон основной» кладём в
  слот SEO, «Подменные номера» — в слот рекламы.)

Адрес — мягкое сравнение (нормализация сокращений, лат/кир букв, дома).

База КП хранится в репозитории как catalogs/{proj}-kp.csv (исходные xlsx в
git не кладём — там много лишнего). Генерация — convert_kp.py.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).parent
CATALOGS_DIR = PROJECT_ROOT / 'catalogs'

# Какой лист и какие колонки брать из КП каждого проекта.
# phone_seo/ad/common — ключевые слова в заголовке колонки (по ним ищем индекс).
KP_LAYOUT = {
    'imp': {
        'sheet': 'Карта присутствия',
        'phone_seo':    ('seo', 'город'),
        'phone_ad':     ('реклама', 'город'),
        'phone_common': ('общий', 'город'),
    },
    'smu': {
        'sheet': 'Справочники',
        'phone_seo':    ('seo', 'город'),
        'phone_ad':     ('реклама', 'город'),
        'phone_common': ('общий', 'город'),
    },
    'mpe': {
        'sheet': 'карта присутствия',
        'phone_seo':    ('телефон основной',),
        'phone_ad':     ('подменные',),
        'phone_common': ('мобильный',),
    },
}


# ── Нормализация ─────────────────────────────────────────────────────


# Латиница, похожая на кириллицу (в адресах «1c1» — латинская c вместо с)
_LAT2CYR = str.maketrans({
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
    'b': 'в', 'h': 'н', 'k': 'к', 'm': 'м', 't': 'т',
})

# Типы улиц — выкидываем при сравнении адресов (могут писаться по-разному)
_STREET_WORDS = {
    'улица', 'ул', 'проспект', 'пр', 'пркт', 'прт', 'переулок', 'пер',
    'шоссе', 'ш', 'набережная', 'наб', 'бульвар', 'бр', 'бул', 'площадь',
    'пл', 'проезд', 'дом', 'д', 'корпус', 'корп', 'к', 'строение', 'стр',
    'литер', 'литера',
}


def normalize_phone(s: Optional[str]) -> str:
    """Телефон → последние 10 цифр (для сравнения вне зависимости от формата)."""
    if not s:
        return ''
    digits = re.sub(r'\D', '', str(s))
    return digits[-10:] if len(digits) >= 10 else digits


def split_phones(s: Optional[str]) -> list[str]:
    """Из ячейки/текста выбрать все номера (последние 10 цифр каждого)."""
    if not s:
        return []
    out = []
    for m in re.findall(r'\+?[78]?[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}', str(s)):
        n = normalize_phone(m)
        if len(n) == 10 and n not in out:
            out.append(n)
    return out


def _norm_addr(s: Optional[str]) -> str:
    if not s:
        return ''
    s = str(s).lower().translate(_LAT2CYR)
    s = re.sub(r'[^\w\s]', ' ', s)        # убрать пунктуацию
    return re.sub(r'\s+', ' ', s).strip()


def address_match(site_addr: str, kp_addr: str) -> bool:
    """
    Мягкое сравнение адресов: совпали номер дома и название улицы —
    считаем, что адрес тот же. «Рязанский проспект, 86/1с1» ≈ «Рязанский
    пр., 86/1c1».
    """
    s, k = _norm_addr(site_addr), _norm_addr(kp_addr)
    if not k:
        return False
    knums = set(re.findall(r'\d+', k))
    snums = set(re.findall(r'\d+', s))
    kwords = [w for w in re.findall(r'[а-яё]+', k)
              if len(w) >= 4 and w not in _STREET_WORDS]
    swords = set(re.findall(r'[а-яё]+', s))
    # Номер дома: хотя бы один общий (или в КП номера нет)
    num_ok = (not knums) or bool(knums & snums)
    # Улица: хотя бы одно значимое слово улицы совпало
    word_ok = (not kwords) or any(w in swords for w in kwords)
    return num_ok and word_ok


# ── Запись из КП (одна строка-город) ─────────────────────────────────


@dataclass
class KPRow:
    domain: str                 # нормализованный хост, напр. 'spb.inmetprom.ru'
    city: str
    phone_seo: str = ''
    phone_ad: str = ''
    phone_common: str = ''
    email: str = ''
    address: str = ''

    def expected_phone(self) -> tuple[str, str]:
        """
        Ожидаемый на сайте номер по приоритету SEO → реклама → общий.
        Возвращает (normalized_phone, источник) или ('', 'critical') если в КП
        вообще нет номера для города.
        """
        for val, src in ((self.phone_seo, 'SEO'),
                         (self.phone_ad, 'Реклама'),
                         (self.phone_common, 'Общий')):
            n = normalize_phone(val)
            if n:
                return n, src
        return '', 'critical'


# ── Результат сверки ─────────────────────────────────────────────────


@dataclass
class KPCheckResult:
    domain: str
    city: str = ''
    matched_kp: bool = False           # нашли строку КП для домена?
    issues: list[dict] = field(default_factory=list)   # [{field, status, comment}]

    @property
    def has_issues(self) -> bool:
        return any(i['status'] in ('bug', 'critical') for i in self.issues)


# ── Загрузка базы КП из репозитория ──────────────────────────────────


def _csv_path(project_id: str) -> Path:
    return CATALOGS_DIR / f'{project_id}-kp.csv'


def load_kp(project_id: str) -> dict[str, KPRow]:
    """Прочитать catalogs/{proj}-kp.csv → {домен: KPRow}. {} если нет файла."""
    p = _csv_path(project_id)
    if not p.exists():
        return {}
    out: dict[str, KPRow] = {}
    with open(p, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            dom = (row.get('domain') or '').strip().lower()
            if not dom:
                continue
            out[dom] = KPRow(
                domain=dom, city=row.get('city', ''),
                phone_seo=row.get('phone_seo', ''),
                phone_ad=row.get('phone_ad', ''),
                phone_common=row.get('phone_common', ''),
                email=row.get('email', ''),
                address=row.get('address', ''),
            )
    return out


def _norm_host(url_or_host: str) -> str:
    s = (url_or_host or '').strip().lower()
    if not s:
        return ''
    if '://' not in s:
        s = 'http://' + s
    host = urlparse(s).hostname or ''
    return host[4:] if host.startswith('www.') else host


# ── Извлечение контактов с самой страницы (шапка+подвал) ──────────────


def extract_site_contacts(html: str) -> dict:
    """Достать из шапки+подвала телефоны, почты и текст адреса."""
    from content_checker import _extract_region
    from text_checker import html_to_visible_text

    region_html = (_extract_region(html, 'header', 'top') + '\n'
                   + _extract_region(html, 'footer', 'bottom'))
    text = html_to_visible_text(region_html)
    phones = split_phones(text) + split_phones(region_html)
    emails = [e.lower() for e in re.findall(
        r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', region_html, re.IGNORECASE)]
    # Текст адреса — кусок видимого текста вокруг уличного маркера
    addr = ''
    m = re.search(r'.{0,40}(?:улиц|пр\.|проспект|шоссе|переул|набережн|бульвар)'
                  r'.{0,40}', text, re.IGNORECASE)
    if m:
        addr = m.group(0).strip()
    return {
        'phones': list(dict.fromkeys(phones)),
        'emails': list(dict.fromkeys(emails)),
        'address': addr,
        'full_text': text,
    }


# ── Главная функция сверки ───────────────────────────────────────────


def check_against_kp(html: str, domain: str, kp: dict[str, KPRow]) -> KPCheckResult:
    """
    Сверить контакты страницы (главной поддомена) с КП.

    html   — HTML главной страницы поддомена
    domain — хост поддомена (например 'spb.inmetprom.ru')
    kp     — словарь из load_kp()
    """
    host = _norm_host(domain)
    res = KPCheckResult(domain=host)
    row = kp.get(host)
    if not row:
        return res            # нет строки КП — сверять не с чем (не баг здесь)
    res.matched_kp = True
    res.city = row.city

    site = extract_site_contacts(html)

    # ── Телефон ──
    expected, src = row.expected_phone()
    if not expected:
        res.issues.append({
            'field': 'Телефон',
            'status': 'critical',
            'comment': f'В КП нет номера для города «{row.city}» (ни SEO, ни '
                       f'рекламного, ни общего) — заполнить КП.',
        })
    elif not site['phones']:
        res.issues.append({
            'field': 'Телефон',
            'status': 'bug',
            'comment': f'На сайте не найден телефон в шапке/подвале. По КП '
                       f'ожидался {src}-номер.',
        })
    elif expected not in site['phones']:
        res.issues.append({
            'field': 'Телефон',
            'status': 'bug',
            'comment': f'Номер на сайте есть, но не совпадает с КП. Ожидался '
                       f'{src}-номер из КП, на сайте: '
                       f'{", ".join(_fmt(p) for p in site["phones"])}.',
        })
    else:
        res.issues.append({'field': 'Телефон', 'status': 'ok',
                           'comment': f'Совпадает с {src}-номером КП.'})

    # ── Почта ──
    kp_email = (row.email or '').strip().lower()
    if kp_email:
        if not site['emails']:
            res.issues.append({'field': 'Почта', 'status': 'bug',
                               'comment': 'На сайте не найдена почта в подвале.'})
        elif kp_email not in site['emails']:
            res.issues.append({
                'field': 'Почта', 'status': 'bug',
                'comment': f'Почта на сайте есть, но не совпадает с КП '
                           f'({kp_email}). На сайте: {", ".join(site["emails"])}.',
            })
        else:
            res.issues.append({'field': 'Почта', 'status': 'ok', 'comment': ''})

    # ── Адрес (мягко) ──
    if row.address:
        if not site['address']:
            res.issues.append({'field': 'Адрес', 'status': 'bug',
                               'comment': 'На сайте не найден адрес в подвале.'})
        elif not address_match(site['address'], row.address):
            res.issues.append({
                'field': 'Адрес', 'status': 'bug',
                'comment': f'Адрес не совпадает с КП. По КП: «{row.address}», '
                           f'на сайте: «{site["address"]}».',
            })
        else:
            res.issues.append({'field': 'Адрес', 'status': 'ok', 'comment': ''})

    return res


def _fmt(norm10: str) -> str:
    """4991306028 → +7 (499) 130-60-28 для читаемого комментария."""
    if len(norm10) != 10:
        return norm10
    return f'+7 ({norm10[:3]}) {norm10[3:6]}-{norm10[6:8]}-{norm10[8:]}'
