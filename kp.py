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
    # МПК (Метпромко). Лист «карта присутствия»: чистая таблица - по СТРОКЕ на
    # город (страна/город/адрес/индекс/домен/телефон/WhatsApp/email/филиал). У МПК
    # НЕТ поддоменов по городам: один сайт metpromko.ru на все города РФ (+ домены
    # СНГ .kz/.by/.kg/.uz/.az/.am). Поэтому per_city=True: НЕ схлопываем города в
    # один домен (иначе оставался бы 1 город на metpromko.ru). Контакты города
    # берём из пикера выбора города прямо в HTML (parse_city_picker) + адрес из
    # блока филиала на /contacts (parse_branch_addresses) - см. variables_run.
    # SEO/Реклама у МПК нет - единственная колонка «телефон» идёт в слот «Общий».
    'mpk': {
        'sheet': 'карта присутствия',
        'phone_seo':    ('seo', 'город'),      # нет таких колонок - слот пустой
        'phone_ad':     ('реклама', 'город'),  # нет таких колонок - слот пустой
        'phone_common': ('телефон',),          # единственная колонка «телефон»
        'per_city': True,                      # один сайт на все города (пикер)
    },
    # МПИ (МетПромИнтекс). Лист «Карта присутсвия» (именно так, с опечаткой в
    # таблице). Структура как у МПЭ/СМУ: у каждого города свой поддомен, телефоны
    # «Общий Город» + «Общий Сотовый». Колонка ссылки идёт БЕЗ заголовка (пустая
    # шапка после «Численность») - берём её по позиции (url_col), как страну/город
    # у АПС.
    'mpi': {
        'sheet': 'Карта присутсвия',
        'phone_seo':    ('seo', 'город'),
        'phone_ad':     ('реклама', 'город'),
        'phone_common': ('общий', 'город'),
        'url_col': 3,
    },
    # SHOPMET. Единственный лист «Лист1», шапка на 2-й строке (над ней -
    # объединённые заголовки блоков Телефония/Мессенджеры/Яндекс Бизнес/2ГИС/
    # Google). Структура как у СМУ/ИМП: у каждого города свой поддомен (колонка
    # «url»), телефоны «Общий/Реклама/SEO Город».
    'sm': {
        'sheet': 'Лист1',
        'phone_seo':    ('seo', 'город'),
        'phone_ad':     ('реклама', 'город'),
        'phone_common': ('общий', 'город'),
    },
    # STEELBORG. Лист «Справочники», структура как у СМУ: у каждого города
    # свой поддомен (колонка «url»), телефоны «Общий/Реклама/SEO Город».
    'stb': {
        'sheet': 'Справочники',
        'phone_seo':    ('seo', 'город'),
        'phone_ad':     ('реклама', 'город'),
        'phone_common': ('общий', 'город'),
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
    s = s.replace('ё', 'е')               # ё/е пишут вперемешку - не путать с расхождением
    s = re.sub(r'[^\w\s]', ' ', s)        # убрать пунктуацию
    return re.sub(r'\s+', ' ', s).strip()


def address_match(site_addr: str, kp_addr: str) -> bool:
    """
    Совпадение адреса КП с текстом сайта - ЛОКАЛЬНО (как Ctrl+F по адресу):
    название улицы из КП должно встретиться на странице, И РЯДОМ с ним (в том же
    месте, а не где-то ещё) - номер дома из КП. Иначе на длинном тексте страницы
    «улица где-то» + «номер где-то ещё» ложно засчитывались как совпадение, и
    изменённый адрес/номер дома не ловился. «Рязанский проспект, 86/1с1» ≈
    «Рязанский пр., 86/1c1»; «улица Люблинская, 151» ≠ «улица Люблинская, 99».
    """
    k_raw = kp_addr or ""
    k = _norm_addr(k_raw)
    if not k:
        return False
    # Значимые слова улицы (без типов «улица/проспект/…» и коротких).
    kwords = [w for w in re.findall(r'[а-яё]+', k)
              if len(w) >= 4 and w not in _STREET_WORDS]
    if not kwords:
        # В КП нет названия улицы (редкий случай) - мягко по номеру дома.
        knums = set(re.findall(r'\d+', k))
        return bool(knums) and bool(knums & set(re.findall(r'\d+',
                                                           _norm_addr(site_addr))))
    # ГЛАВНЫЙ номер дома: первое число в части ПОСЛЕ последней запятой
    # («…, 86/1с1» → 86; «…, 151» → 151; «микрорайон 26-й, 58Б» → 58).
    _tail = k_raw.rsplit(',', 1)[-1] if ',' in k_raw else k_raw
    _hm = re.search(r'\d+', _norm_addr(_tail))
    house = _hm.group(0) if _hm else ''
    s = _norm_addr(site_addr)
    _house_re = re.compile(r'(?<!\d)' + re.escape(house) + r'(?!\d)') if house else None
    for w in kwords:
        for m in re.finditer(re.escape(w), s):
            if _house_re is None:
                return True            # в КП нет номера дома - хватит улицы
            # Окно вокруг названия улицы: номер дома обычно сразу за улицей,
            # иногда перед («5-й проезд»). Смотрим ±немного символов.
            window = s[max(0, m.start() - 12):m.end() + 30]
            if _house_re.search(window):
                return True
    return False


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
    yandex_map_url: str = ''    # ссылка на карточку организации в Яндекс.Картах
    twogis_map_url: str = ''    # ссылка на карточку организации в 2ГИС
    google_map_url: str = ''    # ссылка на карточку организации в Google Maps

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
        yandex_map_url=row.get('yandex_map_url', ''),
        twogis_map_url=row.get('twogis_map_url', ''),
        google_map_url=row.get('google_map_url', ''),
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
                  r'(?<!\d)\+?[78][\s(]?\d{3}|\d{1,2}:\d{2})|$)', text, re.IGNORECASE)
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


# ── Сайты «один домен на все города» (пикер выбора города) ────────────
# У МПК (Метпромко) один сайт metpromko.ru обслуживает ВСЕ города РФ (+ отдельные
# домены СНГ). Контакты каждого города НЕ на своём поддомене, а прямо в HTML:
#   • список выбора города - ссылки <a class="selectCity"> с data-city / data-phone1
#     (телефон) / data-email / data-phone2/phone3 (WhatsApp) - клик по городу лишь
#     перекладывает эти значения в шапку (onclick="return false;", без запроса);
#   • адрес города - в блоке филиала на /contacts: <h4>Город</h4><p>индекс, г. …</p>.
# Мы разбираем эти данные из статического HTML и собираем «страницу города», по
# которой обычная сверка check_variables работает как для отдельного поддомена.


def parse_city_picker(html: str) -> dict:
    """{город → {phone, email, whatsapp}} из ссылок <a class="selectCity" …> с
    data-атрибутами (data-phone1/email/phone2). '' там, где атрибута нет."""
    out = {}
    for m in re.finditer(r'<a\b[^>]*\bclass="[^"]*selectCity[^"]*"[^>]*>', html or '', re.I):
        tag = m.group(0)
        d = dict(re.findall(r'data-([\w-]+)="([^"]*)"', tag))
        city = (d.get('city') or '').strip()
        if not city:
            continue
        out[city] = {
            'phone': (d.get('phone1') or '').strip(),
            'email': (d.get('email') or '').strip(),
            'whatsapp': (d.get('phone2') or d.get('phone3') or '').strip(),
        }
    return out


# Города-синонимы (в КП одно имя, на сайте другое). Астана = Нур-Султан (сайт в
# «Контактах»/пикере пишет «Нур-Султан», а в КП город «Астана»).
CITY_ALIASES = {
    'астана': ['нур-султан', 'нурсултан'],
    'нур-султан': ['астана'],
    'нурсултан': ['астана'],
}


def city_aliases(city: str) -> list:
    """Имя города + его синонимы (нижним регистром, ё→е) - для поиска города на
    сайте, где он может называться иначе (Астана ↔ Нур-Султан)."""
    n = re.sub(r'\s+', ' ', (city or '')).strip().lower().replace('ё', 'е')
    out = [n] + [a.replace('ё', 'е') for a in CITY_ALIASES.get(n, [])]
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def parse_city_branches(html: str) -> dict:
    """{город → {address, phones:[…], email, whatsapp}} из блоков филиалов на
    «Контактах»: <h4>Екатеринбург</h4><p>индекс, г. …, улица, дом</p><ul>
    <li><a href="tel:…">(343) 202-38-83</a>, <a href="tel:…">(343) 202-68-86</a>
    <li><a href="wa.me/…">WhatsApp</a><li><a href="mailto:…">почта</a></ul>.
    Берём ВСЕ телефоны блока (у части городов их 2), почту и WhatsApp. Служебные
    <h4> (напр. «Филиалы в России») без адреса/телефона отсеиваются."""
    out = {}
    parts = re.split(r'<h4\b[^>]*>', html or '')
    for seg in parts[1:]:
        mcity = re.match(r'([^<]{2,40})</h4>', seg)
        if not mcity:
            continue
        city = re.sub(r'\s+', ' ', mcity.group(1)).strip()
        block = seg[mcity.end():]          # до следующего <h4> (уже отрезано split'ом)
        mp = re.search(r'<p\b[^>]*>(.*?)(?:<br|Время\s+работы|</p>)', block, re.S | re.I)
        addr = ''
        if mp:
            a = re.sub(r'<[^>]+>', ' ', mp.group(1)).replace('\xa0', ' ').replace('&nbsp;', ' ')
            addr = re.sub(r'\s+', ' ', a).strip(' ,;')
        phones = re.findall(r'href="tel:([^"]+)"', block)
        wa = re.findall(r'(?:wa\.me/|whatsapp[^"]*?phone=)(\+?\d[\d\-()\s]{7,})', block, re.I)
        # ВСЕ почты блока - и из mailto, и из ВИДИМОГО текста. У части городов на
        # сайте ссылка битая: mailto:ufa@… , а показан kazan@… (Казань). Берём обе,
        # сверка засчитает совпадение по ЛЮБОЙ - как и у 2 телефонов города.
        emails = re.findall(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', block, re.I)
        has_addr = bool(addr and re.search(r'\d', addr)
                        and (re.search(r'\bг\.', addr) or _RE_ADDR_STREET.search(addr)))
        if not has_addr and not phones and not emails:
            continue                       # служебный заголовок, не филиал
        if city in out:
            continue
        out[city] = {
            'address': addr if has_addr else '',
            'phones': list(dict.fromkeys(phones)),
            'emails': list(dict.fromkeys(e.lower() for e in emails)),
            'whatsapp': wa[0].strip() if wa else '',
        }
    return out


def _mpk_field(kp_norms, kp_raw, header_norms, contacts_norms, fmt):
    """Трёхточечная сверка одного поля МПК: КП vs ШАПКА (данные пикера города) vs
    КОНТАКТЫ (блок филиала на /contacts). Возвращает (status, expected, found, note).
    Правило заказчика по МЕСТУ расхождения:
      • совпало и в шапке, и в контактах (или источник отсутствует) → ✓;
      • не совпало в ШАПКЕ (в контактах ок) → ✗ «в шапке сайта…»;
      • не совпало в КОНТАКТАХ (в шапке ок) → ✗ «на сайте…» (стата = «сайт»);
      • не совпало И там, и там → ✗ «на сайте…».
    Значение КП (expected) показываем КАК ЕСТЬ: валидное - в читаемом виде, мусор
    («2») - как в ячейке (заказчик: любое значение в ячейке сравниваем и
    показываем; «–» ТОЛЬКО когда в ячейке совсем пусто). Мусор в КП с сайтом не
    совпадает → ✗."""
    kp = {x for x in kp_norms if x}
    hv = [x for x in header_norms if x]
    cv = [x for x in contacts_norms if x]
    raw = (kp_raw or '').strip()
    raw_meaningful = bool(raw) and raw not in ('–', '-')
    if not kp:
        site = list(dict.fromkeys(cv or hv))
        site_disp = ", ".join(fmt(x) for x in site) if site else "–"
        if raw_meaningful:
            # В КП мусор («2») - показываем его, с сайтом он не совпадает → ✗.
            return "bug", raw, site_disp, "на сайте не совпадает с КП"
        if site:
            return "bug", "–", site_disp, "на сайте есть, а в КП нет"
        return "na", "–", "–", "нет ни в КП, ни на сайте"
    exp = fmt(sorted(kp)[0])
    h_present, c_present = bool(hv), bool(cv)
    h_match, c_match = bool(kp & set(hv)), bool(kp & set(cv))
    if (not h_present or h_match) and (not c_present or c_match):
        both = (set(hv) | set(cv)) & kp
        return "ok", exp, fmt(sorted(both)[0] if both else sorted(kp)[0]), "совпадает с КП"
    h_disp = ", ".join(fmt(x) for x in dict.fromkeys(hv)) if hv else "–"
    c_disp = ", ".join(fmt(x) for x in dict.fromkeys(cv)) if cv else "–"
    if h_present and not h_match and (not c_present or c_match):
        return "bug", exp, h_disp, "в шапке сайта не совпадает с КП"
    if c_present and not c_match and (not h_present or h_match):
        return "bug", exp, c_disp, "на сайте не совпадает с КП"
    return "bug", exp, (c_disp if c_present else h_disp), "на сайте не совпадает с КП"


def check_variables_mpk(row: 'KPRow', header: dict, branch: dict) -> dict:
    """Сверка города МПК (один сайт на все города). Трёхточечно: КП ↔ ШАПКА
    (header = данные пикера выбора города: phone/email/whatsapp) ↔ КОНТАКТЫ
    (branch = блок филиала на /contacts: phones/emails/whatsapp/address).
    Возвращает {'fields': [...]} как check_variables, БЕЗ «Город» (его ставит
    variables_run). У МПК одна колонка «Телефон» (нет SEO/рекламных). Telegram и
    WhatsApp - один номер; Telegram сверяем с колонкой WhatsApp КП (в шапке t.me).
    Значения КП показываем как есть - мусор («2») не прячем за «–»."""
    header = header or {}
    branch = branch or {}
    dial = _dial_for(row)
    fmt = lambda n: _fmt(n, dial)
    fields = []

    def add(field, expected, found, status, note):
        fields.append({"field": field, "expected": expected or "–",
                       "found": found or "–", "status": status, "note": note})

    # ── Телефон ──
    kp_ph = list(dict.fromkeys(phones_in_cell(row.phone_common)))
    h_ph = [normalize_phone(header.get('phone'))]
    c_ph = [normalize_phone(p) for p in (branch.get('phones') or [])]
    st, exp, fnd, note = _mpk_field(kp_ph, row.phone_common, h_ph, c_ph, fmt)
    add("Телефон", exp, fnd, st, note)

    # ── Почта ──
    _em = (row.email or '').strip().lower()
    kp_em = [_em] if re.match(r'[^@\s]+@[^@\s]+\.[^@\s]+$', _em) else []
    h_em = [(header.get('email') or '').strip().lower()]
    c_em = [e.strip().lower() for e in (branch.get('emails') or [])]
    st, exp, fnd, note = _mpk_field(kp_em, row.email, h_em, c_em, lambda x: x)
    add("Почта", exp, fnd, st, note)

    # ── Адрес (только КОНТАКТЫ - в шапке адреса нет; значение КП показываем как есть) ──
    kp_addr = (row.address or '').strip()
    kp_addr_valid = bool(kp_addr) and bool(re.search(r'[а-яё]', _norm_addr(kp_addr)))
    c_addr = (branch.get('address') or '').strip()
    kp_addr_disp = kp_addr if (kp_addr and kp_addr not in ('–', '-')) else '–'
    if not kp_addr_valid:
        if c_addr and kp_addr_disp != '–':
            add("Адрес", kp_addr_disp, c_addr, "bug", "на сайте не совпадает с КП")
        elif c_addr:
            add("Адрес", "–", c_addr, "bug", "на сайте есть, а в КП нет")
        else:
            add("Адрес", kp_addr_disp, "–", "na", "нет ни в КП, ни на сайте")
    elif c_addr and address_match(c_addr, kp_addr):
        add("Адрес", kp_addr, c_addr, "ok", "совпадает с КП")
    elif c_addr:
        add("Адрес", kp_addr, c_addr, "bug", "адрес на сайте не совпадает с КП")
    else:
        add("Адрес", kp_addr, "–", "bug", "адрес на сайте не найден")

    # ── Telegram (у МПК = номер WhatsApp; в шапке t.me/+номер, в контактах нет) ──
    kp_msgr = normalize_phone(row.whatsapp)
    h_tg = [normalize_phone(header.get('whatsapp'))]      # phone2 = и WhatsApp, и Telegram
    st, exp, fnd, note = _mpk_field([kp_msgr] if kp_msgr else [], row.whatsapp, h_tg, [], fmt)
    add("Telegram", exp, fnd, st, note)

    # ── WhatsApp (шапка = phone2, контакты = WhatsApp блока филиала) ──
    h_wa = [normalize_phone(header.get('whatsapp'))]
    c_wa = [normalize_phone(branch.get('whatsapp'))]
    st, exp, fnd, note = _mpk_field([kp_msgr] if kp_msgr else [], row.whatsapp, h_wa, c_wa, fmt)
    add("WhatsApp", exp, fnd, st, note)

    return {"domain": _norm_host(row.domain), "city": row.city,
            "country": row.country, "matched": True, "fields": fields}


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
    # Меню/попап выбора города (баннер «Ваш город: … ? Всё верно / Выбрать город»):
    r'выбрать\s+(?:другой\s+)?город|ваш\s+город|вс[её]\s+верно|сменить\s+город|'
    r'телефон\w*|тел\.|e-?mail|почт\w*[:\s]|почта\b|whatsapp|телеграм|telegram|'
    r'i[şs]\s*saat\w*|əlaqə|elaqe|iş\s*vaxt\w*|'
    # (?<!\d): «7/8 + 3 цифры» - стоп-маркер телефона ПОСЛЕ адреса, но НЕ цифры
    # внутри почтового индекса («198096» → «8096» - это не телефон, адрес не режем).
    r'(?<!\d)\+?[78][\s(]?\d{3}|(?<!\d)\+?\d{11,}|[a-z0-9._%+-]+@).*$', re.I | re.S | re.U)


# Фразы меню/попапа, которые НЕ адрес (даже если рядом оказалась цифра): попап
# выбора города «… ? Всё верно Выбрать город …». Такой текст в «Сайт» не пускаем.
_ADDR_JUNK_RE = re.compile(
    r'выбрать\s+(?:другой\s+)?город|ваш\s+город|вс[её]\s+верно|сменить\s+город|\?',
    re.I | re.U)


def _обрезать_хвост_адреса(s: str) -> str:
    """Срезать с адреса хвост «Контакты: … Время работы: …» (телефон/почта/метки).

    Плюс убрать почтовый индекс (ровно 6 цифр СНГ: 720001/198096/212030/100000…):
    в КП его нет, для показа/сверки он не нужен, а его цифры путались с телефоном
    и резали адрес. Индекс, начинающийся на 7/8 («720001» → «7200»), маркер
    телефона съедал прямо с начала строки и адрес схлопывался в «19»/пусто -
    поэтому индекс убираем ЦЕЛИКОМ (как самостоятельный 6-значный токен), а не
    полагаемся на «7/8 не после цифры»."""
    s = re.sub(r'(?<!\d)\d{6}(?!\d)\s*,?\s*', '', s or '')
    return _ADDR_TAIL_RE.sub('', s).strip(' ,;·|-')


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
    if _ADDR_JUNK_RE.search(cap):        # попап выбора города и т.п. - не адрес
        return ''
    if not (_RE_ADDR_STREET.search(cap)
            or re.search(r'[' + _ADDR_LETTER + r'][' + _ADDR_LETTER + r'\-]{2,}'
                         r'\s*,\s*\d{1,4}\b', cap, re.U)):
        return ''
    return cap


def _site_address_raw(html: str) -> str:
    """Текст поля «Адрес:» со страницы КАК ЕСТЬ - даже если адрес НЕПОЛНЫЙ (без
    улицы и дома), напр. битый «, г. Гродно,» на сайтах МПЭ/Беларусь. В отличие от
    _site_address_full НЕ требует ни номера дома, ни маркера улицы - нужен, чтобы
    в «на сайте» показать, ЧТО реально стоит в поле адреса (а не прятать за «–»),
    и пометить такой адрес расхождением. Отсекаем хвост (телефон/почта/часы) и
    явный мусор (попап выбора города). ЗАПЯТЫЕ СОХРАНЯЕМ - показываем поле РОВНО как
    на сайте (битый «, г. Гомель,» так и выводим, а не «г. Гомель» - просьба
    заказчика). '' - если метки нет или в поле пусто/мусор."""
    try:
        from text_checker import html_to_visible_text
        txt = html_to_visible_text(html)
    except Exception:
        txt = html or ''
    m = re.search(r'(?:адрес|[uü]nvan)[:\s]+(.{2,120})', txt, re.IGNORECASE | re.U)
    if not m:
        return ''
    # Отрезаем почтовый индекс и хвост (телефон/почта/часы/след. поле), но НЕ
    # трогаем запятые - в отличие от _обрезать_хвост_адреса, который срезал бы
    # ведущую/конечную «,» и «, г. Гомель,» превращал в «г. Гомель».
    raw = re.sub(r'(?<!\d)\d{6}(?!\d)\s*,?\s*', '', m.group(1))
    cap = _ADDR_TAIL_RE.sub('', raw).strip()   # .strip() - только пробелы, не запятые
    if _ADDR_JUNK_RE.search(cap):            # попап выбора города и т.п. - не адрес
        return ''
    # Должна остаться осмысленная текстовая часть (слово из букв), а не одни знаки
    # препинания/цифры - иначе «, ,» или случайный «Адрес: 2» дал бы мусор.
    if not re.search(r'[' + _ADDR_LETTER + r']{3,}', cap, re.U):
        return ''
    return cap


def _addr_is_complete(s: str) -> bool:
    """Похоже ли на ПОЛНЫЙ адрес: есть маркер улицы И номер дома (цифра). «г. Гродно»
    - неполный; «улица Токтогула, 125» - полный."""
    return bool(s) and bool(re.search(r'\d', s)) and bool(_RE_ADDR_STREET.search(s))


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
    # ДОСТОВЕРНО отображаемые («кликабельные») номера сайта - из ссылок tel:.
    # Случайный/временный номер, попавший в ТЕКСТ шапки/подвала (виджет обратного
    # звонка, тех. строка, разовая ручная правка), в tel: обычно НЕ оформлен. Мы
    # ищем «новый номер, которого нет в КП» именно среди tel:, а не среди любого
    # текста - иначе выходил ФАНТОМНЫЙ ✗ по номеру, которого на странице уже нет
    # (жалоба заказчика: «в КП не совпадает SEO», а номер найти нельзя). Если
    # tel:-ссылок нет (часть сайтов номер ссылкой не оформляет) - откатываемся на
    # общий разбор текста, чтобы не потерять номер вовсе.
    _tel_ordered = []
    for _m in re.finditer(r'href=["\']tel:([^"\']+)["\']', html or "", re.I):
        _p = normalize_phone(_m.group(1))
        if _p and _p not in _tel_ordered:
            _tel_ordered.append(_p)
    _display_ordered = _tel_ordered or _site_ph_ordered
    site_ph_primary = fmt(_display_ordered[0]) if _display_ordered else "–"
    site_ph_any = site_ph_primary

    # Три АКТИВНЫХ номера города - ТЕКУЩИЕ Общий/Реклама/SEO из КП (первый номер
    # каждой ячейки, без «стар.»), БЕЗ старых из all_phones. Если на сайте вместо
    # Общего показан один из этих активных (напр. SEO-номер вместо обычного) - это
    # штатная подмена коллтрекинга, НЕ ошибка (просьба заказчика). А вот смена
    # Общего на УСТАРЕВШИЙ номер (что лежит только в all_phones) - по-прежнему ✗.
    _active_kp = set()
    for _v in (row.phone_common, row.phone_ad, row.phone_seo):
        _ph = phones_in_cell(_v)
        if _ph:
            _active_kp.add(_ph[0])

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
                if _kp_disp != "–":
                    # В ячейке КП стоит значение (мусор «2») - оно не совпадает с
                    # рекламным номером из кода → ✗ (значение КП показываем).
                    _cfg = ", ".join(fmt(n) for n in sorted(_pool)) or site_ph_primary
                    add(label, _kp_disp, _cfg, "bug",
                        "телефон на сайте не совпадает с КП")
                    continue
                # В КП рекламного номера НЕТ - проверять нечего: прочерк «–»
                # (не ошибка). Городам без своего рекл. номера (СНГ и т.п.) в коде
                # подставляется ОБЩИЙ/глобальный подменный - это не расхождение КП.
                add(label, "–", site_ph_primary, "na",
                    "рекламного номера в КП нет, на сайте общий")
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
                # Ячейка КП ПУСТАЯ (отдельного номера для слота нет) - для
                # ЭТОГО слота сверять нечего, всегда «–»/«–», ЧТО БЫ ни было
                # на сайте (известный номер города или вообще новый). Раньше
                # «новый» номер (не входящий в набор номеров КП) считался ✗ -
                # но пустое поле КП означает «здесь номера просто нет», а не
                # «здесь должен быть номер, и он не совпал» - подменять
                # отсутствие данных на расхождение неверно (просьба заказчика).
                add(label, "–", "–", "na", "отдельного номера в КП для этого слота нет")
        elif exp in site_phones:
            add(label, fmt(exp), fmt(exp), "ok", "совпадает с КП")
        elif label != "Тел. Общий Город" and (site_phones & kp_phones):
            # ТОЛЬКО для рекламного/поискового слота: их номер подменяет
            # коллтрекинг, и СТАТИЧЕСКИ на сайте виден ОБЩИЙ номер города. Если он
            # из набора КП города - засчитываем (✓), «живую» подмену проверит
            # браузер отдельно. Для «Общий Город» так НЕЛЬЗЯ: он виден на сайте
            # напрямую и должен совпадать ТОЧНО (иначе смена одной цифры в КП/на
            # сайте не ловилась - пряталась под «другой номер этого города»).
            add(label, fmt(exp), fmt(sorted(site_phones & kp_phones)[0]), "ok_set",
                "на сайте общий номер города (подменный проверит браузер)")
        elif label == "Тел. Общий Город" and (site_phones & _active_kp):
            # На сайте ВМЕСТО Общего показан ДРУГОЙ АКТИВНЫЙ номер города из КП
            # (SEO/рекламный - штатная подмена коллтрекинга). Это НЕ ошибка (просьба
            # заказчика): номер из КП, просто другого назначения. Старые номера из
            # all_phones сюда НЕ входят - смена Общего на устаревший остаётся ✗.
            _shown = sorted(site_phones & _active_kp)[0]
            _seo0 = (phones_in_cell(row.phone_seo) or [''])[0]
            _ad0 = (phones_in_cell(row.phone_ad) or [''])[0]
            _kind = "SEO" if _shown == _seo0 else ("рекламный" if _shown == _ad0
                                                   else "подменный")
            add(label, fmt(exp), fmt(_shown), "ok_set",
                f"на сайте показан {_kind} номер города из КП (не ошибка)")
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
    # Что реально в поле «Адрес:» на сайте: сначала ПОЛНЫЙ адрес (улица + дом),
    # иначе - НЕПОЛНЫЙ как есть (город без улицы, битый «, г. Гродно,»). Второе
    # обязательно показываем: если в поле адреса есть инфа - это не «пусто», а
    # расхождение (просьба заказчика: «в поле адрес есть инфа - это баг, не прячь»).
    _fb = _обрезать_хвост_адреса((site.get("address") or "").strip())   # из шапки/подвала
    _site_full = (_site_address_full(html) or _site_address_full(contacts_html or "")
                  or (_fb if _addr_is_complete(_fb) else ""))
    _site_raw = (_site_address_raw(html) or _site_address_raw(contacts_html or "")
                 or (_fb if re.search(r'[а-яёa-z]{3,}', _fb, re.I) else ""))
    _site_shown = _site_full or _site_raw

    if not _addr_kp_valid:
        _kp_addr_show = (row.address if row.address
                         and str(row.address).strip() not in ("–", "-") else "–")
        if _kp_addr_show != "–":
            # В КП стоит значение, но это не адрес («2»/мусор) - это ИНФА, и она
            # заведомо не совпадает → всегда ✗. В «на сайте» показываем, что есть
            # (полный адрес или хотя бы «г. Гродно»), а не «–».
            add("Адрес", _kp_addr_show, _site_shown or "–", "bug",
                "адрес на сайте не совпадает с КП")
        elif _site_full:
            # В КП пусто, на сайте ПОЛНЫЙ адрес → ✗.
            add("Адрес", "–", _site_full, "bug", "адрес на сайте не совпадает с КП")
        elif _site_raw:
            # В КП пусто, но на сайте в поле адреса ЕСТЬ инфа, и она НЕПОЛНАЯ
            # (город без улицы/дома, «, г. Гродно,») - это дефект сайта, а не
            # «нечего сверять»: показываем и помечаем ✗ (просьба заказчика).
            add("Адрес", "–", _site_raw, "bug",
                "адрес на сайте неполный - нет улицы или дома")
        else:
            add("Адрес", "–", "–", "na", "нет ни в КП, ни на сайте")
    elif address_match(haystack, row.address):
        # Сверяем по ВСЕМУ видимому тексту главной + «Контактов» (haystack): адрес
        # у части сайтов лежит не в поле «Адрес:», а в блоке контактов/подвале, и
        # поле «Адрес:» у некоторых проектов вообще отдаёт мусор (у ИМП там попап
        # выбора города - «Улан-Удэ Ульяновск …»). address_match требует, чтобы
        # улица И номер дома из КП стояли РЯДОМ, поэтому по названию города в
        # заголовке ложно не срабатывает. «На сайте» показываем чистый фрагмент.
        add("Адрес", row.address,
            _addr_on_page(haystack, row.address) or _found_addr()
            or "совпадает с КП", "ok", "совпадает с КП")
    else:
        # Адрес из КП не совпал. Показываем, ЧТО реально на сайте - полный адрес,
        # иначе хотя бы неполный («г. Гродно»), иначе «–». Прочерк не прячем, если
        # инфа в поле адреса есть.
        add("Адрес", row.address, _site_shown or "–", "bug",
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
        # На сайте читаемого WhatsApp-номера НЕТ.
        if _wa_kp_show != "–":
            # В КП значение есть, а на сайте вотсапа нет → ✗ «отсутствует».
            # Если есть кнопка-ссылка (номер скрыт за JS) - уточним её вживую
            # (variables_run сходит по check_url и допишет, что за ней).
            add("WhatsApp", _wa_kp_show, "–", "bug", "WhatsApp на сайте отсутствует")
            if wa_anchor:
                fields[-1]["check_url"] = wa_anchor[0]
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
    # Все проекты с КП (не хардкод-список - иначе новые проекты, напр. mpk/mpi,
    # выпадали бы из авто-подбора КП по домену, когда row не передан).
    for proj in KP_LAYOUT:
        if proj not in _KP_CACHE:
            # refresh=False: не тянем Google по каждому проекту при переборе -
            # нужный проект уже обновлён явным load_kp(project) в начале прогона.
            _KP_CACHE[proj] = load_kp(proj, refresh=False)
        kp = _KP_CACHE[proj]
        if any(brand == d.split('.')[-2] for d in kp if '.' in d):
            return kp
    return {}
