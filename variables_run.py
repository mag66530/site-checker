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

# Порядок и подписи переменных-колонок.
VAR_COLUMNS = ["Город", "Страна", "Тел. поиск", "Тел. реклама", "Тел. общий",
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


def _fetch(url: str, proxy: str | None):
    """Скачать HTML главной. Возвращает (html, ошибка)."""
    import requests
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36")}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=30,
                         allow_redirects=True)
        if r.status_code >= 400:
            return "", f"HTTP {r.status_code}"
        return r.text or "", ""
    except Exception as e:  # noqa: BLE001
        return "", str(e)


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
            страна.update(found=("есть чужие" if iss else "чисто"),
                          status=("bug" if iss else "ok"),
                          note=(iss[0].get("пояснение", "") if iss else ""))
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
    domains.sort(key=lambda x: x[1].city or x[0])

    proxy = (os.environ.get('proxy_url') or '').strip() or None
    if _use_proxy(a.project) and not proxy:
        _stamp('⚠️ У проекта use_proxy=true, а proxy_url не задан - '
               'зарубежный IP может блокироваться (будут ошибки загрузки).')

    ctx = build_region_context(
        kp, [SimpleNamespace(host=d, city=row.city, country=row.country)
             for d, row in kp.items()])

    _stamp(f'ПРОВЕРКА ПЕРЕМЕННЫХ (1.4) - {PROJECT_NAMES[a.project]} - '
           f'поддоменов: {len(domains)}')

    результаты = []
    for i, (dom, row) in enumerate(domains, 1):
        url = f'https://{dom}/'
        html, err = _fetch(url, proxy)
        if err:
            _stamp(f'  [{i}/{len(domains)}] {dom}: ошибка загрузки - {err}')
            результаты.append({"domain": dom, "city": row.city,
                               "country": row.country, "error": err, "fields": []})
            continue
        var = kpmod.check_variables(html, dom)
        город, страна = _регион_статусы(html, kpmod._norm_host(dom), ctx)
        var["fields"] = [город, страна] + var["fields"]
        var["error"] = ""
        результаты.append(var)
        _плохих = sum(1 for f in var["fields"] if f["status"] == "bug")
        _stamp(f'  [{i}/{len(domains)}] {dom} ({row.city}): '
               + ('все ок' if not _плохих else f'расхождений: {_плохих}'))

    work = WORK_ROOT / a.project
    work.mkdir(parents=True, exist_ok=True)
    xlsx = work / 'variables.xlsx'
    _записать_xlsx(xlsx, PROJECT_NAMES[a.project], результаты)
    _stamp(f'Отчёт сохранён: {xlsx}')
    _stamp('✅ ВСЁ ГОТОВО')
    return 0


def _записать_xlsx(path: Path, proj_name: str, результаты: list) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Переменные"
    hdr_fill = PatternFill("solid", fgColor="EEF3FB")
    headers = ["Поддомен", "Город(КП)", "Страна(КП)"] + VAR_COLUMNS
    # «Город»/«Страна» из VAR_COLUMNS дублируют колонки КП по смыслу - оставляем
    # обе: слева «что ожидаем по КП», в блоке переменных - «статус проверки».
    for c, t in enumerate(headers, 1):
        cell = ws.cell(1, c, t)
        cell.font = Font(bold=True)
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "B2"

    расхождения = []
    r = 2
    for res in результаты:
        ws.cell(r, 1, res["domain"])
        ws.cell(r, 2, res.get("city", ""))
        ws.cell(r, 3, res.get("country", ""))
        by = {f["field"]: f for f in res.get("fields", [])}
        if res.get("error"):
            hc = ws.cell(r, 4, f"ошибка загрузки: {res['error']}")
            hc.font = Font(color="C62828")
            r += 1
            continue
        for c, name in enumerate(VAR_COLUMNS, 4):
            f = by.get(name)
            if not f:
                ws.cell(r, c, "—")
                continue
            cell = ws.cell(r, c, _SYMBOL.get(f["status"], "?"))
            cell.font = Font(color=_COLOR.get(f["status"], "000000"), bold=True)
            cell.alignment = Alignment(horizontal="center")
            # детали в примечание ячейки
            from openpyxl.comments import Comment
            подпись = f"ожидалось: {f['expected']}\nна сайте: {f['found']}"
            if f.get("note"):
                подпись += f"\n{f['note']}"
            cell.comment = Comment(подпись, "1.4")
            if f["status"] == "bug":
                расхождения.append((res["domain"], res.get("city", ""), name,
                                    f["expected"], f["found"], f.get("note", "")))
        r += 1

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 14

    # Лист «Расхождения» - только проблемные ячейки, для быстрого разбора.
    ws2 = wb.create_sheet("Расхождения")
    for c, t in enumerate(["Поддомен", "Город", "Переменная", "Ожидалось (КП)",
                           "На сайте", "Примечание"], 1):
        cell = ws2.cell(1, c, t)
        cell.font = Font(bold=True)
        cell.fill = hdr_fill
    for i, row in enumerate(расхождения, 2):
        for c, v in enumerate(row, 1):
            ws2.cell(i, c, v)
    for col, w in (("A", 32), ("B", 16), ("C", 14), ("D", 30), ("E", 30), ("F", 40)):
        ws2.column_dimensions[col].width = w
    ws2.freeze_panes = "A2"
    if not расхождения:
        ws2.cell(2, 1, "Расхождений не найдено 🎉")

    wb.save(path)


if __name__ == '__main__':
    raise SystemExit(main())
