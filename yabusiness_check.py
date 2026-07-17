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
import base64
import csv
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

# Из storage_state берём только куки Яндекса (для запросов к кабинету).
_YANDEX_COOKIE_DOMAINS = ('yandex.ru', '.yandex.ru', 'ya.ru')


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
_RE_REGION = re.compile(r'"region_code"\s*:\s*"([^"]+)"')
_RE_LOCALITY = re.compile(
    r'"kind"\s*:\s*"locality"[^}]*?"name"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"')
_RE_ADDR = re.compile(r'"formatted"\s*:\s*\{\s*"value"\s*:\s*"([^"]{3,120})"')

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
    m_city = _RE_LOCALITY.search(html)
    m_reg = _RE_REGION.search(html)
    m_addr = _RE_ADDR.search(html)
    profile = {key: bool(rx.search(html)) for key, _lbl, rx in PROFILE_FIELDS}
    # регион/город тоже часть «заполненности».
    profile['region'] = bool(m_reg)
    return {
        'permalink': permalink,
        'city': m_city.group(1) if m_city else None,
        'region': m_reg.group(1) if m_reg else None,
        'addr': m_addr.group(1) if m_addr else None,
        'profile': profile,
    }


def fetch_orgs(state: dict, proxy_url: str = None, log=None):
    """Через БРАУЗЕР (Playwright) с сессией: список организаций аккаунта с
    городом/регионом. requests не годится - кабинет Справочника уводит
    сессию в passport/auth/update (петля), браузер проходит её как при
    обычном заходе. Возвращает (logged_in: bool, orgs: list|None)."""
    from playwright.sync_api import sync_playwright
    ctx_kw = {'user_agent': UA}
    if proxy_url:
        ctx_kw['proxy'] = {'server': proxy_url}
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        try:
            ctx = br.new_context(storage_state=state, **ctx_kw)
            page = ctx.new_page()
            page.goto(f'{CABINET}/companies/?no_redirect=1',
                      wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(4000)
            # Ушли на паспорт/авторизацию - сессия протухла.
            if 'passport' in page.url or 'auth' in page.url.split('?')[0]:
                return False, None
            html = page.content()
            chains = set(_RE_CHAIN_PERMALINK.findall(html))
            plain = set(_RE_PLAIN_PERMALINK.findall(html))
            standalone = sorted(plain - chains)   # отдельные компании (не сети)
            if not (chains or standalone):
                return True, None
            # Карточки только по отдельным компаниям (у сети своего города нет).
            orgs = []
            for p in standalone:
                try:
                    page.goto(f'{CABINET}/{p}/p/edit/main',
                              wait_until='domcontentloaded', timeout=60000)
                    page.wait_for_timeout(1500)
                    orgs.append(_org_card_from_html(page.content(), p))
                except Exception:
                    orgs.append({'permalink': p, 'city': None, 'region': None,
                                 'addr': None, 'profile': {}})
            return True, {'chains': sorted(chains), 'companies': orgs}
        finally:
            br.close()


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
    """«Все филиалы объединены в Сеть». Активные компании с городом - это
    ОТДЕЛЬНЫЕ карточки (не члены сети): если такие есть - филиалы не
    объединены. OK, если отдельных активных компаний нет."""
    standalone = [o for o in companies if o.get('city')]
    ok = len(standalone) == 0
    return {
        'chains': len(chains),
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
