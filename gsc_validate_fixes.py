"""
gsc_validate_fixes.py
=====================
Автоматически запускает «Проверить исправление» по причинам неиндексирования
в Google Search Console – для всех ресурсов (домены и поддомены).

Поток (проверен на живом GSC):
    ресурс → Индексирование/Страницы → таблица причин (tr[data-rowid]) →
    для каждой причины со статусом «Не начато»:
        клик строки → страница причины → кнопка «Проверить исправление» →
        подтверждение → назад → следующая причина (сверху вниз).

Подготовка:
    1. python gsc_save_session.py            # авторизованный Chrome на порту 9222
       (НЕ закрывай его)
    2. python gsc_list_properties.py         # соберёт gsc_properties.json (ресурсы)

Запуск:
    python gsc_validate_fixes.py                       # все ресурсы из gsc_properties.json
    python gsc_validate_fixes.py --filter mepen.ru     # только ресурсы с этой подстрокой
    python gsc_validate_fixes.py --resource "https://vladimir.mepen.ru/"   # один ресурс
    python gsc_validate_fixes.py --dry-run             # без клика кнопки
    python gsc_validate_fixes.py --limit 20            # не больше 20 причин за запуск

Логика статусов:
    Обрабатываем только причины со статусом «Не начато».
    «Идёт проверка»/«Пройдена»/«Не удалось» – пропускаем (уже запускались).
"""

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

CDP_URL = 'http://127.0.0.1:9222'
PROPS_FILE = Path('gsc_properties.json')
LOG_FILE = Path('gsc_validate_log.json')

INDEX_REPORT = 'https://search.google.com/search-console/index?resource_id={rid}'
VALIDATE_TEXT = 'Проверить исправление'
DETAILS_TEXT = 'Подробности'            # span.Zfuf2d на странице причины-ошибки
NEW_CHECK_TEXT = 'Начать новую проверку'  # span.b88Yg на странице деталей

# Статусы столбца «Проверка»
STATUS_NOT_STARTED = 'Не начато'
STATUS_ERROR = 'Ошибка'
# Обрабатываем эти статусы (каждый своим путём)
STATUS_PROCESS = (STATUS_NOT_STARTED, STATUS_ERROR)
# Все известные статусы – чтобы читать значение из ячейки «Проверка» точно
# (а не ловить слово «Ошибка» внутри названия причины «Ошибка сервера (5xx)»).
KNOWN_STATUSES = (
    'Не начато', 'Ошибка', 'Отсутствует', 'Идёт проверка', 'Начата',
    'Пройдена', 'Выполнено', 'Не удалось',
)


def _log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    pfx = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ '}.get(level, '  ')
    print(f'[{ts}] {pfx}{msg}')


def _save_log(entries):
    LOG_FILE.write_text(
        json.dumps({'run_at': datetime.now().isoformat(), 'entries': entries},
                   ensure_ascii=False, indent=2), encoding='utf-8')


def _reason_name(row_text: str) -> str:
    name = row_text
    for src in ('Системы Google', 'Сайт'):
        if src in name:
            name = name.split(src)[0]
            break
    return name.strip().strip('|').strip()


async def _goto_backoff(page, url: str, tries: int = 6) -> bool:
    """Переход с защитой от 429 (Too Many Requests): экспоненциальный бэкофф."""
    delay = 20
    for i in range(tries):
        try:
            resp = await page.goto(url, wait_until='domcontentloaded')
        except Exception as e:
            _log(f'goto упал ({e}) – пауза {delay}с', 'warn')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        await page.wait_for_timeout(2000)
        status = resp.status if resp else 0
        head = (await page.inner_text('body'))[:300]
        if status == 429 or 'Too many requests' in head or 'Слишком много запросов' in head:
            _log(f'⚠ 429 (слишком много запросов) – пауза {delay}с (попытка {i+1})', 'warn')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        return True
    _log('429 не прошёл после ретраев – пропускаю ресурс', 'error')
    return False


async def _open_report(page, rid: str) -> bool:
    ok = await _goto_backoff(page, INDEX_REPORT.format(rid=quote(rid, safe='')))
    if not ok:
        return False
    await page.wait_for_timeout(3000)
    # Скроллим до конца – второй блок «Проблемы с представлением страниц
    # в результатах поиска» подгружается ниже.
    for _ in range(6):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(400)
    await page.wait_for_timeout(800)
    return True


def _status_from_text(txt: str) -> str:
    """Статус столбца «Проверка». Ищем в части строки ПОСЛЕ источника
    («Сайт»/«Системы Google») – там название причины уже отрезано, поэтому
    слово «Ошибка» из названия «Ошибка сервера (5xx)» не мешает.
    Бейдж может содержать иконку/дату ('error Ошибка', 'Ошибка 11.06.2026'),
    поэтому ищем подстрокой, а не точным равенством."""
    tail = txt
    for src in ('Системы Google', 'Сайт'):
        if src in tail:
            tail = tail.split(src, 1)[1]
            break
    for s in KNOWN_STATUSES:
        if s in tail:
            return s
    return '?'


async def _read_reasons(page) -> list[dict]:
    out = []
    for tr in await page.query_selector_all('tr[data-rowid]'):
        try:
            rid = await tr.get_attribute('data-rowid')
            txt = (await tr.inner_text()).strip().replace('\n', ' ')
            if not txt:
                continue
            out.append({'rowid': rid, 'name': _reason_name(txt),
                        'status': _status_from_text(txt), 'text': txt})
        except Exception:
            pass
    return out


async def _validate_error(page, rid: str, reason: dict, dry_run: bool) -> dict:
    """Путь для статуса «Ошибка»: причина → ПОДРОБНОСТИ → НАЧАТЬ НОВУЮ ПРОВЕРКУ."""
    res = {'resource': rid, 'reason': reason['name'],
           'status': 'error', 'message': ''}
    try:
        row = page.get_by_text(reason['name'], exact=False).first
        await row.click(timeout=8000)
        await page.wait_for_timeout(4000)

        # «Подробности» (span.Zfuf2d)
        details = page.locator('span.Zfuf2d').first
        if await details.count() == 0:
            details = page.get_by_text(DETAILS_TEXT, exact=False).first
        try:
            await details.wait_for(state='visible', timeout=8000)
        except Exception:
            res['status'] = 'no_button'
            res['message'] = 'кнопки «Подробности» нет'
            _log(f'  {reason["name"][:50]} (Ошибка) – нет «Подробности»', 'warn')
            return res

        if dry_run:
            res['status'] = 'dry_run'
            res['message'] = '«Ошибка»: «Подробности» найдена, клик пропущен'
            _log(f'  [DRY RUN] {reason["name"][:50]} (Ошибка) – путь есть', 'ok')
            return res

        await details.click()
        await page.wait_for_timeout(3000)

        # «Начать новую проверку» (span.b88Yg)
        newcheck = page.locator('span.b88Yg').first
        if await newcheck.count() == 0:
            newcheck = page.get_by_text(NEW_CHECK_TEXT, exact=False).first
        try:
            await newcheck.wait_for(state='visible', timeout=8000)
            await newcheck.click()
            res['status'] = 'ok'
            res['message'] = '«Ошибка»: новая проверка запущена'
            _log(f'  ✓ (Ошибка) новая проверка: {reason["name"][:50]}', 'ok')
        except Exception:
            res['status'] = 'warn'
            res['message'] = '«Ошибка»: кнопки «Начать новую проверку» нет'
            _log('  (Ошибка) нет «Начать новую проверку»', 'warn')

        # Назад дважды к таблице причин
        await page.go_back()
        await page.wait_for_timeout(1500)
        await page.go_back()
        await page.wait_for_timeout(1500)

    except Exception as e:
        res['status'] = 'error'
        res['message'] = str(e)
        _log(f'  ошибка (Ошибка-путь): {e}', 'error')
    return res


async def _validate_one(page, rid: str, reason: dict, dry_run: bool) -> dict:
    # Статус «Ошибка» – отдельный путь (Подробности → Начать новую проверку)
    if reason['status'] == STATUS_ERROR:
        return await _validate_error(page, rid, reason, dry_run)

    res = {'resource': rid, 'reason': reason['name'],
           'status': 'error', 'message': ''}
    try:
        # Клик по причине ПО ИМЕНИ (rowid повторяется между двумя блоками,
        # поэтому по rowid нельзя – попадём не в тот блок).
        row = page.get_by_text(reason['name'], exact=False).first
        await row.click(timeout=8000)
        await page.wait_for_timeout(4000)

        # Кнопка «Проверить исправление»
        btn = page.get_by_text(VALIDATE_TEXT, exact=False).first
        try:
            await btn.wait_for(state='visible', timeout=8000)
        except Exception:
            res['status'] = 'no_button'
            res['message'] = 'кнопки «Проверить исправление» нет (проверка недоступна)'
            _log(f'  {reason["name"][:50]} – кнопки нет', 'warn')
            return res

        if dry_run:
            res['status'] = 'dry_run'
            res['message'] = 'кнопка найдена, клик пропущен'
            _log(f'  [DRY RUN] {reason["name"][:50]} – кнопка есть', 'ok')
            return res

        await btn.click()
        _log(f'  клик «Проверить исправление»: {reason["name"][:50]}')
        await page.wait_for_timeout(3000)

        # Подтверждение в диалоге (если появится)
        for ok in ('ПОНЯТНО', 'OK', 'Готово', 'Закрыть', 'НАЧАТЬ ПРОВЕРКУ',
                   'Подтвердить'):
            try:
                b = page.get_by_role('button', name=ok)
                if await b.is_visible(timeout=1500):
                    await b.click()
                    break
            except Exception:
                pass

        # Проверяем что проверка началась
        await page.wait_for_timeout(2000)
        body = await page.inner_text('body')
        if 'роверка' in body and ('началась' in body or 'идёт' in body.lower()
                                  or 'Идёт проверка' in body):
            res['status'] = 'ok'
            res['message'] = 'проверка запущена'
            _log('  ✓ проверка запущена', 'ok')
        else:
            res['status'] = 'ok'
            res['message'] = 'клик сделан (подтверждение не распознано)'
            _log('  ✓ клик сделан', 'ok')

    except Exception as e:
        res['status'] = 'error'
        res['message'] = str(e)
        _log(f'  ошибка: {e}', 'error')
    return res


async def _switch_filter(page, data_value: str) -> bool:
    """Переключить фильтр страниц отчёта GSC.
    Кликаем именно стрелку-дропдаун фильтра (div.e2CuFe.mJra4.eU809d) — рядом
    с текстом «Все обработанные страницы», НЕ иконку бокового меню. Затем
    выбираем пункт по data-value. True если переключили."""
    try:
        arrow = await page.query_selector('div.e2CuFe.mJra4.eU809d')
        if not arrow:
            return False
        await arrow.click()
        await page.wait_for_timeout(1500)
        # В DOM несколько элементов с этим data-value (скрытые дубли) —
        # берём ВИДИМЫЙ из открытого меню.
        opts = await page.query_selector_all(f'[data-value="{data_value}"]')
        if not opts:
            return False
        target = None
        for o in opts:
            try:
                if await o.is_visible():
                    target = o
                    break
            except Exception:
                continue
        if target is None:
            # ни один не виден — нативный JS-клик по первому (дёргает jsaction)
            await opts[0].evaluate('e => e.click()')
        else:
            try:
                await target.click(timeout=4000)
            except Exception:
                await target.evaluate('e => e.click()')
        await page.wait_for_timeout(3000)
        return True
    except Exception as e:
        _log(f'  фильтр {data_value}: переключение не удалось — {e}', 'warn')
        return False


async def process_resource(page, rid: str, dry_run: bool,
                           limit: int, done_counter: list) -> list:
    entries = []
    _log(f'\n── Ресурс: {rid} ──')
    if not await _open_report(page, rid):
        return entries

    reasons = await _read_reasons(page)
    if not reasons:
        _log('  причин не найдено (всё проиндексировано или другой layout)')
        return entries

    from collections import Counter
    _dist = Counter(r['status'] for r in reasons)
    _log(f'  причин в отчёте: {len(reasons)}; статусы: {dict(_dist)}')

    processed = set()   # общее для обоих фильтров — не кликать причину дважды

    async def _loop(filter_value: str = None):
        """Прокликать все необработанные причины текущего фильтра."""
        while True:
            if limit and done_counter[0] >= limit:
                _log(f'Достигнут лимит {limit}', 'warn')
                return
            # _open_report ниже перезагружает отчёт и СБРАСЫВАЕТ фильтр —
            # для второго фильтра переустанавливаем его перед чтением причин.
            if filter_value:
                await _switch_filter(page, filter_value)
            reasons = await _read_reasons(page)
            target = next((r for r in reasons
                           if r['status'] in STATUS_PROCESS
                           and r['name'] not in processed), None)
            if not target:
                return
            processed.add(target['name'])

            res = await _validate_one(page, rid, target, dry_run)
            entries.append(res)
            if res['status'] in ('ok', 'dry_run'):
                done_counter[0] += 1

            # Небольшая пауза между причинами – снижает риск 429
            await asyncio.sleep(2 + random.random() * 2)

            # Возврат к отчёту для следующей причины
            if not await _open_report(page, rid):
                return

    # Фильтр 1: «Все обработанные страницы» (дефолт при открытии)
    await _loop()

    # Фильтр 2: «Все отправленные страницы» (ALL_SUBMITTED_URLS)
    if not (limit and done_counter[0] >= limit):
        if await _switch_filter(page, 'ALL_SUBMITTED_URLS'):
            _log('  ── Фильтр: Все отправленные страницы ──')
            await _loop('ALL_SUBMITTED_URLS')
        else:
            _log('  фильтр «Все отправленные страницы» не найден — пропуск')

    return entries


async def run(resources: list, dry_run: bool, limit: int):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('pip install playwright')
        sys.exit(1)

    all_entries = []
    done_counter = [0]

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            _log(f'Нет подключения к Chrome ({CDP_URL}): {e}', 'error')
            _log('Сначала запусти gsc_save_session.py.')
            return

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        for idx, rid in enumerate(resources):
            if limit and done_counter[0] >= limit:
                break
            try:
                all_entries += await process_resource(page, rid, dry_run, limit, done_counter)
            except Exception as e:
                _log(f'Ресурс {rid} упал: {e}', 'error')
            _save_log(all_entries)
            # Пауза между ресурсами с джиттером – снижает риск 429
            if idx < len(resources) - 1:
                await asyncio.sleep(4 + random.random() * 4)

        await browser.close()

    ok = sum(1 for e in all_entries if e['status'] == 'ok')
    dr = sum(1 for e in all_entries if e['status'] == 'dry_run')
    nb = sum(1 for e in all_entries if e['status'] == 'no_button')
    er = sum(1 for e in all_entries if e['status'] == 'error')
    _log(f'\n══ Готово: запущено {ok}, dry-run {dr}, без кнопки {nb}, ошибок {er} ══')
    _log(f'Лог → {LOG_FILE.resolve()}')


def _resources_from_project(pid: str) -> list:
    """Ресурсы GSC из списка поддоменов проекта (catalogs/<pid>-subdomains.csv).
    Каждый URL-префикс домена/поддомена – это ресурс GSC. Ничего собирать не надо."""
    import csv
    csv_path = Path(__file__).parent / 'catalogs' / f'{pid}-subdomains.csv'
    if not csv_path.exists():
        _log(f'Нет файла поддоменов: {csv_path}', 'error')
        return []
    urls = []
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            u = (row.get('url') or '').strip()
            if u.startswith('http'):
                if not u.endswith('/'):
                    u += '/'
                if u not in urls:
                    urls.append(u)
    return urls


def _load_resources(project: str | None, filter_sub: str | None,
                    single: str | None) -> list:
    if single:
        return [single]
    if project:
        return _resources_from_project(project)
    # Фоллбэк: ранее собранный список ресурсов (gsc_list_properties.py)
    if not PROPS_FILE.exists():
        _log(f'Укажи --project <smu|mpe|imp> или --resource. '
             f'({PROPS_FILE} не найден)', 'error')
        return []
    try:
        items = json.loads(PROPS_FILE.read_text(encoding='utf-8'))
    except Exception:
        items = []
    if filter_sub:
        items = [r for r in items if filter_sub.lower() in r.lower()]
    return items


def parse_args():
    ap = argparse.ArgumentParser(
        description='Авто-«Проверить исправление» в Google Search Console')
    ap.add_argument('--project', default=None,
                    help='проект: smu|mpe|imp – ресурсы из catalogs/<pid>-subdomains.csv')
    ap.add_argument('--resource', default=None, help='один ресурс (resource_id)')
    ap.add_argument('--filter', default=None,
                    help='фильтр подстрокой (только для фоллбэка gsc_properties.json)')
    ap.add_argument('--dry-run', action='store_true', help='без клика кнопки')
    ap.add_argument('--limit', type=int, default=0, help='максимум причин за запуск')
    return ap.parse_args()


if __name__ == '__main__':
    a = parse_args()
    res = _load_resources(a.project, a.filter, a.resource)
    if not res:
        _log('Нет ресурсов для обработки.', 'error')
        sys.exit(1)
    _log(f'Ресурсов к обработке: {len(res)}'
         + (f'  [DRY RUN]' if a.dry_run else ''))
    asyncio.run(run(res, a.dry_run, a.limit))
