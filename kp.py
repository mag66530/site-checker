"""
kp.py - сверка контактов на сайте с «Картой присутствия» (КП).

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
  (У МПЭ в КП нет SEO/Реклама/Общий - там «Телефон основной» кладём в
  слот SEO, «Подменные номера» - в слот рекламы.)

Адрес - мягкое сравнение (нормализация сокращений, лат/кир букв, дома).

База КП хранится в репозитории как catalogs/{proj}-kp.csv (исходные xlsx в
git не кладём - там много лишнего). Генерация - convert_kp.py.
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
# phone_seo/ad/common - ключевые слова в заголовке колонки (по ним ищем индекс).
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
    # У МПЭ актуальные данные на листе «КП» (а не «карта присутствия» -
    # там устаревшие номера). Структура как у СМУ/ИМП: Общий/Реклама/Поиск
    # Город + Сотовый. Ссылка на домен - в колонке «Ссылка».
    'mpe': {
        'sheet': 'КП',
        'phone_seo':    ('поиск', 'город'),
        'phone_ad':     ('реклама', 'город'),
        'phone_common': ('общий', 'город'),
    },
    # АПС (Авиапромсталь). Лист «карта присутствия». В этой таблице колонки
    # «страна» и «город» БЕЗ заголовков (первые два столбца) - берём их по
    # позиции (country_col/city_col). Телефоны: «Телефон основной» + «Подменные
    # номера» (рекламные/поисковые подменники).
    'avia': {
        'sheet': 'карта присутствия',
        'phone_seo':    ('подменн',),
        'phone_ad':     ('подменн',),
        'phone_common': ('основн',),
        'country_col': 0,
        'city_col': 1,
    },
}


# ── Нормализация ─────────────────────────────────────────────────────


# Латиница, похожая на кириллицу (в адресах «1c1» - латинская c вместо с)
_LAT2CYR = str.maketrans({
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
    'b': 'в', 'h': 'н', 'k': 'к', 'm': 'м', 't': 'т',
})

# Типы улиц - выкидываем при сравнении адресов (могут писаться по-разному)
_STREET_WORDS = {
    'улица', 'ул', 'проспект', 'пр', 'пркт', 'прт', 'переулок', 'пер',
    'шоссе', 'ш', 'набережная', 'наб', 'бульвар', 'бр', 'бул', 'площадь',
    'пл', 'проезд', 'дом', 'д', 'корпус', 'корп', 'к', 'строение', 'стр',
    'литер', 'литера',
}


def normalize_phone(s: Optional[str]) -> str:
    """
    Телефон → национальный номер для сравнения вне зависимости от формата.
    Учитываем коды стран: Россия/Казахстан +7/8 → 10 цифр; Беларусь +375 и
    Узбекистан +998 → 9 цифр. Excel иногда хранит номер числом («…448.0») -
    отбрасываем хвост «.0».
    """
    if s is None:
        return ''
    s = str(s)
    if s.endswith('.0'):
        s = s[:-2]
    d = re.sub(r'\D', '', s)
    if not d:
        return ''
    if d.startswith('998') and len(d) >= 12:
        return d[-9:]                 # Узбекистан: 9-значный нац. номер
    if d.startswith('375') and len(d) >= 12:
        return d[-9:]                 # Беларусь
    if len(d) >= 11 and d[0] in '78':
        return d[-10:]                # Россия/Казахстан
    if len(d) == 10:
        return d
    return d[-10:]


_PHONE_FIND = re.compile(
    r'\+?998[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'   # Узбекистан
    r'|\+?375[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'  # Беларусь
    r'|\+?[78][\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'  # Россия/Казахстан
    r'|\b\d{11,12}\b'                                               # «голый» из tel:/числа
)


def split_phones(s: Optional[str]) -> list[str]:
    """Найти в тексте/ячейке все телефоны (нормализованные). Понимает любые
    коды стран (+7/8/375/998), формат со скобками и «голые» числа из tel:."""
    if s is None:
        return []
    out = []
    for m in _PHONE_FIND.findall(str(s)):
        n = normalize_phone(m)
        if 9 <= len(n) <= 10 and n not in out:
            out.append(n)
    return out


# Разделители номеров ВНУТРИ одной ячейки КП: перевод строки, пометка «(стар…)»,
# запятая/точка-с-запятой/слэш, « или ». По ним режем и нормализуем КАЖДЫЙ кусок
# отдельно - иначе normalize_phone склеивал цифры двух номеров (и возвращал
# старый/мусор), а строгий split_phones пропускал 4-значные коды (8 (4852)…).
_CELL_SPLIT = re.compile(r'\n|\r|\(?\s*стар[^)]*\)?|[,;/]|\s+или\s+', re.I)


def phones_in_cell(s: Optional[str]) -> list[str]:
    """Номера из ОДНОЙ ячейки КП по порядку (первый = текущий). Режем по
    разделителям и нормализуем каждый кусок; берём только валидные 9-10 цифр."""
    if not s:
        return []
    out = []
    for part in _CELL_SPLIT.split(str(s)):
        n = normalize_phone(part)
        if 9 <= len(n) <= 10 and n not in out:
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
    Мягкое сравнение адресов: совпали номер дома и название улицы -
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
    all_phones: str = ''        # все номера города из КП, через ';' (10-значные)
    email: str = ''
    address: str = ''
    country: str = ''           # страна из КП (Россия / Беларусь / …)
    telegram: str = ''          # username менеджера без @ (напр. 'smu_manager2')
    whatsapp: str = ''          # номер WhatsApp (напр. '7-903-130-36-69')

    def telegram_norm(self) -> str:
        """username Telegram в нижнем регистре, без @ и без t.me/."""
        return normalize_tg(self.telegram)

    def whatsapp_norm(self) -> str:
        """номер WhatsApp - 10 значащих цифр. Через split_phones (в ячейке бывает
        номер + мусор «(Ватсап)+тг» или второй номер - берём первый настоящий)."""
        _w = phones_in_cell(self.whatsapp)
        return _w[0] if _w else normalize_phone(self.whatsapp)

    def phone_set(self) -> set[str]:
        """Все номера города из КП (нормализованные). В ячейке КП бывает НЕСКОЛЬКО
        номеров (напр. «8 (903)… (стар. 8 (861)…)») - берём split_phones, а не
        normalize_phone (тот склеивал цифры двух номеров и возвращал мусор/старый)."""
        nums = {n for n in (self.all_phones or '').split(';') if n}
        for v in (self.phone_seo, self.phone_ad, self.phone_common):
            for n in phones_in_cell(v):
                nums.add(n)
        return nums

    def expected_phone(self) -> tuple[str, str]:
        """
        Предпочтительный номер по приоритету SEO → реклама → общий (для пояснения).
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


_KP_MEM: dict[str, dict] = {}


def load_kp(project_id: str, refresh: bool = True) -> dict[str, KPRow]:
    """КП проекта {домен: KPRow}. Если задана ссылка на Google-таблицу КП
    (kp_sheets.kp_sheet_url), ОДИН раз за процесс обновляет csv из таблицы -
    так проверки берут свежие данные (при недоступности таблицы остаётся снапшот).
    Кэшируется на процесс. refresh=False - только читать csv (без похода в Google)."""
    if project_id in _KP_MEM:
        return _KP_MEM[project_id]
    if refresh:
        try:
            import kp_sheets
            if kp_sheets.kp_sheet_url(project_id):
                kp_sheets.refresh_project(project_id, log=lambda *a, **k: None)
        except Exception:
            pass                       # таблица недоступна - остаётся прежний csv
    kp = _load_kp_csv(project_id)
    _KP_MEM[project_id] = kp
    return kp


def _load_kp_csv(project_id: str) -> dict[str, KPRow]:
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
                all_phones=row.get('all_phones', ''),
                email=row.get('email', ''),
                address=row.get('address', ''),
                # новые колонки могут отсутствовать в старых csv - берём по умолчанию.
                country=row.get('country', ''),
                telegram=row.get('telegram', ''),
                whatsapp=row.get('whatsapp', ''),
            )
    return out


def normalize_tg(s: Optional[str]) -> str:
    """username Telegram → нижний регистр, без @, t.me/, telegram.me/, tg://…domain=."""
    s = (s or '').strip().lower()
    if not s:
        return ''
    # Есть явный префикс ссылки/@ - берём username сразу после него.
    m = re.search(r'(?:t\.me/|telegram\.me/|resolve\?domain=|@)([a-z0-9_]{3,})', s)
    if m:
        return m.group(1)
    # Иначе строка сама и есть username (как в КП: 'smu_manager2').
    m = re.fullmatch(r'[a-z0-9_]{3,}', s)
    return m.group(0) if m else ''


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
    # Маски ввода телефона («+7 (000) 000-00-00») и заглушки с кодом 000 -
    # не настоящие номера, отбрасываем, чтобы не считать их расхождением.
    phones = [p for p in (split_phones(text) + split_phones(region_html))
              if not p.startswith('000')]
    emails = [e.lower() for e in re.findall(
        r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', region_html, re.IGNORECASE)]
    # Текст адреса - кусок видимого текста вокруг уличного маркера
    addr = ''
    m = re.search(r'.{0,40}(?:улиц|пр\.|проспект|шоссе|переул|набережн|бульвар)'
                  r'.{0,40}', text, re.IGNORECASE)
    if m:
        addr = m.group(0).strip()
    # Мессенджеры ищем по ВСЕМУ html: кнопки часто плавающие/виджеты вне шапки-подвала.
    tg = re.findall(r'(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,})', html, re.I)
    tg += re.findall(r'tg://resolve\?domain=([A-Za-z0-9_]{3,})', html, re.I)
    _tg_skip = {'share', 'joinchat', 'iv', 's', 'proxy', 'socks',
                'addstickers', 'joinchannel', 'addlist'}
    tg = [t.lower() for t in tg if t.lower() not in _tg_skip]
    wa_raw = re.findall(
        r'(?:wa\.me/|api\.whatsapp\.com/send[^"\'\s]*?phone=|whatsapp://send\?phone=)'
        r'(\+?\d[\d\-()\s]{7,})', html, re.I)
    wa = [n for n in (normalize_phone(w) for w in wa_raw) if n]
    return {
        'phones': list(dict.fromkeys(phones)),
        'emails': list(dict.fromkeys(emails)),
        'address': addr,
        'telegram': list(dict.fromkeys(tg)),
        'whatsapp': list(dict.fromkeys(wa)),
        'full_text': text,
    }


# ── Главная функция сверки ───────────────────────────────────────────


def check_against_kp(html: str, domain: str, kp: dict[str, KPRow]) -> KPCheckResult:
    """
    Сверить контакты страницы (главной поддомена) с КП.

    html   - HTML главной страницы поддомена
    domain - хост поддомена (например 'spb.inmetprom.ru')
    kp     - словарь из load_kp()
    """
    host = _norm_host(domain)
    res = KPCheckResult(domain=host)
    row = kp.get(host)
    if not row:
        return res            # нет строки КП - сверять не с чем (не баг здесь)
    res.matched_kp = True
    res.city = row.city

    site = extract_site_contacts(html)

    # ── Телефон ──
    # Сверяем с номерами города из КП. Но у сети филиальная модель: город
    # может обслуживаться филиалом и показывать ЕГО номер (напр. Актау →
    # номер Алматы, в КП так и помечено «Филиал: Алматы»). Поэтому:
    #   • номер совпал с номером своего города → ок;
    #   • номер - это номер другого города из КП проекта → ок (филиал);
    #   • номера нет ни в одном городе КП → баг (чужой/неизвестный номер);
    #   • в КП у города нет номеров → критическая;
    #   • на сайте телефона нет совсем → баг.
    kp_phones = row.phone_set()
    all_kp_phones = set()
    for _rr in kp.values():
        all_kp_phones |= _rr.phone_set()
    site_ph = set(site['phones'])
    if not kp_phones:
        res.issues.append({
            'field': 'Телефон', 'status': 'critical',
            'comment': f'В КП нет ни одного номера для города «{row.city}» - заполнить КП.',
        })
    elif not site_ph:
        res.issues.append({
            'field': 'Телефон', 'status': 'bug',
            'comment': 'На сайте не найден телефон в шапке/подвале.',
        })
    elif site_ph & kp_phones:
        res.issues.append({'field': 'Телефон', 'status': 'ok',
                           'comment': 'Номер на сайте есть в КП этого города.'})
    elif site_ph & all_kp_phones:
        res.issues.append({
            'field': 'Телефон', 'status': 'ok',
            'comment': 'Номер обслуживающего филиала (есть в КП проекта).',
        })
    else:
        res.issues.append({
            'field': 'Телефон', 'status': 'bug',
            'comment': f'На сайте номер, которого нет в КП проекта: '
                       f'{", ".join(_fmt(p) for p in site["phones"])}.',
        })

    # ── Почта ──
    # Сверяем, только если в КП реально e-mail. Иногда в поле почты стоит
    # заметка («надо заказывать», «-») - это не адрес, сверять не с чем.
    kp_email = (row.email or '').strip().lower()
    if kp_email and '@' in kp_email:
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
    elif site['emails']:
        # В КП почты для города нет (а таких городов половина), но на сайте она
        # есть - показываем «есть» (статус info), а не пустой «-», чтобы не
        # выглядело как «нет почты». И подсказываем дополнить КП.
        res.issues.append({
            'field': 'Почта', 'status': 'info',
            'comment': f'На сайте есть почта ({", ".join(site["emails"])}), '
                       f'но в КП для города её нет - стоит дополнить КП.',
        })

    # ── Адрес (мягко) ──
    # Сверяем по ВСЕМУ тексту шапки+подвала: есть ли там улица и дом из КП.
    # Так надёжнее, чем вытаскивать строку адреса: на сайтах адрес бывает без
    # слова «улица» («Сухобруса 27») и без метки «Адрес» (тогда экстрактор
    # промахивался и писал ложное «не найден»).
    if row.address:
        haystack = site.get('full_text') or site.get('address') or ''
        if address_match(haystack, row.address):
            res.issues.append({'field': 'Адрес', 'status': 'ok', 'comment': ''})
        else:
            res.issues.append({
                'field': 'Адрес', 'status': 'bug',
                'comment': f'Адрес из КП не найден в шапке/подвале. По КП: '
                           f'«{row.address}».',
            })

    return res


def check_page_phone(html: str, domain: str, kp: dict) -> Optional[dict]:
    """Сверить телефон(ы) на странице с КП города (для /kak-sdelat-pokupku/ и т.п.).
    Возвращает {status, comment} или None если города нет в КП."""
    row = kp.get(_norm_host(domain))
    if not row:
        return None
    site = {p for p in split_phones(html or '') if not p.startswith('000')}
    kp_ph = row.phone_set()
    if not kp_ph:
        return {'status': 'critical',
                'comment': f'в КП нет номера для города «{row.city}»'}
    if not site:
        return {'status': 'bug', 'comment': 'на странице не найден телефон'}
    if site & kp_ph:
        return {'status': 'ok', 'comment': ''}
    all_kp = set()
    for rr in kp.values():
        all_kp |= rr.phone_set()
    if site & all_kp:
        return {'status': 'ok', 'comment': 'номер обслуживающего филиала'}
    return {'status': 'bug',
            'comment': 'телефон на странице не из КП: '
                       + ', '.join(_fmt(p) for p in site)}


# ── Сверка адресов ВСЕХ городов на странице «Контакты» с КП ───────────

# Город и адрес в списке офисов: <b>Город</b><br> Адрес …</div>.
_CONTACTS_PAIR_RE = re.compile(
    r'<b>\s*([А-ЯЁ][^<]{1,40}?)\s*</b>\s*<br[^>]*>\s*([^<]{3,90}?)\s*</', re.I)


def extract_contacts_addresses(html: str) -> dict:
    """Со страницы «Контакты» - пары {город: адрес} из списка офисов по городам."""
    out = {}
    for m in _CONTACTS_PAIR_RE.finditer(html or ''):
        city = re.sub(r'\s+', ' ', m.group(1)).strip()
        addr = re.sub(r'\s+', ' ', m.group(2)).strip()
        # адрес - со уличным маркером или номером дома (а не «Заказать звонок» и т.п.)
        if city and addr and (any(w in addr.lower() for w in (
                'улиц', 'ул.', 'проспект', 'пр.', 'пр-кт', 'шоссе', 'переул',
                'пер.', 'набережн', 'бульвар', 'площад', 'проезд', 'микрорайон'))
                or re.search(r'\d', addr)):
            out[city] = addr
    return out


def check_contacts_addresses(html: str, kp: dict) -> dict:
    """Сверить адреса всех городов на странице «Контакты» с КП.
    Возвращает: {on_page, matched, mismatched:[{city,site,kp}], not_in_kp:[city]}."""
    page = extract_contacts_addresses(html)
    _nc = lambda s: (s or '').strip().lower().replace('ё', 'е')
    kp_by_city = {_nc(row.city): row for row in kp.values() if row.address}
    matched, mismatched, not_in_kp = 0, [], []
    for city, site_addr in page.items():
        row = kp_by_city.get(_nc(city))
        if not row:
            not_in_kp.append(city)
            continue
        if address_match(site_addr, row.address):
            matched += 1
        else:
            mismatched.append({'city': city, 'site': site_addr, 'kp': row.address})
    return {'on_page': len(page), 'matched': matched,
            'mismatched': mismatched, 'not_in_kp': not_in_kp}


def _fmt(norm10: str) -> str:
    """4991306028 → +7 (499) 130-60-28 для читаемого комментария."""
    if len(norm10) != 10:
        return norm10
    return f'+7 ({norm10[:3]}) {norm10[3:6]}-{norm10[6:8]}-{norm10[8:]}'


# ── Пункт 1.4: сверка «главных переменных» поддомена с КП (для вкладки) ──


def _addr_on_page(text: str, kp_addr: str) -> str:
    """Короткий фрагмент адреса со страницы - вокруг названия улицы из КП (для
    наглядного «на сайте: …»). '' если не нашли."""
    words = sorted((w for w in re.findall(r'[А-Яа-яЁё]{5,}', kp_addr or '')
                    if w.lower() not in _STREET_WORDS), key=len, reverse=True)
    for w in words:
        m = re.search(r'.{0,25}' + re.escape(w) + r'.{0,25}', text)
        if m:
            return re.sub(r'\s+', ' ', m.group(0)).strip()
    return ''


def check_variables(html: str, domain: str) -> dict:
    """Сверяет контактные переменные главной страницы поддомена с КП: телефоны
    (поиск/реклама/общий - по правилу «номер на сайте входит в набор КП города»),
    почта, адрес, Telegram, WhatsApp. Город/страна проверяются отдельно
    region_checker'ом. Возвращает {domain, city, country, matched, fields:[...]}
    где каждое поле = {field, expected, found, status, note}.
    status: ok | ok_set | bug | warn | na.
    """
    kp = load_kp_for_domain(domain)
    host = _norm_host(domain)
    row = kp.get(host) if kp else None
    out = {"domain": host, "city": row.city if row else "",
           "country": row.country if row else "", "matched": bool(row), "fields": []}
    if not row:
        return out

    site = extract_site_contacts(html)
    fields = out["fields"]

    def add(field, expected, found, status, note=""):
        fields.append({"field": field, "expected": expected or "—",
                       "found": found or "—", "status": status, "note": note})

    kp_phones = row.phone_set()
    site_phones = {p for p in (normalize_phone(x) for x in site.get("phones", [])) if p}
    site_ph_fmt = ", ".join(_fmt(p) for p in sorted(site_phones)) or "—"

    for label, val in (("Тел. поиск", row.phone_seo),
                       ("Тел. реклама", row.phone_ad),
                       ("Тел. общий", row.phone_common)):
        _exps = phones_in_cell(val)         # первый = текущий номер (не «стар.»)
        exp = _exps[0] if _exps else ''
        if not exp:
            add(label, "—", site_ph_fmt, "na", "нет в КП")
        elif exp in site_phones:
            add(label, _fmt(exp), _fmt(exp), "ok", "виден на сайте")
        elif site_phones & kp_phones:
            add(label, _fmt(exp), site_ph_fmt, "ok_set",
                "на сайте другой номер этого же города из КП")
        elif site_phones:
            add(label, _fmt(exp), site_ph_fmt, "bug",
                "номер на сайте не совпадает с КП города")
        else:
            add(label, _fmt(exp), "—", "warn", "телефон на сайте не найден")

    exp_mail = (row.email or "").strip().lower()
    site_mails = [e.lower() for e in site.get("emails", [])]
    if not exp_mail:
        add("Почта", "—", ", ".join(site_mails[:3]), "na", "нет в КП")
    elif exp_mail in site_mails:
        add("Почта", exp_mail, exp_mail, "ok")
    elif site_mails:
        add("Почта", exp_mail, ", ".join(site_mails[:3]), "bug",
            "почта на сайте не совпадает с КП")
    else:
        add("Почта", exp_mail, "—", "warn", "почта на сайте не найдена")

    # Адрес сверяем по ВСЕМУ тексту шапки+подвала (там на сайтах СМУ и лежит
    # адрес города). Точечный сниппет ловил не то место (напр. «Город: … изменить»)
    # и давал ложное «адрес не найден» - хотя адрес есть в подвале.
    haystack = site.get("full_text") or site.get("address") or ""
    if not row.address:
        add("Адрес", "—", "", "na", "нет в КП")
    elif address_match(haystack, row.address):
        add("Адрес", row.address,
            _addr_on_page(haystack, row.address) or "совпадает с КП", "ok")
    else:
        add("Адрес", row.address, "—", "warn",
            "адрес из КП не найден в шапке/подвале - проверьте вручную")

    exp_tg = row.telegram_norm()
    site_tg = set(site.get("telegram", []))
    if not exp_tg:
        add("Telegram", "—", ", ".join(sorted(site_tg)[:3]), "na", "нет в КП")
    elif exp_tg in site_tg:
        add("Telegram", exp_tg, exp_tg, "ok")
    elif site_tg:
        # Мессенджер часто общий на всю сеть (не per-city) - это не жёсткая
        # ошибка, а повод сверить вручную. warn (⚠), а не bug (✗).
        add("Telegram", exp_tg, ", ".join(sorted(site_tg)[:3]), "warn",
            "на сайте другой Telegram (обычно общий на сеть - проверьте вручную)")
    else:
        add("Telegram", exp_tg, "—", "warn", "ссылка на Telegram не найдена")

    exp_wa = row.whatsapp_norm()
    site_wa = set(site.get("whatsapp", []))
    if not exp_wa:
        add("WhatsApp", "—", ", ".join(_fmt(w) for w in sorted(site_wa)[:3]), "na", "нет в КП")
    elif exp_wa in site_wa:
        add("WhatsApp", _fmt(exp_wa), _fmt(exp_wa), "ok")
    elif site_wa:
        # WhatsApp почти всегда общий на всю сеть (один номер на все поддомены),
        # а в КП он записан по-городам - сравнивать построчно = сплошной шум.
        # warn (⚠, не в «Расхождениях»), а не bug (✗).
        add("WhatsApp", _fmt(exp_wa), ", ".join(_fmt(w) for w in sorted(site_wa)[:3]),
            "warn", "на сайте другой WhatsApp (обычно общий на сеть - проверьте вручную)")
    else:
        add("WhatsApp", _fmt(exp_wa), "—", "warn", "ссылка на WhatsApp не найдена")

    return out


_KP_CACHE: dict[str, dict] = {}


def load_kp_for_domain(domain: str) -> dict:
    """КП того проекта, которому принадлежит домен (по совпадению второго уровня
    хоста с доменом первой строки КП). Кэшируется. Служит check_variables, когда
    проект заранее не передан."""
    host = _norm_host(domain)
    parts = host.split('.')
    brand = parts[-2] if len(parts) >= 2 else host
    for proj in ('smu', 'imp', 'mpe', 'avia'):
        if proj not in _KP_CACHE:
            # refresh=False: не тянем Google по каждому проекту при переборе -
            # нужный проект уже обновлён явным load_kp(project) в начале прогона.
            _KP_CACHE[proj] = load_kp(proj, refresh=False)
        kp = _KP_CACHE[proj]
        if any(brand == d.split('.')[-2] for d in kp if '.' in d):
            return kp
    return {}
