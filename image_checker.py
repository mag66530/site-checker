"""
image_checker.py - проверка изображений (пункт 1.15).

Всё про картинки одним пунктом, отдельным листом «Изображения»:
  • Alt: у каждого <img> есть атрибут alt (пустой alt="" легален -
    декоративные картинки; баг только ПОЛНОЕ отсутствие атрибута).
  • Современные форматы: используются webp/avif (а не только jpg/png/gif).
    Легаси-картинки есть, а webp/avif нет = предупреждение.
  • Оптимизация (вес): свои картинки не должны быть тяжёлыми. Размер берём
    по Content-Length (HEAD, качает http_checker) - тяжелее порога = не
    оптимизировано (предупреждение).
  • Lazy loading: у изображений/видео есть ленивая загрузка (loading="lazy"
    / data-src / preload="none"). Много картинок и ни одной lazy = предупр.

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
LAZY_MIN_IMGS = 4               # с этого числа картинок ждём lazy loading


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

    # ── Оптимизация (вес) ──
    heavy = []
    if image_infos:
        heavy = [i for i in image_infos
                 if isinstance(i.get('bytes'), int) and i['bytes'] > HEAVY_BYTES]
        if heavy:
            warnings.append(f'тяжёлые изображения (не оптимизированы) - '
                            f'больше {HEAVY_BYTES // 1024} КБ')

    return {
        'no_alt': no_alt[:50],
        'legacy': legacy[:50],
        'modern_count': modern,
        'heavy': [{'url': i.get('url', ''), 'kb': (i.get('bytes') or 0) // 1024}
                  for i in heavy][:50],
        'img_total': len(img_tags),
        'lazy_imgs': lazy_imgs,
        'media_total': len(media_tags),
        'lazy_media': lazy_media,
        'issues': issues,
        'warnings': warnings,
    }
