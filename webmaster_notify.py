"""
webmaster_notify.py — уведомления из Яндекс-почты и Gmail.

Источники:
    yandex_webmaster  — Яндекс-почта, папка «Вебмастер», с классификацией по приоритету
    ya_business       — Яндекс-почта, папка «Я.Бизнес», без классификации
    twogis            — Яндекс-почта, папка «2ГИС», без классификации
    gsc               — Gmail, от sc-noreply@google.com, с классификацией по приоритету
    google_accounts   — Gmail, от no-reply@accounts.google.com, без классификации (3 дня)

Приоритеты (4 уровня, только для yandex_webmaster и gsc):
    critical        — критические: сайт недоступен, долгий ответ сервера
    important       — важные: ошибки индексации, значительные проблемы
    recommendation  — рекомендации по улучшению
    info            — информационные уведомления

Кеш: cache/webmaster/{project_id}/{source}/{YYYY-MM-DD}-{uid_hash}.json
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import json
import re
import ssl
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Optional, Callable, List

# Reuse IMAP utilities already implemented in metrika_404
from metrika_404 import (
    _imap_utf7_encode,
    _imap_utf7_decode,
    IMAP4_SSL_via_Proxy,
    YANDEX_IMAP_HOST,
    YANDEX_IMAP_PORT,
)


PROJECT_ROOT = Path(__file__).parent
CACHE_DIR = PROJECT_ROOT / 'cache' / 'webmaster'

GMAIL_IMAP_HOST = 'imap.gmail.com'
GMAIL_IMAP_PORT = 993


# ── Конфиг почтовых ящиков ───────────────────────────────────────────

# Яндекс — те же credentials что у Метрики, папка «Вебмастер»
WEBMASTER_YANDEX_CONFIG = {
    'smu': {
        'folder': 'Вебмастер',
        'secret_email': 'metrika_smu_email',
        'secret_password': 'metrika_smu_password',
    },
    'imp': {
        'folder': 'Вебмастер',
        'secret_email': 'metrika_imp_email',
        'secret_password': 'metrika_imp_password',
    },
    'mpe': {
        'folder': 'Вебмастер',
        'secret_email': 'metrika_mpe_email',
        'secret_password': 'metrika_mpe_password',
    },
}

# Gmail — отдельные ящики для GSC и Google-уведомлений
GSC_GMAIL_CONFIG = {
    'smu': {
        'secret_email': 'gsc_smu_email',
        'secret_password': 'gsc_smu_password',
    },
    'imp': {
        'secret_email': 'gsc_imp_email',
        'secret_password': 'gsc_imp_password',
    },
    'mpe': {
        'secret_email': 'gsc_mpe_email',
        'secret_password': 'gsc_mpe_password',
    },
}

# Яндекс-почта — папка «Я.Бизнес» (те же credentials что у Метрики/Вебмастера)
YABUSINESS_YANDEX_CONFIG = {
    'smu': {'folder': 'Я.Бизнес', 'secret_email': 'metrika_smu_email', 'secret_password': 'metrika_smu_password'},
    'imp': {'folder': 'Я.Бизнес', 'secret_email': 'metrika_imp_email', 'secret_password': 'metrika_imp_password'},
    'mpe': {'folder': 'Я.Бизнес', 'secret_email': 'metrika_mpe_email', 'secret_password': 'metrika_mpe_password'},
}

# Яндекс-почта — папка «2ГИС»
TWOGIS_YANDEX_CONFIG = {
    'smu': {'folder': '2ГИС', 'secret_email': 'metrika_smu_email', 'secret_password': 'metrika_smu_password'},
    'imp': {'folder': '2ГИС', 'secret_email': 'metrika_imp_email', 'secret_password': 'metrika_imp_password'},
    'mpe': {'folder': '2ГИС', 'secret_email': 'metrika_mpe_email', 'secret_password': 'metrika_mpe_password'},
}

# Gmail — те же ящики что GSC, письма от no-reply@accounts.google.com
GOOGLE_ACCOUNTS_CONFIG = {
    'smu': {'secret_email': 'gsc_smu_email', 'secret_password': 'gsc_smu_password'},
    'imp': {'secret_email': 'gsc_imp_email', 'secret_password': 'gsc_imp_password'},
    'mpe': {'secret_email': 'gsc_mpe_email', 'secret_password': 'gsc_mpe_password'},
}

# Порядок приоритетов для сортировки (меньший индекс = выше)
PRIORITY_ORDER = ['critical', 'important', 'recommendation', 'info']

PRIORITY_LABELS = {
    'critical':       '🔴 Критическое',
    'important':      '🟠 Важное',
    'recommendation': '🟡 Рекомендация',
    'info':           '⚪ Информация',
}

CATEGORY_LABELS = {
    'server':    'Сервер',
    'indexing':  'Индексация',
    'speed':     'Скорость',
    'security':  'Безопасность',
    'structure': 'Структура',
    'coverage':  'Покрытие',
    'other':     'Прочее',
}


# ── Структура данных ─────────────────────────────────────────────────


@dataclass
class WebmasterNotification:
    msg_id: str          # уникальный ID письма
    project_id: str
    source: str          # 'yandex_webmaster' | 'gsc'
    date: str            # YYYY-MM-DD
    subject: str
    body_preview: str    # первые ~400 символов тела
    priority: str        # critical | important | recommendation | info
    category: str        # server | indexing | speed | security | structure | coverage | other
    # Доп. поля для отзывов 2ГИС (None для остальных источников):
    rating: Optional[int] = None        # оценка 1..5 (число звёзд)
    review_url: Optional[str] = None    # ссылка «Читать полностью» из письма

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'WebmasterNotification':
        # if k in d — старые кеш-файлы без новых полей не падают (берут default)
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ── Классификация приоритетов и категорий ───────────────────────────


# Правила для Яндекс.Вебмастера — проверяются по subject + начало body
_YW_PRIORITY = [
    ('critical', [
        'долгий ответ', 'сайт недоступен', 'недоступна', 'не отвечает',
        'ошибка сервера', '5xx', '503', '502', '504', '500',
        'вредоносн', 'вирус', 'заблокирован', 'критическ',
        'превышено время', 'timeout',
    ]),
    ('important', [
        'ошибки на сайте', 'проблемы с сайтом', 'страниц с ошибками',
        'нарушени', 'не индексирован', 'robots.txt', 'sitemap',
        '4xx', '404', 'дублей', 'дубли', 'недоступные страниц',
        'ошибки индексирован', 'исключённых страниц',
    ]),
    ('recommendation', [
        'рекоменд', 'улучш', 'оптимизац', 'совет', 'качество',
    ]),
]

# Правила для GSC
_GSC_PRIORITY = [
    ('critical', [
        'critical', 'manual action', 'security issue', 'hacked', 'deindexed',
        'penalty', 'malware', 'phishing',
    ]),
    ('important', [
        'error', 'not indexed', 'coverage', 'issue', 'problem',
        '404', 'server error', 'crawl error', 'redirect error',
    ]),
    ('recommendation', [
        'enhancement', 'improvement', 'mobile usability', 'speed',
        'core web vitals', 'cwv', 'page experience',
    ]),
]

# Категории (общие для обоих источников)
_CATEGORY_RULES = [
    ('server',    ['долгий ответ', 'сервер', '5xx', '503', '502', '504', '500',
                   'server error', 'timeout', 'недоступен', 'не отвечает']),
    ('indexing',  ['индексирован', 'robots.txt', 'sitemap', 'crawl',
                   'not indexed', 'исключён', 'excluded']),
    ('speed',     ['скорост', 'speed', 'core web vitals', 'cwv', 'долгий ответ',
                   'page experience', 'lcp', 'cls', 'fid']),
    ('security',  ['вредоносн', 'вирус', 'безопасност', 'security', 'hacked',
                   'manual action', 'malware', 'phishing']),
    ('structure', ['дублей', 'дубли', 'канонич', 'canonical', 'structured data',
                   'schema', 'микроразметк']),
    ('coverage',  ['покрытие', 'coverage', '404', 'not found', 'недоступные страниц']),
]


def _classify_priority(subject: str, body: str, rules: list) -> str:
    text = (subject + ' ' + body[:600]).lower()
    for priority, kws in rules:
        if any(kw in text for kw in kws):
            return priority
    return 'info'


def _classify_category(subject: str, body: str) -> str:
    text = (subject + ' ' + body[:600]).lower()
    for category, kws in _CATEGORY_RULES:
        if any(kw in text for kw in kws):
            return category
    return 'other'


# ── Утилиты email ────────────────────────────────────────────────────


def _decode_mime_header(value: str) -> str:
    if not value:
        return ''
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            result.append(str(part))
    return ''.join(result)


def _extract_text_body(msg) -> str:
    """Извлечь plain-text тело из email.message."""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == 'text/plain'
                    and 'attachment' not in part.get('Content-Disposition', '')):
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or 'utf-8'
                    return payload.decode(cs, errors='replace')
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            cs = msg.get_content_charset() or 'utf-8'
            return payload.decode(cs, errors='replace')
    return ''


def _extract_html_body(msg) -> str:
    """Извлечь HTML-тело из email.message (для ссылок/звёзд 2ГИС)."""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == 'text/html'
                    and 'attachment' not in part.get('Content-Disposition', '')):
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or 'utf-8'
                    return payload.decode(cs, errors='replace')
    elif msg.get_content_type() == 'text/html':
        payload = msg.get_payload(decode=True)
        if payload:
            cs = msg.get_content_charset() or 'utf-8'
            return payload.decode(cs, errors='replace')
    return ''


# Парсинг отзыва 2ГИС: оценка (число звёзд) + ссылка «Читать полностью».
# Письма 2ГИС: анкор с текстом «читать»/«полностью», оценка — звёзды/число.
_2GIS_READMORE_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(?:(?!</a>).)*?'
    r'(?:читать|полностью|подробн|смотреть\s+отзыв)',
    re.IGNORECASE | re.DOTALL,
)
_2GIS_RATING_PATTERNS = [
    re.compile(r'(\d)\s*из\s*5', re.IGNORECASE),
    re.compile(r'оцен\w*\D{0,15}?([1-5])\b', re.IGNORECASE),
    re.compile(r'\b([1-5])\s*звёзд', re.IGNORECASE),
    re.compile(r'\b([1-5])\s*звезд', re.IGNORECASE),
    re.compile(r'rating["\'>\s:]+([1-5])', re.IGNORECASE),
]


def _parse_2gis_review(html: str, text: str):
    """Вернуть (rating:int|None, review_url:str|None) из письма 2ГИС.

    ВНИМАНИЕ: эвристики проверены на типовом письме 2ГИС, но формат может
    меняться — при сбое возвращаем None (в отчёте будет «—»).
    """
    rating = None
    review_url = None
    h = html or ''
    t = text or ''

    # Ссылка «Читать полностью»
    m = _2GIS_READMORE_RE.search(h)
    if m:
        review_url = m.group(1).strip()

    # Оценка. Приоритет:
    # 1) divʼы 2ГИС с классом «Stars__star-<хэш>» — каждый = 1 закрашенная
    #    звезда (хэш-суффикс плавающий, матчим по базе);
    # 2) глифы ★ / ⭐;
    # 3) числовые шаблоны («N из 5», «N звёзд», «оценка N»).
    star_divs = len(re.findall(r'Stars__star-[A-Za-z0-9]+', h))
    blob = h + '\n' + t
    if 1 <= star_divs <= 5:
        rating = star_divs
    else:
        filled = blob.count('★') + blob.count('⭐')
        if 1 <= filled <= 5:
            rating = filled
        else:
            for pat in _2GIS_RATING_PATTERNS:
                mm = pat.search(blob)
                if mm:
                    rating = int(mm.group(1))
                    break
    return rating, review_url


def _msg_uid_hash(subject: str, date_str: str, frm: str) -> str:
    raw = f'{subject}|{date_str}|{frm}'
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]


# ── IMAP folder select (shared logic) ────────────────────────────────


def _select_folder(M: imaplib.IMAP4_SSL, folder: str, log: Callable = None) -> bool:
    """
    Выбрать папку по IMAP с обработкой кириллических имён.
    Возвращает True если папка открыта.
    """
    def _log(msg):
        if log:
            log('info', msg)

    target_encoded = None
    available = []  # человекочитаемые имена всех папок (для диагностики)
    try:
        status, folders = M.list()
        if status == 'OK' and folders:
            for f in folders:
                try:
                    line = f.decode('ascii', errors='replace') if isinstance(f, bytes) else f
                except Exception:
                    continue
                m = re.search(r'"([^"]+)"\s*$', line)
                if not m:
                    # some servers don't quote
                    m = re.search(r'\s(\S+)\s*$', line)
                if not m:
                    continue
                name_enc = m.group(1)
                try:
                    name_dec = _imap_utf7_decode(name_enc.encode('ascii'))
                except Exception:
                    name_dec = name_enc
                available.append(name_dec)
                # Сравнение без учёта регистра и пробелов по краям
                if name_dec.strip().lower() == folder.strip().lower():
                    target_encoded = name_enc
                    _log(f'Папка найдена: {name_dec} ({name_enc})')
                    break
    except Exception as e:
        _log(f'⚠ list() не удался: {e}')

    if target_encoded is None:
        # Показываем что реально доступно — частая причина «нет писем».
        if available:
            _log(f'Папка «{folder}» не найдена. Доступные папки: {available}')
        target_encoded = _imap_utf7_encode(folder).decode('ascii')
        _log(f'Пробую закодировать вручную: {target_encoded}')

    folder_bytes = target_encoded.encode('ascii', errors='replace')
    select_arg = b'"' + folder_bytes + b'"'
    try:
        status, _ = M.select(select_arg, readonly=True)
        return status == 'OK'
    except Exception as e:
        _log(f'⚠ select() упало: {e}')
        return False


# ── Кеш ─────────────────────────────────────────────────────────────


def _cache_dir(project_id: str, source: str) -> Path:
    p = CACHE_DIR / project_id / source
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_cached(project_id: str, source: str) -> dict[str, WebmasterNotification]:
    """Загрузить кешированные уведомления. Ключ — msg_id."""
    result = {}
    d = _cache_dir(project_id, source)
    for f in d.glob('*.json'):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            n = WebmasterNotification.from_dict(data)
            result[n.msg_id] = n
        except Exception:
            pass
    return result


def _save_notification(n: WebmasterNotification):
    d = _cache_dir(n.project_id, n.source)
    safe = re.sub(r'[^\w\-]', '_', n.msg_id)[:60]
    fname = f'{n.date}-{safe}.json'
    (d / fname).write_text(
        json.dumps(n.to_dict(), ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def load_notifications(
    project_id: str,
    source: str,
    lookback_days: int = 14,
) -> list[WebmasterNotification]:
    """Загрузить кешированные уведомления за последние N дней, отсортированные по приоритету."""
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    cached = _load_cached(project_id, source)
    result = [n for n in cached.values() if n.date >= cutoff]
    result.sort(key=lambda n: (
        PRIORITY_ORDER.index(n.priority),
        n.date,
    ))
    return result


# ── Fetch Yandex Webmaster ───────────────────────────────────────────


def fetch_webmaster_yandex(
    project_id: str,
    email_addr: str,
    password: str,
    folder: str = 'Вебмастер',
    lookback_days: int = 14,
    proxy_url: Optional[str] = None,
    log: Optional[Callable] = None,
) -> dict:
    """
    Скачать уведомления Яндекс.Вебмастера из IMAP.

    Возвращает:
        {
            'notifications': list[WebmasterNotification],  # все за lookback_days
            'fetched': int,    # новых в этот раз
            'skipped': int,    # уже в кеше
            'error': str|None,
        }
    """
    def _log(msg):
        if log:
            log('info', msg)

    existing = _load_cached(project_id, 'yandex_webmaster')
    fetched = 0
    skipped = 0
    error = None

    ssl_ctx = ssl.create_default_context()

    try:
        if proxy_url:
            M = IMAP4_SSL_via_Proxy(
                YANDEX_IMAP_HOST, YANDEX_IMAP_PORT,
                proxy_url=proxy_url,
                ssl_context=ssl_ctx,
                timeout=60,
            )
        else:
            M = imaplib.IMAP4_SSL(
                YANDEX_IMAP_HOST, YANDEX_IMAP_PORT,
                ssl_context=ssl_ctx,
                timeout=60,
            )

        try:
            M.login(email_addr, password)
        except imaplib.IMAP4.error as e:
            raise PermissionError(f'Ошибка входа: {e}')

        _log(f'Вошли в {email_addr}. Открываю папку «{folder}»…')

        if not _select_folder(M, folder, log=log):
            raise FileNotFoundError(f'Папка «{folder}» не найдена')

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%d-%b-%Y')
        status, nums_raw = M.search(None, f'SINCE {since_date}')
        if status != 'OK' or not nums_raw[0]:
            _log(f'Писем от Вебмастера за {lookback_days} дней нет')
            M.logout()
            return {
                'notifications': list(existing.values()),
                'fetched': 0, 'skipped': 0, 'error': None,
            }

        nums = nums_raw[0].split()
        _log(f'Найдено {len(nums)} писем в «{folder}»')

        for num in nums[-100:]:
            try:
                status, data = M.fetch(num, '(RFC822)')
                if status != 'OK' or not data or not data[0]:
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                # Уникальный ID: Message-ID из заголовка или хеш
                raw_id = msg.get('Message-ID', '')
                if raw_id:
                    msg_id = raw_id.strip('<>').strip()
                else:
                    subject_raw = msg.get('Subject', '')
                    date_raw = msg.get('Date', '')
                    frm_raw = msg.get('From', '')
                    msg_id = _msg_uid_hash(subject_raw, date_raw, frm_raw)

                if msg_id in existing:
                    skipped += 1
                    continue

                subject = _decode_mime_header(msg.get('Subject', '(без темы)'))
                date_raw = msg.get('Date', '')
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_raw)
                    date_iso = dt.strftime('%Y-%m-%d')
                except Exception:
                    date_iso = datetime.now().strftime('%Y-%m-%d')

                body = _extract_text_body(msg)
                priority = _classify_priority(subject, body, _YW_PRIORITY)
                category = _classify_category(subject, body)

                n = WebmasterNotification(
                    msg_id=msg_id,
                    project_id=project_id,
                    source='yandex_webmaster',
                    date=date_iso,
                    subject=subject,
                    body_preview=body[:400].strip(),
                    priority=priority,
                    category=category,
                )
                _save_notification(n)
                existing[msg_id] = n
                fetched += 1

            except Exception as e:
                _log(f'⚠ Ошибка при разборе письма: {e}')

        M.logout()

    except Exception as e:
        error = str(e)
        _log(f'❌ Ошибка Яндекс IMAP: {e}')

    all_n = list(existing.values())
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    all_n = [n for n in all_n if n.date >= cutoff]
    all_n.sort(key=lambda n: (PRIORITY_ORDER.index(n.priority), n.date))

    _log(f'Вебмастер: +{fetched} новых, {skipped} в кеше, итого {len(all_n)} за {lookback_days} дней')
    return {
        'notifications': all_n,
        'fetched': fetched,
        'skipped': skipped,
        'error': error,
    }


# ── Fetch GSC (Gmail) ────────────────────────────────────────────────


def fetch_gsc_gmail(
    project_id: str,
    email_addr: str,
    password: str,
    lookback_days: int = 14,
    log: Optional[Callable] = None,
) -> dict:
    """
    Скачать уведомления Google Search Console из Gmail INBOX.
    GSC шлёт письма от sc-noreply@google.com.

    Возвращает тот же формат что fetch_webmaster_yandex.
    """
    def _log(msg):
        if log:
            log('info', msg)

    existing = _load_cached(project_id, 'gsc')
    fetched = 0
    skipped = 0
    error = None

    ssl_ctx = ssl.create_default_context()

    try:
        M = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, ssl_context=ssl_ctx, timeout=60)

        try:
            # App Password Google показывает с пробелами ("abcd efgh ijkl mnop"),
            # а IMAP-логин требует без пробелов — убираем все пробелы.
            M.login(email_addr, ''.join((password or '').split()))
        except imaplib.IMAP4.error as e:
            raise PermissionError(
                f'Ошибка входа в Gmail: {e}. '
                f'Нужен ПАРОЛЬ ПРИЛОЖЕНИЯ (16 букв), не основной пароль Gmail. '
                f'Создать: Google Аккаунт → Безопасность → Двухэтапная аутентификация '
                f'→ Пароли приложений.'
            )

        _log(f'Gmail: вошли как {email_addr}. Ищу письма GSC…')

        # Открываем All Mail — это надмножество ВСЕХ писем Gmail (любые ярлыки и
        # вкладки Primary/Updates/Promotions). INBOX через IMAP может не содержать
        # письма из вкладки «Оповещения», поэтому All Mail надёжнее.
        _folder_ok = False
        for _folder in ('"[Gmail]/All Mail"', '"[Google Mail]/All Mail"', 'INBOX'):
            _s, _ = M.select(_folder, readonly=True)
            if _s == 'OK':
                _log(f'Открыта папка {_folder}')
                _folder_ok = True
                break
        if not _folder_ok:
            raise FileNotFoundError('Не удалось открыть ни одну папку Gmail')

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%d-%b-%Y')
        # Ищем письма от GSC по нескольким известным адресам-отправителям.
        nums = []
        for _from in ('sc-noreply@google.com', 'noreply-search-console@google.com'):
            _st, _raw = M.search(None, f'(FROM "{_from}" SINCE {since_date})')
            if _st == 'OK' and _raw and _raw[0]:
                nums += _raw[0].split()

        # Диагностика: если по FROM ничего, смотрим кто вообще писал за период,
        # чтобы в логе увидеть реальный адрес отправителя GSC.
        if not nums:
            _st, _raw = M.search(None, f'(SINCE {since_date})')
            sample = _raw[0].split()[-40:] if (_st == 'OK' and _raw and _raw[0]) else []
            senders = set()
            for _n in sample:
                try:
                    _sth, _dh = M.fetch(_n, '(BODY.PEEK[HEADER.FIELDS (FROM)])')
                    if _sth == 'OK' and _dh and _dh[0]:
                        senders.add(_decode_mime_header(
                            _dh[0][1].decode('utf-8', 'ignore')).strip())
                except Exception:
                    pass
            _log(f'Писем от GSC нет за {lookback_days} дн. '
                 f'Отправители в ящике (последние): {sorted(senders)[:15]}')
            M.logout()
            return {
                'notifications': list(existing.values()),
                'fetched': 0, 'skipped': 0, 'error': None,
            }

        # Убираем дубли (письмо могло попасть под оба адреса/поиска)
        nums = list(dict.fromkeys(nums))
        _log(f'Найдено {len(nums)} писем от GSC')

        for num in nums[-100:]:
            try:
                status, data = M.fetch(num, '(RFC822)')
                if status != 'OK' or not data or not data[0]:
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                raw_id = msg.get('Message-ID', '')
                if raw_id:
                    msg_id = raw_id.strip('<>').strip()
                else:
                    msg_id = _msg_uid_hash(
                        msg.get('Subject', ''),
                        msg.get('Date', ''),
                        msg.get('From', ''),
                    )

                if msg_id in existing:
                    skipped += 1
                    continue

                subject = _decode_mime_header(msg.get('Subject', '(без темы)'))
                date_raw = msg.get('Date', '')
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_raw)
                    date_iso = dt.strftime('%Y-%m-%d')
                except Exception:
                    date_iso = datetime.now().strftime('%Y-%m-%d')

                body = _extract_text_body(msg)
                priority = _classify_priority(subject, body, _GSC_PRIORITY)
                category = _classify_category(subject, body)

                n = WebmasterNotification(
                    msg_id=msg_id,
                    project_id=project_id,
                    source='gsc',
                    date=date_iso,
                    subject=subject,
                    body_preview=body[:400].strip(),
                    priority=priority,
                    category=category,
                )
                _save_notification(n)
                existing[msg_id] = n
                fetched += 1

            except Exception as e:
                _log(f'⚠ Ошибка при разборе письма GSC: {e}')

        M.logout()

    except Exception as e:
        error = str(e)
        _log(f'❌ Ошибка Gmail IMAP: {e}')

    all_n = list(existing.values())
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    all_n = [n for n in all_n if n.date >= cutoff]
    all_n.sort(key=lambda n: (PRIORITY_ORDER.index(n.priority), n.date))

    _log(f'GSC: +{fetched} новых, {skipped} в кеше, итого {len(all_n)} за {lookback_days} дней')
    return {
        'notifications': all_n,
        'fetched': fetched,
        'skipped': skipped,
        'error': error,
    }


# ── Fetch Yandex folder (generic, no priority classification) ────────


def fetch_yandex_folder_simple(
    project_id: str,
    email_addr: str,
    password: str,
    folder: str,
    source_key: str,
    lookback_days: int = 14,
    proxy_url: Optional[str] = None,
    log: Optional[Callable] = None,
) -> dict:
    """
    Скачать письма из произвольной папки Яндекс-почты без классификации по приоритету.
    Используется для Я.Бизнес, 2ГИС и других папок.
    priority='info', category='other' для всех писем.
    """
    def _log(msg):
        if log:
            log('info', msg)

    existing = _load_cached(project_id, source_key)
    fetched = 0
    skipped = 0
    error = None

    ssl_ctx = ssl.create_default_context()

    try:
        if proxy_url:
            M = IMAP4_SSL_via_Proxy(
                YANDEX_IMAP_HOST, YANDEX_IMAP_PORT,
                proxy_url=proxy_url,
                ssl_context=ssl_ctx,
                timeout=60,
            )
        else:
            M = imaplib.IMAP4_SSL(
                YANDEX_IMAP_HOST, YANDEX_IMAP_PORT,
                ssl_context=ssl_ctx,
                timeout=60,
            )

        try:
            M.login(email_addr, password)
        except imaplib.IMAP4.error as e:
            raise PermissionError(f'Ошибка входа: {e}')

        _log(f'Вошли в {email_addr}. Открываю папку «{folder}»…')

        if not _select_folder(M, folder, log=log):
            raise FileNotFoundError(f'Папка «{folder}» не найдена')

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%d-%b-%Y')
        status, nums_raw = M.search(None, f'SINCE {since_date}')
        if status != 'OK' or not nums_raw[0]:
            _log(f'Писем в «{folder}» за {lookback_days} дней нет')
            M.logout()
            return {'notifications': list(existing.values()), 'fetched': 0, 'skipped': 0, 'error': None}

        nums = nums_raw[0].split()
        _log(f'Найдено {len(nums)} писем в «{folder}»')

        for num in nums[-100:]:
            try:
                status, data = M.fetch(num, '(RFC822)')
                if status != 'OK' or not data or not data[0]:
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                raw_id = msg.get('Message-ID', '')
                if raw_id:
                    msg_id = raw_id.strip('<>').strip()
                else:
                    msg_id = _msg_uid_hash(
                        msg.get('Subject', ''), msg.get('Date', ''), msg.get('From', ''),
                    )

                if msg_id in existing:
                    skipped += 1
                    continue

                subject = _decode_mime_header(msg.get('Subject', '(без темы)'))
                date_raw = msg.get('Date', '')
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_raw)
                    date_iso = dt.strftime('%Y-%m-%d')
                except Exception:
                    date_iso = datetime.now().strftime('%Y-%m-%d')

                body = _extract_text_body(msg)

                _rating = None
                _review_url = None
                if source_key == 'twogis':
                    _html = _extract_html_body(msg)
                    _rating, _review_url = _parse_2gis_review(_html, body)

                n = WebmasterNotification(
                    msg_id=msg_id,
                    project_id=project_id,
                    source=source_key,
                    date=date_iso,
                    subject=subject,
                    body_preview=body[:400].strip(),
                    priority='info',
                    category='other',
                    rating=_rating,
                    review_url=_review_url,
                )
                _save_notification(n)
                existing[msg_id] = n
                fetched += 1

            except Exception as e:
                _log(f'⚠ Ошибка при разборе письма: {e}')

        M.logout()

    except Exception as e:
        error = str(e)
        _log(f'❌ Ошибка Яндекс IMAP ({folder}): {e}')

    all_n = list(existing.values())
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    all_n = [n for n in all_n if n.date >= cutoff]
    all_n.sort(key=lambda n: n.date, reverse=True)

    _log(f'{source_key}: +{fetched} новых, {skipped} в кеше, итого {len(all_n)} за {lookback_days} дней')
    return {'notifications': all_n, 'fetched': fetched, 'skipped': skipped, 'error': error}


# ── Fetch Google Accounts (Gmail, no priority classification) ────────


def fetch_google_accounts(
    project_id: str,
    email_addr: str,
    password: str,
    lookback_days: int = 3,
    log: Optional[Callable] = None,
) -> dict:
    """
    Скачать письма от no-reply@accounts.google.com из Gmail.
    Без классификации по приоритету (priority='info', category='other').
    """
    def _log(msg):
        if log:
            log('info', msg)

    existing = _load_cached(project_id, 'google_accounts')
    fetched = 0
    skipped = 0
    error = None

    ssl_ctx = ssl.create_default_context()

    try:
        M = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, ssl_context=ssl_ctx, timeout=60)

        try:
            # App Password Google показывает с пробелами ("abcd efgh ijkl mnop"),
            # а IMAP-логин требует без пробелов — убираем все пробелы.
            M.login(email_addr, ''.join((password or '').split()))
        except imaplib.IMAP4.error as e:
            raise PermissionError(
                f'Ошибка входа в Gmail: {e}. '
                f'Нужен ПАРОЛЬ ПРИЛОЖЕНИЯ (16 букв), не основной пароль Gmail. '
                f'Создать: Google Аккаунт → Безопасность → Двухэтапная аутентификация '
                f'→ Пароли приложений.'
            )

        _log(f'Gmail: вошли как {email_addr}. Ищу письма от Google…')

        _folder_ok = False
        for _folder in ('INBOX', '"[Gmail]/All Mail"', '"[Google Mail]/All Mail"'):
            _s, _ = M.select(_folder, readonly=True)
            if _s == 'OK':
                _folder_ok = True
                break
        if not _folder_ok:
            raise FileNotFoundError('Не удалось открыть ни одну папку Gmail')

        since_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%d-%b-%Y')
        status, nums_raw = M.search(
            None, 'FROM', '"no-reply@accounts.google.com"', 'SINCE', since_date,
        )
        if status != 'OK' or not nums_raw[0]:
            _log('Писем от Google за последние дни нет')
            M.logout()
            return {'notifications': list(existing.values()), 'fetched': 0, 'skipped': 0, 'error': None}

        nums = nums_raw[0].split()
        _log(f'Найдено {len(nums)} писем от Google')

        for num in nums[-50:]:
            try:
                status, data = M.fetch(num, '(RFC822)')
                if status != 'OK' or not data or not data[0]:
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                raw_id = msg.get('Message-ID', '')
                if raw_id:
                    msg_id = raw_id.strip('<>').strip()
                else:
                    msg_id = _msg_uid_hash(
                        msg.get('Subject', ''), msg.get('Date', ''), msg.get('From', ''),
                    )

                if msg_id in existing:
                    skipped += 1
                    continue

                subject = _decode_mime_header(msg.get('Subject', '(без темы)'))
                date_raw = msg.get('Date', '')
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_raw)
                    date_iso = dt.strftime('%Y-%m-%d')
                except Exception:
                    date_iso = datetime.now().strftime('%Y-%m-%d')

                body = _extract_text_body(msg)

                n = WebmasterNotification(
                    msg_id=msg_id,
                    project_id=project_id,
                    source='google_accounts',
                    date=date_iso,
                    subject=subject,
                    body_preview=body[:400].strip(),
                    priority='info',
                    category='other',
                )
                _save_notification(n)
                existing[msg_id] = n
                fetched += 1

            except Exception as e:
                _log(f'⚠ Ошибка при разборе письма Google: {e}')

        M.logout()

    except Exception as e:
        error = str(e)
        _log(f'❌ Ошибка Gmail IMAP (Google Accounts): {e}')

    all_n = list(existing.values())
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    all_n = [n for n in all_n if n.date >= cutoff]
    all_n.sort(key=lambda n: n.date, reverse=True)

    _log(f'Google Accounts: +{fetched} новых, {skipped} в кеше, итого {len(all_n)} за {lookback_days} дней')
    return {'notifications': all_n, 'fetched': fetched, 'skipped': skipped, 'error': error}


# ── Группировка для UI ───────────────────────────────────────────────


def group_by_priority(
    notifications: list[WebmasterNotification],
) -> dict[str, list[WebmasterNotification]]:
    """Разбить список на 4 группы по приоритету (сортировка внутри — по дате DESC)."""
    groups: dict[str, list] = {p: [] for p in PRIORITY_ORDER}
    for n in notifications:
        groups.setdefault(n.priority, []).append(n)
    for p in groups:
        groups[p].sort(key=lambda n: n.date, reverse=True)
    return groups
