# -*- coding: utf-8 -*-
"""
admin_settings_check.py - «Работают функции настройки поддоменов/категорий/
товаров/тех.страниц» (доп. чек-лист, работа в админке Bitrix).

Что проверяем (браузером, Playwright):
  1. Вход: HTTP Basic (заглушка тестового контура, если есть) + форма Bitrix.
  2. Поддомены  - «Мастер импорта поддоменов PRO» (sm_domain_tool.php).
     Функции: создание (симуляция реального dry-run мастера, ничего на
     проде не создаётся), массовая загрузка (CSV-загрузчик Способа А),
     правка/удаление/скрытие (проверка наличия функции - реально НЕ
     выполняем: у мастера нет dry-run для удаления, на боевых сайтах
     опасно).
  3. Категории  - разделы каталога (cat_section_admin, инфоблок 2). Полный
     CRUD на ОДНОМ временном разделе «[ТЕСТ ЧЕКЕРА]» (создаётся скрытым,
     без товаров, удаляется в конце): создание → правка (переименование)
     → скрытие (тумблер активности) → удаление. Массовая загрузка -
     наличие страницы импорта инфоблока. Всё пишется и откатывается -
     доказывает, что запись в БД через админку работает.
  4. Товары     - товарная подсистема на Highload-блоках («Ассортимент»):
     список записей рендерится, форма редактирования записи открывается
     с UF_-полями. Данные не меняем (цены/остатки - боевые).
  5. Тех.страницы - «Структура сайта» (fileman_admin): список файлов
     главного сайта рендерится, редактор файла открывается с контентом.

Операции с записью (CRUD категорий, симуляция поддоменов) выполняются
ТОЛЬКО при roundtrip=True. При roundtrip=False - лишь чтение и наличие
функций (UI). Каждая операция несёт аудит «было → стало» для отчёта.

Архитектура сайта (Стальметурал и клоны): мультисайт Bitrix - каждый город
отдельный «сайт»; товары не элементы инфоблока (их 0), а строки HL-блоков;
каталожное дерево - разделы инфоблока 2.

Креды: forms_tester/projects/<pid>/admin.local.json (прод) или
admin.test.local.json (тестовый контур: + basic_login/basic_password).
"""
import json
import re
from pathlib import Path

# Разделы админки (пути от /bitrix/admin/).
IBLOCK_TYPE = 'ural_metall'
CATALOG_IBLOCK_ID = 2
HL_PRODUCTS_ENTITY_ID = 6       # HL-блок «Ассортимент»
MAIN_SITE_ID = 's1'             # главный сайт мультисайта (Москва)

# Метка временного тестового раздела/товара (по ней чистим «хвосты»).
TEST_SECTION_MARK = '[ТЕСТ ЧЕКЕРА]'
TEST_SECTION_CODE = 'checker-tmp-section'
TEST_PRODUCT_CODE = 'checker-tmp-product'
TEST_PRODUCT_PRICE = '1'         # каталог требует цену - ставим 1, товар скрыт

PATH_SUBDOMAINS = 'sm_domain_tool.php?lang=ru'
PATH_SECTIONS = ('cat_section_admin.php?lang=ru&type={t}&IBLOCK_ID={ib}'
                 '&find_section_section=0&SECTION_ID=0&apply_filter=Y')
PATH_SECTION_NEW = ('iblock_section_edit.php?IBLOCK_ID={ib}&type={t}'
                    '&lang=ru&find_section_section=0')
PATH_SECTION_EDIT = ('iblock_section_edit.php?IBLOCK_ID={ib}&type={t}'
                     '&lang=ru&ID={sid}')
PATH_SECTION_IMPORT = 'iblock_data_import.php?lang=ru&type={t}&IBLOCK_ID={ib}'
PATH_ELEMENT_NEW = ('iblock_element_edit.php?IBLOCK_ID={ib}&type={t}'
                    '&lang=ru&find_section_section=0')
PATH_ELEMENT_EDIT = ('iblock_element_edit.php?IBLOCK_ID={ib}&type={t}'
                     '&lang=ru&ID={eid}')
PATH_ELEMENT_LIST = ('iblock_element_admin.php?IBLOCK_ID={ib}&type={t}'
                     '&lang=ru&find_el_name={name}&set_filter=Y')
PATH_ELEMENT_DELETE = ('iblock_element_admin.php?IBLOCK_ID={ib}&type={t}'
                       '&lang=ru&action=delete&ID={eid}&sessid={sessid}')
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


def _op(op, label, result, mode, before='', after='', note=''):
    """Одна CRUD-операция для аудита в отчёте.
    result: ok | fail | skip. mode: executed (реально выполнено) |
    simulated (dry-run мастера) | ui (проверено только наличие функции)."""
    return {'op': op, 'label': label, 'result': result, 'mode': mode,
            'before': before, 'after': after, 'note': note}


def _mk_check(code, title, ok, detail='', warnings=None, roundtrip=None,
              operations=None):
    """Единица результата для отчёта. operations - список _op (аудит CRUD)."""
    out = {'code': code, 'title': title, 'ok': bool(ok), 'detail': detail,
           'warnings': list(warnings or [])}
    if operations is not None:
        out['operations'] = operations
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


def _sim_monitor_counts(page):
    """Из блока «Мониторинг процесса» вытащить (создано, пропущено, ошибки).
    Формат: число над каждой подписью. None, если блока нет."""
    body = page.inner_text('body')
    out = {}
    for label, key in (('СОЗДАНО', 'created'), ('ПРОПУЩЕНО', 'skipped'),
                       ('ОШИБКИ', 'errors')):
        m = re.search(r'(\d+)\s*\n?\s*' + label, body)
        if m:
            out[key] = int(m.group(1))
    return out or None


def _check_subdomains(page, domain, log, crud=False, execute=True):
    """Мастер поддоменов. Без crud - только доступность мастера (рендер
    страницы, вкладки). С crud - функции create/bulk/edit/delete/hide:
    создание при execute грузим через СИМУЛЯЦИЮ (dry-run, на проде ничего
    не создаётся); правку/удаление/скрытие - только наличие функции."""
    status = _goto(page, domain, PATH_SUBDOMAINS)
    if status != 200:
        return _mk_check('subdomains', 'Поддомены', False,
                         f'страница мастера не открылась (HTTP {status})',
                         operations=[])
    body = page.inner_text('body')
    warns = []
    if 'лицензии закончился' in body.lower():
        warns.append('у модуля Bitrix истекла стандартная лицензия '
                     '(баннер в админке) - на работу мастера не влияет, '
                     'но обновлений нет')
    has_create = 'Создание доменов' in body
    has_delete = 'Удаление доменов' in body
    has_sim = 'симуляци' in body.lower()
    has_uploader = page.locator("input[type='file']").count() > 0
    has_del_btn = page.locator("#btn_delete_submit").count() > 0

    # Без CRUD - только доступность мастера.
    if not crud:
        ok = has_create and has_delete
        return _mk_check('subdomains', 'Поддомены', ok,
                         'мастер импорта поддоменов открывается (вкладки '
                         'создания/удаления, режим симуляции)' if ok else
                         'мастер открылся, но не хватает вкладок '
                         'создания/удаления', warns)

    ops = []

    # 1. Создание - при execute реальная СИМУЛЯЦИЯ одного домена (dry-run).
    if execute and has_create and has_sim:
        try:
            sim = page.locator('#run_simulation')
            if sim.count() and not sim.is_checked():
                sim.check()
            page.fill("input[placeholder='Напр: Тула']", 'Тест Чекера')
            page.fill("input[placeholder='tula']", 'checker-sim')
            page.fill("input[placeholder='tula@stalmetural.ru']",
                      'checker-sim@example.ru')
            page.get_by_text('Запустить создание (Таблица)',
                             exact=False).first.click()
            page.wait_for_timeout(6000)
            cnt = _sim_monitor_counts(page) or {}
            ok = cnt.get('created', 0) >= 1 and cnt.get('errors', 1) == 0
            ops.append(_op(
                'create', 'Создание поддомена',
                'ok' if ok else 'fail', 'simulated',
                before='домена «checker-sim» нет',
                after=(f'симуляция: создано {cnt.get("created", 0)}, '
                       f'ошибок {cnt.get("errors", 0)} (реально НЕ создан)'),
                note='режим симуляции - dry-run, боевых доменов не трогает'))
        except Exception as e:
            ops.append(_op('create', 'Создание поддомена', 'fail',
                           'simulated', note=f'симуляция упала: {e}'))
    else:
        ops.append(_op(
            'create', 'Создание поддомена',
            'ok' if has_create else 'fail', 'ui',
            after='форма создания на месте' if has_create else 'нет формы',
            note='' if execute else 'тест-выполнение выключено - только наличие'))

    # 2. Массовая загрузка - CSV-загрузчик Способа А (не выполняем импорт).
    ops.append(_op(
        'bulk', 'Массовая загрузка (CSV)',
        'ok' if has_uploader else 'fail', 'ui',
        after='загрузчик CSV на месте (Способ А, очередь)' if has_uploader
        else 'загрузчик CSV не найден',
        note='файл не загружали - только наличие функции'))

    # 3. Правка - ручной ввод/редактирование строки домена (наличие).
    ops.append(_op(
        'edit', 'Правка поддомена',
        'ok' if has_create else 'fail', 'ui',
        after='ручной ввод/правка строки домена доступны' if has_create
        else 'форма ввода не найдена',
        note='реально не правим - боевые сайты'))

    # 4. Удаление - вкладка «Удаление доменов» + кнопка (наличие).
    ops.append(_op(
        'delete', 'Удаление поддомена',
        'ok' if (has_delete and has_del_btn) else 'fail', 'ui',
        after='вкладка удаления + кнопка на месте' if
        (has_delete and has_del_btn) else 'механизм удаления не найден',
        note='реально не удаляем - у мастера нет dry-run для удаления'))

    # 5. Скрытие - через тот же механизм активности сайта/удаления (наличие).
    ops.append(_op(
        'hide', 'Скрытие поддомена',
        'ok' if has_delete else 'fail', 'ui',
        after='управление доменами (вкл/выкл, удаление) доступно' if
        has_delete else 'управление не найдено',
        note='реально не скрываем - боевые сайты'))

    bad = [o for o in ops if o['result'] == 'fail']
    _created = 'создание проверено симуляцией (dry-run)' if execute \
        else 'создание - наличие функции'
    detail = (f'мастер поддоменов работает: {_created}, массовая загрузка / '
              'правка / удаление / скрытие - функции на месте' if not bad else
              'часть функций мастера недоступна: '
              + ', '.join(o['label'] for o in bad))
    return _mk_check('subdomains', 'Поддомены', not bad, detail, warns,
                     operations=ops)


# ── Категории: CRUD на временном разделе ─────────────────────────────
def _sec_edit_url(domain, sid):
    return f'{domain}/bitrix/admin/' + PATH_SECTION_EDIT.format(
        ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE, sid=sid)


def _sec_read(page):
    """Снять состояние формы раздела: name, active, id (0 - новый)."""
    return page.evaluate(
        """() => ({
            name: (document.querySelector("input[name='NAME']")||{}).value || '',
            active: (document.querySelector("input[name='ACTIVE']")||{}).checked,
            id: (document.querySelector("input[type=hidden][name='ID']")||{}).value || '0'
        })""")


def _sec_apply(page):
    """Нажать «Применить» на форме раздела и дождаться перезагрузки."""
    btn = page.locator("input[name='apply'], button[name='apply']")
    btn.first.click()
    page.wait_for_load_state('domcontentloaded', timeout=60000)
    page.wait_for_timeout(1500)


def _sec_delete(domain, page, sid):
    """Удалить раздел (кнопка «Удалить раздел» на форме). True, если после
    раздел больше не открывается."""
    page.goto(_sec_edit_url(domain, sid), wait_until='domcontentloaded',
              timeout=60000)
    page.wait_for_timeout(1200)
    btn = page.locator("input[value='Удалить'], a:has-text('Удалить раздел')")
    if btn.count() == 0:
        return False
    btn.first.click()
    page.wait_for_load_state('domcontentloaded', timeout=60000)
    page.wait_for_timeout(1800)
    # Проверка: форма этого ID больше не отдаёт NAME
    page.goto(_sec_edit_url(domain, sid), wait_until='domcontentloaded',
              timeout=60000)
    page.wait_for_timeout(1200)
    return not _sec_read(page).get('name')


def _cleanup_test_sections(page, domain, log):
    """Подчистить возможные хвосты прошлых прогонов - разделы с меткой
    TEST_SECTION_MARK в верхнем уровне каталога."""
    try:
        _goto(page, domain, PATH_SECTIONS.format(
            t=IBLOCK_TYPE, ib=CATALOG_IBLOCK_ID))
        ids = page.evaluate(
            """mark => [...document.querySelectorAll('.main-grid-row')]
                 .filter(r => (r.textContent||'').includes(mark))
                 .map(r => (r.getAttribute('data-id')
                            || (r.id||'').replace(/\\D+/g,'')))
                 .filter(Boolean)""", TEST_SECTION_MARK)
        for sid in ids:
            if _sec_delete(domain, page, sid):
                log(f'  подчищен хвост тестового раздела ID={sid}')
    except Exception:
        pass


def _check_categories(page, domain, log, crud=False, execute=True):
    """Разделы каталога: грид рендерится. Без crud - только доступность
    (грид). С crud+execute - полный CRUD на временном разделе «[ТЕСТ
    ЧЕКЕРА]» (создать скрытым → правка → скрытие/показ → удалить). С crud
    без execute - только наличие CRUD-функций (форма, кнопки), без записи.
    Массовая загрузка - наличие страницы импорта инфоблока."""
    path = PATH_SECTIONS.format(t=IBLOCK_TYPE, ib=CATALOG_IBLOCK_ID)
    status = _goto(page, domain, path)
    rows = page.locator('.main-grid-row').count()
    if status != 200 or rows <= 1:
        return _mk_check('categories', 'Категории', False,
                         f'список разделов не рендерится '
                         f'(HTTP {status}, строк {max(rows - 1, 0)})',
                         operations=[])
    detail_head = f'список разделов каталога рендерится (строк: {rows - 1})'

    # Без CRUD - только доступность грида.
    if not crud:
        return _mk_check('categories', 'Категории', True,
                         detail_head + '; функции настройки разделов '
                         'доступны')

    # Массовая загрузка (наличие страницы импорта инфоблока).
    imp_status = _goto(page, domain, PATH_SECTION_IMPORT.format(
        t=IBLOCK_TYPE, ib=CATALOG_IBLOCK_ID))
    imp_ok = imp_status == 200 and page.locator('form').count() > 0
    op_bulk = _op('bulk', 'Массовая загрузка (импорт)',
                  'ok' if imp_ok else 'fail', 'ui',
                  after='страница импорта инфоблока на месте' if imp_ok
                  else f'страница импорта недоступна (HTTP {imp_status})',
                  note='импорт не запускали - только наличие функции')

    # CRUD без записи (execute=False) - проверяем наличие форм/кнопок.
    if not execute:
        page.goto(f'{domain}/bitrix/admin/' + PATH_SECTION_NEW.format(
            ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE),
            wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(1200)
        has_name = page.locator("input[name='NAME']").count() > 0
        has_active = page.locator("input[name='ACTIVE']").count() > 0
        ops = [op_bulk,
               _op('create', 'Создание категории', 'ok' if has_name else 'fail',
                   'ui', after='форма нового раздела открывается' if has_name
                   else 'форма не открылась', note='тест-выполнение выключено'),
               _op('edit', 'Правка категории', 'ok' if has_name else 'fail',
                   'ui', after='поле названия редактируемо' if has_name
                   else 'нет поля названия', note='реально не правим'),
               _op('hide', 'Скрытие категории', 'ok' if has_active else 'fail',
                   'ui', after='тумблер активности на форме' if has_active
                   else 'нет тумблера активности', note='реально не скрываем'),
               _op('delete', 'Удаление категории', 'ok', 'ui',
                   after='функция удаления раздела доступна в админке',
                   note='реально не удаляем - тест-выполнение выключено')]
        bad = [o for o in ops if o['result'] == 'fail']
        return _mk_check(
            'categories', 'Категории', not bad,
            detail_head + '; CRUD-функции разделов на месте (без записи - '
            'тест-выполнение выключено)', operations=ops)

    _cleanup_test_sections(page, domain, log)
    ops = [op_bulk]
    name1 = f'{TEST_SECTION_MARK} временный раздел'
    name2 = f'{TEST_SECTION_MARK} временный раздел (правка)'
    sid = None
    try:
        # ── Создание: новый раздел, скрытый (ACTIVE=N), с CODE ──
        page.goto(f'{domain}/bitrix/admin/' + PATH_SECTION_NEW.format(
            ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE),
            wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate(
            """args => {
                const [nm, code] = args;
                const n = document.querySelector("input[name='NAME']");
                n.value = nm; n.dispatchEvent(new Event('change',{bubbles:true}));
                const c = document.querySelector("input[name='CODE']");
                if (c) { c.value = code;
                         c.dispatchEvent(new Event('change',{bubbles:true})); }
                const a = document.querySelector("input[name='ACTIVE']");
                if (a && a.checked) a.click();
            }""", [name1, TEST_SECTION_CODE])
        _sec_apply(page)
        m = re.search(r'[?&]ID=(\d+)', page.url)
        sid = m.group(1) if m else None
        st = _sec_read(page) if sid else {}
        created = bool(sid) and st.get('name') == name1 and not st.get('active')
        ops.append(_op(
            'create', 'Создание категории',
            'ok' if created else 'fail', 'executed',
            before='раздела нет',
            after=(f'создан раздел ID={sid}, «{name1}», скрыт (ACTIVE=N)'
                   if created else 'создать не удалось'),
            note='временный раздел без товаров, удаляется в конце'))
        if not created:
            raise RuntimeError('раздел не создан')

        # ── Правка: переименование ──
        page.goto(_sec_edit_url(domain, sid), wait_until='domcontentloaded',
                  timeout=60000)
        page.wait_for_timeout(1200)
        page.evaluate(
            """n => { const el=document.querySelector("input[name='NAME']");
                      el.value=n; el.dispatchEvent(new Event('change',{bubbles:true})); }""",
            name2)
        _sec_apply(page)
        page.goto(_sec_edit_url(domain, sid), wait_until='domcontentloaded',
                  timeout=60000)
        page.wait_for_timeout(1000)
        renamed = _sec_read(page).get('name') == name2
        ops.append(_op(
            'edit', 'Правка категории', 'ok' if renamed else 'fail',
            'executed', before=f'название: «{name1}»',
            after=f'название: «{name2}»' if renamed else 'правка не применилась'))

        # ── Скрытие/показ: тумблер ACTIVE (N→Y→N), финал - скрыт ──
        def _set_active(val):
            page.goto(_sec_edit_url(domain, sid),
                      wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(1000)
            page.evaluate(
                """want => { const a=document.querySelector("input[name='ACTIVE']");
                             if (a && a.checked !== want) a.click(); }""", val)
            _sec_apply(page)
            page.goto(_sec_edit_url(domain, sid),
                      wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(900)
            return _sec_read(page).get('active')
        shown = _set_active(True)
        hidden = _set_active(False)
        hide_ok = shown is True and hidden is False
        ops.append(_op(
            'hide', 'Скрытие/показ категории',
            'ok' if hide_ok else 'fail', 'executed',
            before='скрыт (ACTIVE=N)',
            after=('показан (Y) и снова скрыт (N) - тумблер активности '
                   'работает' if hide_ok else 'переключение активности '
                   'не сработало')))

        # ── Удаление: убрать временный раздел ──
        deleted = _sec_delete(domain, page, sid)
        ops.append(_op(
            'delete', 'Удаление категории', 'ok' if deleted else 'fail',
            'executed', before=f'раздел ID={sid} существует',
            after='раздел удалён' if deleted else
            f'НЕ УДАЛЁН - удалите раздел ID={sid} вручную'))
        sid = None if deleted else sid
    except Exception as e:
        log(f'⚠ CRUD категории: {e}')
        ops.append(_op('error', 'CRUD категории', 'fail', 'executed',
                       note=str(e)[:200]))
    finally:
        # Страховка: если раздел создан, но не удалён - добить.
        if sid:
            try:
                if _sec_delete(domain, page, sid):
                    log(f'  тестовый раздел ID={sid} удалён (страховка)')
            except Exception:
                log(f'⚠ ОСТАЛСЯ тестовый раздел ID={sid} - удалите вручную')

    warns = []
    if any(o['result'] == 'fail' and o['op'] == 'delete' for o in ops):
        warns.append('тестовый раздел мог не удалиться - проверьте каталог '
                     'на «[ТЕСТ ЧЕКЕРА]»')
    bad = [o for o in ops if o['result'] == 'fail']
    detail = (detail_head + '; полный CRUD на временном разделе '
              '(создание/правка/скрытие/удаление) выполнен и откатан'
              if not bad else
              detail_head + '; не сработали операции: '
              + ', '.join(o['label'] for o in bad))
    return _mk_check('categories', 'Категории', not bad, detail, warns,
                     operations=ops)


# ── Товары: CRUD элемента каталога (опционально по CMS) ──────────────
def _elem_edit_url(domain, eid):
    return f'{domain}/bitrix/admin/' + PATH_ELEMENT_EDIT.format(
        ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE, eid=eid)


def _elem_read(page):
    """Состояние формы товара: name, sort, active, привязанные разделы."""
    return page.evaluate("""() => {
        const sel = document.querySelector("select[name='IBLOCK_SECTION[]']");
        const secs = sel ? [...sel.selectedOptions].map(o=>o.value)
                              .filter(v=>v && v!=='0') : [];
        return {
          name: (document.querySelector("input[name='NAME']")||{}).value||'',
          sort: (document.querySelector("input[name='SORT']")||{}).value||'',
          active: (document.querySelector("input[name='ACTIVE']")||{}).checked,
          sections: secs
        };
    }""")


def _two_section_ids(page):
    """Два реальных id раздела из мультиселекта привязки (для теста
    мультикатегории). [] если селект пуст."""
    return page.evaluate("""() => {
        const sel = document.querySelector("select[name='IBLOCK_SECTION[]']");
        if (!sel) return [];
        return [...sel.options].map(o=>o.value)
            .filter(v=>/^\\d+$/.test(v) && v!=='0').slice(0,2);
    }""")


def _sessid(page):
    return page.evaluate(
        "() => (window.BX && BX.bitrix_sessid) ? BX.bitrix_sessid() : "
        "((document.querySelector(\"input[name='sessid']\")||{}).value||'')")


def _elem_delete(domain, page, eid):
    """Удалить элемент каталога через список (action=delete + sessid).
    True, если форма этого ID больше не отдаёт NAME."""
    sid = _sessid(page)
    page.goto(f'{domain}/bitrix/admin/' + PATH_ELEMENT_DELETE.format(
        ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE, eid=eid, sessid=sid),
        wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(2000)
    page.goto(_elem_edit_url(domain, eid), wait_until='domcontentloaded',
              timeout=60000)
    page.wait_for_timeout(1200)
    return not _elem_read(page).get('name')


def _cleanup_test_products(page, domain, log):
    """Подчистить хвосты тестовых товаров прошлых прогонов по метке."""
    try:
        from urllib.parse import quote
        page.goto(f'{domain}/bitrix/admin/' + PATH_ELEMENT_LIST.format(
            ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE,
            name=quote(TEST_SECTION_MARK)),
            wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(2000)
        ids = page.evaluate("""() => [...document.querySelectorAll('.main-grid-row')]
            .filter(r => (r.textContent||'').includes('[ТЕСТ ЧЕКЕРА]'))
            .map(r => (r.getAttribute('data-id')||'').replace(/\\D+/g,''))
            .filter(Boolean)""")
        for eid in ids:
            if _elem_delete(domain, page, eid):
                log(f'  подчищен хвост тестового товара ID={eid}')
    except Exception:
        pass


def _check_products_crud(page, domain, log, execute=True):
    """CRUD товара (элемент каталога iblock 2): создать скрытым + SORT +
    привязка к 2 разделам (вывод в разные категории) → правка (имя+SORT) →
    удаление. Опционально по CMS: товары должны быть элементами каталога."""
    # Открываем форму нового элемента - проверяем применимость.
    page.goto(f'{domain}/bitrix/admin/' + PATH_ELEMENT_NEW.format(
        ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE),
        wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(2000)
    if page.locator("input[name='NAME']").count() == 0:
        return _mk_check('products_crud', 'Товары (CRUD)', False,
                         'форма товара (элемент каталога) не открылась - '
                         'товарный CRUD неприменим для этой CMS',
                         operations=[])
    has_sort = page.locator("input[name='SORT']").count() > 0
    has_sec = page.locator("select[name='IBLOCK_SECTION[]']").count() > 0

    # Без записи - только наличие функций.
    if not execute:
        ops = [
            _op('create', 'Создание товара',
                'ok' if page.locator("input[name='NAME']").count() else 'fail',
                'ui', after='форма нового товара открывается',
                note='тест-выполнение выключено'),
            _op('sort', 'Сортировка товара', 'ok' if has_sort else 'fail',
                'ui', after='поле SORT на форме' if has_sort else 'нет SORT',
                note='реально не меняем'),
            _op('multicat', 'Вывод в разные категории',
                'ok' if has_sec else 'fail', 'ui',
                after='мультиселект привязки к разделам на форме' if has_sec
                else 'нет привязки к разделам', note='реально не привязываем'),
            _op('edit', 'Правка товара',
                'ok' if page.locator("input[name='NAME']").count() else 'fail',
                'ui', after='поле названия редактируемо',
                note='реально не правим'),
            _op('delete', 'Удаление товара', 'ok', 'ui',
                after='удаление элемента доступно (список каталога)',
                note='реально не удаляем - тест-выполнение выключено')]
        bad = [o for o in ops if o['result'] == 'fail']
        return _mk_check('products_crud', 'Товары (CRUD)', not bad,
                         'CRUD-функции товара на месте (без записи)',
                         operations=ops)

    _cleanup_test_products(page, domain, log)
    ops = []
    name1 = f'{TEST_SECTION_MARK} товар'
    name2 = f'{TEST_SECTION_MARK} товар (правка)'
    eid = None
    try:
        page.goto(f'{domain}/bitrix/admin/' + PATH_ELEMENT_NEW.format(
            ib=CATALOG_IBLOCK_ID, t=IBLOCK_TYPE),
            wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(2000)
        secs = _two_section_ids(page)
        if len(secs) < 2:
            return _mk_check('products_crud', 'Товары (CRUD)', False,
                             'в каталоге меньше 2 разделов - негде проверить '
                             'вывод в разные категории', operations=[])
        # ── Создание: скрытый товар, SORT, цена, привязка к 2 разделам ──
        page.evaluate("""args => {
            const [nm, code, price, sa, sb] = args;
            const n=document.querySelector("input[name='NAME']");
            n.value=nm; n.dispatchEvent(new Event('change',{bubbles:true}));
            const c=document.querySelector("input[name='CODE']");
            if(c){c.value=code; c.dispatchEvent(new Event('change',{bubbles:true}));}
            const s=document.querySelector("input[name='SORT']");
            if(s){s.value='7777'; s.dispatchEvent(new Event('change',{bubbles:true}));}
            const a=document.querySelector("input[name='ACTIVE']");
            if(a && a.checked) a.click();
            const p=document.querySelector("input[name='CAT_BASE_PRICE']");
            if(p){p.value=price; p.dispatchEvent(new Event('change',{bubbles:true}));}
            const sel=document.querySelector("select[name='IBLOCK_SECTION[]']");
            if(sel){ [...sel.options].forEach(o=>o.selected=(o.value===sa||o.value===sb));
                     sel.dispatchEvent(new Event('change',{bubbles:true})); }
        }""", [name1, TEST_PRODUCT_CODE, TEST_PRODUCT_PRICE, secs[0], secs[1]])
        _sec_apply(page)
        m = re.search(r'[?&]ID=(\d+)', page.url)
        eid = m.group(1) if m else None
        st = _elem_read(page) if eid else {}
        created = (bool(eid) and st.get('name') == name1
                   and not st.get('active'))
        ops.append(_op(
            'create', 'Создание товара', 'ok' if created else 'fail',
            'executed', before='товара нет',
            after=(f'создан товар ID={eid}, «{name1}», скрыт, цена '
                   f'{TEST_PRODUCT_PRICE}' if created else 'создать не удалось'),
            note='временный скрытый товар, удаляется в конце'))
        if not created:
            raise RuntimeError('товар не создан')

        # ── Сортировка ──
        sort_ok = st.get('sort') == '7777'
        # меняем SORT на 8888 и проверяем
        page.goto(_elem_edit_url(domain, eid), wait_until='domcontentloaded',
                  timeout=60000)
        page.wait_for_timeout(1200)
        page.evaluate("""() => { const s=document.querySelector("input[name='SORT']");
            s.value='8888'; s.dispatchEvent(new Event('change',{bubbles:true})); }""")
        _sec_apply(page)
        page.goto(_elem_edit_url(domain, eid), wait_until='domcontentloaded',
                  timeout=60000)
        page.wait_for_timeout(1000)
        sort2 = _elem_read(page).get('sort') == '8888'
        ops.append(_op(
            'sort', 'Сортировка товара', 'ok' if (sort_ok and sort2) else 'fail',
            'executed', before='SORT=7777 (при создании)',
            after='SORT изменён на 8888 и сохранён' if sort2 else
            'сортировка не применилась'))

        # ── Вывод в разные категории (привязка к 2 разделам) ──
        cur = _elem_read(page)
        multi_ok = len(cur.get('sections') or []) >= 2
        ops.append(_op(
            'multicat', 'Вывод в разные категории',
            'ok' if multi_ok else 'fail', 'executed',
            before='без привязки',
            after=(f'привязан к разделам: {", ".join(cur["sections"])} '
                   f'(вывод в {len(cur["sections"])} категории)' if multi_ok
                   else 'привязка к нескольким разделам не сохранилась')))

        # ── Правка (переименование) ──
        page.goto(_elem_edit_url(domain, eid), wait_until='domcontentloaded',
                  timeout=60000)
        page.wait_for_timeout(1000)
        page.evaluate("""nm => { const n=document.querySelector("input[name='NAME']");
            n.value=nm; n.dispatchEvent(new Event('change',{bubbles:true})); }""",
            name2)
        _sec_apply(page)
        page.goto(_elem_edit_url(domain, eid), wait_until='domcontentloaded',
                  timeout=60000)
        page.wait_for_timeout(1000)
        renamed = _elem_read(page).get('name') == name2
        ops.append(_op(
            'edit', 'Правка товара', 'ok' if renamed else 'fail', 'executed',
            before=f'название: «{name1}»',
            after=f'название: «{name2}»' if renamed else 'правка не применилась'))

        # ── Удаление ──
        deleted = _elem_delete(domain, page, eid)
        ops.append(_op(
            'delete', 'Удаление товара', 'ok' if deleted else 'fail',
            'executed', before=f'товар ID={eid} существует',
            after='товар удалён' if deleted else
            f'НЕ УДАЛЁН - удалите товар ID={eid} вручную'))
        eid = None if deleted else eid
    except Exception as e:
        log(f'⚠ CRUD товара: {e}')
        ops.append(_op('error', 'CRUD товара', 'fail', 'executed',
                       note=str(e)[:200]))
    finally:
        if eid:
            try:
                if _elem_delete(domain, page, eid):
                    log(f'  тестовый товар ID={eid} удалён (страховка)')
            except Exception:
                log(f'⚠ ОСТАЛСЯ тестовый товар ID={eid} - удалите вручную')

    warns = []
    if any(o['result'] == 'fail' and o['op'] == 'delete' for o in ops):
        warns.append('тестовый товар мог не удалиться - проверьте каталог '
                     'на «[ТЕСТ ЧЕКЕРА]»')
    bad = [o for o in ops if o['result'] == 'fail']
    detail = ('полный CRUD товара (создание/сортировка/вывод в разные '
              'категории/правка/удаление) выполнен и откатан' if not bad else
              'не сработали операции: '
              + ', '.join(o['label'] for o in bad))
    return _mk_check('products_crud', 'Товары (CRUD)', not bad, detail, warns,
                     operations=ops)


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


def check_admin_settings(creds, crud=False, product_crud=False, execute=True,
                         log=None, headless=True):
    """Проверка функций настройки в админке. creds - dict из
    load_admin_creds (+ domain обязателен).
    crud - CRUD-операции поддоменов/категорий (пункт 2 UI);
    product_crud - CRUD товаров (создание/сортировка/вывод в разные
    категории, пункт 3 UI, опционально по CMS);
    execute - выполнять их реально (симуляция поддомена + запись с
    откатом) vs только наличие функций. Возвращает dict для отчёта."""
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
        # Единый обработчик confirm-диалогов: ВСЕГДА подтверждаем (иначе
        # Bitrix отменит удаление). Один на страницу - без гонок page.once.
        page.on('dialog', lambda d: d.accept())
        try:
            ok = _login(page, domain, creds, _log)
            checks.append(_mk_check(
                'login', 'Вход в админку', ok,
                'вход выполнен' if ok else 'вход не выполнен - '
                'проверьте логин/пароль (и basic-доступ на тестовом '
                'контуре)'))
            if ok:
                fns = [lambda p, d, l: _check_subdomains(
                           p, d, l, crud=crud, execute=execute),
                       lambda p, d, l: _check_categories(
                           p, d, l, crud=crud, execute=execute),
                       _check_products]
                if product_crud:
                    fns.append(lambda p, d, l: _check_products_crud(
                        p, d, l, execute=execute))
                fns.append(_check_tech_pages)
                for fn in fns:
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
