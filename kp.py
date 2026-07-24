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
        # 10-значный нац. номер РФ/КЗ не бывает с кода 0 (обрезки чужих чисел -
        # ID виджетов и т.п., напр. «90492027885» → «0492027885» - не телефон).
        if len(n) == 10 and n.startswith('0'):
            continue
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

# <script>/<style> при сборе ОТОБРАЖАЕМЫХ телефонов вырезаем: там лежат «голые»
# 11-значные числа (конфиг коллтрекинга, аналитика, id), которые regex ловил как
# телефон и выдавал ложное расхождение (70492027885 → «+7 (049) 202-78-85» у
# Хабаровска). Номера из КОДА проверяем отдельно (коллтрекинг → check_ad_number).
_SCRIPT_STYLE_RE = re.compile(r'<(script|style)\b[^>]*>[\s\S]*?</\1>', re.I)

# Значения URL-атрибутов (src/href/… КРОМЕ href="tel:…"): цифры из адресов
# виджетов/картинок (напр. yandex.ru/sprav/widget/rating-badge/90492027885) -
# не телефоны, вырезаем перед поиском номеров.
_URL_ATTR_RE = re.compile(
    r'\b(?:src|data-src|srcset|action|poster|content)\s*=\s*["\'][^"\']*["\']'
    r'|\bhref\s*=\s*["\'](?!tel:)[^"\']*["\']', re.I)


def extract_site_contacts(html: str) -> dict:
    """Достать из шапки+подвала телефоны, почты и текст адреса."""
    from content_checker import _extract_region
    from text_checker import html_to_visible_text

    _footer_html = _extract_region(html, 'footer', 'bottom')
    region_html = (_extract_region(html, 'header', 'top') + '\n' + _footer_html)
    text = html_to_visible_text(region_html)
    # Телефоны берём БЕЗ WhatsApp-ссылок (wa.me/…): номер вотсапа не должен
    # утекать в список телефонов. Если этот же номер показан ещё и как телефон
    # (tel:/видимый текст) - он всё равно попадёт (из tel:/текста), поэтому
    # города, где телефон = вотсап (напр. Бишкек), не теряют номер.
    # Скрипты/стили вырезаем: их «голые» числа - не отображаемые телефоны.
    _region_novis = _SCRIPT_STYLE_RE.sub(' ', region_html)
    # Адреса ссылок/картинок/iframe (src=…, href=…) - НЕ телефоны: из URL вида
    # yandex.ru/sprav/widget/rating-badge/90492027885 цифры попадали в «телефоны»
    # и давали ложное «на сайте другой номер» (Хабаровск). href="tel:…" ОСТАВЛЯЕМ -
    # это настоящий источник номера.
    _region_no_url = _URL_ATTR_RE.sub(' ', _region_novis)
    _region_no_wa = _WA_URL_RE.sub(' ', _region_no_url)
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
                  r'часы|режим|график|индекс|контакт|время работы|'
                  r'\+?[78][\s(]?\d{3}|\d{1,2}:\d{2})|$)', text, re.IGNORECASE)
    if m:
        addr = _обрезать_хвост_адреса(m.group(1).strip(' ,;·|'))
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
    # Мессенджеры (Telegram/WhatsApp) ищем ВЕЗДЕ, КРОМЕ ПОДВАЛА: в шапке стоят
    # иконки контакта КОНКРЕТНОГО ГОРОДА (менеджер + вотсап), а в подвале - ССЫЛКИ
    # НА ГЛОБАЛЬНЫЙ канал компании (напр. t.me/inmetprom), не относящийся к городу.
    # Раньше глобальный канал утекал в «на сайте» и давал ложные срабатывания у
    # СНГ-городов (у них своих иконок в шапке нет → должно быть «на сайте нет»).
    # Просьба заказчика: «проверяй по шапке - нет значков, значит на сайте нет».
    # Вырезаем РОВНО блок <footer>…</footer> (глобальный канал компании лежит
    # там). Не через _extract_region - тот добавляет ~24 КБ перед подвалом и на
    # мелких страницах захватывает и шапку.
    _ftr_m = re.search(r'<footer\b[^>]*>.*?</footer>', html, re.I | re.S)
    _msgr_html = (html[:_ftr_m.start()] + ' ' + html[_ftr_m.end():]) if _ftr_m else html
    tg = re.findall(r'(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,})', _msgr_html, re.I)
    tg += re.findall(r'tg://resolve\?domain=([A-Za-z0-9_]{3,})', _msgr_html, re.I)
    _tg_skip = {'share', 'joinchat', 'iv', 's', 'proxy', 'socks',
                'addstickers', 'joinchannel', 'addlist'}
    tg = [t.lower() for t in tg if t.lower() not in _tg_skip]
    wa_raw = re.findall(
        r'(?:wa\.me/|api\.whatsapp\.com/send[^"\'\s]*?phone=|whatsapp://send\?phone=)'
        r'(\+?\d[\d\-()\s]{7,})', _msgr_html, re.I)
    wa = [n for n in (normalize_phone(w) for w in wa_raw) if n]
    # Рабочие chat-ссылки вотсапа (по ним кнопка «переходит в WhatsApp»).
    wa_urls = re.findall(
        r'href=["\']((?:https?:)?//(?:wa\.me|api\.whatsapp\.com|chat\.whatsapp\.com)'
        r'[^"\']*)["\']', _msgr_html, re.I)
    # Кнопка вотсапа ВООБЩЕ есть? (ссылка на wa.me ИЛИ <a> с текстом про вотсап -
    # тогда, если рабочей chat-ссылки нет, кнопка «битая»). Ищем по <a>-тегам.
    wa_anchor_urls = re.findall(
        r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(?:(?!</a>).){0,200}?'
        r'(?:whatsapp|вотсап|ватсап|вацап)', _msgr_html, re.I | re.S)
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


# Код страны (для читаемого показа нац. номера) по названию страны из КП и по
# домену. СНГ-страны с 9-значным нац. номером: Беларусь +375, Узбекистан +998,
# Киргизия +996, Азербайджан +994. Россия/Казахстан - +7 (10 цифр).
_DIAL_BY_COUNTRY = {
    'беларусь': '375', 'белоруссия': '375',
    'кыргызстан': '996', 'киргизия': '996',
    'узбекистан': '998', 'азербайджан': '994',
}
_DIAL_BY_TLD = {'by': '375', 'kg': '996', 'uz': '998', 'az': '994'}


def _dial_for(row: 'KPRow') -> str:
    """Код страны для показа нац. номера: сначала по стране из КП, затем по
    домену (.by/.kg/.uz/.az). По умолчанию '7' (Россия/Казахстан)."""
    if row is not None:
        c = (getattr(row, 'country', '') or '').strip().lower()
        if c in _DIAL_BY_COUNTRY:
            return _DIAL_BY_COUNTRY[c]
        m = re.search(r'\.([a-z]{2})$', getattr(row, 'domain', '') or '')
        if m and m.group(1) in _DIAL_BY_TLD:
            return _DIAL_BY_TLD[m.group(1)]
    return '7'


def _fmt(nat: str, dial: str = '7') -> str:
    """Нац. номер → читаемый вид с кодом страны. 4991306028 → +7 (499) 130-60-28;
    447666258 (Беларусь) → +375 (44) 766-62-58; 221318882 (Киргизия) →
    +996 (221) 31-88-82. Иностранные 9-значные без кода страны раньше писались
    «голыми» цифрами (выглядело как мусор) - теперь показываем с +кодом."""
    nat = re.sub(r'\D', '', str(nat or ''))
    if dial == '7' and len(nat) == 10:
        return f'+7 ({nat[:3]}) {nat[3:6]}-{nat[6:8]}-{nat[8:]}'
    if dial == '996' and len(nat) == 9:              # +996 (221) 31-88-82
        return f'+996 ({nat[:3]}) {nat[3:5]}-{nat[5:7]}-{nat[7:]}'
    if dial in ('375', '998', '994') and len(nat) == 9:   # +375 (44) 766-62-58
        return f'+{dial} ({nat[:2]}) {nat[2:5]}-{nat[5:7]}-{nat[7:]}'
    if len(nat) == 10:                               # запасной вариант - +7
        return f'+7 ({nat[:3]}) {nat[3:6]}-{nat[6:8]}-{nat[8:]}'
    return nat


# ── Пункт 1.4: «Проверка КП» - сверка контактов поддомена с КП (для вкладки) ──


# Хвост НЕ-адреса, приклеивающийся к захваченному адресу на страницах
# «Контакты»: «… 35Д Контакты: +7 (903)… krym@… Время работы: пн-пт…».
# Обрезаем всё, начиная с первого такого маркера (слово-метка, телефон, почта).
# Плюс азербайджанские метки переводного сайта: «İş saatları» (часы работы),
# «Əlaqə» (контакты).
_ADDR_TAIL_RE = re.compile(
    r'\s*(?:контакт\w*|время работы|режим работы|часы работы|график\w*|режим\w*|'
    r'реквизит\w*|прайс\w*|скачать|наш телефон|наша почта|наш адрес|карт[ае]\b|'
    r'телефон\w*|тел\.|e-?mail|почт\w*[:\s]|почта\b|whatsapp|телеграм|telegram|'
    r'i[şs]\s*saat\w*|əlaqə|elaqe|iş\s*vaxt\w*|'
    r'\+?[78][\s(]?\d{3}|\+?\d{11,}|[a-z0-9._%+-]+@).*$', re.I | re.S | re.U)


def _обрезать_хвост_адреса(s: str) -> str:
    """Срезать с адреса хвост «Контакты: … Время работы: …» (телефон/почта/метки)."""
    return _ADDR_TAIL_RE.sub('', s or '').strip(' ,;·|-')


# Буквы адреса: кириллица + латиница + азербайджанские (ə/ı/İ/ö/ü/ç/ş/ğ) -
# на переводном сайте адрес латиницей («Bakı, 23 İzmir küçəsi»).
_ADDR_LETTER = r'A-Za-zА-Яа-яЁёÀ-ɏəƏıİ'


def _site_address_full(html: str) -> str:
    """Адрес со ВСЕЙ страницы (не только шапка/подвал) по метке «Адрес:»/«Ünvan:» -
    для наглядного «Сайт: …» в расхождении. На страницах «Контакты»
    адрес лежит в основном блоке, куда экстрактор шапки/подвала не смотрит. '' -
    если метки нет."""
    try:
        from text_checker import html_to_visible_text
        txt = html_to_visible_text(html)
    except Exception:
        txt = html or ''
    # Метка адреса: «Адрес:» (рус) или «Ünvan:» (азерб. переводного сайта).
    # Захватываем кусок ПОСЛЕ метки (не требуем стоп-маркера справа - иначе
    # адрес, за которым сразу идёт «Реквизиты»/«Скачать» без телефона/почты,
    # вообще не находился, напр. СПб «набережная Обводного канала, 64к2»).
    m = re.search(r'(?:адрес|[uü]nvan)[:\s]+(.{4,120})', txt, re.IGNORECASE | re.U)
    if not m:
        return ''
    # Обрезаем на первом «не-адресном» маркере (следующее поле карточки/меню:
    # «Реквизиты», «Скачать», «Контакты», «Время работы», телефон, почта…).
    cap = _обрезать_хвост_адреса(m.group(1).strip(' ,;·|'))
    # В адресе ОБЯЗАТЕЛЬНО номер дома (цифра) И похожесть на адрес: слово-маркер
    # улицы (рус/азерб) ЛИБО форма «Название, номер» («Ярмарочная, 55»,
    # «Bakı, 23 İzmir küçəsi»). Иначе после случайного «адрес…» захватились бы
    # категории/меню («Уличные фонари…»).
    if not re.search(r'\d', cap):
        return ''
    if not (_RE_ADDR_STREET.search(cap)
            or re.search(r'[' + _ADDR_LETTER + r'][' + _ADDR_LETTER + r'\-]{2,}'
                         r'\s*,\s*\d{1,4}\b', cap, re.U)):
        return ''
    return cap


# Маркер улицы в адресе (для отсева не-адресов вроде «Уличные фонари»).
# + азербайджанские: küçə(si) - улица, prospekt(i) - проспект, döngə - переулок.
_RE_ADDR_STREET = re.compile(
    r'улиц\w*|проспект|просп\w*|шоссе|переул\w*|набережн\w*|бульвар|'
    r'микрорайон|проезд\w*|тракт\w*|площад\w*|'
    r'küçəs\w*|küçə|prospekt\w*|döngəs\w*|məhləs\w*|'
    r'\bул\.|\bпр-?кт\b|\bпр\.\s|\bпер\.|\bнаб\.|\bб-р\b|\bмкр\b|\bпл\.',
    re.I | re.U)


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
        return _обрезать_хвост_адреса(snip)
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

    # Код страны для читаемого показа номеров этого города (Беларусь/Киргизия/…).
    dial = _dial_for(row)
    def fmt(n):
        return _fmt(n, dial)

    # Сверяем ВСЕ номера города из КП с ВСЕМИ номерами сайта - сотовые тоже
    # (у ряда городов, напр. Донецка/Севастополя, ОСНОВНОЙ номер - сотовый
    # +7 903…; раньше сотовые выкидывались и выходило ложное «нет ни в КП, ни
    # на сайте»). Правило заказчика: берём значение КП и сравниваем с сайтом.
    kp_phones = set(row.phone_set())
    # Телефоны сайта В ПОРЯДКЕ появления. Номер WhatsApp в этот список не утекает
    # (wa.me-ссылки вырезаны в extract_site_contacts), но если ТОТ ЖЕ номер
    # показан ещё и как телефон (tel:/текст) - он остаётся.
    _site_ph_ordered = []
    for x in site.get("phones", []):
        p = normalize_phone(x)
        if p and p not in _site_ph_ordered:
            _site_ph_ordered.append(p)
    site_phones = set(_site_ph_ordered)
    site_ph_primary = fmt(_site_ph_ordered[0]) if _site_ph_ordered else "–"
    site_ph_any = site_ph_primary

    # Рекламный номер («Реклама Город») подменяется коллтрекингом ТОЛЬКО при
    # рекламном визите (?utm_source=yandex) - в обычной выдаче/инкогнито на
    # странице стоит обычный (SEO/общий) номер. Поэтому сверять его с ВИДИМЫМ
    # номером нельзя (всегда «не совпадает»). Берём пул подменных номеров из
    # конфига коллтрекинга (Sipuni) прямо в HTML - тот же, что JS показывает
    # рекламе, - и сверяем с ним. None, если в КП нет рекл. номера.
    from calltracking_checker import check_ad_number, parse_config
    _ad = check_ad_number(html, row.phone_ad)
    # Пул подменных (рекламных) номеров ИЗ КОДА (конфиг коллтрекинга). Рекламные
    # номера часто сотовые (напр. +7 962…) - их НЕ исключаем.
    _pool = set(parse_config(html).get("ad_numbers", set()))

    # Колонки телефонов - с префиксом «Тел.», чтобы не путать с колонкой «Город»
    # (проверка города). Порядок как в КП: общий → реклама → SEO.
    for label, val in (("Тел. Общий Город", row.phone_common),
                       ("Тел. Реклама Город", row.phone_ad),
                       ("Тел. SEO Город", row.phone_seo)):
        _exps = phones_in_cell(val)         # первый = текущий номер (не «стар.»)
        exp = _exps[0] if _exps else ''
        raw = str(val).strip() if val is not None else ""
        # В колонке «КП» ВСЕГДА показываем, что реально стоит в ячейке КП: если
        # там валидный номер - в читаемом формате, если мусор/«2»/«.» - как есть,
        # если пусто - «–» (правило заказчика: любое значение КП выводим как есть).
        _kp_disp = fmt(exp) if exp else (raw if raw and raw not in ("–", "-") else "–")
        if label == "Тел. Реклама Город":
            # Рекламный номер живёт в КОДЕ (конфиг коллтрекинга), а не в видимом
            # тексте - поэтому сверяем КП с пулом подмены ИЗ КОДА.
            if _ad and _ad["status"] == "ok":
                add(label, _kp_disp, fmt(exp), "ok",
                    "рекламный номер в коде (коллтрекинг) совпадает с КП")
                continue
            if _ad and _ad["status"] == "bug":
                _cfg = ", ".join(fmt(n) for n in _ad["configured"]) or "–"
                add(label, _kp_disp, _cfg, "bug",
                    "телефон на сайте не совпадает с КП")
                continue
            if not exp:
                _code_new = sorted(n for n in _pool if n not in kp_phones)
                if _code_new:
                    if _kp_disp != "–":
                        # В ячейке КП стоит значение (мусор «2») - оно не совпадает
                        # с рекламным номером из кода → ✗ (значение КП показываем).
                        add(label, _kp_disp, ", ".join(fmt(n) for n in _code_new),
                            "bug", "телефон на сайте не совпадает с КП")
                    else:
                        # КП пусто, а в коде есть рекламный номер, которого в КП
                        # города нет вообще → ⚠ (в КП, видимо, не заведён).
                        add(label, "–", ", ".join(fmt(n) for n in _code_new), "warn",
                            "в коде есть рекламный номер, которого нет в КП города")
                    continue
            # иначе (нет коллтрекинга / обычный номер) - общая логика ниже.
        if not exp:
            if _kp_disp != "–":
                # В ячейке КП стоит ЗНАЧЕНИЕ, но это не номер («2»/мусор). Это
                # ИНФА в КП, и она заведомо не совпадает с сайтом → всегда ✗,
                # прочерк тут запрещён (правило заказчика: есть инфа хоть где-то
                # и она разная - это расхождение). В «Сайт» показываем, что
                # реально на сайте: городской, а если его нет - сотовый.
                add(label, _kp_disp, site_ph_any, "bug",
                    "телефон на сайте не совпадает с КП")
            else:
                # Ячейка КП ПУСТАЯ (отдельного номера для слота нет):
                #   • на сайте НОВЫЙ номер, которого в КП города нет вообще → ✗;
                #   • на сайте только известные номера города (общий) → «–»;
                #   • на сайте номера нет → «–».
                _new = [p for p in _site_ph_ordered if p not in kp_phones]
                if _new:
                    add(label, "–", fmt(_new[0]), "bug",
                        "телефон на сайте не совпадает с КП")
                elif site_phones:
                    add(label, "–", "–", "na",
                        "отдельного номера в КП нет - на сайте общий номер города")
                else:
                    add(label, "–", "–", "na", "нет ни в КП, ни на сайте")
        elif exp in site_phones:
            add(label, fmt(exp), fmt(exp), "ok", "совпадает с КП")
        elif site_phones & kp_phones:
            # На сайте другой ГОРОДСКОЙ номер того же города из КП - засчитываем
            # (✓): значит номер города верный, просто в другой слот. В «На сайте»
            # показываем именно этот совпавший номер.
            add(label, fmt(exp), fmt(sorted(site_phones & kp_phones)[0]), "ok_set",
                "на сайте другой номер этого же города из КП")
        elif site_phones:
            # На сайте городской номер, которого НЕТ в КП (номер сменили/опечатка) -
            # это расхождение ✗.
            add(label, fmt(exp), site_ph_primary, "bug",
                "телефон на сайте не совпадает с КП")
        else:
            # В КП номер есть, а на сайте его нет - это расхождение ✗ (красное),
            # а не «проверьте вручную»: сайт должен показывать номер из КП.
            add(label, fmt(exp), "–", "bug", "телефон на сайте не совпадает с КП")

    exp_mail = (row.email or "").strip().lower()
    # Реальная почта, а не «2»/мусор: есть «@» и точка в домене.
    _mail_valid = bool(re.match(r'[^@\s]+@[^@\s]+\.[^@\s]+$', exp_mail))
    site_mails = [e.lower() for e in site.get("emails", [])]
    _mail_found = ", ".join(site_mails[:3]) if site_mails else "–"
    if not _mail_valid:
        _kp_mail_show = exp_mail if exp_mail and exp_mail not in ("–", "-") else "–"
        if _kp_mail_show != "–":
            # В КП стоит значение, но это не почта («2»/мусор) - это ИНФА, и она
            # заведомо не совпадает → всегда ✗ (даже если на сайте почты нет).
            add("Почта", _kp_mail_show, _mail_found, "bug",
                "почта на сайте не совпадает с КП")
        elif site_mails:
            # В КП пусто, на сайте почта есть → ✗.
            add("Почта", "–", _mail_found, "bug",
                "почта на сайте не совпадает с КП")
        else:
            add("Почта", "–", "–", "na", "нет ни в КП, ни на сайте")
    elif exp_mail in site_mails:
        add("Почта", exp_mail, exp_mail, "ok", "совпадает с КП")
    elif site_mails:
        add("Почта", exp_mail, _mail_found, "bug",
            "почта на сайте не совпадает с КП")
    else:
        add("Почта", exp_mail, "–", "bug", "почта на сайте не совпадает с КП")

    # Адрес ищем как «Ctrl+F по странице»: по ВСЕМУ видимому тексту ГЛАВНОЙ (не
    # только шапка/подвал - адрес бывает и в блоке контактов посреди страницы),
    # А ТАКЖЕ по странице «Контакты», если её передали (там адрес у части
    # проектов - в карточке «Адрес: …»). Если на главной не нашли - variables_run
    # догружает «Контакты» и пересверяет.
    try:
        from text_checker import html_to_visible_text
        _main_text = html_to_visible_text(html)
    except Exception:
        _main_text = html or ""
    contacts_text = ""
    if contacts_html:
        try:
            from text_checker import html_to_visible_text
            contacts_text = html_to_visible_text(contacts_html)
        except Exception:
            contacts_text = contacts_html
    haystack = " ".join(x for x in (_main_text, site.get("address"),
                                    contacts_text) if x)

    def _found_addr() -> str:
        # Чистый адрес «По факту» по метке «Адрес:»: сначала главная, потом
        # «Контакты»; в последнюю очередь - сырой текст из шапки/подвала.
        return (_site_address_full(html)
                or _site_address_full(contacts_html or "")
                or _обрезать_хвост_адреса((site.get("address") or "").strip()))

    # Есть ли в КП РЕАЛЬНЫЙ адрес (а не пусто/«2»/«1.0» - только цифры/мусор).
    _addr_kp_valid = bool(row.address) and bool(re.search(r'[а-яё]',
                                                          _norm_addr(row.address)))
    if not _addr_kp_valid:
        # Что реально на сайте: адрес по метке «Адрес:» (главная → «Контакты»),
        # иначе валидный уличный сниппет из шапки/подвала (цифра + маркер улицы).
        _site = _site_address_full(html) or _site_address_full(contacts_html or "")
        if not _site:
            _fb = _обрезать_хвост_адреса((site.get("address") or "").strip())
            if _fb and re.search(r'\d', _fb) and _RE_ADDR_STREET.search(_fb):
                _site = _fb
        _kp_addr_show = (row.address if row.address
                         and str(row.address).strip() not in ("–", "-") else "–")
        if _kp_addr_show != "–":
            # В КП стоит значение, но это не адрес («2»/мусор) - это ИНФА, и она
            # заведомо не совпадает → всегда ✗ (даже если адрес на сайте не
            # вытащился - в КП инфа есть, прочерк запрещён).
            add("Адрес", _kp_addr_show, _site or "–", "bug",
                "адрес на сайте не совпадает с КП")
        elif _site:
            # В КП пусто, на сайте адрес есть → ✗.
            add("Адрес", "–", _site, "bug", "адрес на сайте не совпадает с КП")
        else:
            add("Адрес", "–", "–", "na", "нет ни в КП, ни на сайте")
    elif address_match(haystack, row.address):
        add("Адрес", row.address,
            _addr_on_page(haystack, row.address) or _found_addr()
            or "совпадает с КП", "ok", "совпадает с КП")
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
            # В КП адрес есть, а на сайте его нет - расхождение ✗ (единообразно
            # с телефоном/почтой). На главной тул ещё догрузит «Контакты» и
            # пересверит - если адрес там, станет ✓; если и там нет - остаётся ✗.
            add("Адрес", row.address, "–", "bug",
                "адрес на сайте не совпадает с КП")

    # Telegram: СТРОГО сверяем аккаунт из КП с аккаунтом на сайте (по просьбе
    # заказчика). Аккаунт в ссылке t.me/<username> нормализуем к username.
    exp_tg = row.telegram_norm()
    site_tg = set(site.get("telegram", []))
    _tg_raw = (row.telegram or "").strip()
    _tg_found = (", ".join("@" + t for t in sorted(site_tg)[:2]) if site_tg else "–")
    # Значение КП для показа: ник (@…) либо сырой мусор («2»), либо «–» если пусто.
    _tg_kp_show = ("@" + exp_tg) if exp_tg else (_tg_raw if _tg_raw
                                                 and _tg_raw not in ("–", "-") else "–")
    if not site_tg:
        # На сайте Telegram НЕТ (в шапке нет значка). Если в КП значение есть -
        # это ✗ «Telegram на сайте отсутствует» (просьба заказчика: так и писать,
        # с крестиком и значением из КП). Если и в КП нет - прочерк.
        if _tg_kp_show != "–":
            add("Telegram", _tg_kp_show, "–", "bug", "Telegram на сайте отсутствует")
        else:
            add("Telegram", "–", "–", "na", "нет ни в КП, ни на сайте")
    elif exp_tg and exp_tg in site_tg:
        add("Telegram", "@" + exp_tg, "@" + exp_tg, "ok", "совпадает с КП")
    else:
        # На сайте Telegram ЕСТЬ, но другой (или в КП мусор) → не совпадает.
        add("Telegram", _tg_kp_show, _tg_found, "bug",
            "Telegram на сайте не совпадает с КП")

    # WhatsApp: СТРОГО сверяем номер из КП с номером в ссылке на сайте. Номер в
    # wa.me/<number> нормализуем к 10 цифрам. Если кнопка есть, но номер в
    # ссылке не извлечь - сверить нельзя (предупреждение).
    exp_wa = row.whatsapp_norm()
    site_wa = set(site.get("whatsapp", []))
    wa_anchor = site.get("whatsapp_anchor_urls", [])    # <a> с текстом «вотсап»
    _wa_raw = (row.whatsapp or "").strip()
    _wa_valid = len(re.sub(r"\D", "", exp_wa)) >= 9     # настоящий номер, не «2»
    _wa_found = (", ".join(fmt(w) for w in sorted(site_wa)[:2]) if site_wa else "–")
    # Значение КП для показа: читаемый номер, либо сырой мусор, либо «–».
    _wa_kp_show = fmt(exp_wa) if _wa_valid else (_wa_raw if _wa_raw
                                                 and _wa_raw not in ("–", "-") else "–")
    if not site_wa:
        # На сайте WhatsApp-номера НЕТ.
        if wa_anchor and _wa_valid:
            # Кнопка вотсапа в шапке есть, но номер в ссылке не читается - сверить
            # нельзя (⚠, проверить вручную).
            add("WhatsApp", fmt(exp_wa), "номер в ссылке не виден", "warn",
                "кнопка WhatsApp есть, номер скрыт - проверьте вручную")
            fields[-1]["check_url"] = wa_anchor[0]
        elif _wa_kp_show != "–":
            # В КП значение есть, а на сайте вотсапа нет → ✗ «отсутствует».
            add("WhatsApp", _wa_kp_show, "–", "bug", "WhatsApp на сайте отсутствует")
        else:
            add("WhatsApp", "–", "–", "na", "нет ни в КП, ни на сайте")
    elif _wa_valid and exp_wa in site_wa:
        add("WhatsApp", fmt(exp_wa), fmt(exp_wa), "ok", "совпадает с КП")
    else:
        # На сайте вотсап ЕСТЬ, но другой (или в КП мусор) → не совпадает.
        add("WhatsApp", _wa_kp_show, _wa_found, "bug",
            "WhatsApp на сайте не совпадает с КП")

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
