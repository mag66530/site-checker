"""
calltracking_browser.py - браузерная проверка работы замены рекламного
номера (уровень 2). Пункт чек-листа «Проверка работы замены рекламного
номера (мониторинг)».

Открывает главную города в реальном браузере с рекламной меткой
(?utm_source=yandex → источник «Яндекс.Директ»), даёт скрипту коллтрекинга
(Sipuni) отработать - он подменяет номер в элементах .ct_phone - и читает
получившийся номер. Если он стал равен рекламному phone_ad из КП, подмена
РЕАЛЬНО работает; если остался прежним (SEO/общий) - подмена не сработала.

В отличие от статической проверки (calltracking_checker.py, по HTML) здесь
JS реально выполняется в браузере - это end-to-end проверка «замена
срабатывает», а не только «настроена в конфиге».

Использование как библиотека:
    from calltracking_browser import run
    results = run([(city, main_url, phone_ad), ...], log=print)

Автономно (локально, для проверки):
    python calltracking_browser.py smu --cities Москва,Санкт-Петербург
"""
import os
import re

# Источник рекламного номера: Яндекс.Директ (в конфиге Sipuni
# 'yadirect': {'utm_source': 'yandex'}). Google Ads - 'utm_source=google'.
AD_PARAM = 'utm_source=yandex'
CT_SELECTOR = '.ct_phone'
SWAP_WAIT_MS = 7000       # сколько ждём срабатывания подмены
SWAP_STEP_MS = 500


def _nat(num) -> str:
    """Национальный номер (10 цифр РФ/КЗ, 9 - BY/UZ) для сверки вне формата."""
    d = re.sub(r'\D', '', str(num or ''))
    if not d:
        return ''
    if d.startswith(('998', '375')) and len(d) >= 12:
        return d[-9:]
    if len(d) >= 11 and d[0] in '78':
        return d[-10:]
    if len(d) == 10:
        return d
    return d[-10:] if len(d) > 10 else d


def _playwright_proxy_from_env():
    """Тот же прокси, что и у прогона форм (сайты, режущие прямое подключение)."""
    from urllib.parse import urlparse, unquote
    raw = (os.environ.get('FORMS_PROXY') or os.environ.get('HTTP_PROXY') or '').strip()
    if not raw:
        return None
    pr = urlparse(raw if '://' in raw else 'http://' + raw)
    if not pr.hostname:
        return None
    server = f"{pr.scheme or 'http'}://{pr.hostname}" + (f":{pr.port}" if pr.port else '')
    conf = {'server': server}
    if pr.username:
        conf['username'] = unquote(pr.username)
    if pr.password:
        conf['password'] = unquote(pr.password)
    return conf


def _read_ct_numbers(page) -> set:
    """Нац. номера, показанные сейчас в .ct_phone (текст + tel:-href)."""
    try:
        raw = page.eval_on_selector_all(
            CT_SELECTOR,
            "els => els.map(e => (e.textContent||'') + '|' "
            "+ (e.getAttribute && e.getAttribute('href') || ''))")
    except Exception:
        return set()
    nums = set()
    for t in raw or []:
        for m in re.findall(r'\+?[78]?[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}', t):
            n = _nat(m)
            if n:
                nums.add(n)
    return nums


def check_city(page, url: str, kp_ad_nat: str, timeout_ms: int = 30000) -> dict:
    """Открыть url с рекламной меткой, дождаться подмены, сверить с phone_ad.
    status: replaced_ok | not_replaced | no_element | error."""
    ad_url = url + ('&' if '?' in url else '?') + AD_PARAM
    page.goto(ad_url, wait_until='domcontentloaded', timeout=timeout_ms)
    # Ждём срабатывания подмены: опрашиваем .ct_phone, выходим сразу как
    # только показался рекламный номер (или по таймауту).
    nums, elapsed = set(), 0
    while elapsed < SWAP_WAIT_MS:
        nums = _read_ct_numbers(page)
        if kp_ad_nat in nums:
            break
        page.wait_for_timeout(SWAP_STEP_MS)
        elapsed += SWAP_STEP_MS
    shown = sorted(nums)
    if not nums:
        return {'status': 'no_element', 'shown': shown, 'kp': kp_ad_nat}
    if kp_ad_nat in nums:
        return {'status': 'replaced_ok', 'shown': shown, 'kp': kp_ad_nat}
    return {'status': 'not_replaced', 'shown': shown, 'kp': kp_ad_nat}


def run(cities, log=print, show: bool = False, timeout_ms: int = 30000) -> list:
    """cities: [(city, main_url, phone_ad)]. По каждому городу - свежий
    контекст (без cookie подмены). Возвращает список результатов-словарей."""
    cities = [(c, u, a) for (c, u, a) in (cities or []) if u and _nat(a)]
    if not cities:
        return []
    from playwright.sync_api import sync_playwright
    results = []
    log(f'☎ Замена рекламного номера: проверяю {len(cities)} город(ов) '
        f'в браузере (метка {AD_PARAM}) …')
    with sync_playwright() as pw:
        kw = dict(headless=not show,
                  args=['--disable-blink-features=AutomationControlled'])
        prx = _playwright_proxy_from_env()
        if prx:
            kw['proxy'] = prx
        b = pw.chromium.launch(**kw)
        try:
            for city, url, kp_ad in cities:
                kp_nat = _nat(kp_ad)
                ctx = b.new_context(locale='ru-RU',
                                    viewport={'width': 1366, 'height': 900})
                page = ctx.new_page()
                try:
                    r = check_city(page, url, kp_nat, timeout_ms=timeout_ms)
                except Exception as e:  # noqa: BLE001
                    r = {'status': 'error', 'shown': [], 'kp': kp_nat,
                         'error': str(e)[:120]}
                r.update({'city': city, 'url': url})
                results.append(r)
                _m = {'replaced_ok': '✅ подменился на рекламный',
                      'not_replaced': '❌ НЕ подменился',
                      'no_element': '⚠ номер (.ct_phone) не найден',
                      'error': '⚠ ошибка'}.get(r['status'], r['status'])
                log(f"  {city}: {_m} (на сайте {', '.join(r['shown']) or '–'}, "
                    f"рекл. КП {kp_nat})")
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            b.close()
    return results


def _main():
    import argparse
    from sources import load_project_config, load_sources
    from kp import load_kp, normalize_phone  # noqa: F401
    ap = argparse.ArgumentParser(description='Браузерная проверка замены рекл. номера')
    ap.add_argument('project', help='id проекта (smu / imp / mpe / avia)')
    ap.add_argument('--cities', default='', help='города через запятую (пусто = все)')
    ap.add_argument('--show', action='store_true', help='видимый браузер')
    args = ap.parse_args()

    kp = load_kp(args.project)
    cfg = load_project_config(args.project)
    src = load_sources(cfg)
    want = {c.strip().lower() for c in args.cities.split(',') if c.strip()}
    cities = []
    for sub in src.subdomains:
        row = kp.get(sub.host) if kp else None
        if not row or not row.phone_ad:
            continue
        if want and (row.city or '').lower() not in want and sub.city.lower() not in want:
            continue
        cities.append((row.city or sub.city, f'https://{sub.host}/', row.phone_ad))
    res = run(cities, show=args.show)
    ok = sum(1 for r in res if r['status'] == 'replaced_ok')
    bad = sum(1 for r in res if r['status'] == 'not_replaced')
    print(f'\nИтого: подмена работает {ok}, НЕ работает {bad}, всего {len(res)}')


if __name__ == '__main__':
    _main()
