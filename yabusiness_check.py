# -*- coding: utf-8 -*-
"""
yabusiness_check.py - проверка Яндекс.Бизнеса (лист «Я.Бизнес/GMB»).

Пункт: «каждый поддомен зарегистрирован под свой регион». Данные тянем из
кабинета Я.Справочника (yandex.ru/sprav) на СЕССИИ (куки) - переиспользуем
ту же сессию Яндекса, что и автокликеры: секрет autoclick_session_<pid> /
cache/autoclick_session_<pid>.b64 (base64 Playwright storage_state).

Почему на сессии, а не OAuth: партнёрский Справочник API (sprav-api.yandex.ru,
scope sprav:all) требует активации партнёрского доступа Яндексом (заявка +
модерация); OAuth к кабинетному API не проходит (488). Сессия работает
сразу. Когда дадут партнёрский доступ - миграция на API без смены логики
сверки (см. check_subdomain_regions).

Пайплайн (проверено вживую):
  1. Список организаций аккаунта: SSR страницы yandex.ru/sprav/companies -
     все permalink/chain_permalink во встроенном JSON.
  2. По каждому permalink: SSR карточки /sprav/<permalink>/p/edit/main -
     город (locality), region_code, адрес. У «сетей» (chain) города нет -
     это группа, пропускаем (её компании - отдельные permalink'и).
  3. Сверка с catalogs/<pid>-subdomains.csv (город на поддомен): у каждого
     поддомена должна быть орг под его городом.
"""
import asyncio
import base64
import csv
import datetime
import json
import re
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

BASE = Path(__file__).parent
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36')
CABINET = 'https://yandex.ru/sprav'
MAPS = 'https://yandex.ru/maps/org'

# Отзывы: за сколько последних календарных месяцев требуем ≥1 отзыв.
REVIEWS_MONTHS = 3

# Из storage_state берём только куки Яндекса (для запросов к кабинету).
_YANDEX_COOKIE_DOMAINS = ('yandex.ru', '.yandex.ru', 'ya.ru')

# Разбор русской даты отзыва («9 апреля», «17 января 2025»).
_RU_MONTHS = [('январ', 1), ('феврал', 2), ('март', 3), ('апрел', 4),
              ('ма[йя]', 5), ('июн', 6), ('июл', 7), ('август', 8),
              ('сентябр', 9), ('октябр', 10), ('ноябр', 11), ('декабр', 12)]
_RU_MONTHS_NOM = ['', 'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
                  'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']


def _parse_review_date(text: str) -> str:
    """«9 апреля» / «17 января 2025» → 'YYYY-MM-DD'. Без года = текущий год
    (Яндекс год не показывает только для текущего). None, если не распознали."""
    m = re.search(r'(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?', (text or '').lower())
    if not m:
        return None
    day, word, year = int(m.group(1)), m.group(2), m.group(3)
    month = next((n for pat, n in _RU_MONTHS if re.match(pat, word)), None)
    if not month:
        return None
    y = int(year) if year else datetime.date.today().year
    try:
        return datetime.date(y, month, day).isoformat()
    except ValueError:
        return None


def _month_label(ym: tuple) -> str:
    y, m = ym
    return f'{_RU_MONTHS_NOM[m]} {y}'


def _norm_city(s: str) -> str:
    """Город для сравнения: нижний регистр, ё→е, без лишних пробелов."""
    return re.sub(r'\s+', ' ', (s or '').strip().lower().replace('ё', 'е'))


def cookies_from_storage_state(b64: str) -> dict:
    """Из base64 Playwright storage_state вернуть dict куки Яндекса."""
    try:
        data = json.loads(base64.b64decode(b64).decode('utf-8'))
    except Exception:
        return {}
    out = {}
    for c in data.get('cookies', []):
        dom = (c.get('domain') or '').lower()
        if any(dom.endswith(d) or dom == d for d in _YANDEX_COOKIE_DOMAINS):
            out[c.get('name')] = c.get('value')
    return out


def _storage_state(b64: str) -> dict:
    """Из base64 - storage_state для Playwright (только куки Яндекса,
    нормализованный sameSite). {} если не разобрать."""
    try:
        data = json.loads(base64.b64decode(b64).decode('utf-8'))
    except Exception:
        return {}
    cks = []
    for c in data.get('cookies', []):
        dom = (c.get('domain') or '').lower()
        if not any(dom.endswith(d) or dom == d for d in _YANDEX_COOKIE_DOMAINS):
            continue
        c = dict(c)
        if c.get('sameSite') not in ('Strict', 'Lax', 'None'):
            c['sameSite'] = 'Lax'
        cks.append(c)
    return {'cookies': cks, 'origins': []}


def load_session_state(project_id: str, b64: str = None) -> dict:
    """storage_state Яндекса: из b64 (секрет autoclick_session_<pid>) или
    cache/autoclick_session_<pid>.b64. {} если сессии нет."""
    if b64:
        return _storage_state(b64)
    f = BASE / 'cache' / f'autoclick_session_{project_id}.b64'
    if f.is_file():
        return _storage_state(f.read_text(encoding='utf-8').strip())
    return {}


# Совместимость: некоторые тесты берут только куки-dict.
def load_session_cookies(project_id: str, b64: str = None) -> dict:
    st = load_session_state(project_id, b64)
    return {c['name']: c['value'] for c in st.get('cookies', [])}


_RE_PERMALINK = re.compile(r'"(?:chain_)?permalink"\s*:\s*(\d{6,})')
_RE_CHAIN_PERMALINK = re.compile(r'"chain_permalink"\s*:\s*(\d{6,})')
_RE_PLAIN_PERMALINK = re.compile(r'"permalink"\s*:\s*(\d{6,})')
# Внутренний id сети в кабинете (роут /sprav/chain/<inner_id>/...) - отличается
# от chain_permalink. Нужен, чтобы открыть страницу состава сети (/branches).
_RE_CHAIN_INNER = re.compile(r'/sprav/chain/(\d+)')

# Предохранитель на пагинацию списка организаций (?page=N).
_MAX_LIST_PAGES = 80
_RE_REGION = re.compile(r'"region_code"\s*:\s*"([^"]+)"')
# Город (locality) из адреса карточки. ВАЖНО: между "kind":"locality" и его
# "name" у Яндекса бывают вложенные объекты ("translated_name":{...} и т.п.).
# Старый [^}]*? обрывался на первом же "}" такого объекта, НЕ доходил до
# своего name и хватал ЧУЖОЙ город из блока другой организации на странице
# (из-за этого карточки Москвы/СПб/… уезжали в «нет карточки»). Пропускаем
# любые символы, но ОГРАНИЧЕННО (в пределах своего компонента, ≤260 симв.) -
# чтобы пройти сквозь вложенность и НЕ перескочить на соседнюю организацию.
_RE_LOCALITY = re.compile(
    r'"kind"\s*:\s*"locality"[\s\S]{0,260}?"name"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"')
# Обратный порядок ключей (name раньше kind) - тоже валидный JSON.
_RE_LOCALITY_REV = re.compile(
    r'"name"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"[\s\S]{0,120}?"kind"\s*:\s*"locality"')
_RE_ADDR = re.compile(r'"formatted"\s*:\s*\{\s*"value"\s*:\s*"([^"]{3,120})"')

# Слова-признаки улицы/дома - чтобы фолбэком по адресу не принять их за город.
_RE_STREET_WORD = re.compile(
    r'улиц|проспект|переул|шоссе|бульвар|площад|набереж|\bнаб\b|\bпер\b|'
    r'\bдом\b|\bд\b|\bстр\b|микрорайон|\bмкр\b|тупик|проезд|аллея|\bкв\b',
    re.I)


def _city_from_addr(addr: str):
    """Город из отформатированного адреса «[Россия,] [область,] Город, улица…».
    Отсекаем ведущие страну/регион и берём первый «городской» сегмент.
    Надёжно, т.к. это СОБСТВЕННЫЙ адрес карточки (а не «первый locality на
    странице», который может оказаться чужим)."""
    if not addr:
        return None
    parts = [p.strip() for p in addr.split(',') if p.strip()]
    # Отбрасываем ведущие «Россия» и регион (область/край/округ/республика).
    while parts and (parts[0] in ('Россия', 'Российская Федерация')
                     or re.search(r'област|\bкра[йя]\b|округ|респ[уо]блик',
                                  parts[0], re.I)):
        parts.pop(0)
    if not parts:
        return None
    first = parts[0]
    if (2 <= len(first) <= 40
            and re.match(r'^[А-ЯЁ][А-Яа-яЁё\- ]+$', first)
            and not _RE_STREET_WORD.search(first)):
        return first
    return None

# Поля профиля Я.Бизнеса: (ключ находки, подпись, regex непустого наличия).
# Проверяем, что массив/поле НЕ пустое (есть хотя бы один элемент).
PROFILE_FIELDS = [
    ('phones', 'телефон', re.compile(r'"phones"\s*:\s*\[\s*\{')),
    ('emails', 'почта', re.compile(r'"emails"\s*:\s*\[\s*[\{"]')),
    ('hours', 'время работы',
     re.compile(r'"(?:base_)?work_intervals"\s*:\s*\[\s*\{')),
    ('social', 'соцсети/мессенджеры', re.compile(r'"accounts"\s*:\s*\[\s*\{')),
    ('photos', 'фото', re.compile(r'"photos"\s*:\s*\[\s*\{')),
    ('rubrics', 'рубрики',
     re.compile(r'"rubric(?:_id)?"\s*:\s*(?:\d+|\{)')),
    ('features', 'особенности', re.compile(r'"features"\s*:\s*\[\s*\{')),
]


def _org_card_from_html(html: str, permalink: str) -> dict:
    m_reg = _RE_REGION.search(html)
    m_addr = _RE_ADDR.search(html)
    addr = m_addr.group(1) if m_addr else None
    # Город. «Первый locality на странице» ненадёжен: у карточки в вёрстке
    # бывают чужие блоки (филиалы сети/похожие), и regex может схватить их
    # город. Поэтому locality ПОДТВЕРЖДАЕМ собственным адресом карточки:
    #   • locality есть И встречается в адресе  → берём locality (совпали);
    #   • иначе, если город выводится из адреса   → берём его (адрес - свой);
    #   • иначе                                    → что распозналось.
    m_city = _RE_LOCALITY.search(html) or _RE_LOCALITY_REV.search(html)
    loc_city = m_city.group(1) if m_city else None
    addr_city = _city_from_addr(addr)
    if loc_city and addr and _norm_city(loc_city) in _norm_city(addr):
        city = loc_city
    elif addr_city:
        city = addr_city
    else:
        city = loc_city
    profile = {key: bool(rx.search(html)) for key, _lbl, rx in PROFILE_FIELDS}
    # регион/город тоже часть «заполненности».
    profile['region'] = bool(m_reg)
    return {
        'permalink': permalink,
        'city': city,
        'region': m_reg.group(1) if m_reg else None,
        'addr': addr,
        'profile': profile,
    }


# Параллелизм тяжёлых фаз (карточки кабинета + отзывы Карт). Каждая задача -
# своя вкладка в общем контексте; семафор ограничивает одновременные.
_CARD_CONCURRENCY = 6
_REVIEW_CONCURRENCY = 6


def fetch_orgs(state: dict, proxy_url: str = None, log=None):
    """Sync-обёртка над async-реализацией (карточки и отзывы тянем параллельно
    - иначе сотни организаций у mpe идут ~20 мин). Возвращает
    (logged_in: bool, orgs: dict|None). Работает в отдельном процессе runner'а,
    где своего event-loop нет, поэтому asyncio.run безопасен."""
    return asyncio.run(_afetch_orgs(state, proxy_url, log))


async def _afetch_orgs(state: dict, proxy_url: str, log):
    """Через БРАУЗЕР (async Playwright) с сессией: полный список организаций
    аккаунта (все страницы + филиалы внутри сетей) с городом/регионом/профилем/
    отзывами. requests не годится - кабинет уводит сессию в passport/auth/update
    (петля), браузер проходит её как обычный заход."""
    from playwright.async_api import async_playwright
    ctx_kw = {'user_agent': UA}
    if proxy_url:
        ctx_kw['proxy'] = {'server': proxy_url}
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        try:
            ctx = await br.new_context(storage_state=state, **ctx_kw)
            page = await ctx.new_page()
            await page.goto(f'{CABINET}/companies/?no_redirect=1&page=1',
                            wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(4000)
            # Ушли на паспорт/авторизацию - сессия протухла.
            if 'passport' in page.url or 'auth' in page.url.split('?')[0]:
                return False, None
            # Список организаций ПАГИНИРОВАН (?page=N, по 10). Читать только
            # первую страницу - терять остальные (mpe: 15 страниц, 143 орг).
            # Идём по страницам, пока не встретим 2 подряд без новых записей.
            chains, plain, inner = set(), set(), set()
            empty = 0
            for pg in range(1, _MAX_LIST_PAGES + 1):
                if pg > 1:
                    await page.goto(
                        f'{CABINET}/companies/?no_redirect=1&page={pg}',
                        wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_timeout(1600)
                html = await page.content()
                pc = set(_RE_CHAIN_PERMALINK.findall(html))
                pp = set(_RE_PLAIN_PERMALINK.findall(html))
                pin = set(_RE_CHAIN_INNER.findall(html))
                new = (pp - plain) | (pc - chains) | (pin - inner)
                chains |= pc
                plain |= pp
                inner |= pin
                if new:
                    empty = 0
                else:
                    empty += 1
                    if empty >= 2:
                        break
            standalone = sorted(plain - chains)   # отдельные компании (не сети)
            inner_ids = sorted(inner)
            # Филиалы, спрятанные внутри сетей: permalink'и со страниц состава
            # (/sprav/chain/<inner>/branches). Без этого города в сетях выглядят
            # «без карточки».
            member_ids = set()
            for iid in inner_ids:
                member_ids |= await _acollect_chain_members(page, iid)
            member_ids -= chains         # сам permalink сети - не филиал
            member_ids -= set(standalone)
            if not (standalone or member_ids):
                return True, None
            _cards = ([(p, False) for p in standalone]
                      + [(p, True) for p in sorted(member_ids)])
            if log:
                log(f'Я.Бизнес: отдельных {len(standalone)}, сетей '
                    f'{len(inner_ids)}, филиалов в сетях {len(member_ids)}; '
                    f'тяну {len(_cards)} карточек (параллельно)…')
            await page.close()
            # КАРТОЧКИ - параллельно (город/регион/профиль).
            csem = asyncio.Semaphore(_CARD_CONCURRENCY)
            orgs = list(await asyncio.gather(
                *[_acard(ctx, csem, p, ic) for p, ic in _cards]))
            if log:
                active = [o for o in orgs if o.get('city')]
                _no_city = len(orgs) - len(active)
                _sample = ', '.join(sorted({o['city'] for o in active})[:12])
                log(f'Я.Бизнес: карточек {len(orgs)}, активных {len(active)}, '
                    f'без города {_no_city}. Города: {_sample}'
                    + (' …' if len(active) > 12 else ''))
            return True, {'chains': sorted(chains), 'companies': orgs}
        finally:
            await br.close()


async def _acollect_chain_members(page, inner_id: str) -> set:
    """Permalink'и филиалов внутри сети со страницы её состава
    (/sprav/chain/<inner_id>/branches). Список виртуализированный (react-window)
    - собираем ссылки, прокручивая, пока их число не перестанет расти."""
    try:
        await page.goto(f'{CABINET}/chain/{inner_id}/branches',
                        wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(3000)
    except Exception:
        return set()
    perms, last, stable = set(), -1, 0
    for _ in range(50):
        for a in await page.query_selector_all('a[href*="/sprav/"]'):
            m = re.search(r'/sprav/(\d{6,})', await a.get_attribute('href') or '')
            if m:
                perms.add(m.group(1))
        try:
            await page.mouse.wheel(0, 2500)
        except Exception:
            pass
        await page.wait_for_timeout(450)
        stable = stable + 1 if len(perms) == last else 0
        last = len(perms)
        if stable >= 6:
            break
    return perms


async def _acard(ctx, sem, permalink: str, in_chain: bool) -> dict:
    """Карточка организации /p/edit/main → город/регион/профиль. Своя вкладка."""
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(f'{CABINET}/{permalink}/p/edit/main',
                            wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(1200)
            card = _org_card_from_html(await page.content(), permalink)
        except Exception:
            card = {'permalink': permalink, 'city': None, 'region': None,
                    'addr': None, 'profile': {}}
        finally:
            try:
                await page.close()
            except Exception:
                pass
        card['in_chain'] = in_chain
        return card


async def _areviews(ctx, sem, permalink: str):
    """Даты отзывов с публичной карточки Карт /maps/org/<perm>/reviews/.
    Своя вкладка. → (permalink, ['YYYY-MM-DD', ...])."""
    async with sem:
        page = await ctx.new_page()
        dates = []
        try:
            await page.goto(f'{MAPS}/{permalink}/reviews/',
                            wait_until='networkidle', timeout=45000)
            await page.wait_for_timeout(2000)
            prev = -1
            for _ in range(10):
                nodes = await page.query_selector_all(
                    '.business-review-view__date')
                if len(nodes) == prev:
                    break
                prev = len(nodes)
                if nodes:
                    try:
                        await nodes[-1].scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                await page.wait_for_timeout(900)
            for n in await page.query_selector_all(
                    '.business-review-view__date'):
                d = _parse_review_date((await n.inner_text() or '').strip())
                if d:
                    dates.append(d)
        except Exception:
            dates = []
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return permalink, dates


def _subdomains(project_id: str) -> list:
    """[(url, city, country)] из catalogs/<pid>-subdomains.csv."""
    f = BASE / 'catalogs' / f'{project_id}-subdomains.csv'
    out = []
    if not f.is_file():
        return out
    with open(f, encoding='utf-8-sig', newline='') as fh:
        for row in csv.DictReader(fh):
            if row.get('url'):
                out.append((row['url'].strip(), (row.get('city') or '').strip(),
                            (row.get('country') or '').strip()))
    return out


def check_subdomain_regions(companies: list, project_id: str) -> dict:
    """Сверка: у каждого поддомена есть орг под его городом."""
    active = [o for o in companies if o.get('city')]
    org_by_city = {}
    for o in active:
        org_by_city.setdefault(_norm_city(o['city']), []).append(o)

    subs = _subdomains(project_id)
    matched, missing = [], []
    for url, city, country in subs:
        orgs_here = org_by_city.get(_norm_city(city))
        if orgs_here:
            matched.append({'url': url, 'city': city, 'org': orgs_here[0]})
        else:
            missing.append({'url': url, 'city': city, 'country': country})
    sub_cities = {_norm_city(c) for _, c, _ in subs}
    orphan_orgs = [o for o in active if _norm_city(o['city']) not in sub_cities]

    return {
        'total_subdomains': len(subs),
        'active_orgs': len(active),
        'matched': matched,
        'missing': missing,
        'orphan_orgs': orphan_orgs,
        'orgs': active,
    }


def check_chain(chains: list, companies: list) -> dict:
    """«Все филиалы объединены в Сеть». Филиал внутри сети помечен in_chain.
    ОТДЕЛЬНЫЕ карточки (city есть, но in_chain=False) - не сведены в сеть:
    если такие есть - филиалы объединены не полностью. OK, если отдельных
    активных компаний нет (все филиалы - внутри сетей)."""
    standalone = [o for o in companies if o.get('city') and not o.get('in_chain')]
    in_chain = [o for o in companies if o.get('city') and o.get('in_chain')]
    ok = len(standalone) == 0
    return {
        'chains': len(chains),
        'chain_members': len(in_chain),
        'standalone_companies': len(standalone),
        'united': ok,
        'standalone_list': [{'permalink': o['permalink'], 'city': o.get('city')}
                            for o in standalone],
    }


def check_profile(companies: list) -> dict:
    """«Максимально заполнен профиль». По каждой активной компании - какие
    поля профиля не заполнены (телефон/почта/часы/соцсети/фото/рубрики/
    особенности/регион)."""
    labels = {k: lbl for k, lbl, _ in PROFILE_FIELDS}
    labels['region'] = 'регион'
    order = [k for k, _, _ in PROFILE_FIELDS] + ['region']
    per_org, all_full = [], True
    for o in companies:
        if not o.get('city'):
            continue
        prof = o.get('profile') or {}
        missing = [labels[k] for k in order if not prof.get(k)]
        if missing:
            all_full = False
        per_org.append({'permalink': o['permalink'], 'city': o.get('city'),
                        'missing': missing,
                        'filled': len(order) - len(missing), 'total': len(order)})
    return {'all_full': all_full, 'orgs': per_org}


def check_reviews(companies: list, months: int = REVIEWS_MONTHS) -> dict:
    """«Закупаются отзывы на важные филиалы (≥1 в месяц)». Важные филиалы =
    все активные орг с городом. Требуем: за каждый из последних `months`
    календарных месяцев ≥1 отзыв. Провал у орг, где хоть один месяц пуст."""
    today = datetime.date.today()
    targets, y, m = [], today.year, today.month
    for _ in range(months):
        targets.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    per_org, all_ok = [], True
    for o in companies:
        if not o.get('city'):
            continue
        dates = []
        for d in (o.get('review_dates') or []):
            try:
                dates.append(datetime.date.fromisoformat(d))
            except (ValueError, TypeError):
                pass
        covered = {(d.year, d.month) for d in dates}
        missing = [ym for ym in targets if ym not in covered]
        ok = not missing
        if not ok:
            all_ok = False
        per_org.append({
            'permalink': o['permalink'], 'city': o.get('city'),
            'total_reviews': len(dates),
            'last_review': max(dates).isoformat() if dates else None,
            'missing_months': [_month_label(ym) for ym in missing],
            'ok': ok,
        })
    return {'months': months, 'all_ok': all_ok, 'orgs': per_org}


def run(project_id: str, session_b64: str = None, proxy_url: str = None,
        log=None) -> dict:
    """Полная проверка Я.Бизнеса на сессии. Возвращает dict для отчёта
    (лист «Я.Бизнес и GMB») или {'available': False, ...}."""
    def _log(m):
        if log:
            log(m)
    state = load_session_state(project_id, session_b64)
    if not state.get('cookies'):
        return {'available': False,
                'note': 'Нет сессии Яндекса (autoclick_session_<pid>). '
                        'Экспортируйте сессию, как для автокликеров.'}
    try:
        logged_in, data = fetch_orgs(state, proxy_url=proxy_url, log=log)
    except Exception as e:
        return {'available': False, 'note': f'Я.Бизнес: {e}'}
    if not logged_in:
        return {'available': False,
                'note': 'Сессия Яндекса протухла (увело на passport) - '
                        'переэкспортируйте сессию.'}
    if not data:
        return {'available': False,
                'note': 'Организаций в Я.Бизнесе аккаунта не найдено '
                        '(или сессия не под тем аккаунтом).'}
    chains = data.get('chains') or []
    companies = data.get('companies') or []
    _log(f'Я.Бизнес: сетей {len(chains)}, отдельных компаний {len(companies)}')
    res = check_subdomain_regions(companies, project_id)
    res['chains_or_empty'] = len(chains)
    res['total_orgs'] = len(chains) + len(companies)
    res['chain_check'] = check_chain(chains, companies)
    res['profile_check'] = check_profile(companies)
    res['available'] = True
    _log(f'Я.Бизнес: активных карточек {res["active_orgs"]}, поддоменов с орг '
         f'{len(res["matched"])}/{res["total_subdomains"]}; в сеть '
         f'объединены: {res["chain_check"]["united"]}; профиль полон: '
         f'{res["profile_check"]["all_full"]}')
    return res
