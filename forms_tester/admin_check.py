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
