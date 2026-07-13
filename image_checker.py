"""
image_checker.py - проверка изображений (пункт 1.15).

Всё про картинки одним пунктом, отдельным листом «Изображения»:
  • Alt: у каждого <img> есть атрибут alt (пустой alt="" легален -
    декоративные картинки; баг только ПОЛНОЕ отсутствие атрибута).
  • Современные форматы: используются webp/avif (а не только jpg/png/gif).
    Легаси-картинки есть, а webp/avif нет = предупреждение.
  • Оптимизация (вес): чек-лист требует ≤100 КБ. Два порога: тяжелее
    100 КБ - замечание (счёт), тяжелее 300 КБ - «тяжёлые» с именами файлов.
    Размер по Content-Length (HEAD, качает http_checker).
  • Lazy loading: у изображений/видео есть ленивая загрузка (loading="lazy"
    / data-src / preload="none"). Много картинок и ни одной lazy = предупр.
  • Имена файлов: чек-лист требует транслит из alt-текста. На Bitrix
    картинки почти всегда /upload/iblock/<хеш>/ - хеш-имена ловим ОДНИМ
    предупреждением на страницу (не перечисляем каждую); для читаемых имён
    сверяем с транслитом alt (частичное совпадение = ок).

Alt и форматы - статикой по HTML; вес - по image_infos [{url, bytes}]
(HEAD своих картинок, собирает http_checker; None - вес не проверяем).
"""
import re
from urllib.parse import urlsplit

_RE_IMG_TAG = re.compile(r'<img\b[^>]*>', re.I)
_RE_HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
_RE_ALT_ATTR = re.compile(r'(?<![\w-])alt\s*(?==|\s|/|>)', re.I)
_RE_IMG_SRC = re.compile(
    r'(?:data-src|src)\s*=\s*(?:["\']([^"\']+)["\']|([^\s>"\']+))', re.I)
_RE_ALT_VAL = re.compile(r'\balt\s*=\s*["\']([^"\']*)["\']', re.I)
_RE_SOURCE = re.compile(r'<source\b[^>]*>', re.I)
_RE_TYPE = re.compile(r'type\s*=\s*["\']image/(webp|avif)["\']', re.I)
_RE_SRCSET = re.compile(r'srcset\s*=\s*["\']([^"\']+)["\']', re.I)

_RE_MEDIA = re.compile(r'<(?:video|iframe)\b[^>]*>', re.I)
_RE_LAZY = re.compile(
    r'loading\s*=\s*["\']lazy|data-src|data-lazy|class\s*=\s*["\'][^"\']*lazy',
    re.I)
_RE_LAZY_MEDIA = re.compile(
    r'loading\s*=\s*["\']lazy|preload\s*=\s*["\']none|data-src', re.I)

_LEGACY_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
_MODERN_EXT = ('.webp', '.avif')
HEAVY_BYTES = 300 * 1024        # тяжёлая картинка (не оптимизирована)
MID_BYTES = 100 * 1024          # порог чек-листа: вес изображения ≤100 КБ
LAZY_MIN_IMGS = 4               # с этого числа картинок ждём lazy loading

# Хеш-имя файла (генерят CMS): длинная hex-строка либо путь Bitrix
# /upload/iblock/ / resize_cache - имена там всегда хеши.
_RE_HEX_NAME = re.compile(r'^[0-9a-f]{8,}$', re.I)
_RE_CMS_HASH_PATH = re.compile(r'/(?:iblock|resize_cache)/', re.I)
# Служебные картинки (логотип, заглушка «нет фото», иконки) - их имя не
# обязано совпадать с alt: alt там про компанию/товар, имя - служебное.
_RE_SKIP_NAME = re.compile(
    r'^(?:logo|no-?image|no-?photo|placeholder|zaglushka|icon|favicon|'
    r'sprite|default)', re.I)

# Транслитерация ru→lat для сверки alt с именем файла.
_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'j', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def _translit(s: str) -> str:
    return ''.join(_TRANSLIT.get(ch, ch) for ch in (s or '').lower())


def _name_tokens(name: str) -> set:
    """Токены имени файла (без расширения и цифровых суффиксов)."""
    base = name.rsplit('.', 1)[0].lower()
    return {t for t in re.split(r'[-_.]+', base) if len(t) >= 4}


def _ext(url: str) -> str:
    path = urlsplit((url or '').split('?')[0]).path.lower()
    for e in _LEGACY_EXT + _MODERN_EXT:
        if path.endswith(e):
            return e
    return ''


def _short(src: str) -> str:
    src = (src or '').strip()
    if src.startswith('data:'):
        return '[inline-картинка]'
    try:
        sp = urlsplit(src)
        return sp.path or src if sp.netloc else src
    except Exception:
        return src


def imgs_no_alt(html: str) -> list:
    """src всех <img> БЕЗ атрибута alt (пустой alt="" - ок)."""
    out = []
    for tag in _RE_IMG_TAG.findall(_RE_HTML_COMMENT.sub(' ', html or '')):
        if _RE_ALT_ATTR.search(tag[4:]):
            continue
        m = _RE_IMG_SRC.search(tag)
        src = ((m.group(1) or m.group(2) or '').strip() if m else '') or '[без src]'
        out.append(_short(src))
    return out


def check_images(html, base_url: str = '', image_infos=None) -> dict:
    """Проверка изображений одной страницы. Возвращает dict для
    CheckResult.images (или None, если html пуст)."""
    html = html or ''
    issues, warnings = [], []

    # ── Alt ──
    # Текст без числа - чтобы страницы группировались в ОДИН блок отчёта
    # (сколько картинок без alt - в детализации по строке).
    no_alt = imgs_no_alt(html)
    if no_alt:
        issues.append('есть картинки без атрибута alt')

    # ── Современные форматы (webp/avif) ──
    clean = _RE_HTML_COMMENT.sub(' ', html)
    legacy, modern = [], 0
    for tag in _RE_IMG_TAG.findall(clean):
        m = _RE_IMG_SRC.search(tag)
        src = (m.group(1) or m.group(2) or '') if m else ''
        e = _ext(src)
        if e in _LEGACY_EXT:
            legacy.append(_short(src))
        elif e in _MODERN_EXT:
            modern += 1
    # <source type="image/webp/avif"> и .webp/.avif в srcset
    if any(_RE_TYPE.search(s) for s in _RE_SOURCE.findall(clean)):
        modern += 1
    for ss in _RE_SRCSET.findall(clean):
        if '.webp' in ss.lower() or '.avif' in ss.lower():
            modern += 1
    # Тексты предупреждений - БЕЗ чисел: лист отчёта группирует страницы по
    # точному тексту, число у каждой страницы своё - раздробило бы группы на
    # «по 1 странице». Числа страницы видны в колонке-контексте листа.
    if legacy and modern == 0:
        warnings.append('современные форматы (webp/avif) не используются - '
                        'картинки в устаревших jpg/png/gif')

    # ── Lazy loading (изображения и видео) ──
    img_tags = _RE_IMG_TAG.findall(clean)
    lazy_imgs = sum(1 for t in img_tags if _RE_LAZY.search(t))
    media_tags = _RE_MEDIA.findall(clean)
    lazy_media = sum(1 for m in media_tags if _RE_LAZY_MEDIA.search(m))
    if len(img_tags) >= LAZY_MIN_IMGS and lazy_imgs == 0:
        warnings.append('ленивая загрузка (lazy loading) не используется - '
                        'картинки грузятся сразу '
                        '(нет loading="lazy"/data-src)')
    if media_tags and lazy_media == 0:
        warnings.append('видео/iframe без ленивой загрузки '
                        '(нет loading="lazy"/preload="none")')

    # ── Оптимизация (вес): чек-лист требует ≤100 КБ, два порога ──
    heavy, mid = [], []
    if image_infos:
        heavy = [i for i in image_infos
                 if isinstance(i.get('bytes'), int) and i['bytes'] > HEAVY_BYTES]
        mid = [i for i in image_infos
               if isinstance(i.get('bytes'), int)
               and MID_BYTES < i['bytes'] <= HEAVY_BYTES]
        if heavy:
            warnings.append(f'тяжёлые изображения (не оптимизированы) - '
                            f'больше {HEAVY_BYTES // 1024} КБ')
        if mid:
            warnings.append(f'изображения тяжелее {MID_BYTES // 1024} КБ '
                            f'(чек-лист: вес ≤{MID_BYTES // 1024} КБ) - '
                            f'дожать сжатием')

    # ── Имена файлов: транслит из alt (чек-лист) ──
    # Bitrix хранит картинки в /upload/iblock/<хеш>/ - имена всегда хеши:
    # это ловим одним предупреждением, не перечисляя каждую картинку.
    hashed, readable, mismatch, mismatch_n = 0, 0, [], 0
    for tag in img_tags:
        m = _RE_IMG_SRC.search(tag)
        src = ((m.group(1) or m.group(2) or '').strip() if m else '')
        if not src or src.startswith('data:'):
            continue
        path = urlsplit(src.split('?')[0]).path
        name = path.rsplit('/', 1)[-1]
        if not name or '.' not in name:
            continue
        if (_RE_CMS_HASH_PATH.search(path)
                or _RE_HEX_NAME.match(name.rsplit('.', 1)[0])):
            hashed += 1
            continue
        if _RE_SKIP_NAME.match(name) or name.lower().endswith('.svg'):
            continue    # логотип/заглушка/svg-иконка - имя служебное, ок
        readable += 1
        am = _RE_ALT_VAL.search(tag)
        alt = (am.group(1) or '').strip() if am else ''
        if alt:
            alt_tokens = {t for t in re.split(r'[^a-z0-9]+', _translit(alt))
                          if len(t) >= 4}
            if (alt_tokens and not (_name_tokens(name) & alt_tokens)
                    and name not in mismatch):
                mismatch_n += 1
                if len(mismatch) < 10:
                    mismatch.append(name)
    if hashed >= 3 and hashed > readable:
        warnings.append('имена файлов картинок - хеши CMS (/upload/iblock/…) '
                        '- транслит имени из alt-текста не настроен')
    if mismatch_n:
        warnings.append('имена файлов картинок не совпадают с alt-текстом '
                        '(транслитерация)')

    return {
        'no_alt': no_alt[:50],
        'legacy': legacy[:50],
        'modern_count': modern,
        'heavy': [{'url': i.get('url', ''), 'kb': (i.get('bytes') or 0) // 1024}
                  for i in heavy][:50],
        'mid_heavy': len(mid),
        'names': {'hashed': hashed, 'readable': readable,
                  'mismatch': mismatch, 'mismatch_n': mismatch_n},
        'img_total': len(img_tags),
        'lazy_imgs': lazy_imgs,
        'media_total': len(media_tags),
        'lazy_media': lazy_media,
        'issues': issues,
        'warnings': warnings,
    }
