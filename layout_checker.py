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
  • Семантическая разметка - используются <header>/<footer>/<main>
    (<article>/<section>/<nav> считаем присутствием, но не требуем - не на
    всех типах страниц уместны). Всё на <div> или нет ядра - предупреждение;
    <main> больше одного - предупреждение.
  • Инлайн-стили - визуальные стили должны жить в CSS-файлах; много
    атрибутов style="…" в HTML - предупреждение (немного инлайна на живых
    сайтах неизбежно: баннеры с background-image, переключатели видимости).
  • Favicon - установлен (<link rel="…icon…"> или дефолтный /favicon.ico)
    и реально грузится; прозвон делает http_checker с главной поддомена
    (favicon сквозной). 404/410 = баг.
  • Единый протокол (https) - на https-странице нет ресурсов по http
    (mixed content: браузер блокирует такие картинки/стили/скрипты - баг)
    и нет внутренних <a>-ссылок по http (лишний редирект - предупреждение).
  • Стили/скрипты во внешних файлах - большие inline-<style>/<script>
    блоки (тяжелее порога) надо выносить в файлы: они не кешируются и
    раздувают каждую страницу (предупреждение). JSON-LD и шаблоны не
    считаем.
  • Отложенный рендеринг скриптов - <script src> в <head> без async/defer
    блокируют отрисовку страницы (предупреждение от 2 штук).

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

# Семантические теги: ядро (требуем) и расширение (считаем, но не требуем).
_SEMANTIC_CORE = ('header', 'footer', 'main')
_SEMANTIC_EXTRA = ('nav', 'article', 'section', 'aside')
_RE_INLINE_STYLE = re.compile(
    r'<[a-z][a-zA-Z0-9-]*\b[^>]*\bstyle\s*=\s*["\']', re.I)
_RE_COMMENT = re.compile(r'<!--.*?-->', re.S)
# Инлайн-стилей больше порога - предупреждение (немного инлайна неизбежно).
_INLINE_STYLE_LIMIT = 15

# Единый протокол: http-РЕСУРСЫ на https-странице (mixed content) и
# внутренние <a>-ссылки по http. <link> считаем ресурсом только когда он
# реально грузится браузером (stylesheet/icon/preload) - rel=alternate и
# т.п. не блокируются.
_RE_MIXED_RES = re.compile(
    r'<(?:img|script|source|iframe|video|audio)\b[^>]*?'
    r'(?:src|href)\s*=\s*["\'](http://[^"\']+)["\']', re.I)

# Вынос стилей/скриптов во внешние файлы: большие inline-блоки не
# кешируются браузером и раздувают каждую страницу.
_RE_INLINE_SCRIPT = re.compile(
    r'<script\b([^>]*)>(.*?)</script>', re.I | re.S)
_INLINE_STYLE_KB = 15        # суммарный inline-<style> больше - предупреждение
_INLINE_SCRIPT_KB = 30       # суммарный inline-<script> больше - предупреждение
# Блокирующие скрипты: <script src> в <head> без async/defer.
_RE_SCRIPT_HEAD = re.compile(r'<script\b[^>]*\bsrc\s*=[^>]*>', re.I)
_BLOCKING_MIN = 2            # от скольких блокирующих скриптов ругаемся

# Псевдоссылки: button/div/span с onclick-переходом вместо <a href> -
# краулер по ним не пройдёт, средняя кнопка мыши/новая вкладка не работают.
_RE_PSEUDO_LINK = re.compile(
    r'<(?:button|div|span)\b[^>]*onclick\s*=\s*["\'][^"\']*'
    r'(?:location\.href|location\s*=|window\.open|window\.location)', re.I)

# @font-face без font-display: swap в inline-<style> (внешние CSS смотрит
# http_checker и кладёт флаги в css_infos).
_RE_FONTFACE = re.compile(r'@font-face\s*\{[^}]*\}', re.I)
_RE_FONT_DISPLAY_OK = re.compile(
    r'font-display\s*:\s*(?:swap|optional|fallback)', re.I)

# Скрытый текст (SEO-спам): классические паттерны сокрытия. display:none
# НЕ считаем - у Bitrix это легитимные попапы/мобильные меню повсюду.
# Двухшагово: элемент со style + отдельная проверка значения стиля.
# ВАЖНО для скорости: только теги с обязательным закрытием - для void-тегов
# (<img style=…>, <input>) движок сканировал бы весь документ в поисках
# несуществующего </img> на КАЖДЫЙ такой тег (прогон вставал колом).
_RE_STYLED_EL = re.compile(
    r'<(div|span|p|a|li|td|section|article|b|i|u|em|strong|font|h[1-6])'
    r'\b[^>]*style\s*=\s*(["\'])(.*?)\2[^>]*>(.*?)</\1\s*>',
    re.I | re.S)
_RE_HIDDEN_VAL = re.compile(
    r'font-size\s*:\s*(?:0(?:px)?|1px)\s*(?:;|!|$)'
    r'|text-indent\s*:\s*-\d{4}'
    r'|opacity\s*:\s*0(?:\.0+)?\s*(?:;|!|$)', re.I)
_HIDDEN_TEXT_MIN = 100       # значимый объём скрытого текста, символов
_RE_STRIP_TAGS = re.compile(r'<[^>]+>')

# Дефекты текста (чек-лист: «нет лишних переносов, слипшихся букв»).
# Слипшиеся слова: слово начинается со СТРОЧНОЙ и содержит ЗАГЛАВНУЮ в
# середине («городеМоскве» - пропал пробел при подстановке). Бренды-
# CamelCase («СтальМет») начинаются с заглавной - не матчатся; единицы
# («кВт», «мВт») отсечены требованием 3+ строчных до заглавной.
_RE_STUCK_WORD = re.compile(r'\b[а-яё]{3,}[А-ЯЁ][а-яё]{2,}')
# Кривой перенос: дефис+пробел ВНУТРИ слова («метал- лопрокат» - копипаста
# из PDF/вёрстки). Легитимное «двух- и трёхкомнатные» исключаем.
_RE_BAD_HYPHEN = re.compile(r'\b[а-яё]{2,}- (?!и\b|или\b)(?=[а-яё]{2,})')
_SOFT_HYPHEN_LIMIT = 30      # мягких переносов больше - замечание

# Меню: пункты-«пустышки» (не прямые ссылки) и прямая ссылка на каталог.
_RE_MENU_ZONE_ALL = re.compile(r'<(header|nav)\b[^>]*>.*?</\1>', re.I | re.S)
_RE_A_DUMMY = re.compile(
    r'<a\b[^>]*href\s*=\s*["\'](?:#|javascript:[^"\']*)["\']', re.I)
_RE_A_CATALOG = re.compile(
    r'<a\b[^>]*href\s*=\s*["\'][^"\']*/catalog[/"\']', re.I)

# Хлебные крошки: контейнер по классу/itemtype.
_RE_BREADCRUMB_BLOCK = re.compile(
    r'<(ol|ul|div|nav)\b[^>]*(?:class\s*=\s*["\'][^"\']*breadcrumb[^"\']*["\']'
    r'|itemtype\s*=\s*["\'][^"\']*BreadcrumbList[^"\']*["\'])[^>]*>(.*?)</\1>',
    re.I | re.S)
_RE_LINK_TAG = re.compile(r'<link\b[^>]*>', re.I)
_RE_LINK_LOAD = re.compile(r'rel\s*=\s*["\'][^"\']*(?:stylesheet|icon|preload)',
                           re.I)
_RE_HTTP_HREF = re.compile(r'href\s*=\s*["\'](http://[^"\']+)["\']', re.I)
_RE_A_HTTP = re.compile(r'<a\b[^>]*href\s*=\s*["\'](http://[^"\']+)["\']', re.I)


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

    # 5. Семантическая разметка: <header>/<footer>/<main> (+расширение).
    # Комментарии вырезаем - выключенная вёрстка не считается.
    body = _RE_COMMENT.sub(' ', html)
    present = [t for t in (_SEMANTIC_CORE + _SEMANTIC_EXTRA)
               if re.search(rf'<{t}[\s>]', body, re.I)]
    missing_core = [t for t in _SEMANTIC_CORE if t not in present]
    main_count = len(re.findall(r'<main[\s>]', body, re.I))
    if html:
        if not present:
            warnings.append(
                'семантические HTML-теги не используются (<header>/<footer>/'
                '<main>/<article>/<section>) - вся вёрстка на <div>')
        elif missing_core:
            warnings.append(
                'семантическая разметка неполная - нет тегов: '
                + ', '.join(f'<{t}>' for t in missing_core))
        if main_count > 1:
            warnings.append('тег <main> встречается несколько раз - '
                            'должен быть один на страницу')

    # 6. Инлайн-стили: визуальные стили должны жить в CSS, не в style="…".
    inline_styles = len(_RE_INLINE_STYLE.findall(body))
    if inline_styles > _INLINE_STYLE_LIMIT:
        warnings.append(
            'много инлайн-стилей (атрибут style="…" в HTML) - визуальные '
            'стили лучше вынести в CSS-файлы')

    # 7. Единый протокол: на https-странице нет http-ресурсов (mixed content)
    # и внутренних <a>-ссылок по http. Тексты без чисел - для группировки.
    mixed, http_links = [], []
    if base_url.startswith('https://'):
        mixed = list(dict.fromkeys(_RE_MIXED_RES.findall(body)))
        for tag in _RE_LINK_TAG.findall(body):
            if _RE_LINK_LOAD.search(tag):
                hm = _RE_HTTP_HREF.search(tag)
                if hm and hm.group(1) not in mixed:
                    mixed.append(hm.group(1))
        for u in _RE_A_HTTP.findall(body):
            if _norm_host(urlsplit(u).netloc) == own_host:
                http_links.append(u)
        http_links = list(dict.fromkeys(http_links))
        if mixed:
            issues.append('ресурсы грузятся по http на https-странице '
                          '(mixed content) - браузер их блокирует, '
                          'картинки/стили/скрипты ломаются')
        if http_links:
            warnings.append('внутренние ссылки со старым протоколом http:// - '
                            'должны быть https (лишний редирект на каждый '
                            'переход)')

    # 8. Стили/скрипты во внешних файлах: большие inline-блоки.
    inline_style_kb = sum(len(b) for b in _RE_STYLE_BLOCK.findall(body)) // 1024
    inline_script_kb = 0
    for attrs, code in _RE_INLINE_SCRIPT.findall(body):
        low_attrs = attrs.lower()
        if 'src=' in low_attrs:
            continue                             # внешний - не inline
        if 'ld+json' in low_attrs or 'template' in low_attrs:
            continue                             # разметка/шаблон - не код
        inline_script_kb += len(code)
    inline_script_kb //= 1024
    if inline_style_kb > _INLINE_STYLE_KB:
        warnings.append('большие inline-<style> блоки - стили не кешируются, '
                        'вынести во внешний CSS-файл')
    if inline_script_kb > _INLINE_SCRIPT_KB:
        warnings.append('большие inline-<script> блоки - скрипты не '
                        'кешируются, вынести во внешний JS-файл')

    # 9. Отложенный рендеринг: <script src> в <head> без async/defer
    # блокируют отрисовку страницы.
    head_end = body.lower().find('</head>')
    head = body[:head_end] if head_end > 0 else ''
    blocking = [t for t in _RE_SCRIPT_HEAD.findall(head)
                if 'async' not in t.lower() and 'defer' not in t.lower()]
    if len(blocking) >= _BLOCKING_MIN:
        warnings.append('скрипты в <head> без async/defer - блокируют '
                        'отрисовку страницы (отложенный рендеринг не настроен)')

    # 10. Псевдоссылки: переходы через onclick на button/div вместо <a href>.
    pseudo_links = len(_RE_PSEUDO_LINK.findall(body))
    if pseudo_links:
        warnings.append('ссылки оформлены не тегом <a href>, а button/div с '
                        'onclick-переходом - краулер по ним не пройдёт, '
                        '«открыть в новой вкладке» не работает')

    # 11. font-display: swap - шрифты без него прячут текст до загрузки,
    # макет дёргается (CLS). Внешние CSS - флаги из css_infos, плюс
    # inline-<style>.
    ff_noswap = sum(c.get('fontface_noswap') or 0 for c in css_infos)
    for block in _RE_STYLE_BLOCK.findall(body):
        for face in _RE_FONTFACE.findall(block):
            if not _RE_FONT_DISPLAY_OK.search(face):
                ff_noswap += 1
    if ff_noswap:
        warnings.append('шрифты @font-face без font-display: swap - текст '
                        'скрыт до загрузки шрифта, возможен сдвиг макета')

    # 12. Скрытый текст (SEO-спам): font-size:0/1px, text-indent:-9999,
    # opacity:0 у элементов со значимым текстом.
    hidden_text = []
    # Дешёвый гейт: на большинстве страниц спам-паттернов нет вовсе -
    # детальный проход по элементам не запускаем.
    if _RE_HIDDEN_VAL.search(body):
        for m in _RE_STYLED_EL.finditer(body):
            if not _RE_HIDDEN_VAL.search(m.group(3)):
                continue
            txt = re.sub(r'\s+', ' ',
                         _RE_STRIP_TAGS.sub(' ', m.group(4))).strip()
            if len(txt) >= _HIDDEN_TEXT_MIN:
                hidden_text.append(txt[:120])
            if len(hidden_text) >= 5:
                break
    if hidden_text:
        warnings.append('возможен скрытый текст (font-size:0 / text-indent:'
                        '-9999 / opacity:0 со значимым текстом) - проверить '
                        'вручную, поисковики наказывают за скрытый текст')

    # 13. Дефекты текста: слипшиеся слова, кривые переносы, море &shy;.
    # По ВИДИМОМУ тексту (скрипты/стили/атрибуты не считаются).
    # Кап 600КБ: дефекты шаблонные, встречаются в начале; strip_non_visible
    # на мегабайтных листингах - заметный CPU на каждую страницу.
    stuck, bad_hyphens = [], []
    try:
        from text_checker import strip_non_visible
        vis = re.sub(r'\s+', ' ', _RE_STRIP_TAGS.sub(
            ' ', strip_non_visible(html[:600_000])))
        stuck = list(dict.fromkeys(_RE_STUCK_WORD.findall(vis)))[:10]
        bad_hyphens = list(dict.fromkeys(
            m.group(0) + '…' for m in _RE_BAD_HYPHEN.finditer(vis)))[:10]
        if len(stuck) >= 2:
            warnings.append('слипшиеся слова в тексте (пропал пробел на '
                            'стыке слов/подстановки)')
        else:
            stuck = []                  # единичное - вероятно бренд, молчим
        if len(bad_hyphens) >= 2:
            warnings.append('кривые переносы в тексте (дефис с пробелом '
                            'внутри слова - копипаста из PDF/вёрстки)')
        else:
            bad_hyphens = []
        if html.count('­') + html.lower().count('&shy;') > _SOFT_HYPHEN_LIMIT:
            warnings.append('очень много мягких переносов (&shy;) в тексте - '
                            'проверить, не расставлены ли они автозаменой')
    except Exception:
        pass

    # 13а. Состояния интерактивных элементов: :hover/:focus/:active в CSS
    # (внешние стили + inline <style>). Нет :hover = мёртвый UI на десктопе,
    # нет :focus = недоступно с клавиатуры (предупреждения).
    _all_css_text = ' '.join(_RE_STYLE_BLOCK.findall(body))
    has_hover = (any(c.get('has_hover') for c in css_infos)
                 or ':hover' in _all_css_text)
    has_focus = (any(c.get('has_focus') for c in css_infos)
                 or ':focus' in _all_css_text)
    has_active = (any(c.get('has_active') for c in css_infos)
                  or ':active' in _all_css_text)
    if css_infos:                    # без стилей судить не о чем
        if not has_hover:
            warnings.append('в стилях нет :hover - у интерактивных элементов '
                            'нет реакции на наведение')
        if not has_focus:
            warnings.append('в стилях нет :focus - состояние фокуса не '
                            'оформлено (недоступно с клавиатуры)')

    # 14. Меню прямыми ссылками (не скриптами) + прямая ссылка на каталог.
    # Кап 500КБ: шапка всегда в начале документа, а зонный regex с .*? по
    # мегабайтному листингу - лишний CPU.
    menu_dummy, menu_catalog = 0, False
    _menu_html = ' '.join(m.group(0)
                          for m in _RE_MENU_ZONE_ALL.finditer(body[:500_000]))
    if _menu_html:
        menu_dummy = len(_RE_A_DUMMY.findall(_menu_html))
        menu_catalog = bool(_RE_A_CATALOG.search(_menu_html))
        if menu_dummy >= 2:
            warnings.append('пункты меню не прямыми ссылками (href="#"/'
                            'javascript:) - краулер по ним не пройдёт')
        if not menu_catalog:
            warnings.append('в меню шапки нет прямой ссылки на каталог '
                            '(/catalog…) - категории недоступны в один клик')

    # 15. Последняя хлебная крошка должна быть БЕЗ ссылки (текущая страница).
    # Только на страницах СО ВЛОЖЕННОСТЬЮ - на главной/корне крошек нет.
    crumb_last_link = False
    _nested = bool((urlsplit(base_url).path or '/').strip('/'))
    # Крошки - сразу после шапки: ищем в первых 500КБ.
    m_bc = _RE_BREADCRUMB_BLOCK.search(body[:500_000]) if _nested else None
    if m_bc:
        inner = m_bc.group(2)
        last_a_end = inner.rfind('</a>')
        if last_a_end != -1:
            # Значимый текст ПОСЛЕ последней ссылки = крошки заканчиваются
            # текстом (текущая страница без ссылки) - так и должно быть.
            tail = _RE_STRIP_TAGS.sub(' ', inner[last_a_end + 4:])
            tail = re.sub(r'[\s>»/·|→-]+', '', tail)
            if len(tail) < 3:
                crumb_last_link = True
                warnings.append('последняя хлебная крошка - ссылка: текущая '
                                'страница в крошках должна быть текстом '
                                'без ссылки')

    return {
        'viewport': viewport,
        'css_total': len(css_infos),
        'css_broken': [{'url': c.get('url', ''), 'status': c.get('status')}
                       for c in broken],
        'has_media': has_media,
        'assets': assets,
        'semantic': {'present': present, 'missing_core': missing_core,
                     'main_count': main_count},
        'inline_styles': inline_styles,
        'mixed_content': mixed[:20],
        'http_links': len(http_links),
        'inline_style_kb': inline_style_kb,
        'inline_script_kb': inline_script_kb,
        'blocking_scripts': len(blocking),
        'pseudo_links': pseudo_links,
        'fontface_noswap': ff_noswap,
        'hidden_text': hidden_text,
        'stuck_words': stuck,
        'bad_hyphens': bad_hyphens,
        'menu_dummy': menu_dummy,
        'menu_catalog': menu_catalog,
        'crumb_last_link': crumb_last_link,
        'states': {'hover': has_hover, 'focus': has_focus,
                   'active': has_active},
        'issues': issues,
        'warnings': warnings,
    }


# ── Favicon: установлен и реально грузится ──────────────────────────

_RE_FAVICON = re.compile(
    r'<link\b[^>]*rel\s*=\s*["\'][^"\']*icon[^"\']*["\'][^>]*>', re.I)
_RE_HREF_ATTR = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)


def extract_favicon(html: str, base_url: str):
    """(url, from_tag): адрес favicon из <link rel="…icon…"> или дефолтный
    /favicon.ico, если тега нет (браузеры сами пробуют этот путь)."""
    for m in _RE_FAVICON.finditer(html or ''):
        hm = _RE_HREF_ATTR.search(m.group(0))
        if hm and hm.group(1).strip():
            href = hm.group(1).strip()
            if href.startswith('data:'):     # инлайн-иконка - грузится всегда
                return None, True
            return urljoin(base_url, href), True
    sp = urlsplit(base_url)
    return f'{sp.scheme}://{sp.netloc}/favicon.ico', False


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
