# -*- coding: utf-8 -*-
"""
admin_settings_check.py - «Работают функции настройки поддоменов/категорий/
товаров/тех.страниц» (доп. чек-лист, работа в админке Bitrix).

Что проверяем (браузером, Playwright):
  1. Вход: HTTP Basic (заглушка тестового контура, если есть) + форма Bitrix.
  2. Поддомены  - «Мастер импорта поддоменов PRO» (sm_domain_tool.php):
     страница открывается, на месте вкладки создания/удаления доменов,
     кнопки запуска и режим симуляции.
  3. Категории  - разделы каталога (cat_section_admin, инфоблок 2): грид
     рендерится со строками; round-trip: SORT раздела +1 → сохранить →
     перечитать → откатить → перечитать (проверка, что запись в БД через
     админку реально проходит).
  4. Товары     - товарная подсистема на Highload-блоках («Ассортимент»):
     список записей рендерится, форма редактирования записи открывается
     с UF_-полями. Данные не меняем (цены/остатки - боевые).
  5. Тех.страницы - «Структура сайта» (fileman_admin): список файлов
     главного сайта рендерится, редактор файла открывается с контентом.

Архитектура сайта (Стальметурал и клоны): мультисайт Bitrix - каждый город
отдельный «сайт»; товары не элементы инфоблока (их 0), а строки HL-блоков;
каталожное дерево - разделы инфоблока 2.

Креды: forms_tester/projects/<pid>/admin.local.json (прод) или
admin.test.local.json (тестовый контур: + basic_login/basic_password).
"""
import json
import re
from pathlib import Path

# Разделы админки (пути от /bitrix/admin/). SECTION_SORT_ID - раздел для
# round-trip (верхнеуровневый, существует и на тесте, и на проде).
IBLOCK_TYPE = 'ural_metall'
CATALOG_IBLOCK_ID = 2
SECTION_SORT_ID = 5545          # «Сетка металлическая»
HL_PRODUCTS_ENTITY_ID = 6       # HL-блок «Ассортимент»
MAIN_SITE_ID = 's1'             # главный сайт мультисайта (Москва)

PATH_SUBDOMAINS = 'sm_domain_tool.php?lang=ru'
PATH_SECTIONS = ('cat_section_admin.php?lang=ru&type={t}&IBLOCK_ID={ib}'
                 '&find_section_section=0&SECTION_ID=0&apply_filter=Y')
PATH_SECTION_EDIT = ('iblock_section_edit.php?IBLOCK_ID={ib}&type={t}'
                     '&lang=ru&ID={sid}')
PATH_HL_ROWS = 'highloadblock_rows_list.php?ENTITY_ID={eid}&lang=ru'
PATH_FILEMAN = 'fileman_admin.php?lang=ru&site={site}&logical=Y&path=%2F'
PATH_FILE_EDIT = 'fileman_file_edit.php?lang=ru&site={site}&path=%2Findex.php'


def load_admin_creds(project_dir, test=False):
    """Креды админки из admin.local.json / admin.test.local.json.
    Возвращает dict {domain?, login, password, basic_login?, basic_password?}
    или None. Пустые шаблоны (ВПИШИ_/ВАШ_) игнорируются."""
    name = 'admin.test.local.json' if test else 'admin.local.json'
    f = Path(project_dir) / name
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return None
    login = str(d.get('login') or '')
    if not login or not d.get('password') or 'ВПИШИ' in login.upper() \
            or 'ВАШ_' in login.upper():
        return None
    return {k: d[k] for k in ('domain', 'login', 'password',
                              'basic_login', 'basic_password') if d.get(k)}


def _mk_check(code, title, ok, detail='', warnings=None, roundtrip=None):
    """Единица результата для отчёта."""
    out = {'code': code, 'title': title, 'ok': bool(ok), 'detail': detail,
           'warnings': list(warnings or [])}
    if roundtrip is not None:
        out['roundtrip'] = roundtrip
    return out


def summarize(checks):
    """Общий вердикт по чекам: ok / warn / fail."""
    if any(not c['ok'] for c in checks):
        return 'fail'
    if any(c.get('warnings') for c in checks):
        return 'warn'
    return 'ok'


# ── Браузерная часть ─────────────────────────────────────────────────
def _login(page, domain, creds, log):
    """Вход: страница админки уже за basic (креды - в контексте браузера);
    здесь - форма Bitrix. True/False."""
    page.goto(f'{domain}/bitrix/admin/index.php?lang=ru',
              wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(1500)
    if page.locator("input[name='USER_LOGIN']").count() > 0:
        page.fill("input[name='USER_LOGIN']", creds['login'])
        page.fill("input[name='USER_PASSWORD']", creds['password'])
        page.keyboard.press('Enter')
        page.wait_for_load_state('domcontentloaded', timeout=60000)
        page.wait_for_timeout(2500)
    html = page.content()
    if 'USER_PASSWORD' in html and 'adm-' not in html:
        log('❌ Вход в Bitrix не удался (снова форма логина)')
        return False
    if page.locator("input[name='USER_LOGIN']").count() > 0:
        log('❌ Вход в Bitrix не удался (неверный логин/пароль)')
        return False
    return True


def _goto(page, domain, path):
    resp = page.goto(f'{domain}/bitrix/admin/{path}',
                     wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(2500)
    return resp.status if resp else 0


def _page_errors(page):
    return page.locator('.adm-error-message, .errortext').count()


def _check_subdomains(page, domain, log):
    """Мастер поддоменов: вкладки создания/удаления, кнопки запуска,
    режим симуляции."""
    status = _goto(page, domain, PATH_SUBDOMAINS)
    if status != 200:
        return _mk_check('subdomains', 'Поддомены', False,
                         f'страница мастера не открылась (HTTP {status})')
    body = page.inner_text('body')
    missing = [need for need in
               ('Создание доменов', 'Удаление доменов', 'симуляци')
               if need.lower() not in body.lower()]
    warns = []
    if 'лицензии закончился' in body.lower():
        warns.append('у модуля Bitrix истекла стандартная лицензия '
                     '(баннер в админке) - на работу мастера не влияет, '
                     'но обновлений нет')
    if missing:
        return _mk_check('subdomains', 'Поддомены', False,
                         'мастер открылся, но нет элементов: '
                         + ', '.join(missing), warns)
    return _mk_check('subdomains', 'Поддомены', True,
                     'мастер импорта поддоменов на месте: вкладки '
                     'создания/удаления, режим симуляции', warns)


def _read_sort(page):
    el = page.locator("input[name='SORT']")
    return el.first.input_value() if el.count() else None


def _save_sort(page, value):
    """Поставить SORT (поле может быть на неактивной вкладке - через JS)
    и нажать «Применить»/«Сохранить»."""
    page.evaluate(
        """v => { const el = document.querySelector("input[name='SORT']");
                  el.value = v;
                  el.dispatchEvent(new Event('change', {bubbles: true})); }""",
        value)
    btn = page.locator("input[name='apply'], button[name='apply']")
    if btn.count() == 0:
        btn = page.locator("input[name='save'], button[name='save']")
    btn.first.click()
    page.wait_for_load_state('domcontentloaded', timeout=60000)
    page.wait_for_timeout(1500)


def _check_categories(page, domain, log, roundtrip=True,
                      section_id=SECTION_SORT_ID):
    """Разделы каталога: грид рендерится; round-trip SORT на разделе."""
    path = PATH_SECTIONS.format(t=IBLOCK_TYPE, ib=CATALOG_IBLOCK_ID)
    status = _goto(page, domain, path)
    rows = page.locator('.main-grid-row').count()
    if status != 200 or rows <= 1:
        return _mk_check('categories', 'Категории', False,
                         f'список разделов не рендерится '
                         f'(HTTP {status}, строк {max(rows - 1, 0)})')
    detail = f'список разделов каталога рендерится (строк: {rows - 1})'
    if not roundtrip:
        return _mk_check('categories', 'Категории', True, detail)

    # Round-trip: SORT +1 → apply → перечитать → откат → перечитать.
    edit = PATH_SECTION_EDIT.format(ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE,
                                    sid=section_id)
    rt = {'section_id': section_id, 'field': 'SORT', 'orig': None,
          'saved': False, 'reverted': False}
    _goto(page, domain, edit)
    orig = _read_sort(page)
    rt['orig'] = orig
    if orig is None:
        return _mk_check('categories', 'Категории', False,
                         detail + '; форма редактирования раздела '
                         f'{section_id} не открылась (нет поля SORT)',
                         roundtrip=rt)
    try:
        new_val = str(int(orig) + 1)
        _save_sort(page, new_val)
        _goto(page, domain, edit)
        rt['saved'] = (_read_sort(page) == new_val)
        _save_sort(page, orig)
        _goto(page, domain, edit)
        rt['reverted'] = (_read_sort(page) == orig)
    except Exception as e:
        log(f'⚠ round-trip категории: {e}')
    if not rt['reverted'] and rt['saved']:
        # Сохранение прошло, откат нет - критично: вернуть руками!
        return _mk_check('categories', 'Категории', False,
                         detail + f'; СОХРАНЕНИЕ ПРОШЛО, НО ОТКАТ НЕ '
                         f'ПОДТВЕРДИЛСЯ: верните SORT={orig} разделу '
                         f'{section_id} вручную', roundtrip=rt)
    if not rt['saved']:
        return _mk_check('categories', 'Категории', False,
                         detail + '; сохранение изменения раздела не '
                         'сработало (значение не применилось)',
                         roundtrip=rt)
    return _mk_check('categories', 'Категории', True,
                     detail + f'; round-trip: SORT раздела {section_id} '
                     'изменён, сохранён и откатан - запись через админку '
                     'работает', roundtrip=rt)


def _check_products(page, domain, log,
                    entity_id=HL_PRODUCTS_ENTITY_ID):
    """Товарная подсистема (HL-блок «Ассортимент»): список записей +
    форма редактирования записи с UF_-полями."""
    status = _goto(page, domain, PATH_HL_ROWS.format(eid=entity_id))
    rows = page.locator('table.adm-list-table tr').count()
    if status != 200 or rows <= 1:
        return _mk_check('products', 'Товары', False,
                         f'список записей HL-блока «Ассортимент» не '
                         f'рендерится (HTTP {status}, строк {max(rows-1,0)})')
    detail = (f'список записей товарной подсистемы (HL «Ассортимент») '
              f'рендерится (строк: {rows - 1})')
    # Форма редактирования первой записи
    href = None
    for a in page.eval_on_selector_all(
            'a[href*="highloadblock_row_edit"]',
            "els => els.map(e => e.getAttribute('href'))"):
        if a and re.search(r'[?&]ID=\d+', a):
            href = a
            break
    if not href:
        return _mk_check('products', 'Товары', False,
                         detail + '; ссылки редактирования записи не '
                         'найдены')
    href = href if href.startswith('/') else f'/bitrix/admin/{href}'
    page.goto(f'{domain}{href}', wait_until='domcontentloaded',
              timeout=60000)
    page.wait_for_timeout(2000)
    uf = page.locator("[name^='UF_']").count()
    if uf == 0:
        return _mk_check('products', 'Товары', False,
                         detail + '; форма редактирования записи '
                         'открылась без UF_-полей')
    return _mk_check('products', 'Товары', True,
                     detail + f'; форма редактирования записи открывается '
                     f'(UF-полей: {uf})')


def _check_tech_pages(page, domain, log, site_id=MAIN_SITE_ID):
    """Тех.страницы: «Структура сайта» главного сайта + редактор файла."""
    status = _goto(page, domain, PATH_FILEMAN.format(site=site_id))
    rows = page.locator('table.adm-list-table tr').count()
    if status != 200 or rows <= 1:
        return _mk_check('tech_pages', 'Тех. страницы', False,
                         f'структура сайта не рендерится '
                         f'(HTTP {status}, строк {max(rows - 1, 0)})')
    detail = f'структура сайта {site_id} рендерится (строк: {rows - 1})'
    # Редактор файла - по прямому пути (в списке ссылки в JS-меню, прямых нет)
    status = _goto(page, domain, PATH_FILE_EDIT.format(site=site_id))
    has_editor = (page.locator('textarea').count() > 0
                  or page.locator('.CodeMirror, [contenteditable="true"]')
                  .count() > 0)
    if status != 200 or not has_editor or _page_errors(page):
        return _mk_check('tech_pages', 'Тех. страницы', False,
                         detail + f'; редактор файла index.php не открылся '
                         f'(HTTP {status})')
    return _mk_check('tech_pages', 'Тех. страницы', True,
                     detail + '; редактор файла открывается с контентом')


def check_admin_settings(creds, roundtrip=True, log=None,
                         headless=True):
    """Полная проверка функций настройки в админке. creds - dict из
    load_admin_creds (+ domain обязателен). Возвращает dict для отчёта."""
    def _log(m):
        if log:
            log(m)
    domain = (creds.get('domain') or '').rstrip('/')
    if not domain:
        return {'available': False, 'note': 'домен админки не задан'}

    from playwright.sync_api import sync_playwright
    checks = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=headless)
        ctx_kw = {'viewport': {'width': 1600, 'height': 1000}}
        if creds.get('basic_login'):
            ctx_kw['http_credentials'] = {
                'username': creds['basic_login'],
                'password': creds['basic_password']}
        ctx = br.new_context(**ctx_kw)
        page = ctx.new_page()
        try:
            ok = _login(page, domain, creds, _log)
            checks.append(_mk_check(
                'login', 'Вход в админку', ok,
                'вход выполнен' if ok else 'вход не выполнен - '
                'проверьте логин/пароль (и basic-доступ на тестовом '
                'контуре)'))
            if ok:
                for fn in (_check_subdomains,
                           lambda p, d, l: _check_categories(
                               p, d, l, roundtrip=roundtrip),
                           _check_products, _check_tech_pages):
                    try:
                        c = fn(page, domain, _log)
                    except Exception as e:
                        c = _mk_check('error', 'Проверка упала', False,
                                      str(e)[:300])
                    checks.append(c)
                    _log(('✅ ' if c['ok'] else '❌ ')
                         + f'{c["title"]}: {c["detail"]}')
        finally:
            ctx.close()
            br.close()
    return {'available': True, 'domain': domain,
            'verdict': summarize(checks), 'checks': checks}
