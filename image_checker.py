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

_LEGACY_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
_MODERN_EXT = ('.webp', '.avif')
HEAVY_BYTES = 300 * 1024        # тяжёлая картинка (не оптимизирована)


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
    if legacy and modern == 0:
        warnings.append(f'современные форматы (webp/avif) не используются - '
                        f'{len(legacy)} картинок в устаревших jpg/png/gif')

    # ── Оптимизация (вес) ──
    heavy = []
    if image_infos:
        heavy = [i for i in image_infos
                 if isinstance(i.get('bytes'), int) and i['bytes'] > HEAVY_BYTES]
        if heavy:
            warnings.append(f'тяжёлые изображения (не оптимизированы): '
                            f'{len(heavy)} шт. больше '
                            f'{HEAVY_BYTES // 1024} КБ')

    return {
        'no_alt': no_alt[:50],
        'legacy': legacy[:50],
        'modern_count': modern,
        'heavy': [{'url': i.get('url', ''), 'kb': (i.get('bytes') or 0) // 1024}
                  for i in heavy][:50],
        'img_total': len(_RE_IMG_TAG.findall(clean)),
        'issues': issues,
        'warnings': warnings,
    }
