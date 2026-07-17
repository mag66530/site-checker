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
_RE_REGION = re.compile(r'"region_code"\s*:\s*"([^"]+)"')
_RE_LOCALITY = re.compile(
    r'"kind"\s*:\s*"locality"[^}]*?"name"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"')
_RE_ADDR = re.compile(r'"formatted"\s*:\s*\{\s*"value"\s*:\s*"([^"]{3,120})"')


def _org_card_from_html(html: str, permalink: str) -> dict:
    m_city = _RE_LOCALITY.search(html)
    m_reg = _RE_REGION.search(html)
    m_addr = _RE_ADDR.search(html)
    return {
        'permalink': permalink,
        'city': m_city.group(1) if m_city else None,
        'region': m_reg.group(1) if m_reg else None,
        'addr': m_addr.group(1) if m_addr else None,
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
            perms = sorted(set(_RE_PERMALINK.findall(html)))
            if not perms:
                return True, []
            orgs = []
            for p in perms:
                try:
                    page.goto(f'{CABINET}/{p}/p/edit/main',
                              wait_until='domcontentloaded', timeout=60000)
                    page.wait_for_timeout(1500)
                    orgs.append(_org_card_from_html(page.content(), p))
                except Exception:
                    orgs.append({'permalink': p, 'city': None,
                                 'region': None, 'addr': None})
            return True, orgs
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


def check_subdomain_regions(orgs: list, project_id: str) -> dict:
    """Сверка: у каждого поддомена есть орг под его городом. orgs - список
    карточек (fetch_org_card). Возвращает dict для отчёта."""
    active = [o for o in orgs if o.get('city')]
    org_by_city = {}
    for o in active:
        org_by_city.setdefault(_norm_city(o['city']), []).append(o)

    subs = _subdomains(project_id)
    matched, missing = [], []
    for url, city, country in subs:
        orgs_here = org_by_city.get(_norm_city(city))
        if orgs_here:
            matched.append({'url': url, 'city': city,
                            'org': orgs_here[0]})
        else:
            missing.append({'url': url, 'city': city, 'country': country})
    # Орги, чей город не совпал ни с одним поддоменом (лишние/чужие).
    sub_cities = {_norm_city(c) for _, c, _ in subs}
    orphan_orgs = [o for o in active if _norm_city(o['city']) not in sub_cities]

    return {
        'total_subdomains': len(subs),
        'total_orgs': len(orgs),
        'active_orgs': len(active),
        'chains_or_empty': len(orgs) - len(active),
        'matched': matched,
        'missing': missing,
        'orphan_orgs': orphan_orgs,
        'orgs': active,
    }


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
        logged_in, orgs = fetch_orgs(state, proxy_url=proxy_url, log=log)
    except Exception as e:
        return {'available': False, 'note': f'Я.Бизнес: {e}'}
    if not logged_in:
        return {'available': False,
                'note': 'Сессия Яндекса протухла (увело на passport) - '
                        'переэкспортируйте сессию.'}
    if orgs is None or len(orgs) == 0:
        return {'available': False,
                'note': 'Организаций в Я.Бизнесе аккаунта не найдено '
                        '(или сессия не под тем аккаунтом).'}
    _log(f'Я.Бизнес: организаций/сетей в аккаунте {len(orgs)}')
    res = check_subdomain_regions(orgs, project_id)
    res['available'] = True
    _log(f'Я.Бизнес: активных карточек {res["active_orgs"]}, '
         f'поддоменов с орг {len(res["matched"])}/{res["total_subdomains"]}')
    return res
