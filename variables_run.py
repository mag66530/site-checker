"""
variables_run.py - фоновый прогон проверки «главных переменных» (пункт 1.4).

Для каждого поддомена из «Карты присутствия» (catalogs/{proj}-kp.csv) качает
главную страницу и сверяет с КП:
  • город / страна - нет ли чужого (region_checker);
  • телефоны (поиск/реклама/общий) - номер на сайте входит в набор КП города;
  • почта, адрес, Telegram, WhatsApp - совпадают с КП.
Результат пишется в cache/variables/<proj>/variables.xlsx (лист «Переменные» +
лист «Расхождения»). Прогресс идёт в stdout, откуда его читает вкладка.

Запуск:
    python variables_run.py --project smu
    python variables_run.py --project imp --cities "Москва,Казань"
Прокси (для проектов, блокирующих зарубежный IP) - через env proxy_url.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent
WORK_ROOT = ROOT / 'cache' / 'variables'

PROJECT_NAMES = {
    'smu': 'СМУ - Стальметурал', 'imp': 'ИМП - Инметпром',
    'mpe': 'МПЭ - Мепэн', 'avia': 'АПС - Авиапромсталь',
}

# Порядок и подписи переменных-колонок. «Страна» убрана по просьбе заказчика
# (проверка чужих стран давала шум); справочная колонка «Страна(КП)» остаётся.
VAR_COLUMNS = ["Город", "Тел. поиск", "Тел. реклама", "Тел. общий",
               "Почта", "Адрес", "Telegram", "WhatsApp"]

_SYMBOL = {"ok": "✓", "ok_set": "✓", "bug": "✗", "warn": "⚠", "na": "—"}
_COLOR = {"ok": "1E8E3E", "ok_set": "1E8E3E", "bug": "C62828",
          "warn": "B26A00", "na": "9E9E9E"}


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _use_proxy(project: str) -> bool:
    p = ROOT / 'projects' / f'{project}.json'
    try:
        return bool(json.loads(p.read_text(encoding='utf-8')).get('use_proxy'))
    except Exception:
        return False


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Для повторов «как из другого браузера»: разные User-Agent + всегда без кеша.
_UA_POOL = [
    _UA,
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
     "Gecko/20100101 Firefox/125.0"),
    ("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"),
]


def _proxy_parts(proxy):
    """(proxy_host, proxy_port, proxy_headers|None) из proxy-URL. () если нет."""
    if not proxy:
        return None
    from urllib.parse import urlparse
    pr = urlparse(proxy if '://' in proxy else 'http://' + proxy)
    if not pr.hostname:
        return None
    headers = {}
    if pr.username:
        import base64
        tok = base64.b64encode(
            f"{pr.username}:{pr.password or ''}".encode()).decode()
        headers['Proxy-Authorization'] = f'Basic {tok}'
    return pr.hostname, pr.port or 8080, headers


def _fetch_one(dom, proxy_parts, ua=None):
    """Скачивает https://<dom>/ через http.client (CONNECT-туннель с
    Proxy-Authorization в CONNECT-запросе - надёжный способ прокси-авторизации
    для HTTPS, в отличие от aiohttp, который упорно отдавал 407). Один редирект
    в пределах того же/родственного хоста поддерживаем. Каждый вызов - свежее
    соединение без кеша/куки (по сути «инкогнито»); ua позволяет притвориться
    другим браузером на повторах. → (html, ошибка)."""
    import http.client
    import ssl
    ua = ua or _UA

    def _get(host, path, depth=0):
        conn = None
        try:
            if proxy_parts:
                phost, pport, phdrs = proxy_parts
                conn = http.client.HTTPSConnection(
                    phost, pport, timeout=30, context=ssl.create_default_context())
                conn.set_tunnel(host, 443, headers=dict(phdrs))
            else:
                conn = http.client.HTTPSConnection(host, 443, timeout=30)
            conn.request('GET', path or '/', headers={
                'User-Agent': ua, 'Accept-Encoding': 'identity',
                'Accept': 'text/html,application/xhtml+xml',
                'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and depth < 3:
                loc = resp.getheader('Location') or ''
                resp.read()
                conn.close()
                from urllib.parse import urlparse, urljoin
                nu = urlparse(urljoin(f'https://{host}{path or "/"}', loc))
                return _get(nu.hostname or host,
                            (nu.path or '/') + (f'?{nu.query}' if nu.query else ''),
                            depth + 1)
            if resp.status >= 400:
                resp.read()
                return '', f'HTTP {resp.status}'
            data = resp.read()
            html = data.decode('utf-8', 'replace')
            # Антибот/заглушка вместо страницы (частый ответ при частых запросах):
            # либо совсем короткий ответ, либо явные маркеры проверки браузера.
            # Помечаем ошибкой - тогда fetch_all повторит попытку свежим соединением.
            low = html[:5000].lower()
            _block = ('ddos-guard', 'challenge-platform', 'attention required',
                      'checking your browser', 'проверяем ваш браузер',
                      'проверка вашего браузера', 'запрос отправили вы, а не робот',
                      'доступ ограничен', 'captcha-delivery')
            if len(html) < 1500 or (len(html) < 25000 and any(m in low for m in _block)):
                return '', 'похоже на антибот/капчу'
            return html, ''
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    try:
        return _get(dom, '/')
    except Exception as e:  # noqa: BLE001
        return '', (str(e)[:200] or e.__class__.__name__)


def fetch_all(domains, proxy, log, retries=3):
    """Качает главные всех поддоменов параллельно (пул потоков), печатает
    прогресс «[i/N]». Не загрузившиеся (500/таймаут/антибот) повторяет ещё
    `retries` раз - только их, «как из другого браузера»: свежее соединение
    без кеша (инкогнито) + другой User-Agent на каждом заходе, помягче (меньший
    пул + пауза). Успешные повторно не трогает. → {domain: (html, ошибка)}."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time
    parts = _proxy_parts(proxy)
    out: dict = {}
    N = len(domains)

    def _pass(items, workers, counting, ua=None):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_one, d, parts, ua): (d, row.city)
                    for d, row in items}
            for k, fut in enumerate(as_completed(futs), 1):
                dom, city = futs[fut]
                try:
                    html, err = fut.result()
                except Exception as e:  # noqa: BLE001
                    html, err = '', str(e)[:200]
                out[dom] = (html, err)
                if counting:
                    log(f'  [{k}/{N}] {dom} ({city}): '
                        + (f'ошибка загрузки - {err}' if err else 'загружено'))
                else:
                    log(f'    повтор {dom} ({city}): '
                        + (f'снова ошибка - {err}' if err else 'загружено ✓'))

    _pass(domains, 6, counting=True)
    # Повторяем ТОЛЬКО упавшие: сервер часто отдаёт 500 при частых параллельных
    # запросах - на повторе меньшим пулом, с паузой и от имени другого браузера
    # большинство доходит. Успешные не перезапрашиваем.
    for attempt in range(1, retries + 1):
        failed = [(d, row) for d, row in domains if out.get(d, ('', ''))[1]]
        if not failed:
            break
        ua = _UA_POOL[attempt % len(_UA_POOL)]
        log(f'↻ Повтор {attempt}/{retries}: заново пробуем {len(failed)} '
            'не загрузившихся, как из другого браузера (успешные не трогаем)…')
        time.sleep(4)
        _pass(failed, 3, counting=False, ua=ua)
    n_ok = sum(1 for v in out.values() if not v[1])
    n_fail = N - n_ok
    log(f'Итог загрузки: {n_ok} из {N}'
        + (f' (осталось с ошибкой: {n_fail})' if n_fail else ' (все загрузились)'))
    return out


def _регион_статусы(html, host, ctx):
    """Город/страна через region_checker → (город_dict, страна_dict) в формате
    check_variables-поля {field, expected, found, status, note}."""
    import region_checker as rc
    город = {"field": "Город", "expected": ctx.host_city.get(host, "—"),
             "found": "—", "status": "na", "note": ""}
    страна = {"field": "Страна", "expected": ctx.host_country.get(host, "—"),
              "found": "—", "status": "na", "note": ""}
    try:
        rv = rc.check_region_vars(html, host, ctx)
        if rv is not None:
            iss = rv.get("issues", [])
            город.update(found=("чужой город" if iss else "свой"),
                         status=("bug" if iss else "ok"),
                         note=(iss[0].get("пояснение", "") if iss else ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        cm = rc.check_cis_mentions(html, host, ctx)
        if cm is None:
            страна.update(status="na", note="РФ - проверка чужих стран не нужна"
                          if ctx.host_country.get(host) == "Россия" else "")
        else:
            iss = cm.get("issues", [])
            if iss:
                _zru = {'title': 'title', 'description': 'description',
                        'h1': 'H1', 'текст': 'текст страницы'}
                parts = []
                for it in iss[:2]:
                    z = _zru.get(it.get('зона', ''), it.get('зона', ''))
                    ctxt = (it.get('контекст') or '').strip()
                    parts.append(f'«{it.get("найдено", "?")}» в {z}: …{ctxt}…'
                                 if ctxt else f'«{it.get("найдено", "?")}» в {z}')
                note = '; '.join(parts)
                if len(iss) > 2:
                    note += f' (и ещё {len(iss) - 2})'
                страна.update(found="есть чужая страна", status="bug", note=note)
            else:
                страна.update(found="чисто", status="ok", note="")
    except Exception:  # noqa: BLE001
        pass
    return город, страна


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True, choices=list(PROJECT_NAMES))
    ap.add_argument('--cities', default='', help='города через запятую (пусто = все)')
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    import kp as kpmod
    from region_checker import build_region_context

    kp = kpmod.load_kp(a.project)
    if not kp:
        _stamp(f'✗ Нет базы КП catalogs/{a.project}-kp.csv')
        return 2

    wanted = {c.strip().lower() for c in a.cities.split(',') if c.strip()}
    domains = [(d, row) for d, row in kp.items()
               if not wanted or (row.city or '').lower() in wanted]
    # Порядок как в КП: страны в порядке появления в КП, но Россия первой;
    # внутри страны сохраняем исходный порядок КП (сортировка стабильная).
    _country_seq = []
    for _d, _row in kp.items():
        _c = (_row.country or '').strip()
        if _c and _c not in _country_seq:
            _country_seq.append(_c)

    def _crank(row):
        c = (row.country or '').strip()
        if c.lower() in ('россия', 'рф'):
            return -1
        return _country_seq.index(c) if c in _country_seq else 10 ** 6
    domains.sort(key=lambda x: _crank(x[1]))

    # Прокси используем ТОЛЬКО для проектов с use_proxy=true (напр. ИМП, который
    # блокирует зарубежный IP). СМУ/МПЭ (use_proxy=false) качаем напрямую - им
    # прокси не нужен, а сломанный proxy_url иначе давал бы им ложный 407.
    proxy = (os.environ.get('proxy_url') or '').strip() or None
    if _use_proxy(a.project):
        if not proxy:
            _stamp('⚠️ У проекта use_proxy=true, а proxy_url не задан - '
                   'зарубежный IP может блокироваться (будут ошибки загрузки).')
    else:
        if proxy:
            _stamp(f'Проект {a.project}: use_proxy=false - страницы качаем '
                   'напрямую, без прокси.')
        proxy = None
    # Диагностика прокси (без вывода самих логина/пароля).
    _pp = _proxy_parts(proxy)
    if _pp:
        _ph, _pport, _phdrs = _pp
        _stamp(f'Прокси: {_ph}:{_pport}; авторизация в proxy_url: '
               + ('есть' if _phdrs.get('Proxy-Authorization')
                  else 'НЕТ - в ссылке нет логина:пароля (будет 407)'))
    elif proxy:
        _stamp('⚠️ proxy_url задан, но не разобрался '
               '(ожидается http://логин:пароль@хост:порт).')

    ctx = build_region_context(
        kp, [SimpleNamespace(host=d, city=row.city, country=row.country)
             for d, row in kp.items()])

    _stamp(f'ПРОВЕРКА ПЕРЕМЕННЫХ (1.4) - {PROJECT_NAMES[a.project]} - '
           f'поддоменов: {len(domains)}')

    html_map = fetch_all(domains, proxy, _stamp)
    _n407 = sum(1 for h, e in html_map.values() if '407' in (e or ''))
    if _n407 and _n407 == len(html_map):
        _stamp('⚠️ ВСЕ страницы вернули 407 Proxy Authentication Required - '
               'прокси отклонил авторизацию. Проверь логин:пароль в секрете '
               'proxy_url (формат http://логин:пароль@хост:порт).')
    _stamp('Загрузка завершена, сверяю с КП …')
    результаты = []
    for dom, row in domains:
        html, err = html_map.get(dom, ("", "не загружено"))
        if err:
            результаты.append({"domain": dom, "city": row.city,
                               "country": row.country, "error": err, "fields": []})
            continue
        var = kpmod.check_variables(html, dom)
        город, _страна = _регион_статусы(html, kpmod._norm_host(dom), ctx)
        var["fields"] = [город] + var["fields"]   # «Страна» убрана из отчёта
        var["error"] = ""
        результаты.append(var)

    work = WORK_ROOT / a.project
    work.mkdir(parents=True, exist_ok=True)
    xlsx = work / 'variables.xlsx'
    _записать_xlsx(xlsx, PROJECT_NAMES[a.project], результаты)
    _stamp(f'Отчёт сохранён: {xlsx}')
    _stamp('✅ ВСЁ ГОТОВО')
    return 0


_ЛЕГЕНДА = [
    ("Как читать результат", True),
    ("", False),
    ("✓  — значение на сайте совпадает с КП "
     "(для телефона: номер входит в набор номеров города из КП).", False),
    ("✗  — расхождение. В примечании ячейки: «Ожидалось (КП) / По факту (на сайте)». "
     "Все расхождения также собраны на листе «Расхождения».", False),
    ("⚠  — на сайте не найдено (телефон / почта / адрес / мессенджер).", False),
    ("—  — в КП этого поля нет (проверять не с чем).", False),
]


def _написать_легенду(ws) -> None:
    from openpyxl.styles import Font, Alignment
    ws.column_dimensions["A"].width = 100
    for i, (text, bold) in enumerate(_ЛЕГЕНДА, 1):
        cell = ws.cell(i, 1, text)
        cell.font = Font(bold=bold, size=14 if (bold and i == 1) else 11)
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _записать_xlsx(path: Path, proj_name: str, результаты: list) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    # Лист-подсказка «Как читать результат» - первым (перед данными). Просьба заказчика.
    _написать_легенду(wb.active)
    wb.active.title = "Как читать результат"
    ws = wb.create_sheet("Переменные")
    hdr_fill = PatternFill("solid", fgColor="EEF3FB")
    # Порядок по просьбе заказчика: Страна(КП), затем Город(КП) со ссылкой на
    # домен/поддомен, дальше проверяемые переменные.
    headers = ["Страна(КП)", "Город(КП)"] + VAR_COLUMNS
    for c, t in enumerate(headers, 1):
        cell = ws.cell(1, c, t)
        cell.font = Font(bold=True)
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "C2"

    from openpyxl.comments import Comment
    LINK_FONT = Font(color="1155CC", underline="single")
    # Заливка только у проблемных ячеек, чтобы зелёные ✓ оставались чистыми
    # (без «тревожного» красного уголка-примечания на каждой ячейке).
    BUG_FILL = PatternFill("solid", fgColor="FDE3E3")   # мягкий красный
    WARN_FILL = PatternFill("solid", fgColor="FFF2DA")  # мягкий оранжевый
    _FIRST_VAR_COL = 3   # A=Страна(КП), B=Город(КП), переменные с C

    расхождения = []
    r = 2
    for res in результаты:
        ws.cell(r, 1, res.get("country", ""))
        # Город(КП) - текст города, кликом ведёт на домен/поддомен.
        gcell = ws.cell(r, 2, res.get("city", "") or res["domain"])
        gcell.hyperlink = f'https://{res["domain"]}'
        gcell.font = LINK_FONT
        by = {f["field"]: f for f in res.get("fields", [])}
        if res.get("error"):
            hc = ws.cell(r, _FIRST_VAR_COL, f"ошибка загрузки: {res['error']}")
            hc.font = Font(color="C62828")
            r += 1
            continue
        for c, name in enumerate(VAR_COLUMNS, _FIRST_VAR_COL):
            f = by.get(name)
            if not f:
                ws.cell(r, c, "—")
                continue
            status = f["status"]
            cell = ws.cell(r, c, _SYMBOL.get(status, "?"))
            cell.font = Font(color=_COLOR.get(status, "000000"), bold=True)
            cell.alignment = Alignment(horizontal="center")
            # Примечание + заливку вешаем ТОЛЬКО на проблемные ячейки (✗ и ⚠) -
            # тогда зелёные ✓ чистые, а красные сразу видно (красная заливка +
            # уголок-примечание с деталями). Просьба заказчика.
            if status in ("bug", "warn"):
                # Примечание - ВВЕРХУ (сразу видно, в чём дело), ниже - что ждали
                # и что по факту на сайте.
                note = (f.get("note") or "").strip()
                подпись = f"Примечание: {note}\n\n" if note else ""
                подпись += (f"Ожидалось (КП): {f['expected']}\n"
                            f"По факту (на сайте): {f['found']}")
                # Длинное не расписываем в ячейке - отсылаем на лист «Расхождения».
                if len(подпись) > 220:
                    подпись = подпись[:210].rstrip() + "…\n→ см. лист «Расхождения»"
                cm = Comment(подпись, "1.4")
                cm.width, cm.height = 340, 170   # чтобы текст влезал в окошко
                cell.comment = cm
                cell.fill = BUG_FILL if status == "bug" else WARN_FILL
            if status == "bug":
                расхождения.append((res["domain"], res.get("city", ""), name,
                                    f["expected"], f["found"], f.get("note", "")))
        r += 1

    ws.column_dimensions["A"].width = 16   # Страна(КП)
    ws.column_dimensions["B"].width = 26   # Город(КП) со ссылкой

    # Лист «Расхождения» - только проблемные ячейки, для быстрого разбора.
    ws2 = wb.create_sheet("Расхождения")
    for c, t in enumerate(["Поддомен", "Город", "Переменная", "Ожидалось (КП)",
                           "На сайте", "Примечание"], 1):
        cell = ws2.cell(1, c, t)
        cell.font = Font(bold=True)
        cell.fill = hdr_fill
    for i, row in enumerate(расхождения, 2):
        for c, v in enumerate(row, 1):
            cell = ws2.cell(i, c, v)
            if c in (4, 5, 6):     # «Ожидалось», «На сайте», «Примечание» - переносим
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in (("A", 32), ("B", 16), ("C", 14), ("D", 34), ("E", 34), ("F", 70)):
        ws2.column_dimensions[col].width = w
    ws2.freeze_panes = "A2"
    if not расхождения:
        ws2.cell(2, 1, "Расхождений не найдено 🎉")

    wb.save(path)


if __name__ == '__main__':
    raise SystemExit(main())
