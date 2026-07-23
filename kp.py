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
    Учитываем коды стран: Россия/Казахстан +7/8 → 10 цифр; Беларусь +375,
    Узбекистан +998, Киргизия +996, Азербайджан +994 → 9 цифр. Excel иногда
    хранит номер числом («…448.0») - отбрасываем хвост «.0».
    """
    if s is None:
        return ''
    s = str(s)
    if s.endswith('.0'):
        s = s[:-2]
    d = re.sub(r'\D', '', s)
    if not d:
        return ''
    # СНГ-коды с 9-значным нац. номером: отбрасываем код страны.
    if d.startswith(('998', '375', '996', '994')) and len(d) >= 12:
        return d[-9:]                 # Узбекистан / Беларусь / Киргизия / Азербайджан
    if len(d) >= 11 and d[0] in '78':
        return d[-10:]                # Россия/Казахстан
    if len(d) == 10:
        return d
    return d[-10:]


_PHONE_FIND = re.compile(
    r'\+?998[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'   # Узбекистан
    r'|\+?375[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}'  # Беларусь
    # Киргизия +996 / Азербайджан +994: код страны + 9 цифр при ЛЮБОЙ группировке
    # (напр. «+996 221 31 88 82», «+994 12 345 67 89»).
    r'|(?<!\d)\+?99[64](?:[\s\-()]*\d){9}(?!\d)'
    # Россия/Казахстан: 8/+7 и ещё 10 цифр при ЛЮБОЙ группировке - и «(495) 266-29-46»
    # (3-3-2-2), и «(4852) 66-29-46» (4-значный код малых городов, 4-2-2-2).
    r'|(?<!\d)\+?[78](?:[\s\-()]*\d){10}(?!\d)'
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
    # Статическая сверка рекламного подменного номера (коллтрекинг ↔ phone_ad):
    # {status, comment, configured, kp} - показывается в секции «Замена рекл.
    # номера» листа «Аналитика», не в контактах.
    ad_check: dict = None

    @property
    def has_issues(self) -> bool:
        return any(i['status'] in ('bug', 'critical') for i in self.issues)


# ── Загрузка базы КП из репозитория ──────────────────────────────────


def _csv_path(project_id: str) -> Path:
    return CATALOGS_DIR / f'{project_id}-kp.csv'


_KP_MEM: dict[str, dict] = {}
_KP_ROWS_MEM: dict[str, list] = {}


def load_kp_rows(project_id: str, refresh: bool = True) -> list[KPRow]:
    """Строки КП списком (по одному городу-владельцу на сайт) - для «Проверки КП».
    convert_kp уже оставляет один город на ссылку: у СНГ-стран все города делят
    один сайт (stalmetural.kz/.by/.uz), безссылочные города-спутники в КП не
    берём - иначе они сверялись бы с чужим городским сайтом и давали ложные
    ошибки. Порядок строк как в CSV. Кэш на процесс."""
    if project_id in _KP_ROWS_MEM:
        return _KP_ROWS_MEM[project_id]
    if refresh:
        try:
            import kp_sheets
            if kp_sheets.kp_sheet_url(project_id):
                kp_sheets.refresh_project(project_id, log=lambda *a, **k: None)
        except Exception:
            pass
    rows = _load_kp_rows_csv(project_id)
    _KP_ROWS_MEM[project_id] = rows
    return rows


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


def _row_from_csv(row: dict) -> Optional[KPRow]:
    """Одна строка CSV → KPRow (None если нет домена)."""
    dom = (row.get('domain') or '').strip().lower()
    if not dom:
        return None
    return KPRow(
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


def _load_kp_csv(project_id: str) -> dict[str, KPRow]:
    """catalogs/{proj}-kp.csv → {домен: KPRow}. У СНГ-стран несколько городов на
    одном домене - в dict берём ПЕРВЫЙ (главный чекер проверяет сайт, ему нужен
    один город на домен; порядок как раньше). {} если нет файла."""
    p = _csv_path(project_id)
    if not p.exists():
        return {}
    out: dict[str, KPRow] = {}
    with open(p, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            kr = _row_from_csv(row)
            if kr and kr.domain not in out:
                out[kr.domain] = kr
    return out


def _load_kp_rows_csv(project_id: str) -> list[KPRow]:
    """ВСЕ строки КП списком (каждый город отдельно, СНГ на общем домене - тоже)."""
    p = _csv_path(project_id)
    if not p.exists():
        return []
    with open(p, encoding='utf-8') as f:
        return [kr for kr in (_row_from_csv(r) for r in csv.DictReader(f)) if kr]


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


# WhatsApp-ссылки (wa.me / api.whatsapp.com / chat.whatsapp.com / whatsapp://) -
# вырезаем перед извлечением телефонов, чтобы номер вотсапа не попадал в телефоны.
_WA_URL_RE = re.compile(
    r'(?:https?:)?//(?:wa\.me|api\.whatsapp\.com|chat\.whatsapp\.com)[^"\'\s>]*'
    r'|whatsapp://[^"\'\s>]*', re.I)


def extract_site_contacts(html: str) -> dict:
    """Достать из шапки+подвала телефоны, почты и текст адреса."""
    from content_checker import _extract_region
    from text_checker import html_to_visible_text

    region_html = (_extract_region(html, 'header', 'top') + '\n'
                   + _extract_region(html, 'footer', 'bottom'))
    text = html_to_visible_text(region_html)
    # Телефоны берём БЕЗ WhatsApp-ссылок (wa.me/…): номер вотсапа не должен
    # утекать в список телефонов. Если этот же номер показан ещё и как телефон
    # (tel:/видимый текст) - он всё равно попадёт (из tel:/текста), поэтому
    # города, где телефон = вотсап (напр. Бишкек), не теряют номер.
    _region_no_wa = _WA_URL_RE.sub(' ', region_html)
    # Маски ввода телефона («+7 (000) 000-00-00») и заглушки с кодом 000 -
    # не настоящие номера, отбрасываем, чтобы не считать их расхождением.
    phones = [p for p in (split_phones(text) + split_phones(_region_no_wa))
              if not p.startswith('000')]
    emails = [e.lower() for e in re.findall(
        r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', region_html, re.IGNORECASE)]
    # Текст адреса. Сначала - после метки «Адрес:» (там он на сайтах МПЭ/АПС),
    # обрезаем на следующем поле (телефон/почта/часы/индекс). Если метки нет -
    # берём кусок вокруг уличного маркера, включая СОКРАЩЕНИЯ (ул./пр-кт/пер./наб.),
    # иначе «ул.Свердлова» не ловилось и выходило «По факту: –».
    addr = ''
    m = re.search(r'адрес[:\s]+(.{6,90}?)(?:\s*(?:телефон|тел\.|e-?mail|почт|'
                  r'часы|режим|график|индекс|\d{1,2}:\d{2})|$)', text, re.IGNORECASE)
    if m:
        addr = m.group(1).strip(' ,;·|')
    if not addr:
        # От уличного маркера ВПЕРЁД (не тянем мусор слева: индекс, e-mail,
        # «сать в Telegram») и обрезаем по номеру дома (+литер/офис).
        m = re.search(r'(?:улиц\w*|\bул\.?\s?[А-ЯЁ]|проспект|пр-?кт|\bпр\.\s|'
                      r'шоссе|переул\w*|\bпер\.|набережн\w*|\bнаб\.|бульвар|\bб-р|'
                      r'микрорайон|\bмкр)[^;|№\n]{0,45}', text, re.IGNORECASE)
        if m:
            addr = m.group(0).strip(' ,;·|')
            m2 = re.match(r'.*?\d[\d/]*(?:\s*(?:литер\w*|лит|корп\w*|стр\w*|офис|оф)'
                          r'\.?\s*[\w/]*)?', addr, re.IGNORECASE)
            if m2 and m2.group(0).strip(' ,;·|'):
                addr = m2.group(0).strip(' ,;·|')
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
    # Рабочие chat-ссылки вотсапа (по ним кнопка «переходит в WhatsApp»).
    wa_urls = re.findall(
        r'href=["\']((?:https?:)?//(?:wa\.me|api\.whatsapp\.com|chat\.whatsapp\.com)'
        r'[^"\']*)["\']', html, re.I)
    # Кнопка вотсапа ВООБЩЕ есть? (ссылка на wa.me ИЛИ <a> с текстом про вотсап -
    # тогда, если рабочей chat-ссылки нет, кнопка «битая»). Ищем по <a>-тегам.
    wa_anchor_urls = re.findall(
        r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(?:(?!</a>).){0,200}?'
        r'(?:whatsapp|вотсап|ватсап|вацап)', html, re.I | re.S)
    return {
        'phones': list(dict.fromkeys(phones)),
        'emails': list(dict.fromkeys(emails)),
        'address': addr,
        'telegram': list(dict.fromkeys(tg)),
        'whatsapp': list(dict.fromkeys(wa)),
        'whatsapp_urls': list(dict.fromkeys(wa_urls)),
        'whatsapp_anchor_urls': list(dict.fromkeys(wa_anchor_urls)),
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

    # ── Рекламный номер (подмена коллтрекингом), п. «замена рекл. номера» ──
    # Статически (по HTML) сверяем рекламный подменный номер в конфиге
    # коллтрекинга (Sipuni) с phone_ad города из КП. Результат кладём в
    # отдельное поле ad_check (секция «Замена рекл. номера» в «Аналитике»),
    # а НЕ в контакты - чтобы не смешивать с телефон/почта/адрес.
    try:
        from calltracking_checker import check_ad_number
        res.ad_check = check_ad_number(html, row.phone_ad)
    except Exception:
        res.ad_check = None

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


# ── Пункт 1.4: «Проверка КП» - сверка контактов поддомена с КП (для вкладки) ──


def _site_address_full(html: str) -> str:
    """Адрес со ВСЕЙ страницы (не только шапка/подвал) по метке «Адрес:» - для
    наглядного «На сайте: …» в расхождении. На страницах «Контакты»
    адрес лежит в основном блоке, куда экстрактор шапки/подвала не смотрит. '' -
    если метки нет."""
    try:
        from text_checker import html_to_visible_text
        txt = html_to_visible_text(html)
    except Exception:
        txt = html or ''
    m = re.search(r'адрес[:\s]+(.{6,90}?)(?:\s*(?:телефон|тел\.|e-?mail|почт|часы|'
                  r'режим|график|индекс|\d{1,2}:\d{2})|$)', txt, re.IGNORECASE)
    if not m:
        return ''
    cap = m.group(1).strip(' ,;·|')
    # В адресе ОБЯЗАТЕЛЬНО номер дома (цифра) И слово-маркер улицы. Иначе после
    # случайного «адрес…» захватились категории/меню («Уличные фонари, Урны…»).
    # ВАЖНО: полные слова через префикс+\w* («улиц\w*» = «улица/улице», но НЕ
    # «Уличные» - там «улич» через Ч), сокращения - с обязательной точкой
    # («ул.»), иначе «\bул» ловило бы «Уличные/улучшение».
    if not re.search(r'\d', cap):
        return ''
    if not _RE_ADDR_STREET.search(cap):
        return ''
    return cap


# Маркер улицы в адресе (для отсева не-адресов вроде «Уличные фонари»).
_RE_ADDR_STREET = re.compile(
    r'улиц\w*|проспект|просп\w*|шоссе|переул\w*|набережн\w*|бульвар|'
    r'микрорайон|проезд\w*|тракт\w*|площад\w*|'
    r'\bул\.|\bпр-?кт\b|\bпр\.\s|\bпер\.|\bнаб\.|\bб-р\b|\bмкр\b|\bпл\.',
    re.I)


_STREET_PREFIX_RE = re.compile(
    r'((?:ул|улиц\w*|просп\w*|проспект|пр|шоссе|переул\w*|наб|набережн\w*|'
    r'бульвар|б-р|мкр|микрорайон)\.?\s*)$', re.I)


def _addr_on_page(text: str, kp_addr: str) -> str:
    """Короткий ЧИСТЫЙ фрагмент адреса со страницы - от названия улицы из КП
    ВПЕРЁД (+ уличный префикс «ул.»/«просп.», если он слева). Так не тянем мусор
    слева («сать в Telegram», индекс, e-mail). '' если не нашли."""
    words = sorted((w for w in re.findall(r'[А-Яа-яЁё]{5,}', kp_addr or '')
                    if w.lower() not in _STREET_WORDS), key=len, reverse=True)
    for w in words:
        m = re.search(re.escape(w) + r'[^;|№\n]{0,32}', text)
        if not m:
            continue
        snip = m.group(0)
        pm = _STREET_PREFIX_RE.search(text[:m.start()])   # «ул. » / «просп. » слева
        if pm:
            snip = pm.group(1) + snip
        snip = re.sub(r'\s+', ' ', snip).strip(' ,;|·-')
        # Обрезаем хвост после номера дома (+ литер/корп/строение/офис), чтобы не
        # тянуть соседний текст («Экспресс заявка», кнопки и т.п.).
        m2 = re.match(r'.*?\d[\d/]*(?:\s*(?:литер\w*|лит|корп\w*|стр\w*|офис|оф)\.?'
                      r'\s*[\w/]*)?', snip, re.I)
        if m2 and m2.group(0).strip(' ,;|·-'):
            snip = m2.group(0).strip(' ,;|·-')
        return snip
    return ''


def check_variables(html: str, domain: str, contacts_html: str = "",
                    row: 'KPRow' = None) -> dict:
    """Сверяет контактные переменные главной страницы поддомена с КП: телефоны
    (поиск/реклама/общий - по правилу «номер на сайте входит в набор КП города»),
    почта, адрес, Telegram, WhatsApp. Город/страна проверяются отдельно
    region_checker'ом. Возвращает {domain, city, country, matched, fields:[...]}
    где каждое поле = {field, expected, found, status, note}.
    status: ok | ok_set | bug | warn | na.

    contacts_html - HTML страницы «Контакты» (необязательно). У части проектов
    (МПЭ/mepen) адрес города выводится ТОЛЬКО там, в карточке «Адрес: …», а в
    подвале главной его нет - без этой страницы адрес по одной главной не
    находился («⚠ адрес не найден» у всех городов). Телефоны/почта берутся из
    шапки главной и от этого параметра не зависят.
    """
    # row задан (конкретный город - у СНГ несколько городов на одном домене) -
    # сверяем его; иначе берём город по домену из КП (как раньше).
    host = _norm_host(domain)
    if row is None:
        kp = load_kp_for_domain(domain)
        row = kp.get(host) if kp else None
    out = {"domain": host, "city": row.city if row else "",
           "country": row.country if row else "", "matched": bool(row), "fields": []}
    if not row:
        return out

    site = extract_site_contacts(html)
    fields = out["fields"]

    def add(field, expected, found, status, note=""):
        fields.append({"field": field, "expected": expected or "–",
                       "found": found or "–", "status": status, "note": note})

    def _is_mobile(n: str) -> bool:
        # Российский мобильный: 10 цифр, начинается на 9. По просьбе заказчика
        # сотовые в проверке телефонов НЕ учитываем (ни в КП, ни на сайте) -
        # сверяем только городские (стационарные) номера.
        return len(n) == 10 and n.startswith("9")

    kp_phones = {p for p in row.phone_set() if not _is_mobile(p)}
    # Телефоны сайта В ПОРЯДКЕ появления, БЕЗ сотовых. Номер WhatsApp в этот
    # список уже не утекает (wa.me-ссылки вырезаны в extract_site_contacts), но
    # если ТОТ ЖЕ номер показан ещё и как телефон (tel:/текст) - он остаётся:
    # города, где телефон = вотсап (напр. Бишкек), номер не теряют. В «На сайте»
    # кладём ОДИН - первый городской номер, а не свалку всех найденных.
    _site_ph_ordered = []
    for x in site.get("phones", []):
        p = normalize_phone(x)
        if p and not _is_mobile(p) and p not in _site_ph_ordered:
            _site_ph_ordered.append(p)
    site_phones = set(_site_ph_ordered)
    site_ph_primary = _fmt(_site_ph_ordered[0]) if _site_ph_ordered else "–"

    # Колонки телефонов - с префиксом «Тел.», чтобы не путать с колонкой «Город»
    # (проверка города). Порядок как в КП: общий → реклама → SEO.
    for label, val in (("Тел. Общий Город", row.phone_common),
                       ("Тел. Реклама Город", row.phone_ad),
                       ("Тел. SEO Город", row.phone_seo)):
        _exps = phones_in_cell(val)         # первый = текущий номер (не «стар.»)
        exp = _exps[0] if _exps else ''
        raw = str(val).strip() if val is not None else ""
        if not exp:
            # Пусто в КП - проверять нечего («–»). Но если в ячейке ЕСТЬ значение,
            # а телефон из него не разобрался (опечатка/мусор, напр. «2»), это
            # ошибка КП: показываем ✗, а что́ в КП - в колонке «КП».
            if raw and raw not in ("–", "-"):
                # Показываем и телефон С САЙТА (как у почты/Telegram/WhatsApp):
                # видно, что на сайте номер ЕСТЬ, а сломан именно КП.
                add(label, raw, site_ph_primary, "bug",
                    "телефон в КП не распознан - проверьте КП")
            else:
                add(label, "–", site_ph_primary, "na", "нет в КП")
        elif _is_mobile(exp):
            # В этой ячейке КП - сотовый: по просьбе заказчика не проверяем.
            add(label, _fmt(exp), "–", "na", "сотовый - не проверяем")
        elif exp in site_phones:
            add(label, _fmt(exp), _fmt(exp), "ok", "совпадает с КП")
        elif site_phones & kp_phones:
            # На сайте другой ГОРОДСКОЙ номер того же города из КП - засчитываем
            # (✓): значит номер города верный, просто в другой слот. В «На сайте»
            # показываем именно этот совпавший номер.
            add(label, _fmt(exp), _fmt(sorted(site_phones & kp_phones)[0]), "ok_set",
                "на сайте другой номер этого же города из КП")
        elif site_phones:
            # На сайте городской номер, которого НЕТ в КП (номер сменили/опечатка) -
            # это расхождение ✗.
            add(label, _fmt(exp), site_ph_primary, "bug",
                "телефон на сайте не совпадает с КП")
        else:
            # В КП номер есть, а на сайте его нет - это расхождение ✗ (красное),
            # а не «проверьте вручную»: сайт должен показывать номер из КП.
            add(label, _fmt(exp), "–", "bug", "телефон на сайте не найден")

    exp_mail = (row.email or "").strip().lower()
    site_mails = [e.lower() for e in site.get("emails", [])]
    if not exp_mail:
        add("Почта", "–", ", ".join(site_mails[:3]), "na", "нет в КП")
    elif exp_mail in site_mails:
        add("Почта", exp_mail, exp_mail, "ok")
    elif site_mails:
        add("Почта", exp_mail, ", ".join(site_mails[:3]), "bug",
            "почта на сайте не совпадает с КП")
    else:
        add("Почта", exp_mail, "–", "bug", "почта на сайте не найдена")

    # Адрес сверяем по ВСЕМУ тексту шапки+подвала главной (там на сайтах СМУ и
    # лежит адрес города), А ТАКЖЕ по странице «Контакты», если её передали: у
    # части проектов (МПЭ/mepen) адрес только на «Контактах», в карточке
    # «Адрес: …», а в подвале главной его нет - тогда сверка по одной главной
    # давала ложное «адрес не найден». Точечный сниппет ловил не то место (напр.
    # «Город: … изменить») - поэтому сверяем по всему тексту.
    contacts_text = ""
    if contacts_html:
        try:
            from text_checker import html_to_visible_text
            contacts_text = html_to_visible_text(contacts_html)
        except Exception:
            contacts_text = contacts_html
    haystack = " ".join(x for x in (site.get("full_text"),
                                    site.get("address"), contacts_text) if x)

    def _found_addr() -> str:
        # Чистый адрес «По факту» по метке «Адрес:»: сначала главная, потом
        # «Контакты»; в последнюю очередь - сырой текст из шапки/подвала.
        return (_site_address_full(html)
                or _site_address_full(contacts_html or "")
                or (site.get("address") or "").strip())

    if not row.address:
        add("Адрес", "–", "", "na", "нет в КП")
    elif not re.search(r'[а-яё]', _norm_addr(row.address)):
        # В КП адрес не распознан (только цифры/мусор, напр. «1.0» / «2»): сверять
        # не с чем - проблема в КП (✗ «проверьте КП»). НО адрес С САЙТА всё равно
        # ПОКАЗЫВАЕМ, если он чисто извлёкся по метке «Адрес:» (с номером дома и
        # улицей): иначе выходило «На сайте: –», хотя адрес на странице ЕСТЬ - и
        # человек думал, что тул его «не нашёл». Берём только валидный адрес
        # (_site_address_full: цифра+улица), без мусора шапки/подвала.
        _site = _site_address_full(html) or _site_address_full(contacts_html or "")
        add("Адрес", row.address, _site or "–", "bug",
            "адрес в КП не распознан (нет улицы) - проверьте КП")
    elif address_match(haystack, row.address):
        add("Адрес", row.address,
            _addr_on_page(haystack, row.address) or _found_addr()
            or "совпадает с КП", "ok")
    else:
        # Адрес из КП не совпал. Показываем, ЧТО реально на сайте (иначе выходило
        # непонятное «По факту: –», хотя адрес на странице есть - просто другой).
        # Предпочитаем адрес по метке «Адрес:» (главная → «Контакты»), и только
        # если его нет - берём из шапки/подвала. Так в «на сайте» нет мусора.
        site_addr = _found_addr()
        # site.get('address') из шапки/подвала бывает мусорным (схваченные
        # категории без номера дома) - показываем как «другой адрес» только
        # если это правда похоже на адрес (есть номер дома).
        if site_addr and re.search(r'\d', site_addr):
            # На сайте РЕАЛЬНО другой адрес (с номером дома) - это расхождение
            # ✗ (по просьбе заказчика): нашли конкретный адрес, и он не тот.
            add("Адрес", row.address, site_addr, "bug",
                "адрес на сайте не совпадает с КП")
        else:
            # Адрес из КП на странице не нашли вовсе - это расхождение ✗
            # (единообразно с телефоном/почтой: в КП есть, на сайте нет).
            # Сначала (на главной) тул ещё догрузит «Контакты» и пересверит -
            # если адрес там, станет ✓; если и там нет - остаётся ✗.
            add("Адрес", row.address, "–", "bug",
                "адрес на сайте не найден")

    # Telegram: СТРОГО сверяем аккаунт из КП с аккаунтом на сайте (по просьбе
    # заказчика). Аккаунт в ссылке t.me/<username> нормализуем к username.
    exp_tg = row.telegram_norm()
    site_tg = set(site.get("telegram", []))
    _tg_raw = (row.telegram or "").strip()
    # В «На сайте» - только фактическое значение (без приписки «есть:»).
    _tg_found = (", ".join("@" + t for t in sorted(site_tg)[:2]) if site_tg else "–")
    if not exp_tg:
        if _tg_raw and _tg_raw not in ("–", "-"):
            # В КП есть значение, но это не Telegram-ник (напр. «2» / мусор) -
            # ошибка КП, как у телефонов, а не «нет в КП».
            add("Telegram", _tg_raw, _tg_found, "bug",
                "Telegram в КП не распознан - проверьте КП")
        else:
            add("Telegram", "–", _tg_found, "na", "нет в КП")
    elif exp_tg in site_tg:
        add("Telegram", "@" + exp_tg, "@" + exp_tg, "ok", "совпадает с КП")
    elif site_tg:
        add("Telegram", "@" + exp_tg,
            ", ".join("@" + t for t in sorted(site_tg)[:2]),
            "bug", "Telegram на сайте не совпадает с КП")
    else:
        add("Telegram", "@" + exp_tg, "–", "bug", "Telegram на сайте не найден")

    # WhatsApp: СТРОГО сверяем номер из КП с номером в ссылке на сайте. Номер в
    # wa.me/<number> нормализуем к 10 цифрам. Если кнопка есть, но номер в
    # ссылке не извлечь - сверить нельзя (предупреждение).
    exp_wa = row.whatsapp_norm()
    site_wa = set(site.get("whatsapp", []))
    wa_anchor = site.get("whatsapp_anchor_urls", [])    # <a> с текстом «вотсап»
    _wa_raw = (row.whatsapp or "").strip()
    _wa_valid = len(re.sub(r"\D", "", exp_wa)) >= 9     # настоящий номер, не «2»
    # В «На сайте» - только фактическое значение (без приписки «есть:»).
    _wa_found = (", ".join(_fmt(w) for w in sorted(site_wa)[:2]) if site_wa else "–")
    if not _wa_valid:
        if _wa_raw and _wa_raw not in ("–", "-"):
            # В КП есть значение, но это не номер (напр. «2») - ошибка КП.
            add("WhatsApp", _wa_raw, _wa_found, "bug",
                "WhatsApp в КП не распознан - проверьте КП")
        else:
            add("WhatsApp", "–", _wa_found, "na", "нет в КП")
    elif exp_wa in site_wa:
        add("WhatsApp", _fmt(exp_wa), _fmt(exp_wa), "ok", "совпадает с КП")
    elif site_wa:
        add("WhatsApp", _fmt(exp_wa),
            ", ".join(_fmt(w) for w in sorted(site_wa)[:2]),
            "bug", "WhatsApp на сайте не совпадает с КП")
    elif wa_anchor:
        add("WhatsApp", _fmt(exp_wa),
            "номер в ссылке не виден", "warn",
            "кнопка WhatsApp есть, номер скрыт - проверьте вручную")
        fields[-1]["check_url"] = wa_anchor[0]
    else:
        add("WhatsApp", _fmt(exp_wa), "–", "bug", "WhatsApp на сайте не найден")

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
