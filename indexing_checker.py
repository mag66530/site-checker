"""
indexing_checker.py - проверка индексации страниц (пункт 1.7 чек-листа).

Эталон - robots.txt сайта: он говорит, что должно быть закрыто от индексации.
Проверяем СОГЛАСОВАННОСТЬ сигналов индексации между собой:

  • Страница из выборки (главная/каталог/категория/фильтр/товар/тех.) - это
    SEO-страница, она должна быть ОТКРЫТА:
      - закрыта Disallow в robots.txt        → баг
      - <meta name="robots" … noindex>       → баг (противоречит robots)
      - заголовок X-Robots-Tag: noindex      → баг
      - rel=canonical на URL, закрытый в robots → баг (канонизируем в «никуда»)
      - rel=canonical на ДРУГОЙ открытый URL → предупреждение (для фильтров
        каноникл на категорию бывает намеренным)
  • Кросс-проверка sitemap ↔ robots: URL из sitemap (= «хочу в индекс»),
    но закрыт Disallow → противоречие, баг.
  • hreflang (если есть мультиязычность): теги валидируются - коды языков,
    абсолютные URL, self-reference; отсутствие тегов - не ошибка.
  • ЧПУ и формат адресов (по ВСЕМ путям каталога): адрес не технический
    (?ID=…, index.php), в сегментах пути только латиница/цифры/дефис в
    нижнем регистре (кириллица/заглавные/подчёркивания/спецсимволы -
    находки).

Правила разбираются для групп User-agent: * / Yandex / Googlebot по стандарту:
wildcard «*», якорь «$», выигрывает самое ДЛИННОЕ правило, при равенстве - Allow.
"""
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, unquote

# Каких роботов проверяем (специфичная группа перекрывает «*»)
AGENTS = ('*', 'yandex', 'googlebot')


# ── Разбор robots.txt ────────────────────────────────────────────────


@dataclass
class RobotsInfo:
    """Разобранный robots.txt одного хоста."""
    host: str
    status: Optional[int] = None          # HTTP-код ответа robots.txt
    fetched: bool = False                 # удалось ли скачать
    # agent → список (allow: bool, pattern: str, regex)
    groups: dict = field(default_factory=dict)
    sitemaps: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.fetched and self.status == 200


def _pattern_to_regex(pattern: str):
    """Правило robots.txt → компилированный regex по стандарту REP."""
    # Экранируем всё, потом возвращаем смысл «*» и якоря «$»
    anchored = pattern.endswith('$')
    if anchored:
        pattern = pattern[:-1]
    parts = [re.escape(p) for p in pattern.split('*')]
    rx = '.*'.join(parts)
    return re.compile('^' + rx + ('$' if anchored else ''))


def parse_robots(text: str, host: str = '') -> RobotsInfo:
    """Разобрать текст robots.txt: группы правил по агентам + Sitemap."""
    info = RobotsInfo(host=host, fetched=True, status=200)
    groups: dict = {}
    current_agents: list = []
    last_was_agent = False
    for raw in (text or '').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip().lower()
        val = val.strip()
        if key == 'user-agent':
            agent = val.lower()
            if last_was_agent:
                current_agents.append(agent)
            else:
                current_agents = [agent]
            last_was_agent = True
            for a in current_agents:
                groups.setdefault(a, [])
            continue
        last_was_agent = False
        if key == 'sitemap':
            if val:
                info.sitemaps.append(val)
            continue
        if key in ('allow', 'disallow'):
            if not current_agents:
                continue                    # правило вне группы - игнор
            if key == 'disallow' and not val:
                continue                    # пустой Disallow = «всё можно»
            if not val:
                continue
            try:
                rx = _pattern_to_regex(val)
            except Exception:
                continue
            rule = (key == 'allow', val, rx)
            for a in current_agents:
                groups.setdefault(a, []).append(rule)
    info.groups = groups
    return info


def _rules_for_agent(info: RobotsInfo, agent: str):
    """Правила для агента: своя группа, иначе группа «*» (по стандарту).
    Матчим подстрокой: группа «yandexbot» подходит агенту «yandex» (роботы
    сверяют токен продукта, а в robots.txt пишут и Yandex, и YandexBot)."""
    if agent != '*':
        if agent in info.groups:
            return info.groups[agent]
        for name, rules in info.groups.items():
            if name != '*' and (agent in name or name in agent):
                return rules
    return info.groups.get('*', [])


def robots_verdict(info: RobotsInfo, url: str):
    """Закрыт ли URL в robots.txt хоть для одного из AGENTS.

    Возвращает (disallowed: bool, rule: str|None, agent: str|None).
    Выигрывает самое длинное правило; при равной длине - Allow."""
    if not info or not info.ok:
        return False, None, None
    sp = urlsplit(url)
    path = unquote(sp.path or '/')
    if sp.query:
        path += '?' + sp.query
    for agent in AGENTS:
        rules = _rules_for_agent(info, agent)
        best = None            # (len(pattern), allow, pattern)
        for allow, pattern, rx in rules:
            if rx.match(path):
                cand = (len(pattern), allow, pattern)
                if best is None or cand[0] > best[0] or (
                        cand[0] == best[0] and allow and not best[1]):
                    best = cand
        if best is not None and not best[1]:
            return True, best[2], agent
    return False, None, None


# ── Скачивание robots.txt ────────────────────────────────────────────


async def fetch_robots(session, host: str, *, proxy_url=None,
                       timeout_ms: int = 20000) -> RobotsInfo:
    """Скачать и разобрать robots.txt хоста (https)."""
    import aiohttp
    url = f'https://{host}/robots.txt'
    to = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    try:
        async with session.get(url, timeout=to, allow_redirects=True,
                               proxy=proxy_url) as r:
            status = r.status
            if status != 200:
                return RobotsInfo(host=host, status=status, fetched=True)
            data = await r.read()
            text = data.decode('utf-8', errors='replace')
            info = parse_robots(text, host)
            info.status = status
            return info
    except Exception as e:
        return RobotsInfo(host=host, fetched=False, error=str(e))


# ── Сигналы на самой странице ────────────────────────────────────────


_META_RE = re.compile(
    r'<meta\b[^>]*name\s*=\s*["\'](robots|yandex|googlebot)["\'][^>]*>', re.I)
_CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']*)["\']', re.I)
_CANONICAL_RE = re.compile(r'<link\b[^>]*rel\s*=\s*["\']canonical["\'][^>]*>', re.I)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_HREFLANG_RE = re.compile(
    r'<link\b[^>]*hreflang\s*=\s*["\']([^"\']+)["\'][^>]*>', re.I)
# Код языка hreflang: ll / ll-CC / ll-Script / x-default (упрощённо по BCP47)
_HREFLANG_CODE_RE = re.compile(
    r'^(?:[a-z]{2,3}(?:-[a-z0-9]{2,8})?|x-default)$', re.I)


def _find_meta_robots(html: str):
    """(значение content, есть ли noindex) по meta robots/yandex/googlebot."""
    head = html[:200_000]
    for m in _META_RE.finditer(head):
        cm = _CONTENT_RE.search(m.group(0))
        if not cm:
            continue
        content = cm.group(1).strip()
        tokens = {t.strip().lower() for t in content.split(',')}
        if 'noindex' in tokens or 'none' in tokens:
            return content, True
    return None, False


def _find_canonicals(html: str) -> list:
    """href ВСЕХ rel=canonical на странице (валидно - ровно один)."""
    out = []
    for m in _CANONICAL_RE.finditer(html[:200_000]):
        hm = _HREF_RE.search(m.group(0))
        if hm and hm.group(1).strip():
            out.append(hm.group(1).strip())
    return out


def _find_hreflangs(html: str) -> list:
    """[(lang, href)] всех <link rel="alternate" hreflang=…> страницы."""
    out = []
    for m in _HREFLANG_RE.finditer(html[:200_000]):
        tag = m.group(0)
        if 'alternate' not in tag.lower():
            continue
        hm = _HREF_RE.search(tag)
        out.append((m.group(1).strip(), hm.group(1).strip() if hm else ''))
    return out


def _norm_url(u: str) -> str:
    """Нормализация для сравнения canonical с адресом страницы."""
    sp = urlsplit(u)
    host = (sp.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    path = unquote(sp.path or '/').rstrip('/') or '/'
    q = f'?{sp.query}' if sp.query else ''
    return f'{host}{path}{q}'


def _x_robots_noindex(headers: dict):
    """(значение X-Robots-Tag, есть ли noindex). headers - ключи в lower."""
    val = (headers or {}).get('x-robots-tag')
    if not val:
        return None, False
    low = val.lower()
    return val, ('noindex' in low or re.search(r'\bnone\b', low) is not None)


def analyze_page_indexing(html: Optional[str], headers: Optional[dict],
                          url: str, robots: Optional[RobotsInfo],
                          page_type: str = '') -> dict:
    """Сверить сигналы индексации страницы с robots.txt (эталоном).

    Ошибка = РАСХОЖДЕНИЕ с robots.txt:
      • в robots страница открыта, а на ней noindex (meta / X-Robots-Tag);
      • canonical ведёт на URL, закрытый в robots.
    Закрыта в robots + noindex = согласовано (так задумано) - молчим.
    Закрыта в robots без noindex = тоже задумано (robots прав) - молчим;
    противоречия «в sitemap, но под Disallow» ловит отдельная кросс-проверка.
    Возвращает dict для CheckResult.indexing."""
    out = {
        'checked': True,
        'robots_status': robots.status if robots else None,
        'robots_disallowed': False, 'robots_rule': None, 'robots_agent': None,
        'meta_robots': None, 'meta_noindex': False,
        'x_robots': None, 'x_robots_noindex': False,
        'canonical': None, 'canonical_count': 0,
        'canonical_self': None, 'canonical_disallowed': False,
        'hreflang_count': 0,
        'issues': [], 'warnings': [],
    }
    issues, warnings = out['issues'], out['warnings']

    # 1. robots.txt - ЭТАЛОН. Disallow сам по себе не ошибка: так задумано.
    closed_by_robots = False
    if robots is not None and robots.ok:
        dis, rule, agent = robots_verdict(robots, url)
        out['robots_disallowed'] = dis
        out['robots_rule'] = rule
        out['robots_agent'] = agent
        closed_by_robots = dis
    elif robots is not None and robots.fetched and robots.status != 200:
        warnings.append(f'robots.txt отдаёт {robots.status} - правила не применяются')
    elif robots is not None and not robots.fetched:
        warnings.append('robots.txt не удалось скачать')

    # 2. meta robots (в т.ч. name=yandex / name=googlebot) - сверяем с robots:
    # noindex на открытой в robots странице = расхождение = ошибка;
    # noindex на закрытой в robots = согласовано, молчим.
    if html:
        meta_val, meta_noidx = _find_meta_robots(html)
        out['meta_robots'] = meta_val
        out['meta_noindex'] = meta_noidx
        if meta_noidx and not closed_by_robots:
            issues.append('расхождение с robots.txt: в robots страница открыта, '
                          'но на ней noindex (meta robots)')

    # 3. X-Robots-Tag - так же сверяем с robots
    x_val, x_noidx = _x_robots_noindex(headers)
    out['x_robots'] = x_val
    out['x_robots_noindex'] = x_noidx
    if x_noidx and not closed_by_robots:
        issues.append('расхождение с robots.txt: в robots страница открыта, '
                      'но на ней noindex (заголовок X-Robots-Tag)')

    # 4. canonical - «верно настроен rel=canonical»:
    #    ровно один тег; указывает на себя; не на чужой хост; не на URL,
    #    закрытый в robots. Отсутствие тега - предупреждение.
    if html:
        canons = _find_canonicals(html)
        out['canonical'] = canons[0] if canons else None
        out['canonical_count'] = len(canons)
        if not canons:
            warnings.append('нет rel="canonical" на странице')
        else:
            if len(canons) >= 2:
                issues.append('несколько rel="canonical" на странице - '
                              'поисковики игнорируют такой сигнал')
            canon = canons[0]
            try:
                self_ref = _norm_url(canon) == _norm_url(url)
            except Exception:
                self_ref = None
            out['canonical_self'] = self_ref
            if self_ref is False:
                _page_host = (urlsplit(url).netloc or '').lower().removeprefix('www.')
                _can_host = (urlsplit(canon).netloc or '').lower().removeprefix('www.')
                if _can_host and _can_host != _page_host:
                    # Канонизация на другой домен/поддомен: страница города
                    # отдаёт свой вес чужому хосту - выпадает из поиска.
                    issues.append('canonical ведёт на другой домен/поддомен')
                else:
                    # Каноникл на закрытый robots'ом URL - канонизируем
                    # «в никуда» (robots того же хоста).
                    if robots is not None and robots.ok:
                        c_dis, _c_rule, _ = robots_verdict(robots, canon)
                        if c_dis:
                            out['canonical_disallowed'] = True
                            issues.append('расхождение с robots.txt: canonical '
                                          'ведёт на URL, закрытый в robots')
                    if not out['canonical_disallowed']:
                        warnings.append('canonical ведёт на другой URL')

    # 5. hreflang (если есть мультиязычность). Отсутствие тегов - НЕ ошибка:
    # одноязычному сайту hreflang не нужен. Если теги есть - валидируем:
    # корректные коды языков, абсолютные URL, self-reference.
    if html:
        hl = _find_hreflangs(html)
        out['hreflang_count'] = len(hl)
        if hl:
            bad_codes = [c for c, _ in hl if not _HREFLANG_CODE_RE.match(c)]
            rel_urls = [h for _, h in hl
                        if h and not urlsplit(h).scheme]
            try:
                self_ref = any(_norm_url(h) == _norm_url(url)
                               for _, h in hl if h)
            except Exception:
                self_ref = True
            if bad_codes:
                warnings.append('hreflang: некорректные коды языков')
            if rel_urls:
                warnings.append('hreflang: относительные URL - '
                                'должны быть абсолютными')
            if not self_ref:
                warnings.append('hreflang: нет ссылки на саму страницу '
                                '(self-reference)')

    out['verdict'] = 'closed' if issues else ('warn' if warnings else 'open')
    return out


# ── Кросс-проверка sitemap ↔ robots (весь каталог, не только выборка) ─


# Типовой «мусор», который ДОЛЖЕН быть закрыт в robots (ТЗ 3.3.4.2 + доп.
# чек-лист «Robots.txt»): пагинация/сортировка строятся от реальной категории,
# остальное - типовые пути Bitrix-проектов. Находка = путь ОТКРЫТ в robots И
# реально отвечает 200 (несуществующие страницы не считаем - закрывать нечего).
_JUNK_FIXED = [
    ('поиск', '/search/'),
    ('корзина', '/basket/'),
    ('корзина', '/cart/'),
    ('сравнение', '/compare/'),
    ('личный кабинет', '/personal/'),
    ('авторизация', '/auth/'),
    # доп. чек-лист: оформление/отправленные заказы, вход в админку
    ('оформление заказа', '/order/'),
    ('оформление заказа', '/checkout/'),
    ('отправленные заказы', '/personal/order/'),
    ('админ. панель', '/bitrix/'),
    ('админ. панель', '/admin/'),
    # доп. чек-лист: общие технические каталоги + AJAX-обработчики (попапы)
    ('AJAX-обработчик', '/ajax/'),
    ('служебный каталог /local/', '/local/'),
    ('служебный каталог /cgi-bin/', '/cgi-bin/'),
]


async def _status_direct(session, url, proxy_url, *, timeout_ms=15000,
                         follow_redirects=False):
    """Код ответа URL. По умолчанию БЕЗ редиректов: 3xx на пагинации значит
    «дубля нет» (страница сводится к базовой). HEAD, при 405/501 - GET."""
    import aiohttp
    to = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    try:
        async with session.head(url, timeout=to,
                                allow_redirects=follow_redirects,
                                proxy=proxy_url) as r:
            if r.status not in (405, 501):
                return r.status
    except Exception:
        pass
    try:
        async with session.get(url, timeout=to,
                               allow_redirects=follow_redirects,
                               proxy=proxy_url) as r:
            return r.status
    except Exception:
        return None


async def check_paths_against_robots(host: str, paths: list, *,
                                     proxy_url=None, limit: int = 300,
                                     sample_category: str = None,
                                     project_sitemap_url: str = None) -> dict:
    """Гигиена robots.txt главного хоста (ТЗ 3.3):

    1. Все известные пути каталога (категории/фильтры/товары из sitemap и
       выгрузок) через robots: путь в sitemap = «хочу в индекс», Disallow на
       нём = противоречие (ТЗ 3.3.4.1).
    2. Типовой мусор закрыт (ТЗ 3.3.4.2): пагинация/сортировка реальной
       категории + поиск/корзина/сравнение/ЛК. Открыт в robots И отвечает
       200 = находка.
    3. Sitemap-директивы (ТЗ 3.3.6): есть хотя бы одна; каждая отдаёт 200;
       совпадает ли с sitemap проекта.
    4. Доп. чек-лист «Robots.txt»:
       • нет буквальной директивы «Disallow: /» (сайт закрыт целиком) - баг;
       • заданы отдельные группы User-agent для Yandex и Googlebot -
         отсутствие = предупреждение (правила и так наследуются от «*»);
       • .css/.js главной страницы не закрыты Disallow - Google требует
         доступ к ресурсам для рендеринга, закрытые = баг.

    Возвращает {'host', 'robots_status', 'sitemaps', 'checked',
                'disallowed': [...], 'junk_open': [...],
                'sitemap_checks': {...}, 'blanket_disallow': [...],
                'ua_groups': {...}, 'assets_checked': int,
                'assets_closed': [...], 'error'}."""
    import aiohttp
    from http_checker import make_browser_headers
    out = {'host': host, 'robots_status': None, 'sitemaps': [],
           'checked': 0, 'disallowed': [], 'junk_open': [],
           'sitemap_checks': None, 'blanket_disallow': [],
           'ua_groups': None, 'assets_checked': 0, 'assets_closed': [],
           'error': None}
    try:
        async with aiohttp.ClientSession(
                headers=make_browser_headers()) as session:
            info = await fetch_robots(session, host, proxy_url=proxy_url)
            out['robots_status'] = info.status
            out['sitemaps'] = info.sitemaps
            if not info.ok:
                out['error'] = info.error or f'robots.txt: HTTP {info.status}'
                return out

            # ── 0а. «Disallow: /» - сайт закрыт целиком (доп. чек-лист) ──
            # Буквальное правило: даже если частично перекрыто Allow,
            # такая директива в боевом robots - ошибка. Смотрим ТОЛЬКО
            # поисковых роботов: «Disallow: /» для GPTBot/ClaudeBot и
            # прочих AI-краулеров - намеренная блокировка, не находка.
            for _agent, _rules in info.groups.items():
                if not (_agent == '*' or 'yandex' in _agent
                        or 'google' in _agent):
                    continue
                if any(not _allow and _pat == '/'
                       for _allow, _pat, _rx in _rules):
                    out['blanket_disallow'].append(_agent)

            # ── 0б. Отдельные группы User-agent (доп. чек-лист) ──
            _names = set(info.groups)
            out['ua_groups'] = {
                'star': '*' in _names,
                'yandex': any('yandex' in n for n in _names),
                'google': any('google' in n for n in _names),
            }

            # ── 1. sitemap ↔ robots (пути каталога не закрыты) ──
            seen = set()
            for p in paths or []:
                p = (p or '').strip()
                if not p or p in seen:
                    continue
                seen.add(p)
                out['checked'] += 1
                dis, rule, agent = robots_verdict(info, f'https://{host}{p}')
                if dis and len(out['disallowed']) < limit:
                    out['disallowed'].append(
                        {'path': p, 'rule': rule, 'agent': agent})

            # ── 2. Мусор закрыт (ТЗ 3.3.4.2 + доп. чек-лист) ──
            # Параметрические URL всегда отвечают 200 (сервер игнорит лишний
            # GET-параметр), поэтому для них проверка = «закрыт ли в robots».
            # База - реальная категория (иначе главная): на ней параметры
            # порождают дубль/служебную выдачу.
            _base = '/'
            if sample_category:
                _base = '/' + sample_category.strip('/') + '/'
            _param_junk = [
                # сортировки: цена/новизна/популярность/алфавит
                ('сортировка', f'{_base}?sort=price'),
                ('сортировка', f'{_base}?sort=date'),
                ('сортировка', f'{_base}?sort=popularity'),
                ('сортировка', f'{_base}?sort=name'),
                # пагинация
                ('пагинация', f'{_base}?PAGEN_1=2'),
                ('пагинация', f'{_base}?page=2'),
                # дубли-метки: UTM/трекинг + версия для печати
                ('метки UTM/трекинг', f'{_base}?utm_source=test'),
                ('метки UTM/трекинг', f'{_base}?from=test'),
                ('версия для печати', f'{_base}?print=Y'),
                # AJAX-эндпоинты (попапы Битрикса)
                ('AJAX-запрос', f'{_base}?bxajaxid=test'),
                ('AJAX-запрос', f'{_base}?ajax=Y'),
                # служебные экшены: вход/регистрация/сброс пароля/возврат в админку
                ('служебный экшен', f'{_base}?login=yes'),
                ('служебный экшен', f'{_base}?register=yes'),
                ('служебный экшен', f'{_base}?forgot_password=yes'),
                ('служебный экшен', f'{_base}?change_password=yes'),
                ('служебный экшен', f'{_base}?back_url_admin=/'),
            ]
            junk = _param_junk + list(_JUNK_FIXED)
            for label, path in junk:
                # одна находка на сущность: «сортировка» открыта - хватит
                # одного примера, варианты параметров не перечисляем
                if any(j['label'] == label for j in out['junk_open']):
                    continue
                dis, _rule, _agent = robots_verdict(info, f'https://{host}{path}')
                if dis:
                    continue                     # закрыт - как и должно быть
                status = await _status_direct(
                    session, f'https://{host}{path}', proxy_url)
                if status == 200:                # существует И открыт = находка
                    out['junk_open'].append(
                        {'label': label, 'path': path})

            # ── 3. Sitemap-директивы (ТЗ 3.3.6) ──
            sm = {'has_directive': bool(info.sitemaps),
                  'directives': [], 'matches_project': None}
            for u in info.sitemaps[:5]:
                st = await _status_direct(session, u, proxy_url,
                                          follow_redirects=True)
                sm['directives'].append({'url': u, 'status': st})
            if project_sitemap_url and info.sitemaps:
                def _norm_sm(x):
                    x = (x or '').strip().rstrip('/').lower()
                    return x.replace('://www.', '://')
                sm['matches_project'] = any(
                    _norm_sm(u) == _norm_sm(project_sitemap_url)
                    for u in info.sitemaps)
            out['sitemap_checks'] = sm

            # ── 4. .css/.js не закрыты Disallow (доп. чек-лист) ──
            # Google рендерит страницы: закрытые стили/скрипты = страница
            # «без вёрстки» в глазах робота. Берём ресурсы главной.
            try:
                _assets = await _collect_assets(
                    session, f'https://{host}/', proxy_url)
                out['assets_checked'] = len(_assets)
                for _u in _assets:
                    _dis, _rule, _agent = robots_verdict(info, _u)
                    if _dis:
                        out['assets_closed'].append(
                            {'url': _u, 'rule': _rule, 'agent': _agent})
            except Exception:
                pass
    except Exception as e:
        out['error'] = str(e)
    return out


_RE_CSS = re.compile(
    r'<link\b[^>]*href\s*=\s*["\']([^"\']+\.css(?:\?[^"\']*)?)["\']', re.I)
_RE_JS = re.compile(
    r'<script\b[^>]*src\s*=\s*["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', re.I)


async def _collect_assets(session, page_url: str, proxy_url,
                          *, limit: int = 30) -> list:
    """URL .css/.js СВОЕГО хоста со страницы (для проверки по robots).
    Чужие CDN не проверяем - их robots нам не подчиняется."""
    import aiohttp
    from urllib.parse import urljoin
    to = aiohttp.ClientTimeout(total=20)
    async with session.get(page_url, timeout=to, allow_redirects=True,
                           proxy=proxy_url) as r:
        if r.status != 200:
            return []
        html = (await r.read()).decode('utf-8', errors='replace')
    page_host = (urlsplit(page_url).netloc or '').lower().removeprefix('www.')
    seen, urls = set(), []
    for m in list(_RE_CSS.finditer(html)) + list(_RE_JS.finditer(html)):
        u = urljoin(page_url, m.group(1).strip())
        h = (urlsplit(u).netloc or '').lower().removeprefix('www.')
        if h != page_host or u in seen:
            continue
        seen.add(u)
        urls.append(u)
        if len(urls) >= limit:
            break
    return urls


# ── ЧПУ и формат адресов (по всем путям каталога, без запросов) ──────


_RE_CYR = re.compile(r'[а-яё]', re.I)
_RE_UPPER = re.compile(r'[A-Z]')
_RE_SLUG_JUNK = re.compile(r'[^a-z0-9\-_./]')

_EXAMPLES = 10        # сколько примеров каждого типа показывать в отчёте


def check_url_format(paths: list) -> dict:
    """ЧПУ и формат адресов. paths - пути каталога и тех. страниц
    ('/catalog/truba/…'). Запросов не делает - чистая валидация строк.

    Находки:
      • non_sef  - технический адрес (query ?ID=…, index.php и т.п.) - не ЧПУ;
      • cyrillic - кириллица в пути (включая %-энкод и punycode xn--);
      • uppercase - ЗАГЛАВНЫЕ буквы (риск дублей /Catalog/ vs /catalog/);
      • underscore - подчёркивания (чек-лист требует дефисы);
      • junk_chars - прочие символы (пробелы и спецсимволы)."""
    out = {'checked': 0, 'non_sef': [], 'cyrillic': [], 'uppercase': [],
           'underscore': [], 'junk_chars': []}

    def _add(kind, p):
        if len(out[kind]) < _EXAMPLES:
            out[kind].append(p)
        out[kind + '_n'] += 1

    for kind in ('non_sef', 'cyrillic', 'uppercase', 'underscore',
                 'junk_chars'):
        out[kind + '_n'] = 0
    for p in paths or []:
        if not p or p == '/':
            continue
        out['checked'] += 1
        low = p.lower()
        # Не-ЧПУ: значимая часть адреса в query или скриптовое расширение.
        if ('?' in p or '.php' in low.split('?')[0]
                or '.asp' in low.split('?')[0]):
            _add('non_sef', p)
            continue
        path = unquote(p)                       # %D0%BF… → кириллица
        if _RE_CYR.search(path) or 'xn--' in low:
            _add('cyrillic', p)
            continue
        if _RE_UPPER.search(path):
            _add('uppercase', p)
            continue
        # Подчёркивания: только ВНЕ /filter/-части. Слаги свойств умного
        # фильтра Bitrix (gost_tu-is-…) содержат «_» штатно - не шумим,
        # а вот категории/товары с «_» - находка.
        if '_' in path.split('/filter/')[0]:
            _add('underscore', p)
            continue
        if _RE_SLUG_JUNK.search(path):
            _add('junk_chars', p)
    out['total_bad'] = sum(out[k + '_n'] for k in
                           ('non_sef', 'cyrillic', 'uppercase', 'underscore',
                            'junk_chars'))
    return out
