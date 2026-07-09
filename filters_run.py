"""
filters_run.py - проверка, что функции фильтрации товаров работают корректно
(доп. чек-лист). Отдельный процесс: тяжёлый браузер (Playwright), поэтому
запускается по галочке, как автокликер, и пишет результат в JSON.

Почему пер-проектно: стандартной разметки смарт-фильтра у проектов нет
(виджеты кастомные/JS-рендерные), генерик-клик «вслепую» даёт ложные
красные. Поэтому селекторы фильтра задаются на проект в
catalogs/filters-<pid>.json:

    {
      "cases": [
        {
          "name": "Арматура - по длине",
          "category": "https://site.ru/catalog/armatura/",
          "card":   ".catalog-item",         // селектор карточки товара
          "filter": ".filter-block input[type=checkbox]",  // что кликнуть
          "apply":  ".filter-submit",         // кнопка «Показать» (null = AJAX)
          "wait_ms": 2500                      // ждать после применения
        }
      ]
    }

Нет файла/пустой cases → тест пропускается (в отчёте «селекторы не заданы»).

Логика на кейс: открыть категорию → счётчик карточек (база) → кликнуть
фильтр → применить/дождаться → счётчик снова. Вердикт:
  ok           - 0 < после < база (фильтр сузил выдачу);
  empty        - после = 0 или текст «ничего не найдено» = фильтр ломает выдачу;
  not_narrowed - после = база (фильтр не применился, дубль категории);
  http_error   - категория/выдача отдала 4xx/5xx;
  no_cards     - карточки не распознаны на базовой категории (проверить card);
  filter_absent- селектор фильтра не найден на странице.

Локально - обычный headless Chromium; в облаке (env CCR_AGENT_PROXY_ENABLED)
трафик идёт через сетевой стек драйвера (route.fetch). Логина не требует -
каталог публичный.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CATALOGS = ROOT / 'catalogs'
CACHE = ROOT / 'cache'
CACHE.mkdir(exist_ok=True)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_EMPTY_MARKERS = (
    'ничего не найдено', 'ничего не нашлось', 'товаров не найдено',
    'по вашему запросу ничего', 'список пуст', 'нет товаров',
    'товары не найдены', 'ничего не найдено по выбранным',
)

# Кандидаты селектора карточки, если в конфиге card не задан.
# Первыми - классы реальных проектов (SMU/IMP), потом общие.
_CARD_FALLBACK = ('.catalog-product-card-item', '.card-product',
                  '.catalog-item', '.product-item', '.catalog_item',
                  '.product-card', '.catalog_item_wrapp',
                  '[class*="product-item"]', '[class*="catalog-item"]',
                  '[data-entity="items-row-item"]')


def _config_path(pid: str) -> Path:
    return CATALOGS / f'filters-{pid}.json'


def load_cases(pid: str) -> list:
    p = _config_path(pid)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return list(data.get('cases') or [])
    except Exception as e:
        print(f'⚠ Конфиг {p.name} не читается: {e}')
        return []


def _count(page, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return -1


def _best_card_count(page, card: str | None) -> tuple[int, str]:
    """(число карточек, использованный селектор). Если card задан - он;
    иначе берём кандидата с наибольшим числом совпадений."""
    if card:
        return _count(page, card), card
    best_n, best_sel = 0, ''
    for sel in _CARD_FALLBACK:
        n = _count(page, sel)
        if n > best_n:
            best_n, best_sel = n, sel
    return best_n, best_sel


def _click(loc) -> str:
    """Клик по элементу: сначала обычный (force), при неудаче - JS-клик
    (el.click()). Смарт-фильтр Битрикса прячет чекбоксы/лейблы в свёрнутых
    дропдаунах (display:none) - обычный клик их не берёт, JS-клик берёт.
    Возвращает '' при успехе или текст ошибки."""
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        loc.click(timeout=5000, force=True)
        return ''
    except Exception as e1:
        try:
            loc.evaluate('el => el.click()')
            return ''
        except Exception as e2:
            return f'{e1} | JS: {e2}'


def _has_empty_text(page) -> bool:
    try:
        txt = (page.inner_text('body') or '').lower()
    except Exception:
        return False
    return any(m in txt for m in _EMPTY_MARKERS)


def run_case(page, case: dict, log) -> dict:
    name = case.get('name') or case.get('category') or 'фильтр'
    url = case.get('category') or ''
    card = case.get('card')
    filt = case.get('filter')
    apply_sel = case.get('apply')
    wait_ms = int(case.get('wait_ms') or 2500)
    out = {'name': name, 'category': url, 'verdict': None,
           'baseline': None, 'after': None, 'detail': ''}

    if not url or not filt:
        out['verdict'] = 'config_error'
        out['detail'] = 'в кейсе нет category или filter'
        return out

    # 1. Открыть категорию
    try:
        resp = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        status = resp.status if resp else None
    except Exception as e:
        out['verdict'] = 'http_error'
        out['detail'] = f'категория не открылась: {e}'
        return out
    if status and status >= 400:
        out['verdict'] = 'http_error'
        out['detail'] = f'категория отдала HTTP {status}'
        return out
    page.wait_for_timeout(1500)

    # 2. Счётчик карточек (база)
    baseline, used_sel = _best_card_count(page, card)
    out['baseline'] = baseline
    if baseline <= 0:
        out['verdict'] = 'no_cards'
        out['detail'] = (f'карточки не распознаны (селектор '
                         f'{card or "авто"}) - нечего сравнивать')
        return out

    # 3. Кликнуть фильтр
    try:
        loc = page.locator(filt).first
        if page.locator(filt).count() == 0:
            out['verdict'] = 'filter_absent'
            out['detail'] = f'селектор фильтра не найден: {filt}'
            return out
    except Exception as e:
        out['verdict'] = 'filter_absent'
        out['detail'] = f'селектор фильтра невалиден: {e}'
        return out
    _err = _click(loc)
    if _err:
        out['verdict'] = 'filter_absent'
        out['detail'] = f'по фильтру не удалось кликнуть: {_err}'
        return out

    # 4. Применить (кнопка) или дождаться AJAX.
    # Смарт-фильтр Битрикса после клика чекбокса делает AJAX-пересчёт и
    # только ПОТОМ кнопка «Показать» ведёт на /filter/.../ - жать её сразу
    # бесполезно (выдача не сузится). Ждём пересчёт перед применением.
    page.wait_for_timeout(int(case.get('pre_apply_ms') or 3000))

    nav_status = {'code': None}

    def _on_resp(r):
        try:
            if r.request.is_navigation_request() and r.frame == page.main_frame:
                nav_status['code'] = r.status
        except Exception:
            pass
    page.on('response', _on_resp)

    if apply_sel and page.locator(apply_sel).count():
        ap = page.locator(apply_sel).first
        # кнопка «Показать» навигирует на отфильтрованный URL - ждём переход
        try:
            with page.expect_navigation(timeout=wait_ms + 4000):
                _click(ap)
        except Exception:
            pass
    else:
        # авто-применение (AJAX по клику чекбокса, без кнопки) - просто ждём
        page.wait_for_timeout(wait_ms)
    page.wait_for_timeout(1200)
    try:
        page.wait_for_load_state('networkidle', timeout=8000)
    except Exception:
        pass

    if nav_status['code'] and nav_status['code'] >= 400:
        out['verdict'] = 'http_error'
        out['detail'] = f'после фильтра HTTP {nav_status["code"]}'
        return out

    # 5. Счётчик после фильтра
    after, _ = _best_card_count(page, card)
    out['after'] = after

    if after <= 0 or _has_empty_text(page):
        out['verdict'] = 'empty'
        out['detail'] = 'после фильтра нет товаров / «ничего не найдено»'
    elif after >= baseline:
        out['verdict'] = 'not_narrowed'
        out['detail'] = (f'выдача не сузилась (было {baseline}, стало {after}) '
                         f'- фильтр не применился')
    else:
        out['verdict'] = 'ok'
        out['detail'] = f'фильтр сузил: {baseline} → {after}'
    return out


def _launch_and_run(pid: str, cases: list, log) -> list:
    from playwright.sync_api import sync_playwright
    _via_driver = bool(os.environ.get('CCR_AGENT_PROXY_ENABLED'))
    results = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = b.new_context(
            locale='ru-RU', viewport={'width': 1440, 'height': 900},
            ignore_https_errors=_via_driver, user_agent=_UA,
            extra_http_headers={'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        if _via_driver:
            def _route(route, request):
                try:
                    r = ctx.request.fetch(request)
                    route.fulfill(response=r)
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass
            ctx.route('**/*', _route)
        page = ctx.new_page()
        ctx.on('page', lambda p: p != page and p.close())
        for i, case in enumerate(cases, 1):
            log(f'  [{i}/{len(cases)}] {case.get("name", "")}…')
            try:
                res = run_case(page, case, log)
            except Exception as e:
                res = {'name': case.get('name', ''),
                       'category': case.get('category', ''),
                       'verdict': 'http_error', 'detail': f'сбой: {e}',
                       'baseline': None, 'after': None}
            log(f'      → {res["verdict"]}: {res["detail"]}')
            results.append(res)
        try:
            ctx.close(); b.close()
        except Exception:
            pass
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    a = ap.parse_args()
    pid = a.project

    def log(msg):
        print(msg, flush=True)

    cases = load_cases(pid)
    out_path = CACHE / f'filters_{pid}.json'
    if not cases:
        log(f'Фильтр-тест: селекторы для «{pid}» не заданы '
            f'(catalogs/filters-{pid}.json) - пропуск.')
        out_path.write_text(json.dumps(
            {'available': False, 'cases': [],
             'note': f'Нет конфига catalogs/filters-{pid}.json - '
                     f'фильтр-тест пропущен.'}, ensure_ascii=False),
            encoding='utf-8')
        return

    log(f'Фильтр-тест: кейсов {len(cases)}, запускаю браузер…')
    try:
        results = _launch_and_run(pid, cases, log)
        payload = {'available': True, 'cases': results, 'note': None}
    except Exception as e:
        log(f'⚠ Фильтр-тест: {e}')
        payload = {'available': True, 'cases': [],
                   'note': f'Браузер не запустился: {e}'}
    out_path.write_text(json.dumps(payload, ensure_ascii=False),
                        encoding='utf-8')
    _ok = sum(1 for r in payload['cases'] if r.get('verdict') == 'ok')
    log(f'✓ Фильтр-тест: ok {_ok} из {len(payload["cases"])}')


if __name__ == '__main__':
    main()
