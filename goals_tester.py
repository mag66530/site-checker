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


def _план_для_домена(домен: str) -> dict:
    """Универсальный план прогона для сайта на той же платформе, что СМУ РФ
    (СНГ-домены stalmetural.*, smg.az и т.п.): те же кнопки и ожидаемые цели,
    только другой домен. Товар/доставку не трогаем - их адреса у стран разные."""
    d = (домен or '').rstrip('/')
    if not d:
        return {'страницы': []}
    return {
        'страницы': [
            ('Главная', d + '/',
             ['#call-back-form', '#txt-back-form', '#txt-back-form-footer',
              '#call-back-form-main, [class*="manager-connect"], a:has-text("Связаться с менеджером")',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Контакты', d + '/contacts/', []),
            ('Каталог', d + '/catalog/',
             ['.breadcrumbs a[href^="tel:"], [class*="breadcrumb"] a[href^="tel:"]',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Страница 404', d + '/nesuschestvuyushaya-404-xyz/', []),
        ],
        'ожидаемые': ACTIONS['smu']['ожидаемые'],
    }


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
    каталог = загрузить_каталог(pid) or {}
    # Явный план проекта; для суб-проектов (страны СМУ) - универсальный по домену.
    план = ACTIONS.get(pid) or _план_для_домена(каталог.get('домен', ''))
    counter = str(каталог.get('счётчик') or '')
    fired: set[str] = set()
    привязки: set[str] = set()       # reachGoal-идентификаторы, найденные явно
    визиты: dict[str, bool] = {}     # url → watch-hit отправлен
    страницы_инфо = []
    _re_reach = re.compile(r"reachGoal\W{1,4}([\w\-]+)")
    _seen_js: set[str] = set()
    _код_части: list[str] = []        # весь код страниц + их JS (для поиска целей)

    def _собрать_привязки(html: str, base_url: str):
        """Копит reachGoal-идентификаторы И ВЕСЬ код (HTML + все JS того же хоста),
        чтобы потом надёжно проверить, упоминается ли цель в коде сайта вообще
        (как строковый литерал), а не только сразу после reachGoal(."""
        привязки.update(_re_reach.findall(html))
        _код_части.append(html.lower())
        try:
            import requests as _rq
            host = re.sub(r'^https?://', '', base_url).split('/')[0].split(':')[0]
            base_host = '.'.join(host.split('.')[-2:])   # stalmetural.ru
            srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
            for src in srcs[:40]:
                u = src if src.startswith('http') else urljoin(base_url, src)
                # берём JS с того же домена/поддоменов (там и живут reachGoal)
                if base_host not in u or u in _seen_js:
                    continue
                _seen_js.add(u)
                try:
                    js = _rq.get(u, timeout=15, headers={'User-Agent': 'Mozilla/5.0'},
                                 verify=os.environ.get('REQUESTS_CA_BUNDLE', True)).text
                    привязки.update(_re_reach.findall(js))
                    _код_части.append(js.lower())
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

    return {'fired': fired, 'страницы': страницы_инфо, 'привязки': привязки,
            'код': ''.join(_код_части)}


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


# Цели, которым нужно ОСОБОЕ действие (его автотест намеренно не делает: это не
# «нет в коде», а «нужен ручной шаг / отдельная настройка автотеста»). Ключи -
# подстроки в названии/условии/идентификаторе цели → человеческое описание.
_СПЕЦ_ДЕЙСТВИЯ = [
    (('вход', 'авториз', 'логин', 'login', 'акка', 'регистрац', 'registr'),
     'вход или регистрация в личном кабинете'),
    (('избранн', 'favorit', 'wishlist', 'сравн', 'sravn'),
     'добавление товара в избранное/сравнение'),
    (('купон', 'kupon', 'скидочн', 'промокод', 'promo'),
     'применение купона или промокода'),
    (('скачив', 'скачать', 'download', 'реквизит', 'rekvizit'),
     'скачивание файла или реквизитов'),
    (('смотреть', 'smotretvse', 'показать', 'показат', 'ещё', 'eshe', 'load', 'pagina'),
     'просмотр/подгрузка каталога (пагинация, «смотреть всё»)'),
    (('оплат', 'оформ', 'oformit', 'checkout'),
     'оформление или оплата заказа'),
]


def _нужно_спец_действие(g: dict) -> str | None:
    """Если цель завязана на особое действие (вход, оплата, купон, избранное,
    скачивание, пагинация) - вернуть человеческое описание, иначе None."""
    text = ((g.get('название', '') + ' ' + g.get('условие', '') + ' '
             + ' '.join(g.get('идентификаторы') or [])).lower())
    for keys, label in _СПЕЦ_ДЕЙСТВИЯ:
        if any(k in text for k in keys):
            return label
    return None


def построить_отчёт(pid: str, каталог: dict, прогон: dict,
                    out_path: str | Path) -> Path:
    """Сводит каталог целей с результатами прогона в xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    import re as _re2
    fired = прогон['fired']
    страницы = прогон['страницы']
    привязки = {i.lower() for i in прогон.get('привязки', set())}
    код = прогон.get('код', '')       # весь код страниц + JS (нижний регистр)
    формные = _формные_цели(pid)
    форм_статусы = _результаты_форм(pid)
    url_map = _url_цели_проверка(каталог, страницы)
    _план = ACTIONS.get(pid) or _план_для_домена(каталог.get('домен', ''))
    ожидаемые = {i.lower() for i in _план.get('ожидаемые', [])}
    _код_кэш: dict[str, bool] = {}

    def _id_в_коде(gid: str) -> bool:
        """Цель упоминается в коде сайта: есть в reachGoal-списке ИЛИ встречается
        как строковый литерал ('id' / \"id\") где-либо в HTML/JS проверенных
        страниц. Литерал в кавычках защищает от ложных совпадений (tel в hotel)."""
        gid = (gid or '').lower()
        if not gid:
            return False
        if gid in привязки or any(gid in b for b in привязки):
            return True
        if gid in _код_кэш:
            return _код_кэш[gid]
        found = bool(_re2.search(r'["\']' + _re2.escape(gid) + r'["\']', код)) if код else False
        _код_кэш[gid] = found
        return found

    def _привязана(g) -> str:
        ids = g.get('идентификаторы') or []
        return 'есть' if any(_id_в_коде(i) for i in ids) else 'не найдена'

    def _форма_поймала(g) -> str | None:
        """Идентификатор цели, которую ПОЙМАЛА «Проверка форм» (лист «Цели» её
        отчёта). Формы отправляются по-настоящему, поэтому цели на onsubmit там
        реально фиксируются - даже если статический скан кода их не нашёл. Это
        снимает ложное «нет в коде сайта» с целей отправки форм."""
        for gid in (g.get('идентификаторы') or []):
            st = (форм_статусы.get(gid) or '').lower()
            if st.startswith('сработал') or 'зафиксир' in st:
                return gid
        return None

    def _авто_действие_сделано(условие: str) -> bool:
        """Автоцель Метрики: выполнил ли автотест соответствующее действие.
        Клики по телефону/почте/соцсетям/мессенджерам мы делаем; формы/файлы/
        поиск - нет (формы закрывает «Проверка форм»)."""
        c = (условие or '').lower()
        делаем = ('телефон', 'номер', 'email', 'почт', 'соц', 'мессенджер',
                  'whatsapp', 'telegram', 'вконтакте')
        не_делаем = ('форм', 'файл', 'скачив', 'поиск', 'чат', 'контактные данные',
                     'оформлени')
        if any(k in c for k in не_делаем):
            return False
        return any(k in c for k in делаем)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Цели Метрики'
    ws.sheet_view.showGridLines = False
    headers = ['№ цели', 'Название', 'Статус', 'Что это значит', 'Условие (из Метрики)']
    for c, (h, w) in enumerate(zip(headers, (12, 42, 20, 62, 44)), 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='EEF3FB')
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = 'A2'

    GREEN, RED, GREY, BLUE = '1E8E3E', 'C62828', '757575', '1565C0'
    счёт = {'ok': 0, 'ok_forms': 0, 'bad': 0, 'no_code': 0, 'forms': 0,
            'special': 0, 'manual': 0, 'info': 0}
    # Прозрачен ли код сайта для статического анализа. Если reachGoal почти не
    # нашли (сайт грузит цели через GTM/минифицированный бандл - как ИМП), то
    # вывод «нет в коде» НЕЛЬЗЯ делать - это была бы наша слепота, а не баг сайта.
    _код_надёжен = len(привязки) >= 3
    _строки: list[dict] = []
    for g in каталог.get('цели', []):
        t = g['тип']
        способ = статус = детали = ''
        цвет = GREY
        if t == 'js':
            hit = _совпало(g, fired)
            форма_id = _форма_поймала(g)
            в_формах = any(gid in формные for gid in (g.get('идентификаторы') or []))
            _особое = _нужно_спец_действие(g)
            if hit:
                способ, статус, цвет = 'клики автотеста', 'Сработала', GREEN
                детали = f'зафиксирован идентификатор «{hit}»'
                счёт['ok'] += 1
            elif форма_id:
                # Цель реально сработала при ОТПРАВКЕ формы («Проверка форм») -
                # даже если статический скан reachGoal не нашёл. Это НЕ «нет в коде».
                способ, статус, цвет = 'через формы', 'Сработала (формы)', GREEN
                детали = ('зафиксирована при отправке формы на странице «Проверка '
                          f'форм» (идентификатор «{форма_id}»)')
                счёт['ok_forms'] += 1
            elif в_формах:
                # Цель привязана к отправке формы в конфиге, но результата форм ещё
                # нет: подскажем запустить «Проверку форм» (результат подтянется сам).
                способ, статус, цвет = 'через формы', 'Проверяется формами', BLUE
                детали = ('цель срабатывает при отправке формы - запустите «Проверку '
                          'форм» (её результат автоматически подтянется в этот отчёт)')
                счёт['forms'] += 1
            elif (any(gid.lower() in ожидаемые for gid in (g.get('идентификаторы') or []))
                  and _код_надёжен and _привязана(g) == 'есть'):
                # Действие мы выполняли (клик), reachGoal ЕСТЬ в коде, но цель не
                # поймалась - это реальная проблема настройки/привязки.
                способ, статус, цвет = 'клики автотеста', 'НЕ сработала', RED
                детали = ('действие выполнялось (клик по телефону/почте/кнопке), '
                          'reachGoal есть в коде, но цель не зафиксирована - '
                          'проверьте её настройку/привязку')
                счёт['bad'] += 1
            elif _особое and (not _код_надёжен or _привязана(g) == 'есть'):
                # reachGoal в коде есть (или код непрозрачен), но цель срабатывает
                # только на особое действие, которого автотест не делает.
                способ, статус, цвет = 'спец-действие', 'Нужно спец-действие', GREY
                _прив = f"; reachGoal в коде: {_привязана(g)}" if _код_надёжен else ''
                детали = (f'нужно {_особое} - автотест этот шаг не выполняет '
                          f'(можно добавить отдельным сценарием){_прив}')
                счёт['special'] += 1
            elif _код_надёжен and _привязана(g) == 'не найдена':
                # Код сайта прозрачен, а reachGoal этой цели в нём не встречается -
                # это не наша слепота, а отсутствие отправки на стороне сайта.
                способ, статус, цвет = 'проверка кода', 'Нет в коде сайта', RED
                детали = ('reachGoal этой цели НЕ найден в коде сайта - цель создана '
                          'в Метрике, но сайт её не отправляет')
                счёт['no_code'] += 1
            elif _особое:
                способ, статус, цвет = 'спец-действие', 'Нужно спец-действие', GREY
                детали = (f'нужно {_особое} - автотест этот шаг не выполняет '
                          '(можно добавить отдельным сценарием)')
                счёт['special'] += 1
            else:
                # Код непрозрачен (GTM/бандл) или действие не входило в прогон.
                способ, статус, цвет = 'вручную', 'Не проверено', GREY
                _подск = f"; привязка reachGoal в коде: {_привязана(g)}" if _код_надёжен else ''
                детали = ('цель грузится через GTM/бандл или требует действия, '
                          'которого не было в прогоне - проверяется вручную/в '
                          'Метрике' + _подск)
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
                детали = (f"цель = визит на страницу «{g.get('url_часть','')}», а эта "
                          "страница не входит в список открываемых (напр. «спасибо»/"
                          "заказ/оплата - на них не попасть без реального заказа). "
                          "Добавьте её адрес в прогон - и автопроверка появится")
                счёт['manual'] += 1
        elif t == 'auto':
            сделано = _авто_действие_сделано(g.get('условие', ''))
            способ = 'автоцель Метрики'
            if сделано:
                статус, цвет = 'Действие выполнено', GREEN
                детали = ('автоцель Метрики (клик по телефону/почте/соцсети). Такие '
                          'цели Метрика считает САМА на своём сервере - отдельного '
                          'goal-сигнала в трафике нет, увидеть факт срабатывания извне '
                          'нельзя. Но нужное действие автотест выполнил - значит в '
                          'Метрике цель зачтётся')
                счёт['ok'] += 1
            else:
                статус, цвет = 'Только в Метрике', BLUE
                детали = ('автоцель Метрики (форма/файл/поиск/контакты). Считается на '
                          'сервере Яндекса без goal-сигнала в трафике, поэтому мы не '
                          'видим факт срабатывания напрямую. Само действие закрывает '
                          '«Проверка форм» (для форм) либо оно происходит у реального '
                          'посетителя - смотрите цифры в самой Метрике')
                счёт['info'] += 1
        elif t == 'jivo':
            способ, статус, цвет = 'вручную', 'Вручную', GREY
            детали = 'события чата Jivo зависят от посетителя/оператора'
            счёт['manual'] += 1
        else:  # composite
            способ, статус, цвет = 'по шагам', 'Составная', BLUE
            детали = 'Метрика вычисляет из шагов - смотри цели-шаги выше/ниже'
            счёт['info'] += 1

        _строки.append({'номер': g['номер'], 'название': g['название'],
                        'условие': g['условие'], 'статус': статус,
                        'детали': детали, 'цвет': цвет})

    # ── Лист «Цели Метрики»: сортировка по статусу + цветные плашки + фильтр ──
    _ФОН = {GREEN: 'E6F4EA', RED: 'FCE8E6', BLUE: 'E8F0FE', GREY: 'F1F3F4'}
    # порядок вывода: сначала главное, потом проблемы, потом информация
    _ПОРЯДОК = {'Сработала': 0, 'Действие выполнено': 1, 'Сработает': 2,
                'Нет в коде сайта': 3, 'НЕ сработала': 4, 'Проблема': 5,
                'Не проверено': 6, 'Нужно спец-действие': 6, 'Нет автопроверки': 7,
                'Прогоном форм': 8, 'Только в Метрике': 9, 'Составная': 10,
                'Вручную': 11}
    _строки.sort(key=lambda x: (_ПОРЯДОК.get(x['статус'].split(':')[0].strip(), 20),
                                x['название']))
    # Неяркая граница-сетка, чтобы лист читался таблицей, а не сплошным полотном.
    _tside = Side(style='thin', color='D9DCE1')
    _tbord = Border(left=_tside, right=_tside, top=_tside, bottom=_tside)
    ws.delete_rows(2, ws.max_row)   # заголовок уже есть, чистим тело
    r = 2
    for s in _строки:
        vals = [s['номер'], s['название'], s['статус'], s['детали'], s['условие']]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r, c, v)
            cell.alignment = Alignment(wrap_text=(c in (2, 3, 4)), vertical='top')
            cell.border = _tbord
        st = ws.cell(r, 3)
        st.font = Font(color=s['цвет'], bold=True)
        st.fill = PatternFill('solid', fgColor=_ФОН.get(s['цвет'], 'FFFFFF'))
        r += 1
    # Граница и у строки заголовков - тогда таблица «в рамке» целиком.
    for c in range(1, 6):
        ws.cell(1, c).border = _tbord
    ws.auto_filter.ref = f"A1:E{max(2, r - 1)}"

    # ── Лист «Сводка»: заголовок + сгруппированная таблица категорий + страницы ──
    sm = wb.create_sheet('Сводка', 0)
    sm.sheet_view.showGridLines = False
    sm.column_dimensions['A'].width = 4
    sm.column_dimensions['B'].width = 32
    sm.column_dimensions['C'].width = 9
    sm.column_dimensions['D'].width = 74
    _thin = Side(style='thin', color='D9DCE1')
    _bord = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)

    sm.merge_cells('A1:D1')
    sm['A1'] = f"Проверка целей Метрики - {каталог.get('проект','')}"
    sm['A1'].font = Font(bold=True, size=15)
    sm.merge_cells('A2:D2')
    sm['A2'] = (f"Счётчик {каталог.get('счётчик','')} · целей в каталоге: "
                f"{len(каталог.get('цели', []))} · прогон "
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')}")
    sm['A2'].font = Font(italic=True, color='5F6368')

    _подтв = счёт['ok'] + счёт['ok_forms']
    _пробл = счёт['no_code'] + счёт['bad']
    sm.merge_cells('A3:D3')
    sm['A3'] = (f"Подтверждено: {_подтв}   ·   Проблемы: {_пробл}   ·   "
                f"Требует действия/вручную: {счёт['special'] + счёт['manual']}   ·   "
                f"Формы и авто: {счёт['forms'] + счёт['info']}")
    sm['A3'].font = Font(bold=True, color='3C4043')

    # Категории сгруппированы по смыслу; между группами - пустая строка-разделитель.
    _группы = [
        ('ПОДТВЕРЖДЕНО', [
            ('✅ Сработали при кликах', счёт['ok'], GREEN,
             'цель реально зафиксирована во время кликов автотеста'),
            ('✅ Сработали через формы', счёт['ok_forms'], GREEN,
             'цель поймана при отправке формы («Проверка форм») - reachGoal рабочий'),
        ]),
        ('ПРОБЛЕМЫ (к разработчикам)', [
            ('❌ Нет в коде сайта', счёт['no_code'], RED,
             'reachGoal этой цели в коде сайта не найден - цель в Метрике есть, но '
             'сайт её не отправляет'),
            ('❌ НЕ сработала (reachGoal есть)', счёт['bad'], RED,
             'reachGoal в коде есть, действие выполняли, но цель не поймалась - '
             'проверить её настройку/кнопку'),
        ]),
        ('ТРЕБУЕТ ДЕЙСТВИЯ / ВРУЧНУЮ', [
            ('🟡 Нужно спец-действие', счёт['special'], GREY,
             'цель на вход/оплату/купон/избранное/скачивание - автотест этот шаг '
             'пока не делает (можно добавить сценарием)'),
            ('🖐 Не проверено', счёт['manual'], GREY,
             'цель грузится через GTM/бандл или её страницы не было в прогоне - '
             'смотрится вручную/в Метрике'),
        ]),
        ('ФОРМЫ И АВТО-ЦЕЛИ', [
            ('📝 Проверяется формами', счёт['forms'], BLUE,
             'цель отправки формы - запустите «Проверку форм», результат подтянется сюда'),
            ('ℹ️ Авто / составные', счёт['info'], BLUE,
             'Метрика считает сама на сервере (goal-сигнала в трафике нет - увидеть '
             'извне нельзя)'),
        ]),
    ]

    hr = 5
    for c, h in enumerate(['', 'Категория', 'Кол-во', 'Что это значит'], 1):
        cell = sm.cell(hr, c, h)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='5B6470')
        cell.border = _bord
        cell.alignment = Alignment(horizontal='center' if c == 3 else 'left')
    rr = hr + 1
    for заг, строки in _группы:
        sm.cell(rr, 2, заг).font = Font(bold=True, size=9, color='80868B')
        rr += 1
        for назв, кол, цв, пояс in строки:
            sm.cell(rr, 2, назв).font = Font(bold=True, color=цв)
            cc = sm.cell(rr, 3, кол)
            cc.font = Font(bold=True, color=цв)
            cc.alignment = Alignment(horizontal='center')
            cc.fill = PatternFill('solid', fgColor=_ФОН.get(цв, 'FFFFFF'))
            пc = sm.cell(rr, 4, пояс)
            пc.alignment = Alignment(wrap_text=True, vertical='top')
            for c in (2, 3, 4):
                sm.cell(rr, c).border = _bord
            sm.row_dimensions[rr].height = 30
            rr += 1
        rr += 1   # пустая строка-разделитель между группами

    if not _код_надёжен:
        sm.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=4)
        sm.cell(rr, 2, '⚠️ Код сайта грузится через GTM/бандл - reachGoal статически '
                       'не виден, поэтому «нет в коде» здесь не выносим (чтобы не '
                       'обвинять сайт зря).').font = Font(italic=True, color='B06000')
        rr += 2

    sm.cell(rr, 2, 'Страницы прогона').font = Font(bold=True)
    rr += 1
    for s in страницы:
        sm.cell(rr, 2, s['название']).font = Font(bold=True)
        sm.cell(rr, 4, f"{s['url']} - код {s['код']}, "
                       f"счётчик {'✓' if s['счётчик'] else '✗ НЕ найден'}")
        rr += 1

    wb.active = 0
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
