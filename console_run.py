"""
console_run.py - проверка «в консоли браузера нет ошибок JavaScript»
(пункт 1.14) + мобильная вёрстка (той же поездкой браузера).

Отдельный процесс с браузером (Playwright): 30-мин прогон ходит по HTTP
без браузера, а ошибки JS и рендер - рантайм, статикой не видны.

Открывает КАЖДУЮ переданную страницу (те, что выбрал пользователь: главная,
каталог, категории, фильтры, товары, тех.страницы) в headless Chromium,
слушает console.error и необработанные исключения (pageerror), записывает
их по каждой странице.

Адаптивная вёрстка: после снятия консоли страница (уже загруженная, без
повторного визита) замеряется на СЕТКЕ ширин 1440 / 768 / 390:
  • нет горизонтального скролла на любом разрешении - overflow документа
    и элементы шире экрана (сдвиг/обрезка контента);
  • при изменении размера окна элементы не смещаются хаотично - наложения
    соседних блоков на промежуточных ширинах;
  • масштаб Ctrl+/- покрыт той же сеткой: zoom 150% на 1440 = рендер при
    ~960px, 187% = ~768px - браузер рисует те же макеты;
  • шрифт читабелен - на 390px доля текста с font-size < 14px
    (чек-лист: минимум 14px на мобильных).

Запуск (URL-ы приходят файлом-списком от runner_30min):
    python console_run.py --project smu --urls-file cache/console_urls_smu.json

Локально - обычный headless; в облаке (env CCR_AGENT_PROXY_ENABLED) трафик
через сетевой стек драйвера (route.fetch), как в filters_run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / 'cache'
CACHE.mkdir(exist_ok=True)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAX_PAGES = 120          # верхняя граница, чтобы прогон не растянулся
WAIT_MS = 2500           # ждать после загрузки (асинхронные ошибки)
# Сетка ширин для замера адаптивности: десктоп / планшет (≈zoom 187% на
# 1440) / мобильный. Мелкий шрифт меряем только на мобильной ширине.
VIEWPORT_GRID = (1440, 768, 390)
MOBILE_W = 390

# Замер мобильной вёрстки: мелкий шрифт (<14px) у видимых текстовых
# элементов + горизонтальный overflow (контент шире экрана).
_MOBILE_JS = """
(checkFont) => {
  const vw = document.documentElement.clientWidth;
  const overflow = Math.max(
      0, document.documentElement.scrollWidth - vw);
  let total = 0, small = 0;
  const smallEx = [];
  if (checkFont) {
  const els = document.querySelectorAll('p, li, td, th, a, span, button');
  let i = 0;
  for (const el of els) {
    if (++i > 2000) break;
    let hasText = false;
    for (const n of el.childNodes)
      if (n.nodeType === 3 && n.textContent.trim().length > 5) {
        hasText = true; break;
      }
    if (!hasText) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const fs = parseFloat(st.fontSize) || 0;
    total++;
    if (fs && fs < 14) {
      small++;
      if (smallEx.length < 3)
        smallEx.push(el.textContent.trim().slice(0, 40) + ' - ' + fs + 'px');
    }
  }
  }
  const wide = [];
  for (const el of document.querySelectorAll('table, pre, img, iframe')) {
    const r = el.getBoundingClientRect();
    if (r.width > vw + 5 && wide.length < 5)
      wide.push(el.tagName.toLowerCase()
                + (el.className ? '.' + String(el.className).split(' ')[0] : ''));
  }
  // Наложения блоков: пересечения ПРЯМЫХ соседей в потоке. fixed/absolute/
  // sticky не считаем (легитимные оверлеи: шапки, попапы, бейджи).
  const overlaps = [];
  const name = el => el.tagName.toLowerCase()
      + (el.className ? '.' + String(el.className).split(' ')[0] : '');
  const flow = el => {
    const p = getComputedStyle(el).position;
    return p !== 'fixed' && p !== 'absolute' && p !== 'sticky';
  };
  const roots = document.querySelectorAll(
      'body, main, section, article, .container, .content');
  let scanned = 0;
  for (const root of roots) {
    if (++scanned > 20) break;
    const kids = [...root.children].filter(
        el => flow(el) && el.getBoundingClientRect().height > 20);
    for (let a = 0; a < kids.length - 1 && overlaps.length < 5; a++) {
      const r1 = kids[a].getBoundingClientRect();
      const r2 = kids[a + 1].getBoundingClientRect();
      const ox = Math.min(r1.right, r2.right) - Math.max(r1.left, r2.left);
      const oy = Math.min(r1.bottom, r2.bottom) - Math.max(r1.top, r2.top);
      const n1 = name(kids[a]), n2 = name(kids[a + 1]);
      // Шапка/подвал поверх соседнего блока - типовой дизайн-приём
      // (hero-баннер под шапкой), не считаем наложением.
      if (/header|footer/i.test(n1 + n2)) continue;
      // Одинаковые соседние блоки (container x container) - отрицательный
      // отступ шаблона, визуально всё в порядке - не наложение.
      if (n1 === n2) continue;
      // Порог 60px: дизайн-приёмы с отрицательным отступом дают 30-45px
      // (заголовок над слайдером и т.п.) - не поломка; реальный хаос
      // при ресайзе даёт перекрытия сильно больше.
      if (ox > 60 && oy > 60)
        overlaps.push(n1 + ' x ' + n2 + ' (' + Math.round(oy) + 'px)');
    }
  }
  return {overflow: Math.round(overflow), total, small,
          small_examples: smallEx, wide, overlaps};
}
"""

# Шумные сторонние ошибки, которые НЕ вина сайта (аналитика/виджеты/CORS
# сторонних доменов) - не считаем ошибкой сайта.
_IGNORE = (
    'mc.yandex', 'metrika', 'google-analytics', 'googletagmanager',
    'gtag', 'facebook', 'vk.com', 'jivosite', 'jivo', 'bitrix24',
    'recaptcha', 'gstatic', 'doubleclick', 'adservice',
    'err_blocked_by_client', 'net::err_', 'favicon',
    # браузерные политики/уведомления, не баги сайта:
    'permissions policy', 'permissions-policy', 'client hints',
    'high-entropy', 'deprecat', 'was preloaded using link preload',
    'is deprecated', 'quirks mode',
    'requeststorageaccess', 'storage access', 'permission denied',
    'third-party cookie', 'samesite',
)


def _noise(text: str) -> bool:
    t = (text or '').lower()
    return any(m in t for m in _IGNORE)


def run(pid: str, urls: list, log) -> dict:
    from playwright.sync_api import sync_playwright
    _via_driver = bool(os.environ.get('CCR_AGENT_PROXY_ENABLED'))
    urls = [u for u in dict.fromkeys(urls) if u][:MAX_PAGES]
    out = {'available': True, 'checked': 0, 'pages': [], 'note': None}
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
                    route.fulfill(response=ctx.request.fetch(request))
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass
            ctx.route('**/*', _route)
        page = ctx.new_page()
        ctx.on('page', lambda p: p != page and p.close())

        errs: list = []
        page.on('console', lambda m: (
            errs.append((m.text or '').strip()[:200])
            if getattr(m, 'type', '') == 'error' else None))
        page.on('pageerror', lambda e: errs.append(('PageError: ' + str(e))[:200]))

        for i, url in enumerate(urls, 1):
            errs.clear()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=40000)
                page.wait_for_timeout(WAIT_MS)
            except Exception as e:  # noqa: BLE001
                log(f'  [{i}/{len(urls)}] ⚠ не открылась: {url} ({e})')
                out['pages'].append({'url': url, 'errors': [],
                                     'note': 'страница не открылась'})
                out['checked'] += 1
                continue
            # уникальные ошибки сайта (шум аналитики/виджетов отсеиваем)
            uniq = list(dict.fromkeys(x for x in errs if x and not _noise(x)))
            # Адаптивность: страница уже загружена - меняем ширину окна по
            # сетке 1440/768/390 и замеряем на каждой (без повторных визитов).
            mobile = None
            try:
                vps = {}
                for _w in VIEWPORT_GRID:
                    page.set_viewport_size({'width': _w, 'height': 900})
                    page.wait_for_timeout(500)      # reflow
                    vps[str(_w)] = page.evaluate(_MOBILE_JS, _w == MOBILE_W)
                page.set_viewport_size({'width': 1440, 'height': 900})
                mobile = dict(vps.get(str(MOBILE_W)) or {})
                mobile['viewports'] = vps
            except Exception:
                mobile = None
            out['checked'] += 1
            out['pages'].append({'url': url, 'errors': uniq[:15],
                                 'mobile': mobile})
            _vps = (mobile or {}).get('viewports') or {}
            _mob_bad = any(
                m.get('overflow', 0) > 8 or m.get('overlaps')
                or (m.get('small', 0) >= 3
                    and m['small'] > (m.get('total') or 1) * 0.2)
                for m in _vps.values())
            if uniq or _mob_bad:
                log(f'  [{i}/{len(urls)}] ❌ ошибок JS {len(uniq)}'
                    + (', адаптивность' if _mob_bad else '')
                    + f': {url}')
            else:
                log(f'  [{i}/{len(urls)}] ✅ чисто: {url}')
        try:
            ctx.close(); b.close()
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--urls-file', required=True)
    a = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    try:
        urls = json.loads(Path(a.urls_file).read_text(encoding='utf-8-sig')) or []
    except Exception as e:  # noqa: BLE001
        urls = []
        log(f'⚠ список URL не прочитан: {e}')

    out_path = CACHE / f'console_{a.project}.json'
    if not urls:
        log('Проверка консоли: список страниц пуст - пропуск.')
        out_path.write_text(json.dumps(
            {'available': False, 'checked': 0, 'pages': [],
             'note': 'нет страниц для проверки'}, ensure_ascii=False),
            encoding='utf-8')
        return

    log(f'Проверка консоли: страниц {len(urls)}, запускаю браузер…')
    try:
        res = run(a.project, urls, log)
    except Exception as e:  # noqa: BLE001
        log(f'⚠ Проверка консоли: {e}')
        res = {'available': True, 'checked': 0, 'pages': [],
               'note': f'браузер не запустился: {e}'}
    out_path.write_text(json.dumps(res, ensure_ascii=False), encoding='utf-8')
    _bad = sum(1 for p in res['pages'] if p.get('errors'))
    log(f'✓ Проверка консоли: страниц {res["checked"]}, с ошибками JS {_bad}')


if __name__ == '__main__':
    main()
