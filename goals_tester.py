"""
goals_tester.py - проверка ВСЕХ целей Яндекс.Метрики проекта (страница
«Проверка целей» в панели).

Эталон - каталог целей, выгруженный из Метрики («Конверсии»):
catalogs/goals-<проект>.json (номер, название, условие, тип). Движок открывает
страницы сайта в браузере, выполняет безопасные действия (клики по телефонам,
почте, соцсетям/мессенджерам, кнопкам открытия форм - БЕЗ отправки заявок) и
слушает запросы к Метрике: каждая сработавшая JS-цель шлёт hit вида
goal://<хост>/<идентификатор>.

Вердикты по типам целей:
  js         - Сработала / НЕ сработала (если ждали от кликов) / Нет автодействия;
               формные цели (отправка заявки) не дублируем заявкой - статус
               «Прогоном форм» + подтягиваем результат из последнего отчёта форм.
  url/url_re - открываем страницу: 200 + счётчик отправил визит → «Сработает».
  auto       - автоцель Метрики (клики tel/mailto, формы, файлы…) - фиксируется
               Метрикой автоматически, отдельная проверка не нужна (информируем).
  jivo       - события чата Jivo запускает оператор/посетитель - только вручную.
  composite  - составная, Метрика считает её из шагов - смотрим цели-шаги.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin

ROOT = Path(__file__).parent
CATALOGS = ROOT / 'catalogs'

# Метрика: hit сработавшей JS-цели
_RE_GOAL = re.compile(r"goal://[^/]+/([^&\s\"?#]+)")

# Безопасные «общие» клики на каждой странице (без отправки форм):
# телефоны, почта, мессенджеры и соцсети (клик фиксируется, переход гасим).
GENERIC_CLICK_SELECTORS = [
    "a[href^='tel:']",
    "a[href^='mailto:']",
    "a[href*='wa.me'], a[href*='api.whatsapp'], a[href*='whatsapp:']",
    "a[href*='t.me'], a[href*='tg://']",
    "a[href*='vk.com'], a[href*='vk.me']",
    "a[href*='ok.ru']",
    "a[href*='dzen.ru'], a[href*='zen.yandex']",
    "a[href*='rutube.ru']",
    "a[href*='max.ru'], a[href*='web.max']",
    "a[href*='yandex.ru/profile'], a[href*='yandex.ru/maps/org']",
]
MAX_PER_SELECTOR = 3     # кликаем не больше N элементов на селектор (шапка+подвал)

# План действий по проектам: какие страницы открыть и какие кнопки нажать
# (кнопки открытия модалок дают цели «*click»; сами формы НЕ отправляем).
# «ожидаемые» - идентификаторы js-целей, которые ДОЛЖНЫ сработать от этих
# действий: если такая не сработала - это красное «НЕ сработала». Цели без
# автодействия получают серый статус «Нет автодействия» (не шумим ложным красным).
ACTIONS = {
    'smu': {
        'страницы': [
            ('Главная',   'https://stalmetural.ru/',
             ['#call-back-form', '#txt-back-form', '#txt-back-form-footer',
              '#call-back-form-main, [class*="manager-connect"], a:has-text("Связаться с менеджером")',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Контакты',  'https://stalmetural.ru/contacts/', []),
            # breadcrumbphone - клик по номеру в «хлебных крошках» каталога
            ('Каталог',   'https://stalmetural.ru/catalog/',
             ['.breadcrumbs a[href^="tel:"], [class*="breadcrumb"] a[href^="tel:"]',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Товар',     'https://stalmetural.ru/catalog/izgotovlenie-pruzhin/1285453-izgotovlenie-pruzhin-rastyazheniya/',
             ['.one-click-to-buy', '#call-back-form-product',
              '.copy-btn:has(.an-ico-link-price)',
              '[class*="favorite"], [class*="favourite"], [class*="to-fav"], button:has(.an-ico-heart)',
              '[class*="share"], .an-ico-share, button:has-text("Поделиться")',
              'text=Добавить в корзину']),
            ('Доставка',  'https://stalmetural.ru/delivery/', ['#call-back-form-delivery']),
            # 404: несуществующий адрес - должна сработать цель 404error
            ('Страница 404', 'https://stalmetural.ru/nesuschestvuyushaya-404-xyz/', []),
        ],
        'ожидаемые': [
            'tel', 'email', 'clickwapp', 'clicktg', 'clickvk', 'clickmax',
            'click_vk_podval', 'click_ok_podval', 'click_tg_podval',
            'click_dzen_podval', 'click_rutube_podval', 'click_max_podval',
            'click_yandexorg_podval', 'breadcrumbphone',
            'callorderclick', 'zayavkaclick', 'svyazclick', 'oneclickbuy',
            'managerclick', 'morecatalog', 'gotomorecatalog', 'moreuslugi',
            'moreproizvodstvo', 'click_favorites', 'click_share', 'addocart',
            'tocart', '404error',
        ],
    },
    'imp': {
        'страницы': [
            ('Главная',   'https://inmetprom.ru/',
             ['[data-my-modal="#modal-callback"]', '.banner-fast-order__application']),
            ('Контакты',  'https://inmetprom.ru/contacts/', []),
            ('Листинг',   'https://inmetprom.ru/catalog/listovoj-prokat/', ['text=Быстрый заказ']),
            ('Товар',     'https://inmetprom.ru/list-gesti-0-2-mm-klass-1-gost-13345-85/', []),
        ],
        'ожидаемые': [
            'klik-na-tg-v-mobilke', 'klik-na-whatsapp-v-mobilke',
            'click-telephone-utf-gorod',
        ],
    },
    'mpe': {
        'страницы': [
            ('Главная',   'https://mepen.ru/',
             ['header.header-kostyl .bottom-header-right button.popup_form']),
            ('Контакты',  'https://mepen.ru/contacts/', []),
            ('Товар',     'https://mepen.ru/catalog/tovar/telezhka-tip-b-gcl/',
             ['text=Нужна консультация', 'text=Нашли дешевле']),
        ],
        'ожидаемые': ['tel', 'email', 'catalog'],
    },
}


def загрузить_каталог(pid: str) -> dict | None:
    f = CATALOGS / f'goals-{pid}.json'
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding='utf-8'))


def _формные_цели(pid: str) -> set[str]:
    """Идентификаторы целей, привязанных к ОТПРАВКЕ форм в конфиге форм-тестера
    (их не триггерим здесь, чтобы не слать заявки)."""
    p = ROOT / 'forms_tester' / 'projects' / pid / 'config.py'
    if not p.is_file():
        return set()
    txt = p.read_text(encoding='utf-8')
    return set(re.findall(r'"цель"\s*:\s*"([\w\-.]+)"', txt))


def _результаты_форм(pid: str) -> dict[str, str]:
    """Статусы целей из последнего отчёта форм (лист «Цели»): идентификатор → статус."""
    f = ROOT / 'cache' / 'forms' / pid / 'log_forms.xlsx'
    out: dict[str, str] = {}
    if not f.is_file():
        return out
    try:
        from openpyxl import load_workbook
        wb = load_workbook(f, data_only=True)
        if 'Цели' not in wb.sheetnames:
            return out
        ws = wb['Цели']
        hdr = [str(c.value or '').strip() for c in ws[1]]
        i_id = hdr.index('Цель (идентификатор)') if 'Цель (идентификатор)' in hdr else -1
        i_st = hdr.index('Статус') if 'Статус' in hdr else -1
        if i_id < 0 or i_st < 0:
            return out
        for row in ws.iter_rows(min_row=2, values_only=True):
            gid = str(row[i_id] or '').strip()
            st = str(row[i_st] or '').strip()
            if gid:
                out[gid] = st          # последняя строка по цели побеждает
    except Exception:
        pass
    return out


def _совпало(цель: dict, fired: set[str]) -> str | None:
    """Какой из сработавших идентификаторов закрывает цель (учитывая «содержит»)."""
    ids = цель.get('идентификаторы') or []
    for gid in ids:
        if цель.get('содержит'):
            for f in fired:
                if gid.lower() in f.lower():
                    return f
        elif gid in fired:
            return gid
    return None


def выполнить_прогон(pid: str, headless: bool = True, log=print, stop=None) -> dict:
    """Открывает страницы, кликает, слушает Метрику. Возвращает
    {'fired': set(id), 'страницы': [{'название','url','код','счётчик','визит'}]}."""
    план = ACTIONS.get(pid) or {'страницы': []}
    каталог = загрузить_каталог(pid) or {}
    counter = str(каталог.get('счётчик') or '')
    fired: set[str] = set()
    привязки: set[str] = set()       # reachGoal-идентификаторы в коде сайта
    визиты: dict[str, bool] = {}     # url → watch-hit отправлен
    страницы_инфо = []
    _re_reach = re.compile(r"reachGoal\W{1,4}([\w\-]+)")
    _seen_js: set[str] = set()

    def _собрать_привязки(html: str, base_url: str):
        """reachGoal(...) в HTML страницы и её же JS-файлах (кэш по URL)."""
        привязки.update(_re_reach.findall(html))
        try:
            import requests as _rq
            host = re.sub(r'^https?://', '', base_url).split('/')[0]
            for src in re.findall(r'<script[^>]+src="([^"]+)"', html)[:15]:
                u = src if src.startswith('http') else urljoin(base_url, src)
                if host not in u or u in _seen_js:
                    continue
                _seen_js.add(u)
                try:
                    js = _rq.get(u, timeout=15, headers={'User-Agent': 'Mozilla/5.0'},
                                 verify=os.environ.get('REQUESTS_CA_BUNDLE', True)).text
                    привязки.update(_re_reach.findall(js))
                except Exception:
                    continue
        except Exception:
            pass

    from playwright.sync_api import sync_playwright
    # Облачная среда (агентский прокси режет TLS браузера): гоняем трафик страницы
    # через сетевой стек драйвера (route.fetch). Локально флага нет - напрямую.
    _via_driver = bool(os.environ.get('CCR_AGENT_PROXY_ENABLED'))
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless,
                               args=["--disable-blink-features=AutomationControlled",
                                     "--no-sandbox"])
        ctx = b.new_context(locale='ru-RU', viewport={'width': 1440, 'height': 900},
                            ignore_https_errors=_via_driver)
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()

        текущий_url = {'u': ''}

        def _на_запрос(req):
            u = req.url
            if 'mc.yandex' in u or 'mc.webvisor' in u:
                m = _RE_GOAL.search(unquote(u))
                if m:
                    gid = m.group(1)
                    if gid not in fired:
                        fired.add(gid)
                        log(f"   🎯 цель: {gid}")
                if f'/watch/{counter}' in u:
                    визиты[текущий_url['u']] = True

        ctx.on('request', _на_запрос)
        # новые вкладки (клики по соцсетям с target=_blank) сразу закрываем
        ctx.on('page', lambda p: p != page and p.close())
        if _via_driver:
            def _route(route, request):
                try:
                    route.fulfill(response=route.fetch(timeout=40000))
                except Exception:
                    try:
                        route.abort()
                    except Exception:
                        pass
            ctx.route('**/*', _route)

        _всего = len(план['страницы'])
        for _idx, (название, url, клики) in enumerate(план['страницы'], 1):
            if stop and stop():
                log('⛔ Остановлено')
                break
            log(f"ПРОГРЕСС {_idx}/{_всего}")
            log(f"- Страница: {название}  {url}")
            текущий_url['u'] = url
            код = 0
            try:
                resp = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                код = resp.status if resp else 0
                page.wait_for_timeout(1500)
            except Exception as e:
                log(f"   ⚠️ не открылась: {e}")
                страницы_инфо.append({'название': название, 'url': url, 'код': код,
                                      'счётчик': False, 'визит': False})
                continue
            html = page.content()
            есть_счётчик = counter in html if counter else False
            _собрать_привязки(html, url)

            # прокрутка вниз (ленивые блоки + подвал с соцсетями)
            try:
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(1200)
            except Exception:
                pass

            # общие безопасные клики (переход гасим сразу возвратом)
            for sel in GENERIC_CLICK_SELECTORS:
                try:
                    els = page.locator(sel)
                    n = min(els.count(), MAX_PER_SELECTOR)
                    for i in range(n):
                        el = els.nth(i)
                        try:
                            el.scroll_into_view_if_needed(timeout=2000)
                            el.click(timeout=2500, no_wait_after=True)
                            page.wait_for_timeout(350)
                        except Exception:
                            continue
                        if page.url != url:      # утащило по ссылке - вернёмся
                            try:
                                page.go_back(wait_until='domcontentloaded',
                                             timeout=15000)
                                page.wait_for_timeout(800)
                            except Exception:
                                page.goto(url, wait_until='domcontentloaded',
                                          timeout=30000)
                except Exception:
                    continue

            # подвал: кликаем ТОЛЬКО соцсети/мессенджеры (открываются новой
            # вкладкой - её закрываем; на текущей странице ничего не ломается).
            # Внутренние ссылки не трогаем, чтобы не уходить со страницы.
            _soc = ("vk.com", "vk.me", "ok.ru", "t.me", "dzen.ru", "rutube.ru",
                    "max.ru", "wa.me", "whatsapp", "yandex.ru/maps",
                    "yandex.ru/profile")
            try:
                foot = page.locator("footer a[href], .footer a[href]")
                for i in range(min(foot.count(), 25)):
                    try:
                        el = foot.nth(i)
                        href = (el.get_attribute("href") or "").lower()
                        if not any(s in href for s in _soc):
                            continue
                        el.scroll_into_view_if_needed(timeout=1200)
                        el.click(timeout=1800, no_wait_after=True)
                        page.wait_for_timeout(200)
                    except Exception:
                        continue
            except Exception:
                pass

            # клики проекта (кнопки модалок форм и т.п.). Escape ДО клика снимает
            # модалку прошлого клика (перекрытие - главная причина «через раз»),
            # Escape ПОСЛЕ закрывает открытую. Без перезагрузок - быстро; если
            # элемента нет или клик не прошёл, просто идём дальше.
            for sel in клики:
                try:
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(150)
                    el = page.locator(sel).first
                    if el.count() == 0:
                        continue
                    el.scroll_into_view_if_needed(timeout=1500)
                    try:
                        el.click(timeout=2000)
                    except Exception:
                        el.click(timeout=2000, force=True)
                    page.wait_for_timeout(600)
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(200)
                except Exception:
                    pass
            page.wait_for_timeout(300)

            страницы_инфо.append({'название': название, 'url': url, 'код': код,
                                  'счётчик': есть_счётчик,
                                  'визит': визиты.get(url, False)})
        b.close()

    return {'fired': fired, 'страницы': страницы_инфо, 'привязки': привязки}


def _url_цели_проверка(каталог: dict, страницы_инфо: list) -> dict[str, dict]:
    """Для url-целей: найти открытую страницу, чей адрес содержит «url_часть»."""
    out = {}
    for g in каталог.get('цели', []):
        if g['тип'] not in ('url', 'url_re'):
            continue
        часть = g.get('url_часть') or ''
        hit = None
        for s in страницы_инфо:
            try:
                ok = (re.search(часть, s['url']) if g['тип'] == 'url_re'
                      else часть.lower() in s['url'].lower())
            except re.error:
                ok = False
            if ok:
                hit = s
                break
        out[g['номер']] = {'страница': hit}
    return out


def построить_отчёт(pid: str, каталог: dict, прогон: dict,
                    out_path: str | Path) -> Path:
    """Сводит каталог целей с результатами прогона в xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    fired = прогон['fired']
    страницы = прогон['страницы']
    привязки = {i.lower() for i in прогон.get('привязки', set())}
    формные = _формные_цели(pid)
    форм_статусы = _результаты_форм(pid)
    url_map = _url_цели_проверка(каталог, страницы)
    ожидаемые = {i.lower() for i in (ACTIONS.get(pid) or {}).get('ожидаемые', [])}

    def _привязана(g) -> str:
        ids = [i.lower() for i in (g.get('идентификаторы') or [])]
        if any(i in привязки for i in ids):
            return 'есть'
        if g.get('содержит') and any(any(i in b for b in привязки) for i in ids):
            return 'есть'
        return 'не найдена'

    wb = Workbook()
    ws = wb.active
    ws.title = 'Цели Метрики'
    headers = ['№ цели', 'Название', 'Условие (из Метрики)', 'Тип',
               'Как проверяем', 'Статус', 'Детали']
    fill = PatternFill('solid', fgColor='FFF3E0')
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = fill
        ws.column_dimensions[get_column_letter(c)].width = \
            {1: 12, 2: 44, 3: 52, 4: 11, 5: 22, 6: 18, 7: 56}[c]
    ws.freeze_panes = 'A2'

    GREEN, RED, GREY, BLUE = '1E8E3E', 'C62828', '757575', '1565C0'
    счёт = {'ok': 0, 'bad': 0, 'no_code': 0, 'forms': 0, 'manual': 0, 'info': 0}
    r = 2
    for g in каталог.get('цели', []):
        t = g['тип']
        способ = статус = детали = ''
        цвет = GREY
        if t == 'js':
            hit = _совпало(g, fired)
            в_формах = any(gid in формные for gid in (g.get('идентификаторы') or []))
            if hit:
                способ, статус, цвет = 'клики автотеста', 'Сработала', GREEN
                детали = f'зафиксирован идентификатор «{hit}»'
                счёт['ok'] += 1
            elif в_формах or any(gid.lower().endswith('form') or 'goal' in gid.lower()
                                 for gid in (g.get('идентификаторы') or [])):
                способ = 'прогоном форм'
                st = next((форм_статусы.get(gid) for gid in g.get('идентификаторы', [])
                           if gid in форм_статусы), '')
                if st:
                    статус = f'Формы: {st}'
                    цвет = GREEN if 'сработала' in st.lower() and not st.lower().startswith('не') else RED
                    счёт['ok' if цвет == GREEN else 'bad'] += 1
                else:
                    статус, цвет = 'Прогоном форм', BLUE
                    детали = 'цель отправки формы - проверяется страницей «Проверка форм»'
                    счёт['forms'] += 1
            elif _привязана(g) == 'не найдена':
                # цель есть в Метрике, но САЙТ её не отправляет (нет reachGoal в коде).
                # Это важнее «не сработала»: сколько ни кликай, она не сработает.
                способ, статус, цвет = 'проверка кода', 'Нет в коде сайта', RED
                детали = ('reachGoal этой цели НЕ найден в коде сайта - цель создана '
                          'в Метрике, но сайт её никогда не отправит (к разработчикам)')
                счёт['no_code'] += 1
            elif any(gid.lower() in ожидаемые for gid in (g.get('идентификаторы') or [])):
                способ, статус, цвет = 'клики автотеста', 'НЕ сработала', RED
                детали = ('действие выполнялось (клик по телефону/почте/кнопке), '
                          'но цель не зафиксирована - проверьте её настройку/привязку')
                счёт['bad'] += 1
            else:
                способ, статус, цвет = 'вручную', 'Нужно спец-действие', GREY
                детали = ('reachGoal в коде есть, но нужно особое действие (вход, '
                          'скачивание, избранное, купон, оформление заказа) - '
                          'проверяется вручную; можно добавить автодействие')
                счёт['manual'] += 1
        elif t in ('url', 'url_re'):
            способ = 'визит страницы'
            s = (url_map.get(g['номер']) or {}).get('страница')
            if s and s['код'] == 200 and s['счётчик']:
                статус, цвет = 'Сработает', GREEN
                детали = (f"страница {s['url']} открыта (200), счётчик установлен"
                          + (', визит отправлен' if s['визит'] else ''))
                счёт['ok'] += 1
            elif s:
                статус, цвет = 'Проблема', RED
                детали = f"страница {s['url']}: код {s['код']}, счётчик {'есть' if s['счётчик'] else 'НЕ найден'}"
                счёт['bad'] += 1
            else:
                статус, цвет = 'Нет автопроверки', GREY
                детали = f"условие «{g['условие']}» - страница не входит в прогон"
                счёт['manual'] += 1
        elif t == 'auto':
            способ, статус, цвет = 'автоцель Метрики', 'Авто', BLUE
            детали = ('срабатывает автоматически на действия посетителей '
                      '(Метрика фиксирует сама) - отдельная проверка не нужна')
            счёт['info'] += 1
        elif t == 'jivo':
            способ, статус, цвет = 'вручную', 'Вручную', GREY
            детали = 'события чата Jivo зависят от посетителя/оператора'
            счёт['manual'] += 1
        else:  # composite
            способ, статус, цвет = 'по шагам', 'Составная', BLUE
            детали = 'Метрика вычисляет из шагов - смотри цели-шаги выше/ниже'
            счёт['info'] += 1

        vals = [g['номер'], g['название'], g['условие'], t, способ, статус, детали]
        for c, v in enumerate(vals, 1):
            ws.cell(r, c, v)
        ws.cell(r, 6).font = Font(color=цвет, bold=True)
        r += 1

    # сводка на первом листе сверху не нужна - отдельный лист
    sm = wb.create_sheet('Сводка', 0)
    sm['A1'] = f"Проверка целей Метрики - {каталог.get('проект','')} (счётчик {каталог.get('счётчик','')})"
    sm['A1'].font = Font(bold=True, size=13)
    sm['A3'] = (f"Всего целей: {len(каталог.get('цели', []))} · сработало: {счёт['ok']} · "
                f"НЕ сработало: {счёт['bad']} · нет в коде сайта: {счёт['no_code']} · "
                f"через формы: {счёт['forms']} · нужно спец-действие/вручную: {счёт['manual']} · "
                f"авто/составные: {счёт['info']}")
    sm['A5'] = f"Дата прогона: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    sm['A7'] = 'Страницы прогона:'
    rr = 8
    for s in страницы:
        sm.cell(rr, 1, f"  {s['название']}: {s['url']} - код {s['код']}, "
                       f"счётчик {'✓' if s['счётчик'] else '✗'}")
        rr += 1
    rr += 1
    sm.cell(rr, 1, 'Сработавшие идентификаторы (' + str(len(fired)) + '): '
            + ', '.join(sorted(fired)))
    rr += 1
    sm.cell(rr, 1, 'reachGoal-привязки, найденные в коде сайта ('
            + str(len(привязки)) + '): ' + ', '.join(sorted(привязки)))
    sm.column_dimensions['A'].width = 120

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
