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


def load_session_cookies(project_id: str, b64: str = None) -> dict:
    """Куки Яндекса: из переданного b64 (секрет autoclick_session_<pid>) или
    из cache/autoclick_session_<pid>.b64. {} если сессии нет."""
    if b64:
        return cookies_from_storage_state(b64)
    f = BASE / 'cache' / f'autoclick_session_{project_id}.b64'
    if f.is_file():
        return cookies_from_storage_state(f.read_text(encoding='utf-8').strip())
    return {}


def _session(cookies: dict, proxy_url: str = None):
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept-Language': 'ru'})
    s.cookies.update(cookies or {})
    if proxy_url:
        s.proxies.update({'http': proxy_url, 'https': proxy_url})
    return s


def _is_logged_in(s) -> bool:
    """Сессия жива и это нужный аккаунт (в куках есть yandex_login)."""
    return bool(s.cookies.get('yandex_login') or s.cookies.get('Session_id'))


def fetch_org_permalinks(s) -> list:
    """Все permalink/chain_permalink аккаунта из SSR страницы списка."""
    try:
        html = s.get(f'{CABINET}/companies/?no_redirect=1', timeout=30).text
    except Exception:
        return []
    return sorted(set(re.findall(r'"(?:chain_)?permalink"\s*:\s*(\d{6,})', html)))


_RE_REGION = re.compile(r'"region_code"\s*:\s*"([^"]+)"')
_RE_LOCALITY = re.compile(
    r'"kind"\s*:\s*"locality"[^}]*?"name"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"')
_RE_ADDR = re.compile(r'"formatted"\s*:\s*\{\s*"value"\s*:\s*"([^"]{3,120})"')
_RE_ORGNAME = re.compile(r'"company"[^{]*\{[^}]*?"name"[^}]*?"value"\s*:\s*"([^"]{2,80})"')


def fetch_org_card(s, permalink: str) -> dict:
    """Карточка организации: город (locality), region_code, адрес, имя.
    Для «сети» город/адрес = None (группа без единого города)."""
    try:
        html = s.get(f'{CABINET}/{permalink}/p/edit/main', timeout=30).text
    except Exception:
        html = ''
    m_city = _RE_LOCALITY.search(html)
    m_reg = _RE_REGION.search(html)
    m_addr = _RE_ADDR.search(html)
    m_name = _RE_ORGNAME.search(html)
    return {
        'permalink': permalink,
        'city': m_city.group(1) if m_city else None,
        'region': m_reg.group(1) if m_reg else None,
        'addr': m_addr.group(1) if m_addr else None,
        'name': m_name.group(1) if m_name else None,
    }


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
    if requests is None:
        return {'available': False, 'note': 'requests не установлен'}
    cookies = load_session_cookies(project_id, session_b64)
    if not cookies:
        return {'available': False,
                'note': 'Нет сессии Яндекса (autoclick_session_<pid>). '
                        'Экспортируйте сессию, как для автокликеров.'}
    s = _session(cookies, proxy_url)
    if not _is_logged_in(s):
        return {'available': False,
                'note': 'Сессия Яндекса без Session_id/yandex_login.'}
    perms = fetch_org_permalinks(s)
    if not perms:
        return {'available': False,
                'note': 'Список организаций не получен - сессия протухла '
                        'или аккаунт без организаций в Я.Бизнесе.'}
    _log(f'Я.Бизнес: организаций/сетей в аккаунте {len(perms)}')
    orgs = [fetch_org_card(s, p) for p in perms]
    res = check_subdomain_regions(orgs, project_id)
    res['available'] = True
    _log(f'Я.Бизнес: активных карточек {res["active_orgs"]}, '
         f'поддоменов с орг {len(res["matched"])}/{res["total_subdomains"]}')
    return res
