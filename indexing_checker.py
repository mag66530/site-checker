"""
indexing_checker.py – проверка индексации страниц (пункт 1.7 чек-листа).

Эталон – robots.txt сайта: он говорит, что должно быть закрыто от индексации.
Проверяем СОГЛАСОВАННОСТЬ сигналов индексации между собой:

  • Страница из выборки (главная/каталог/категория/фильтр/товар/тех.) – это
    SEO-страница, она должна быть ОТКРЫТА:
      – закрыта Disallow в robots.txt        → баг
      – <meta name="robots" … noindex>       → баг (противоречит robots)
      – заголовок X-Robots-Tag: noindex      → баг
      – rel=canonical на URL, закрытый в robots → баг (канонизируем в «никуда»)
      – rel=canonical на ДРУГОЙ открытый URL → предупреждение (для фильтров
        каноникл на категорию бывает намеренным)
  • Кросс-проверка sitemap ↔ robots: URL из sitemap (= «хочу в индекс»),
    но закрыт Disallow → противоречие, баг.

Правила разбираются для групп User-agent: * / Yandex / Googlebot по стандарту:
wildcard «*», якорь «$», выигрывает самое ДЛИННОЕ правило, при равенстве – Allow.
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
                continue                    # правило вне группы – игнор
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
    Выигрывает самое длинное правило; при равной длине – Allow."""
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


def _find_canonical(html: str):
    """href первого rel=canonical (или None)."""
    m = _CANONICAL_RE.search(html[:200_000])
    if not m:
        return None
    hm = _HREF_RE.search(m.group(0))
    return hm.group(1).strip() if hm else None


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
    """(значение X-Robots-Tag, есть ли noindex). headers – ключи в lower."""
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
    Закрыта в robots + noindex = согласовано (так задумано) – молчим.
    Закрыта в robots без noindex = тоже задумано (robots прав) – молчим;
    противоречия «в sitemap, но под Disallow» ловит отдельная кросс-проверка.
    Возвращает dict для CheckResult.indexing."""
    out = {
        'checked': True,
        'robots_status': robots.status if robots else None,
        'robots_disallowed': False, 'robots_rule': None, 'robots_agent': None,
        'meta_robots': None, 'meta_noindex': False,
        'x_robots': None, 'x_robots_noindex': False,
        'canonical': None, 'canonical_self': None, 'canonical_disallowed': False,
        'issues': [], 'warnings': [],
    }
    issues, warnings = out['issues'], out['warnings']

    # 1. robots.txt – ЭТАЛОН. Disallow сам по себе не ошибка: так задумано.
    closed_by_robots = False
    if robots is not None and robots.ok:
        dis, rule, agent = robots_verdict(robots, url)
        out['robots_disallowed'] = dis
        out['robots_rule'] = rule
        out['robots_agent'] = agent
        closed_by_robots = dis
    elif robots is not None and robots.fetched and robots.status != 200:
        warnings.append(f'robots.txt отдаёт {robots.status} – правила не применяются')
    elif robots is not None and not robots.fetched:
        warnings.append('robots.txt не удалось скачать')

    # 2. meta robots (в т.ч. name=yandex / name=googlebot) – сверяем с robots:
    # noindex на открытой в robots странице = расхождение = ошибка;
    # noindex на закрытой в robots = согласовано, молчим.
    if html:
        meta_val, meta_noidx = _find_meta_robots(html)
        out['meta_robots'] = meta_val
        out['meta_noindex'] = meta_noidx
        if meta_noidx and not closed_by_robots:
            issues.append('расхождение с robots.txt: в robots страница открыта, '
                          'но на ней noindex (meta robots)')

    # 3. X-Robots-Tag – так же сверяем с robots
    x_val, x_noidx = _x_robots_noindex(headers)
    out['x_robots'] = x_val
    out['x_robots_noindex'] = x_noidx
    if x_noidx and not closed_by_robots:
        issues.append('расхождение с robots.txt: в robots страница открыта, '
                      'но на ней noindex (заголовок X-Robots-Tag)')

    # 4. canonical
    if html:
        canon = _find_canonical(html)
        out['canonical'] = canon
        if canon:
            try:
                self_ref = _norm_url(canon) == _norm_url(url)
            except Exception:
                self_ref = None
            out['canonical_self'] = self_ref
            if self_ref is False:
                # Каноникл на закрытый robots'ом URL – расхождение:
                # канонизируем «в никуда»
                if robots is not None and robots.ok:
                    c_dis, _c_rule, _ = robots_verdict(robots, canon)
                    if c_dis:
                        out['canonical_disallowed'] = True
                        issues.append('расхождение с robots.txt: canonical '
                                      'ведёт на URL, закрытый в robots')
                if not out['canonical_disallowed']:
                    warnings.append('canonical ведёт на другой URL')

    out['verdict'] = 'closed' if issues else ('warn' if warnings else 'open')
    return out


# ── Кросс-проверка sitemap ↔ robots (весь каталог, не только выборка) ─


async def check_paths_against_robots(host: str, paths: list, *,
                                     proxy_url=None, limit: int = 300) -> dict:
    """Прогнать ВСЕ известные пути каталога (категории/фильтры/товары из
    sitemap и выгрузок) через robots.txt главного хоста.

    Пути в sitemap = «хочу в индекс»; Disallow на них = противоречие.
    Возвращает {'host', 'robots_status', 'sitemaps', 'checked',
                'disallowed': [{'path', 'rule', 'agent'}], 'error'}."""
    import aiohttp
    out = {'host': host, 'robots_status': None, 'sitemaps': [],
           'checked': 0, 'disallowed': [], 'error': None}
    try:
        async with aiohttp.ClientSession() as session:
            info = await fetch_robots(session, host, proxy_url=proxy_url)
    except Exception as e:
        out['error'] = str(e)
        return out
    out['robots_status'] = info.status
    out['sitemaps'] = info.sitemaps
    if not info.ok:
        out['error'] = info.error or f'robots.txt: HTTP {info.status}'
        return out
    seen = set()
    for p in paths or []:
        p = (p or '').strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out['checked'] += 1
        dis, rule, agent = robots_verdict(info, f'https://{host}{p}')
        if dis:
            out['disallowed'].append({'path': p, 'rule': rule, 'agent': agent})
            if len(out['disallowed']) >= limit:
                break
    return out
