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
# SEO (поисковый) номер подменяется НЕ меткой в URL, а по РЕФЕРРЕРУ: скрипт
# коллтрекинга видит переход из органической выдачи и показывает поисковый
# номер (phone_seo). Поэтому SEO-визит эмулируем открытием главной БЕЗ метки,
# но с реферрером органического поиска Яндекса. Если у сайта органика привязана
# к другому реферреру (напр. google.com), значение можно поменять здесь.
SEO_REFERER = 'https://yandex.ru/search/?text=site'
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


def _probe(page, goto_url: str, target_nat: str, referer: str = None,
           timeout_ms: int = 30000) -> dict:
    """Открыть goto_url (опц. с реферрером), дождаться, пока в .ct_phone появится
    target_nat, вернуть {status, shown, kp}.
    status: replaced_ok | not_replaced | no_element | error."""
    try:
        if referer:
            page.goto(goto_url, referer=referer,
                      wait_until='domcontentloaded', timeout=timeout_ms)
        else:
            page.goto(goto_url, wait_until='domcontentloaded', timeout=timeout_ms)
    except Exception as e:  # noqa: BLE001
        return {'status': 'error', 'shown': [], 'kp': target_nat,
                'error': str(e)[:120]}
    # Опрашиваем .ct_phone, выходим сразу как только показался нужный номер.
    nums, elapsed = set(), 0
    while elapsed < SWAP_WAIT_MS:
        nums = _read_ct_numbers(page)
        if target_nat in nums:
            break
        page.wait_for_timeout(SWAP_STEP_MS)
        elapsed += SWAP_STEP_MS
    shown = sorted(nums)
    if not nums:
        return {'status': 'no_element', 'shown': shown, 'kp': target_nat}
    if target_nat in nums:
        return {'status': 'replaced_ok', 'shown': shown, 'kp': target_nat}
    return {'status': 'not_replaced', 'shown': shown, 'kp': target_nat}


def check_city(page, url: str, kp_ad_nat: str, timeout_ms: int = 30000) -> dict:
    """Рекламная подмена: открыть url с меткой ?utm_source=yandex, сверить
    показанный номер с рекламным phone_ad."""
    ad_url = url + ('&' if '?' in url else '?') + AD_PARAM
    return _probe(page, ad_url, kp_ad_nat, referer=None, timeout_ms=timeout_ms)


def check_city_seo(page, url: str, kp_seo_nat: str, timeout_ms: int = 30000) -> dict:
    """SEO-подмена: открыть главную БЕЗ метки, но с реферрером органической
    выдачи (SEO_REFERER), сверить показанный номер с поисковым phone_seo."""
    return _probe(page, url, kp_seo_nat, referer=SEO_REFERER, timeout_ms=timeout_ms)


def run(cities, log=print, show: bool = False, timeout_ms: int = 30000) -> list:
    """cities: [(city, main_url, phone_ad[, phone_seo])]. phone_seo необязателен.
    На КАЖДУЮ пробу (реклама/поиск) - свежий контекст (без cookie подмены).
    Возвращает список словарей: верхний уровень (status/shown/kp) - РЕКЛАМНАЯ
    подмена (обратная совместимость), плюс r['seo'] = {status, shown, kp} -
    поисковая подмена (если было с чем сверять: phone_seo задан и ≠ phone_ad)."""
    norm = []
    for it in (cities or []):
        c = it[0] if len(it) > 0 else ''
        u = it[1] if len(it) > 1 else ''
        na = _nat(it[2]) if len(it) > 2 else ''
        ns = _nat(it[3]) if len(it) > 3 else ''
        if u and (na or ns):
            norm.append((c, u, na, ns))
    if not norm:
        return []
    from playwright.sync_api import sync_playwright
    results = []
    log(f'☎ Замена номера: проверяю {len(norm)} город(ов) в браузере '
        f'(реклама - метка {AD_PARAM}; поиск - реферрер органики) …')
    _M = {'replaced_ok': '✅ работает', 'not_replaced': '❌ не работает',
          'no_element': '⚠ номер не найден', 'error': '⚠ ошибка',
          'na': '— номера в КП нет'}
    with sync_playwright() as pw:
        kw = dict(headless=not show,
                  args=['--disable-blink-features=AutomationControlled'])
        prx = _playwright_proxy_from_env()
        if prx:
            kw['proxy'] = prx
        b = pw.chromium.launch(**kw)

        def _one(probe, target):
            """Свежий контекст (без cookie предыдущей подмены) под одну пробу."""
            ctx = b.new_context(locale='ru-RU',
                                viewport={'width': 1366, 'height': 900})
            page = ctx.new_page()
            try:
                return probe(page)
            except Exception as e:  # noqa: BLE001
                return {'status': 'error', 'shown': [], 'kp': target,
                        'error': str(e)[:120]}
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            for city, url, kp_ad, kp_seo in norm:
                r = {'city': city, 'url': url,
                     'status': 'na', 'shown': [], 'kp': kp_ad}
                # Рекламная подмена (если в КП есть рекламный номер).
                if kp_ad:
                    ad = _one(lambda p: check_city(p, url, kp_ad, timeout_ms), kp_ad)
                    r.update(ad)
                    r['kp'] = kp_ad
                # SEO-подмена - отдельным свежим контекстом. Сверяем, только если
                # поисковый номер задан и отличается от рекламного (иначе нечего
                # отличать - тот же номер проверять второй раз бессмысленно).
                if kp_seo and kp_seo != kp_ad:
                    r['seo'] = _one(
                        lambda p: check_city_seo(p, url, kp_seo, timeout_ms), kp_seo)
                results.append(r)
                _line = f"  {city}: реклама {_M.get(r.get('status'), r.get('status'))}"
                if 'seo' in r:
                    _line += (f" · поиск "
                              f"{_M.get(r['seo'].get('status'), r['seo'].get('status'))}")
                log(_line + f" (на сайте {', '.join(r.get('shown') or []) or '–'})")
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
        if not row or not (row.phone_ad or row.phone_seo):
            continue
        if want and (row.city or '').lower() not in want and sub.city.lower() not in want:
            continue
        cities.append((row.city or sub.city, f'https://{sub.host}/',
                       row.phone_ad, row.phone_seo))
    res = run(cities, show=args.show)
    ok = sum(1 for r in res if r.get('status') == 'replaced_ok')
    bad = sum(1 for r in res if r.get('status') == 'not_replaced')
    seo_ok = sum(1 for r in res if (r.get('seo') or {}).get('status') == 'replaced_ok')
    seo_bad = sum(1 for r in res if (r.get('seo') or {}).get('status') == 'not_replaced')
    print(f'\nИтого рекл.: работает {ok}, не работает {bad}; '
          f'поиск: работает {seo_ok}, не работает {seo_bad}; всего {len(res)}')


if __name__ == '__main__':
    _main()
