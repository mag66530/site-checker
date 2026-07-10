"""
layout_checker.py - вёрстка и адаптивность (пункт 1.11 чек-листа, ТЗ 2.1/2.1.1).

Реальный рендер браузером в прогон не встраиваем (тяжело) - проверяем
честные сигналы по уже скачанному HTML и CSS:

  • ТЗ 2.1.1 - задан тег <meta name="viewport"> (без него мобильная версия
    не масштабируется). Баг = тега нет вовсе; содержимое не придираем.
  • ТЗ 2.1 «стили выводятся» - каждый подключённый <link rel=stylesheet>
    своего хоста реально грузится: явный 4xx/5xx = битый стиль = баг
    (страница открывается без вёрстки). Сетевые сбои не считаем - флаки.
  • Адаптивность - в стилях (внешних CSS или inline <style>) есть
    @media-запросы по ширине (max-width/min-width). Нет ни одного -
    предупреждение «адаптивность не обнаружена» (косвенный сигнал).

CSS не качаем повторно: http_checker уже тянет стили страницы для проверки
видимости цены/кнопок - оттуда же берём статус и признак @media (кэш на батч).
"""
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

_RE_VIEWPORT = re.compile(
    r'<meta\b[^>]*name\s*=\s*["\']viewport["\'][^>]*>', re.I)
_RE_STYLE_BLOCK = re.compile(r'<style\b[^>]*>(.*?)</style>', re.I | re.S)
_RE_MEDIA_WIDTH = re.compile(r'@media[^{]*\b(?:max|min)-width', re.I)
_RE_SCRIPT_SRC = re.compile(
    r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)

# Пороги «объединения»: больше стольких СВОИХ файлов - вероятно не объединены.
_CSS_COMBINE_LIMIT = 4
_JS_COMBINE_LIMIT = 6


def _norm_host(h: str) -> str:
    return (h or '').lower().removeprefix('www.')


def check_layout(html: Optional[str], css_infos: Optional[list],
                 base_url: str = '') -> dict:
    """Проверка вёрстки одной страницы.

    css_infos - список {'url', 'status', 'has_media', 'minified'} по
    подключённым CSS (из кэша http_checker). base_url - адрес страницы (для
    определения СВОИХ CSS/JS). Возвращает dict для CheckResult.layout."""
    html = html or ''
    css_infos = css_infos or []
    issues, warnings = [], []

    # 1. viewport (ТЗ 2.1.1)
    viewport = bool(_RE_VIEWPORT.search(html[:300_000]))
    if not viewport:
        issues.append('нет тега viewport - мобильная версия не масштабируется')

    # 2. Битые стили: явный 4xx/5xx по подключённому CSS (ТЗ 2.1)
    broken = [c for c in css_infos
              if isinstance(c.get('status'), int) and c['status'] >= 400]
    if broken:
        issues.append('не грузится часть CSS-стилей (битые ссылки на файлы '
                      'стилей) - страница может выводиться без вёрстки')

    # 3. Адаптивность: @media по ширине во внешних CSS или inline <style>
    has_media = any(c.get('has_media') for c in css_infos)
    if not has_media:
        for block in _RE_STYLE_BLOCK.findall(html):
            if _RE_MEDIA_WIDTH.search(block):
                has_media = True
                break
    if not has_media:
        warnings.append('в стилях не найдено @media-запросов по ширине - '
                        'адаптивность под мобильные не обнаружена')

    # 4. Минификация и объединение CSS/JS (доп. чек-лист, «если применимо»).
    # СВОИ файлы (тот же хост): чужие CDN/аналитику не трогаем.
    own_host = _norm_host(urlsplit(base_url).netloc) if base_url else ''
    css_own = [c for c in css_infos
               if own_host and _norm_host(urlsplit(c.get('url', '')).netloc)
               == own_host]
    js_own = []
    if own_host:
        seen = set()
        for src in _RE_SCRIPT_SRC.findall(html):
            u = urljoin(base_url, src.strip())
            if _norm_host(urlsplit(u).netloc) == own_host and u not in seen:
                seen.add(u)
                js_own.append(u)

    # Минификация: CSS - по содержимому (флаг minified из http_checker),
    # JS - по имени файла (.min), контент JS не качаем (тяжело).
    css_not_min = [c for c in css_own if c.get('minified') is False]
    js_not_min = [u for u in js_own if '.min.' not in u.lower()
                  and not u.lower().endswith('.min.js')]

    assets = {
        'css_own': len(css_own), 'js_own': len(js_own),
        'css_not_min': len(css_not_min), 'js_not_min': len(js_not_min),
    }
    if own_host:
        # Объединение: много отдельных своих файлов.
        if len(css_own) > _CSS_COMBINE_LIMIT or len(js_own) > _JS_COMBINE_LIMIT:
            warnings.append(
                f'CSS/JS похоже не объединены (своих файлов: CSS {len(css_own)}, '
                f'JS {len(js_own)}) - если применимо, объедините для скорости')
        # Минификация CSS (по содержимому - надёжно).
        if css_not_min:
            warnings.append(
                f'не минифицированы CSS-файлы: {len(css_not_min)} из '
                f'{len(css_own)} (лишние пробелы/переносы)')
        # Минификация JS (по имени файла - грубо, если совсем нет .min).
        if len(js_own) >= 3 and len(js_not_min) == len(js_own):
            warnings.append(
                f'JS-файлы без признака минификации (.min в имени): все '
                f'{len(js_own)} - проверить, минифицированы ли')

    return {
        'viewport': viewport,
        'css_total': len(css_infos),
        'css_broken': [{'url': c.get('url', ''), 'status': c.get('status')}
                       for c in broken],
        'has_media': has_media,
        'assets': assets,
        'issues': issues,
        'warnings': warnings,
    }


# ── Меню шапки (ТЗ 2.2/2.3): переходы по тех. страницам и каталогу ───
# Меню сквозное - прозваниваем ссылки один раз на поддомен (с его главной).

_RE_MENU_ZONE = re.compile(r'<(header|nav)\b[^>]*>.*?</\1>', re.I | re.S)
_RE_A_HREF = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)

MENU_LINKS_LIMIT = 40    # ссылок меню на поддомен (тех. страницы + каталог)


def extract_menu_links(html: str, base_url: str, limit: int = MENU_LINKS_LIMIT) -> list:
    """Внутренние ссылки из шапки (<header>/<nav>): меню тех. страниц и меню
    каталога. Только свой хост, без якорей/tel:/mailto:, без дублей.
    Закомментированная вёрстка (<!-- … -->) вырезается - выключенные из меню
    ссылки не проверяем: пользователь по ним перейти не может."""
    from urllib.parse import urljoin, urlsplit
    host = (urlsplit(base_url).netloc or '').lower().removeprefix('www.')
    html = _RE_HTML_COMMENT.sub(' ', html or '')
    out, seen = [], set()
    for zone in _RE_MENU_ZONE.finditer(html):
        for href in _RE_A_HREF.findall(zone.group(0)):
            href = href.strip()
            if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                continue
            absu = urljoin(base_url, href).split('#')[0]
            sp = urlsplit(absu)
            if sp.scheme not in ('http', 'https'):
                continue
            if (sp.netloc or '').lower().removeprefix('www.') != host:
                continue                     # только свой сайт
            if absu in seen:
                continue
            seen.add(absu)
            out.append(absu)
            if len(out) >= limit:
                return out
    return out
