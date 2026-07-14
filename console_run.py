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
MENU_PROBE_PAGES = 3     # бургер-меню сквозное - пробуем на первых страницах

# Кандидаты кнопки мобильного меню (бургер).
_BURGER_SEL = ("[class*='burger'], [class*='hamburger'], .menu-toggle, "
               "[class*='menu-btn'], [class*='menu-button'], "
               "[class*='nav-toggle']")
# Десктоп: кнопка каталога/меню, открывающая панель по клику.
_DESKTOP_MENU_SEL = (_BURGER_SEL + ", [class*='catalog-btn'], "
                     "[class*='btn-catalog'], [class*='catalog-button'], "
                     "button:has-text('Каталог')")
# Пометить видимые крупные элементы (до открытия меню). Сначала снимаем
# метки ПРЕДЫДУЩЕЙ пробы: без этого проверка «осталась видимой» после
# формы смотрела на старую метку меню - вечный ложный not_closed.
_MARK_JS = """
() => {
  for (const el of document.querySelectorAll(
      '[data-mcp-seen], [data-mcp-menu]')) {
    el.removeAttribute('data-mcp-seen');
    el.removeAttribute('data-mcp-menu');
  }
  let n = 0;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width * r.height > 30000 && r.width > 100) {
      el.setAttribute('data-mcp-seen', '1'); n++;
    }
    if (n > 3000) break;
  }
}
"""
# Найти НОВЫЙ крупный видимый элемент (появившееся меню/модалка).
# mode='form': только блок С ФОРМОЙ не на весь экран (бокс модалки).
# mode='menu': только menu-подобные (nav/menu/drawer/offcanvas в классе или
# тег NAV) - иначе первым «новым» ловится lazy-карточка контента (ложняк).
_FIND_NEW_JS = """
(mode) => {
  const vw = document.documentElement.clientWidth;
  const mark = el => { el.setAttribute('data-mcp-menu', '1');
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height}; };
  let formFallback = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.hasAttribute('data-mcp-seen')) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width * r.height < 30000 || r.width < 100) continue;
    if (mode === 'form') {
      if (el.querySelector('form') && r.width < vw * 0.95) return mark(el);
      if (!formFallback) formFallback = el;
    } else if (mode === 'menu') {
      const isMenu = el.tagName === 'NAV'
          || /menu|nav|drawer|offcanvas|sidebar/i.test(el.className || '')
          || el.querySelector(':scope > nav, :scope nav');
      // Берём САМОГО КРУПНОГО кандидата (внешний контейнер меню):
      // внутренний nav меньше панели - клик «мимо nav» попадал бы в саму
      // открытую панель и давал ложный not_closed.
      if (isMenu && (!formFallback
          || r.width * r.height >
             formFallback.getBoundingClientRect().width *
             formFallback.getBoundingClientRect().height))
        formFallback = el;
    } else {
      return mark(el);
    }
  }
  if (mode === 'menu' && formFallback) return mark(formFallback);
  if (mode === 'form' && formFallback) {
    // Полноэкранный новый слой: возьмём вложенный блок с формой поуже.
    const inner = formFallback.querySelector('form');
    if (inner) {
      const box = inner.closest('div') || inner;
      const r = box.getBoundingClientRect();
      if (r.width < vw * 0.95) return mark(box);
    }
  }
  return null;
}
"""
_STILL_VISIBLE_JS = """
() => {
  const el = document.querySelector('[data-mcp-menu]');
  if (!el) return false;
  const st = getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') return false;
  const r = el.getBoundingClientRect();
  return r.width > 10 && r.height > 10;
}
"""


# Доступность + рендер картинок (десктоп 1440, один замер на страницу):
# контраст текста по WCAG (4.5:1 обычный, 3:1 крупный/жирный), битые
# картинки (naturalWidth=0) и искажённые пропорции (rendered vs natural
# расходятся >25% при object-fit: fill - «не соответствует дизайну»).
_A11Y_JS = """
() => {
  const lum = (r, g, b) => {
    const f = v => { v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const parse = c => { const m = (c || '').match(/\\d+(\\.\\d+)?/g);
    return m ? m.map(Number) : null; };
  let total = 0, low = 0; const ex = [];
  let i = 0;
  for (const el of document.querySelectorAll(
      'p, li, a, span, td, h1, h2, h3, button')) {
    if (++i > 800) break;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    let hasText = false;
    for (const n of el.childNodes)
      if (n.nodeType === 3 && n.textContent.trim().length > 3) {
        hasText = true; break;
      }
    if (!hasText) continue;
    let bg = null, p = el;
    for (let d = 0; d < 5 && p && p !== document.documentElement;
         d++, p = p.parentElement) {
      const a = parse(getComputedStyle(p).backgroundColor);
      if (a && (a.length < 4 || a[3] > 0.9)) { bg = a; break; }
    }
    // Фон не найден (картинка/градиент/глубже 5 уровней) - контраст
    // посчитать честно нельзя, пропускаем (белый дефолт давал ложняки
    // «белое на белом» у текста на тёмных фоновых картинках).
    if (!bg) continue;
    const fg = parse(st.color);
    if (!fg) continue;
    const L1 = lum(fg[0], fg[1], fg[2]), L2 = lum(bg[0], bg[1], bg[2]);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    total++;
    const fs = parseFloat(st.fontSize) || 14;
    const need = (fs >= 24 || (fs >= 18.66 && parseInt(st.fontWeight) >= 700))
        ? 3 : 4.5;
    if (ratio < need) {
      low++;
      if (ex.length < 3)
        ex.push(el.textContent.trim().slice(0, 30)
                + ' (' + ratio.toFixed(1) + ':1)');
    }
  }
  const imgBroken = [], imgDist = [];
  let ic = 0;
  for (const img of document.images) {
    if (++ic > 400) break;
    const r = img.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) continue;
    if (img.complete && img.naturalWidth === 0) {
      if (imgBroken.length < 5)
        imgBroken.push((img.currentSrc || img.src || '')
                       .split('/').pop().slice(0, 50));
      continue;
    }
    if (img.naturalWidth > 0 && img.naturalHeight > 0) {
      const fit = getComputedStyle(img).objectFit;
      if (fit === 'fill' || fit === 'none' || fit === '') {
        const nr = img.naturalWidth / img.naturalHeight;
        const rr = r.width / r.height;
        if (nr / rr > 1.25 || rr / nr > 1.25) {
          if (imgDist.length < 5)
            imgDist.push((img.currentSrc || img.src || '')
                         .split('/').pop().slice(0, 40));
        }
      }
    }
  }
  return {contrast_total: total, contrast_low: low, contrast_ex: ex,
          img_broken: imgBroken, img_distorted: imgDist};
}
"""


# Смоук слайдера: контейнер + стрелка «вперёд».
_SLIDER_SEL = ('.swiper, .slick-slider, .owl-carousel, [class*="carousel"], '
               '[class*="slider"]')
# Только ЯВНЫЕ классы стрелок: generic [class*="next"] ловил сам слайд
# (swiper-slide-next) - клик по нему ничего не листает, ложный fail.
_SLIDER_NEXT_SEL = ('.swiper-button-next, .slick-next, .owl-next, '
                    '[class*="arrow-next"], [class*="btn-next"], '
                    '[class*="button-next"]')
# Состояние слайдера: transform трека + активный слайд.
_SLIDER_STATE_JS = """
(root) => {
  const track = root.querySelector(
      '.swiper-wrapper, .slick-track, .owl-stage') || root;
  const act = root.querySelector(
      '[class*="active"]');
  return (getComputedStyle(track).transform || '') + '|' +
         (act ? act.className : '');
}
"""


def _slider_probe(page):
    """Слайдер листается по стрелке: 'ok' | 'fail' | None (нет слайдера/
    стрелки - молчим)."""
    try:
        root = page.query_selector(_SLIDER_SEL)
        if root is None or not root.is_visible():
            return None
        nxt = root.query_selector(_SLIDER_NEXT_SEL)
        if nxt is None or not nxt.is_visible():
            return None
        before = page.evaluate(_SLIDER_STATE_JS, root)
        nxt.click(timeout=3000)
        page.wait_for_timeout(900)              # анимация
        after = page.evaluate(_SLIDER_STATE_JS, root)
        return 'ok' if after != before else 'fail'
    except Exception:
        return None


def _dropdown_probe(page):
    """Выпадающее меню открывается по hover: 'ok' | 'fail' | None."""
    try:
        li = page.query_selector('header nav li:has(ul), nav li:has(ul), '
                                 'header li:has(ul)')
        if li is None or not li.is_visible():
            return None
        sub = li.query_selector('ul')
        if sub is None:
            return None
        if sub.is_visible():
            return 'ok'                         # раскрыто всегда - работает
        li.hover(timeout=3000)
        page.wait_for_timeout(600)
        return 'ok' if sub.is_visible() else 'fail'
    except Exception:
        return None


# Кнопки-триггеры модальной формы (обратный звонок/заявка). Текстовые -
# запасные (Playwright :has-text). Ложняков не даёт: проба засчитывает
# «модалку» только если появился блок С ФОРМОЙ (кнопка-скролл к инлайн-
# форме вернёт None - пропуск, не находка).
_FORM_TRIGGER_SEL = ('[class*="callback"], [data-fancybox], [data-modal], '
                     '[class*="popup-open"], [class*="open-popup"], '
                     '[class*="open-form"], [class*="callorder"], '
                     '[class*="call-order"], [class*="zayavka"], '
                     'button:has-text("звонок"), a:has-text("звонок"), '
                     'button:has-text("Заявка"), a:has-text("Заявка")')


def _close_on_outside(page, trigger, mode):
    """Общая проба «клик вне закрывает»: клик по триггеру -> появился
    блок (меню/модалка) -> клик СТРОГО вне блока -> блок должен скрыться.
    mode: 'menu' | 'form'. Возвращает 'ok' | 'not_closed' | None (не
    нашли/не поняли - молчим)."""
    def _esc():
        # Уборка: открытый блок засорял бы СЛЕДУЮЩУЮ пробу (перекрывает
        # триггеры). Escape закрывает большинство модалок/меню.
        try:
            page.keyboard.press('Escape')
            page.wait_for_timeout(300)
        except Exception:
            pass

    try:
        page.evaluate(_MARK_JS)
        trigger.click(timeout=3000)
        page.wait_for_timeout(700)
        box = page.evaluate(_FIND_NEW_JS, mode)
        if not box:
            _esc()
            return None
        vw0 = page.viewport_size['width']
        if box['w'] > vw0 * 0.7:
            # Блок (меню/модалка) шире 70% экрана - «кликнуть вне»
            # физически негде (на мобильном модалки часто fullscreen,
            # закрываются крестиком - это не нарушение). Молчим.
            _esc()
            return None
        # Точка вне блока: справа / слева / снизу, по вертикальному центру.
        # Верх не используем (шапка: клик в лого/бургер исказит результат).
        # Блок на весь экран - честно проверить нельзя, молчим.
        vw = page.viewport_size['width']
        vh = page.viewport_size['height']
        y_mid = min(box['y'] + box['h'] / 2, vh - 15)
        pt = None
        if box['x'] + box['w'] + 40 < vw:                 # справа свободно
            pt = (box['x'] + box['w'] + 25, y_mid)
        elif box['x'] > 40:                               # слева свободно
            pt = (box['x'] - 25, y_mid)
        elif box['y'] + box['h'] + 40 < vh:               # снизу свободно
            pt = (vw / 2, box['y'] + box['h'] + 25)
        if pt is None:
            _esc()
            return None
        page.mouse.click(int(pt[0]), int(pt[1]))
        page.wait_for_timeout(600)
        result = 'ok' if not page.evaluate(_STILL_VISIBLE_JS) else 'not_closed'
        if result == 'not_closed':
            _esc()
        return result
    except Exception:
        _esc()
        return None


def _menu_close_probe(page, selector=_BURGER_SEL):
    """Меню (бургер на мобильном / кнопка каталога на ПК) закрывается по
    клику вне области."""
    try:
        burger = page.query_selector(selector)
    except Exception:
        return None
    if burger is None or not burger.is_visible():
        return None
    return _close_on_outside(page, burger, mode='menu')


# Название открытой модалки: заголовок внутри бокса, иначе текст кнопки.
_MODAL_NAME_JS = """
() => {
  const el = document.querySelector('[data-mcp-menu]');
  if (!el) return '';
  const h = el.querySelector('h1, h2, h3, h4, legend, [class*="title"]');
  return h ? h.textContent.trim().slice(0, 60) : '';
}
"""


def _form_close_probe(page):
    """Модальная форма (звонок/заявка) закрывается по клику вне неё.
    Возвращает {'status': 'ok'|'not_closed', 'name': str} | None."""
    try:
        trig = page.query_selector(_FORM_TRIGGER_SEL)
        if trig is None or not trig.is_visible():
            return None
        trig_text = (trig.text_content() or '').strip()[:60]
    except Exception:
        return None
    status = _close_on_outside(page, trig, mode='form')
    if status is None:
        return None
    try:
        name = page.evaluate(_MODAL_NAME_JS) or trig_text or 'модальная форма'
    except Exception:
        name = trig_text or 'модальная форма'
    return {'status': status, 'name': name}

# Замер мобильной вёрстки: мелкий шрифт (<14px) у видимых текстовых
# элементов + горизонтальный overflow (контент шире экрана).
_MOBILE_JS = """
(checkFont) => {
  const vw = document.documentElement.clientWidth;
  const overflow = Math.max(
      0, document.documentElement.scrollWidth - vw);
  let total = 0, small = 0;
  const smallEx = [];
  // Тач-таргеты (только мобильный замер): кнопки/иконки меньше 44x44.
  // Инлайн-ссылки в тексте не считаем (WCAG-исключение) - иначе шум.
  let touchTotal = 0, touchSmall = 0;
  const touchEx = [];
  if (checkFont) {
    for (const el of document.querySelectorAll(
        'button, input[type=button], input[type=submit], a')) {
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      if (el.tagName === 'A') {
        // ссылка-«кнопка»/иконка: блочная или без текста; инлайн в тексте
        // пропускаем.
        const disp = st.display;
        const txt = (el.textContent || '').trim();
        const btnLike = /btn|button|icon/i.test(el.className || '');
        if (disp === 'inline' && txt.length > 1 && !btnLike) continue;
      }
      touchTotal++;
      if (r.width < 44 || r.height < 44) {
        touchSmall++;
        if (touchEx.length < 3) {
          const t = (el.textContent || el.className || el.tagName)
              .toString().trim().slice(0, 30);
          touchEx.push(t + ' (' + Math.round(r.width) + 'x'
                       + Math.round(r.height) + ')');
        }
      }
      if (touchTotal > 1500) break;
    }
  }
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
          small_examples: smallEx, wide, overlaps,
          touch_total: touchTotal, touch_small: touchSmall,
          touch_examples: touchEx};
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
            # Интерактив (десктоп, первые страницы): слайдер листается,
            # выпадающее меню открывается. До ресайзов - на 1440.
            ux = None
            if i <= MENU_PROBE_PAGES:
                ux = {'slider': _slider_probe(page),
                      'dropdown': _dropdown_probe(page)}
            # Доступность (контраст) + рендер картинок - на 1440, до ресайзов.
            a11y = None
            try:
                a11y = page.evaluate(_A11Y_JS)
            except Exception:
                a11y = None
            # Адаптивность: страница уже загружена - меняем ширину окна по
            # сетке 1440/768/390 и замеряем на каждой (без повторных визитов).
            mobile = None
            try:
                vps = {}
                for _w in VIEWPORT_GRID:
                    page.set_viewport_size({'width': _w, 'height': 900})
                    page.wait_for_timeout(500)      # reflow
                    vps[str(_w)] = page.evaluate(_MOBILE_JS, _w == MOBILE_W)
                mobile = dict(vps.get(str(MOBILE_W)) or {})
                mobile['viewports'] = vps
                # Бургер-проба (клик вне закрывает меню) - в самом конце:
                # клик мимо может увести со страницы, метрики уже сняты.
                # Меню сквозное - только первые страницы списка.
                # Пробы «клик вне закрывает» - И на мобильном, И на ПК.
                # В самом конце: клики могли бы исказить метрики выше.
                if i <= MENU_PROBE_PAGES:
                    mobile['menu_close'] = _menu_close_probe(page)   # моб.
                    page.wait_for_timeout(300)
                    mobile['form_close_m'] = _form_close_probe(page)  # моб.
                page.set_viewport_size({'width': 1440, 'height': 900})
                if i <= MENU_PROBE_PAGES:
                    page.wait_for_timeout(400)
                    mobile['menu_close_d'] = _menu_close_probe(
                        page, _DESKTOP_MENU_SEL)                     # ПК
                    page.wait_for_timeout(300)
                    mobile['form_close'] = _form_close_probe(page)   # ПК
            except Exception:
                mobile = None
            out['checked'] += 1
            out['pages'].append({'url': url, 'errors': uniq[:15],
                                 'mobile': mobile, 'ux': ux, 'a11y': a11y})
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
