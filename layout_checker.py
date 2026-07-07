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

_RE_VIEWPORT = re.compile(
    r'<meta\b[^>]*name\s*=\s*["\']viewport["\'][^>]*>', re.I)
_RE_STYLE_BLOCK = re.compile(r'<style\b[^>]*>(.*?)</style>', re.I | re.S)
_RE_MEDIA_WIDTH = re.compile(r'@media[^{]*\b(?:max|min)-width', re.I)


def check_layout(html: Optional[str], css_infos: Optional[list]) -> dict:
    """Проверка вёрстки одной страницы.

    css_infos - список {'url', 'status', 'has_media'} по подключённым CSS
    (из кэша http_checker). Возвращает dict для CheckResult.layout."""
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

    return {
        'viewport': viewport,
        'css_total': len(css_infos),
        'css_broken': [{'url': c.get('url', ''), 'status': c.get('status')}
                       for c in broken],
        'has_media': has_media,
        'issues': issues,
        'warnings': warnings,
    }
