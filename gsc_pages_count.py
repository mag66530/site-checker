"""
gsc_pages_count.py - количество страниц в Google Search Console по статусам
индексации (пункт чек-листа «Количество страниц в ГСК»).

Три числа из отчёта «Индексирование → Страницы» (в API их нет, только UI):
  • indexed             - «Проиндексировано» (верхняя сводка отчёта);
  • crawled_not_indexed - строка причины «Просканировано, но пока не
    проиндексировано» в таблице «Почему страницы не индексируются»;
  • not_indexed_total   - сумма всех причин (всего «Не проиндексировано»);
  • total (сумма)       - indexed + not_indexed_total (сколько страниц Google
    вообще видит).

Как берём: браузером (сохранённая сессия Google - та же, что у «404 в индексе
(GSC браузер)» и автокликеров) открываем отчёт и читаем DOM. Официального API
для этих счётчиков нет.

Ограничение (как у всех браузерных GSC-проверок): нужна живая сессия Google. На
облаке сессия часто слетает - тогда числа не снимутся, вернём понятную ошибку и
не уроним прогон.

ВАЖНО: разбор таблицы причин опирается на проверенный путь
(gsc_validate_fixes._read_reasons читает те же tr[data-rowid] в проде). Верхнее
число «Проиндексировано» - в отдельной сводке; его селектор подтверждается на
живом отчёте, поэтому модуль ПОДРОБНО логирует, что реально прочитал.

История и сравнение периодов - cache/gsc_pages/{pid}.csv.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "cache" / "gsc_pages"

# Названия причины «просканировано, но не проиндексировано» (рус/англ, терпимо).
_CRAWLED_NI_MARKERS = (
    ("просканировано", "не проиндексирован"),
    ("crawled", "not indexed"),
)

# Число с разделителями тысяч: обычный/неразрывный/узкий-неразрывный пробел,
# запятая, точка.   = nbsp,   = narrow nbsp.
_SEP = "   .,"
_NUM = r"\d[\d" + _SEP + r"]*\d|\d"


# ── Разбор чисел (чистые функции - тестируются без браузера) ─────────────────
def parse_int(s) -> int | None:
    """Локализованное число → int. Терпит «1 234», «1 234», «1,234»."""
    if s is None:
        return None
    m = re.search(_NUM, str(s))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else None


def last_number(text: str) -> int | None:
    """Последнее число в строке (в таблице причин счётчик страниц - справа)."""
    if not text:
        return None
    nums = re.findall(_NUM, text)
    return parse_int(nums[-1]) if nums else None


def is_crawled_not_indexed(name: str) -> bool:
    low = (name or "").lower()
    return any(all(part in low for part in parts) for parts in _CRAWLED_NI_MARKERS)


def parse_reasons(rows: list[str]) -> list[dict]:
    """Строки таблицы причин → [{name, count, raw}]. name - текст без хвостовых
    чисел; count - последнее число в строке."""
    out = []
    for raw in rows:
        cnt = last_number(raw)
        name = re.split(r"\s{2,}|\t", raw.strip())[0].strip()
        name = re.sub(r"[\d" + _SEP + r"]+$", "", name).strip() or raw.strip()
        out.append({"name": name, "count": cnt, "raw": raw})
    return out


def find_indexed(candidates: list[str]) -> int | None:
    """Число «Проиндексировано» из кандидатов-блоков.

    Аккуратно НЕ путаем с «Не проиндексировано» (тот же корень): требуем, чтобы
    перед «проиндексирован…» не стояло «не »."""
    text = "  ".join(candidates)
    # число сразу ПОСЛЕ «Проиндексировано» (не «Не проиндексировано»)
    m = re.search(r"(?<![Нн]е )проиндексирован\w*[\s:]*(" + _NUM + r")",
                  text, re.IGNORECASE)
    if m and parse_int(m.group(1)) is not None:
        return parse_int(m.group(1))
    # запасной вариант: число ПЕРЕД «Проиндексировано»
    m = re.search(r"(" + _NUM + r")\s*(?<![Нн]е )проиндексирован",
                  text, re.IGNORECASE)
    if m:
        return parse_int(m.group(1))
    return None


def summarize(indexed: int | None, reasons: list[dict]) -> dict:
    """Собрать итоговые числа из indexed + распарсенных причин."""
    crawled_ni = None
    not_indexed_total = 0
    have_any = False
    for r in reasons:
        c = r.get("count")
        if isinstance(c, int):
            not_indexed_total += c
            have_any = True
            if is_crawled_not_indexed(r.get("name", "")):
                crawled_ni = c
    if not have_any:
        not_indexed_total = None
    total = None
    if isinstance(indexed, int) and isinstance(not_indexed_total, int):
        total = indexed + not_indexed_total
    return {"indexed": indexed, "crawled_not_indexed": crawled_ni,
            "not_indexed_total": not_indexed_total, "total": total}


# ── Снятие с браузера ────────────────────────────────────────────────────────
_FF_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) '
          'Gecko/20100101 Firefox/133.0')


async def _open_browser_gsc(p, log):
    """Открыть браузер для чтения отчёта GSC.

    На облаке вход в Google делается через Firefox (страница «Вход в Google»),
    поэтому и читаем отчёт Firefox'ом с той же сессией - cookies одного движка
    полностью совместимы, Google не переспрашивает вход. Локально - обычный
    open_browser (твой залогиненный Chrome по CDP)."""
    from autoclick_browser import is_cloud_mode, SESSION_FILE_ENV
    state = os.environ.get(SESSION_FILE_ENV, "")
    if is_cloud_mode() and state and os.path.exists(state):
        try:
            _fp = p.firefox.executable_path
        except Exception:
            _fp = None
        if not (_fp and os.path.exists(_fp)):
            import subprocess
            import sys as _sys
            log("Ставлю Firefox для чтения отчёта…")
            subprocess.run([_sys.executable, "-m", "playwright", "install", "firefox"],
                           check=False, capture_output=True, timeout=900)
        browser = await p.firefox.launch(headless=True, firefox_user_prefs={
            "dom.webdriver.enabled": False,
            "general.useragent.override": _FF_UA,
        })
        ctx = await browser.new_context(
            storage_state=state, user_agent=_FF_UA, locale="ru-RU",
            timezone_id="Europe/Moscow", viewport={"width": 1440, "height": 900})
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()
        log("Облачный браузер: Firefox + сессия из входа по скриншотам")
        return browser, page
    from autoclick_browser import open_browser
    return await open_browser(p, log)


async def _scrape(pid: str, scout: bool, log) -> dict:
    from playwright.async_api import async_playwright
    from index_gsc_run import GSC_REPORT, _ensure_logged_in, _gsc_target

    res, acct = _gsc_target(pid)
    if not res:
        return {"error": "не задан GSC-ресурс (gsc_resource / root_domain)"}
    log(f"GSC-страницы: ресурс {res}, аккаунт /u/{acct}/")

    email = os.environ.get("GSC_LOGIN_EMAIL") or ""
    password = os.environ.get("GSC_LOGIN_PASSWORD") or ""

    async with async_playwright() as p:
        try:
            browser, page = await _open_browser_gsc(p, log)
        except Exception as e:  # noqa: BLE001
            return {"error": f"браузер/сессия недоступны: {e}"}
        try:
            if not await _ensure_logged_in(page, res, acct, email, password, log):
                return {"error": ("НЕ АВТОРИЗОВАН в Google: сессия слетела, а "
                                  "автовход не прошёл. Переэкспортируй сессию.")}
            url = GSC_REPORT.format(acct=acct, res=res)
            log(f"GSC-страницы: открываю отчёт «Страницы» {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)   # дать Angular отрисовать сводку/таблицу

            rows = []
            for tr in await page.query_selector_all("tr[data-rowid]"):
                try:
                    if not await tr.is_visible():
                        continue
                    t = (await tr.inner_text()).strip().replace("\n", " ")
                    if t:
                        rows.append(re.sub(r"\s{2,}", "  ", t))
                except Exception:
                    pass

            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass
            cand = []
            lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
            for i, ln in enumerate(lines):
                if "проиндексирован" in ln.lower():
                    cand.append(" ".join(lines[max(0, i - 1):i + 2]))

            # ДИАГНОСТИКА: печатаем, что реально прочитали (для первого прогона)
            log(f"GSC-страницы: строк таблицы причин - {len(rows)}")
            for r in rows[:15]:
                log(f"   причина| {r[:140]}")
            log(f"GSC-страницы: кандидатов на «Проиндексировано» - {len(cand)}")
            for c in cand[:6]:
                log(f"   индекс?| {c[:140]}")

            return {"error": None, "rows": rows, "indexed_candidates": cand}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


def check_gsc_pages(pid: str, *, scout: bool = False, log=None) -> dict:
    """Снять числа страниц из GSC. Возвращает dict с числами (или error)."""
    def _log(m):
        if log:
            try:
                log("info", m)
            except TypeError:
                log(m)

    try:
        raw = asyncio.run(_scrape(pid, scout, _log))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": f"снятие GSC не удалось: {e}"}

    if raw.get("error"):
        return {"available": False, "error": raw["error"]}

    reasons = parse_reasons(raw.get("rows", []))
    indexed = find_indexed(raw.get("indexed_candidates", []))
    nums = summarize(indexed, reasons)

    out = {"available": True, "error": None, "project": pid, **nums,
           "reasons": [{"name": r["name"], "count": r["count"]} for r in reasons]}

    _log(f"GSC-страницы: проиндексировано={nums['indexed']}, "
         f"просканировано-не-индексировано={nums['crawled_not_indexed']}, "
         f"не-проиндексировано-всего={nums['not_indexed_total']}, "
         f"сумма={nums['total']}")
    if nums["indexed"] is None:
        _log("⚠ GSC-страницы: не удалось прочитать число «Проиндексировано» - "
             "смотри кандидатов выше, пришли лог, доточу селектор.")
    return out


# ── История и сравнение периодов ─────────────────────────────────────────────
_COLUMNS = ["date", "indexed", "crawled_not_indexed", "not_indexed_total", "total"]


def _history_path(pid: str) -> Path:
    return DATA_DIR / f"{pid}.csv"


def append_history(pid: str, nums: dict, when: str | None = None) -> None:
    when = when or datetime.date.today().isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _history_path(pid)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(_COLUMNS)
        w.writerow([when, nums.get("indexed"), nums.get("crawled_not_indexed"),
                    nums.get("not_indexed_total"), nums.get("total")])


def previous_row(pid: str, before: str | None = None) -> dict | None:
    path = _history_path(pid)
    if not path.exists():
        return None
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows if not before or (r.get("date", "") < before)]
    return rows[-1] if rows else None


def deltas(nums: dict, prev: dict | None) -> dict:
    def _d(key):
        cur = nums.get(key)
        if prev is None or not isinstance(cur, int):
            return None
        try:
            return cur - int(prev.get(key))
        except (TypeError, ValueError):
            return None
    return {k: _d(k) for k in ("indexed", "crawled_not_indexed",
                               "not_indexed_total", "total")}


def save_manual(pid: str, indexed: int, crawled_ni: int, when: str | None = None) -> dict:
    """Ручной ввод чисел из GSC (без браузера, самый надёжный путь). Сумма =
    проиндексировано + просканировано-не-индексировано. Пишет в историю и
    считает дельту к прошлому снятию."""
    nums = {"available": True, "error": None, "project": pid, "manual": True,
            "indexed": int(indexed), "crawled_not_indexed": int(crawled_ni),
            "not_indexed_total": None,
            "total": int(indexed) + int(crawled_ni)}
    prev = previous_row(pid, before=when)
    nums["deltas"] = deltas(nums, prev)
    append_history(pid, nums, when=when)
    return nums


# ── CLI ──────────────────────────────────────────────────────────────────────
def _main():
    import json
    ap = argparse.ArgumentParser(description="Количество страниц в GSC по статусам")
    ap.add_argument("--project", required=True)
    ap.add_argument("--scout", action="store_true", help="только диагностика DOM")
    ap.add_argument("--save", action="store_true", help="дописать в историю")
    ap.add_argument("--out", default="", help="файл для JSON результата")
    a = ap.parse_args()

    res = check_gsc_pages(a.project, scout=a.scout, log=lambda lvl, m: print(m))

    # дельта к прошлому периоду + запись в историю
    if a.save and res.get("available"):
        prev = previous_row(a.project)
        res["deltas"] = deltas(res, prev)
        append_history(a.project, res)
        print(f"\nΔ к прошлому периоду: {res['deltas']}")

    # результат в JSON (его читает ранер)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")

    if res.get("error"):
        print(f"Ошибка: {res['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
