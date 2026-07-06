"""
Проверка админки Bitrix «Уведомления с форм» (модуль pixana_forms_list).

Идея: после отправки форм тест заходит в админку, открывает список заявок за
сегодня и проверяет, что НАША заявка реально там появилась (форма долетела до
бэкенда, а не только показала «спасибо»).

Сопоставление — по «Тип формы» + времени отправки (без метки в заявке):
админка пишет время до секунды, а движок знает, когда отправил форму.
Логин/пароль берём из локального файла admin.local.json (в git не хранится).
"""
import json
import re
from datetime import datetime
from pathlib import Path


def построить_url_списка(домен: str, дата: datetime) -> str:
    """URL списка заявок за один день (фильтр прямо в параметрах, как в примере)."""
    d = дата.strftime("%Y-%m-%d")
    домен = домен.rstrip("/")
    return (f"{домен}/bitrix/admin/pixana_forms_list.php?lang=ru&form_type=all"
            f"&find_date_from={d}&find_date_to={d}")


def _текст(html: str) -> str:
    html = html.replace("&nbsp;", " ")
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def разобрать_заявки(html: str) -> list:
    """Разбирает таблицу adm-list-table в список заявок:
    [{id, дата, время, дата_время, тип_формы, город, имя, телефон, email, все_данные}]."""
    заявки = []
    # строки таблицы
    строки = re.split(r'<tr[^>]*class="[^"]*adm-list-table-row', html)[1:]
    for s in строки:
        cells = re.findall(r'<td[^>]*class="[^"]*adm-list-table-cell[^"]*"[^>]*>(.*?)</td>',
                           s, re.S)
        vals = [_текст(c) for c in cells]
        if len(vals) < 7:
            continue
        # колонки: 0 ID | 1 Дата | 2 Тип формы | 3 Город | 4 Имя | 5 Телефон | 6 Email | 7 Файл | 8 Все данные
        дт = vals[1]
        m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})", дт)
        дата = m.group(1) if m else ""
        время = m.group(2) if m else ""
        заявки.append({
            "id": vals[0],
            "дата": дата,
            "время": время,
            "дата_время": дт,
            "тип_формы": vals[2] if len(vals) > 2 else "",
            "город": vals[3] if len(vals) > 3 else "",
            "имя": vals[4] if len(vals) > 4 else "",
            "телефон": vals[5] if len(vals) > 5 else "",
            "email": vals[6] if len(vals) > 6 else "",
            "все_данные": vals[8] if len(vals) > 8 else "",
        })
    return заявки


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _тип_похож(тип_админ: str, наше_название: str) -> bool:
    """Нестрогое совпадение «Тип формы» из админки и нашего названия формы
    (без учёта регистра/скобок/пробелов; подстрока в любую сторону)."""
    a, b = _norm(тип_админ), _norm(наше_название)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _это_наша_заявка(row: dict, почта: str, телефон: str, имя: str) -> bool:
    """True, если строка админки похожа на НАШУ тестовую заявку (по почте /
    телефону / имени) — так отсеиваем реальные клиентские заявки."""
    if почта and _norm(row.get("email", "")) == _norm(почта):
        return True
    n = _norm(имя)
    if n and n in _norm(row.get("имя", "")):
        return True
    # Телефон: сайт может переформатировать номер (сдвиг кода страны), поэтому
    # сравниваем ХВОСТ цифр — для телефонных форм без имени/почты это единственная зацепка.
    d, rd = _digits(телефон), _digits(row.get("телефон", ""))
    if len(d) >= 7 and len(rd) >= 7 and rd[-7:] == d[-7:]:
        return True
    return False


def _parse_iso(ts: str):
    try:
        return datetime.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _parse_admin_dt(s: str):
    try:
        return datetime.strptime((s or "")[:19], "%d.%m.%Y %H:%M:%S")
    except Exception:
        return None


def _hhmmss(ts: str) -> str:
    d = _parse_iso(ts)
    return d.strftime("%H:%M:%S") if d else ""


def сопоставить(заявки: list, отправки: list, сверять_город: bool = True) -> list:
    """Сверяет наши отправленные формы с заявками из админки.

    Для каждой отправки ищет НАШУ (тестовую) заявку (в том же городе, если
    сверять_город); среди кандидатов берёт заявку с совпадающим «Тип формы»,
    при нескольких — ближайшую по времени. Каждая заявка засчитывается один раз.

    сверять_город=False — когда админка обслуживает один город, но подписывает
    его иначе, чем мы (например, Steelgroup у нас, а в админке «Bakı»): тогда
    город не сверяем, полагаясь на тип формы + наши тест-маркеры.
    Возвращает (результаты, свободные_наши_заявки).
    """
    почта = телефон = имя = ""
    for o in отправки:
        почта = почта or (o.get("почта") or "")
        телефон = телефон or (o.get("телефон") or "")
        имя = имя or (o.get("имя") or "")

    наши = [z for z in заявки if _это_наша_заявка(z, почта, телефон, имя)]
    использованные = set()
    результаты = []

    for o in отправки:
        gn = _norm(o.get("город", "")) if сверять_город else ""
        кандидаты = []
        for i, z in enumerate(наши):
            if i in использованные:
                continue
            if gn and _norm(z.get("город", "")) != gn:
                continue
            кандидаты.append((i, z))

        выбор = _выбрать(кандидаты, o)
        база = {"город": o.get("город", ""), "название": o.get("название", ""),
                "ts": o.get("ts", ""), "страница": o.get("страница", "")}
        if выбор is None:
            if not наши:
                прим = "в админке нет наших тестовых заявок за сегодня"
            else:
                прим = "заявка этого типа в админке не найдена"
            результаты.append({**база, "статус": "НЕ найдено",
                               "заявка": None, "примечание": прим})
        else:
            i, z = выбор
            использованные.add(i)
            результаты.append({**база, "статус": "Есть в админке",
                               "заявка": z, "примечание": ""})

    # Наши тестовые заявки, которые остались без пары (мы их не отправляли ИЛИ
    # не смогли сопоставить тип) — вернём отдельно, чтобы подсказать в логе.
    свободные = [z for i, z in enumerate(наши) if i not in использованные]
    return результаты, свободные


def _выбрать(кандидаты: list, o: dict):
    """Выбирает заявку СТРОГО по совпадению типа формы (по «админ_тип» из конфига,
    иначе по названию формы). При нескольких подходящих — ближайшую по времени.
    Если совпадения типа нет — None (никогда не «угадываем» по одному времени,
    иначе форма, которой в админке нет, ошибочно займёт чужую заявку)."""
    if not кандидаты:
        return None
    ожид = (o.get("админ_тип") or o.get("название") or "")
    похожие = [(i, z) for i, z in кандидаты
               if _тип_похож(z.get("тип_формы", ""), ожид)]
    if not похожие:
        return None
    ts = _parse_iso(o.get("ts", ""))

    def dist(pair):
        zt = _parse_admin_dt(pair[1].get("дата_время", ""))
        if ts and zt:
            return abs((zt - ts).total_seconds())
        return 1e9

    return sorted(похожие, key=dist)[0]


def записать_лист_админка(excel_path: str, результаты: list, дата_str: str) -> None:
    """Пишет/пересоздаёт лист «Админка» в log_forms.xlsx: по строке на каждую
    отправленную форму — есть ли она в «Уведомлениях с форм» админки."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = load_workbook(excel_path)
    if "Админка" in wb.sheetnames:
        del wb["Админка"]
    # Ставим сразу после «Сводки» (если она есть) — Уровень 1 должен быть на виду.
    поз = (wb.sheetnames.index("Сводка") + 1) if "Сводка" in wb.sheetnames else 0
    ws = wb.create_sheet("Админка", поз)

    headers = ["Дата", "Время отправки", "Город", "Форма (наш тест)",
               "Статус", "Заявка в админке", "Имя / Почта в заявке",
               "Админка", "Примечание"]
    fill = PatternFill("solid", fgColor="E3F2FD")   # мягкий голубой – «Уровень 1»
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = fill
        ws.column_dimensions[get_column_letter(c)].width = len(h) + 4

    r = 2
    for res in результаты:
        z = res.get("заявка")
        заявка_txt = (f"#{z['id']} · {z['тип_формы']} · {z['время']}" if z else "—")
        имяпочта = (f"{z.get('имя','')} / {z.get('email','')}".strip(" /") if z else "")
        vals = [дата_str, _hhmmss(res.get("ts", "")),
                res.get("город", "") or "(основной)",
                res.get("название", ""), res.get("статус", ""),
                заявка_txt, имяпочта, res.get("домен_кратко", ""),
                res.get("примечание", "")]
        for c, v in enumerate(vals, 1):
            ws.cell(r, c, v)
            L = get_column_letter(c)
            cur = ws.column_dimensions[L].width or 10
            ws.column_dimensions[L].width = min(max(cur, len(str(v)) + 3), 70)
        st = ws.cell(r, 5)
        if res.get("статус", "").startswith("Есть"):
            st.font = Font(color="1E8E3E", bold=True)   # зелёный
        else:
            st.font = Font(color="C62828", bold=True)   # красный
        r += 1

    try:
        ws.freeze_panes = "A2"
    except Exception:
        pass
    wb.save(excel_path)


def _домен_кратко(домен: str) -> str:
    """https://stalmetural.uz/... → stalmetural.uz (для колонки «Админка»)."""
    m = re.sub(r"^https?://", "", (домен or "").strip()).split("/")[0]
    return m


def _зона_отправки(зоны: list, город: str):
    """Зона (админка) для нашего города: сперва по явному списку городов зоны,
    иначе — зона по умолчанию (с пустым списком «города», обычно РФ)."""
    for z in зоны:
        gs = z.get("города") or []
        if gs and город in gs:
            return z
    for z in зоны:
        if not (z.get("города") or []):
            return z
    return None


def выполнить_проверку(проект_дир, зоны, excel_path: str = "log_forms.xlsx",
                       submitted_path: str = "submitted_forms.json",
                       show: bool = False, log=print) -> bool:
    """Уровень 1: по каждой АДМИН-ЗОНЕ (РФ / СНГ / Steelgroup — у них разные
    админки, но один логин/пароль) логинится, читает «Уведомления с форм» за
    сегодня и сверяет с нашими отправками. Пишет единый лист «Админка».

    `зоны` — список {домен, города}. города=[] — зона по умолчанию («все
    остальные», обычно РФ). Тихо пропускается без admin.local.json / отправок.
    """
    creds = загрузить_креды(проект_дир)
    if not creds:
        log("ℹ️ Проверка админки пропущена: нет файла admin.local.json "
            "(логин/пароль не заданы).")
        return False

    p = Path(submitted_path)
    отправки = []
    if p.is_file():
        try:
            отправки = [o for o in (json.loads(p.read_text(encoding="utf-8")) or []) if o]
        except Exception:
            отправки = []
    if not отправки:
        log("ℹ️ Проверка админки: нет отправленных форм для сверки.")
        return False

    if not зоны:
        зоны = [{"домен": "", "города": []}]

    # Раскладываем отправки по зонам (по нашему городу).
    по_зонам = {}
    for o in отправки:
        z = _зона_отправки(зоны, o.get("город", ""))
        if z is None:
            continue
        по_зонам.setdefault(id(z), (z, []))[1].append(o)

    дата = datetime.now()
    from playwright.sync_api import sync_playwright
    все_результаты = []
    итог_есть = итог_нет = 0

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=not show,
                               args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="ru-RU")
        try:
            for z, з_отправки in по_зонам.values():
                домен = z.get("домен") or ""
                кратко = _домен_кратко(домен) or "(основной)"
                log(f"🔎 Админка [{кратко}]: вход и чтение заявок ({len(з_отправки)} форм) …")
                page = ctx.new_page()
                try:
                    html = войти_и_получить(page, домен, creds["login"],
                                            creds["password"], дата)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                if "USER_LOGIN" in html and "pixana" not in html.lower():
                    log(f"⚠️ Админка [{кратко}]: не удалось войти — проверьте "
                        f"admin.local.json (логин/пароль общие для всех зон).")
                    for o in з_отправки:
                        все_результаты.append({
                            "город": o.get("город", ""), "название": o.get("название", ""),
                            "ts": o.get("ts", ""), "статус": "НЕ найдено", "заявка": None,
                            "домен_кратко": кратко, "примечание": "не удалось войти в админку",
                        })
                        итог_нет += 1
                    continue

                заявки = разобрать_заявки(html)
                # Если в зоне один город (напр. Steelgroup, а в админке он подписан
                # «Bakı») — город не сверяем: тип формы + тест-маркеры и так однозначны.
                города_зоны = {o.get("город", "") for o in з_отправки}
                сверять = len(города_зоны) > 1
                результаты, свободные = сопоставить(заявки, з_отправки, сверять_город=сверять)
                for r in результаты:
                    r["домен_кратко"] = кратко
                    все_результаты.append(r)
                е = sum(1 for r in результаты if r["статус"].startswith("Есть"))
                итог_есть += е
                итог_нет += len(результаты) - е
                log(f"   [{кратко}] заявок в админке: {len(заявки)}; "
                    f"найдено {е} из {len(результаты)}.")
                if свободные:
                    типы = sorted({zz.get("тип_формы", "") for zz in свободные})
                    log(f"   ⚠️ [{кратко}] наши заявки без пары (типы): "
                        + ", ".join(f"«{t}»" for t in типы if t))
        finally:
            b.close()

    записать_лист_админка(excel_path, все_результаты, дата.strftime("%d.%m.%Y"))
    log(f"✅ Админка (Уровень 1): найдено {итог_есть}, НЕ найдено {итог_нет}. "
        f"Подробности — на листе «Админка».")
    return True


def найти_заявку(заявки: list, тип_формы_админ: str, город: str = "",
                 минут_окно: int = 8, после=None):
    """Ищет заявку по «Тип формы» (нестрогое совпадение) + опц. городу + свежести.
    после — datetime: заявка должна быть не старше (минут_окно) от него.
    Возвращает найденную заявку или None."""
    цель = _norm(тип_формы_админ)
    гнорм = _norm(город) if город else ""
    кандидаты = []
    for z in заявки:
        t = _norm(z["тип_формы"])
        if not (t == цель or цель in t or t in цель):
            continue
        if гнорм and _norm(z["город"]) != гнорм:
            continue
        кандидаты.append(z)
    if после is not None and кандидаты:
        def свежесть(z):
            try:
                zt = datetime.strptime(z["дата_время"][:19], "%d.%m.%Y %H:%M:%S")
                return abs((zt - после).total_seconds())
            except Exception:
                return 1e9
        кандидаты = [z for z in кандидаты if свежесть(z) <= минут_окно * 60]
        кандидаты.sort(key=свежесть)
    return кандидаты[0] if кандидаты else None


def загрузить_креды(проект_дир: Path):
    """Читает admin.local.json проекта: {login, password}. None, если файла нет."""
    f = Path(проект_дир) / "admin.local.json"
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("login") and d.get("password"):
            return d
    except Exception:
        return None
    return None


def войти_и_получить(page, домен: str, login: str, password: str, дата: datetime) -> str:
    """Логинится в админку Bitrix и возвращает HTML списка заявок за день.
    Стандартная форма входа: поля USER_LOGIN / USER_PASSWORD, кнопка входа."""
    домен = домен.rstrip("/")
    page.goto(f"{домен}/bitrix/admin/index.php?lang=ru", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(800)
    # если форма входа показана — авторизуемся
    try:
        if page.locator("input[name='USER_LOGIN']").count() > 0:
            page.fill("input[name='USER_LOGIN']", login)
            page.fill("input[name='USER_PASSWORD']", password)
            # кнопка входа: name=Login (иногда input[type=submit])
            btn = page.locator("input[name='Login'], button[name='Login'], "
                               "input[type='submit'], button[type='submit']").first
            btn.click(timeout=8000)
            page.wait_for_timeout(2000)
    except Exception:
        pass
    # открываем список заявок за сегодня
    page.goto(построить_url_списка(домен, дата), wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)
    return page.content()
