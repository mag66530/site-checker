"""
pagespeed_history.py - локальная история прогонов скорости и выбор периода для
сравнения. Хранилище - CSV-файл на проект: pagespeed_data/{project_id}.csv
(дописывается каждым прогоном; одна строка = один URL одного прогона).

Почему CSV локально: просто, открывается в Excel, не требует Google. Важно: на
Streamlit Cloud файловая система эфемерна - поэтому в приложении есть выгрузка/
загрузка истории (export_csv / import_csv), чтобы переносить её между машинами и
переживать редеплой. Для регулярного накопления - локальный запуск или CLI.

Модуль ничего не знает о сети и PSI - работает поверх PageResult из
pagespeed_checker.
"""
from __future__ import annotations

import csv
import datetime
import io
from pathlib import Path
from typing import Optional

from pagespeed_checker import MetricSet, PageResult, aggregate

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "pagespeed_data"

COLUMNS = [
    "run_ts", "url", "type_code",
    "desktop_score", "desktop_fcp", "desktop_lcp", "desktop_cls", "desktop_tbt", "desktop_error",
    "mobile_score", "mobile_fcp", "mobile_lcp", "mobile_cls", "mobile_tbt", "mobile_error",
]

TS_FMT = "%Y-%m-%d %H:%M:%S"


def now_ts() -> str:
    """Метка времени прогона в формате хранения."""
    return datetime.datetime.now().strftime(TS_FMT)


def history_path(project_id: str) -> Path:
    return DATA_DIR / f"{project_id}.csv"


def _num(v):
    """Строка CSV -> float или None (пустая ячейка/мусор -> None)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _cell(v):
    """float/None -> строка для CSV."""
    return "" if v is None else v


# ── Запись ───────────────────────────────────────────────────────────────────
def append_run(project_id: str, run_ts: str, results: list[PageResult]) -> Path:
    """Дописать прогон в CSV проекта. Возвращает путь к файлу."""
    path = history_path(project_id)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)
        for r in results:
            d, m = r.desktop, r.mobile
            w.writerow([
                run_ts, r.url, r.type_code,
                _cell(d.score), _cell(d.fcp_val), _cell(d.lcp_val),
                _cell(d.cls_val), _cell(d.tbt_val), _cell(d.error or ""),
                _cell(m.score), _cell(m.fcp_val), _cell(m.lcp_val),
                _cell(m.cls_val), _cell(m.tbt_val), _cell(m.error or ""),
            ])
    return path


# ── Чтение ───────────────────────────────────────────────────────────────────
def load_rows(project_id: str) -> list[dict]:
    """Все строки истории проекта как list[dict] (значения - строки CSV)."""
    path = history_path(project_id)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run_timestamps(project_id: str) -> list[str]:
    """Уникальные метки прогонов по возрастанию."""
    seen = {}
    for row in load_rows(project_id):
        ts = row.get("run_ts", "")
        if ts:
            seen[ts] = True
    return sorted(seen.keys())


def _rows_to_results(rows: list[dict]) -> list[PageResult]:
    """Восстановить PageResult из строк одного прогона (для агрегата)."""
    out = []
    for row in rows:
        d = MetricSet(
            score=_num(row.get("desktop_score")),
            fcp_val=_num(row.get("desktop_fcp")), lcp_val=_num(row.get("desktop_lcp")),
            cls_val=_num(row.get("desktop_cls")), tbt_val=_num(row.get("desktop_tbt")),
            error=(row.get("desktop_error") or None),
        )
        m = MetricSet(
            score=_num(row.get("mobile_score")),
            fcp_val=_num(row.get("mobile_fcp")), lcp_val=_num(row.get("mobile_lcp")),
            cls_val=_num(row.get("mobile_cls")), tbt_val=_num(row.get("mobile_tbt")),
            error=(row.get("mobile_error") or None),
        )
        out.append(PageResult(url=row.get("url", ""),
                              type_code=row.get("type_code", "other"),
                              desktop=d, mobile=m))
    return out


def aggregate_for_run(project_id: str, run_ts: str) -> Optional[dict]:
    """Агрегат (средние по типам) конкретного прошлого прогона, или None."""
    rows = [r for r in load_rows(project_id) if r.get("run_ts") == run_ts]
    if not rows:
        return None
    return aggregate(_rows_to_results(rows))


# ── Выбор периода для сравнения ──────────────────────────────────────────────
def _ts_date(ts: str) -> Optional[datetime.date]:
    try:
        return datetime.datetime.strptime(ts, TS_FMT).date()
    except (ValueError, TypeError):
        return None


def pick_previous_run(
    project_id: str,
    current_ts: str,
    mode: str = "prev",
    days: Optional[int] = None,
) -> Optional[str]:
    """Выбрать метку прошлого прогона для сравнения.

    mode:
      'prev'  - последний прогон строго раньше current_ts;
      'week'  - последний прогон не позже, чем current_date - 7 дней;
      'month' - последний прогон не позже, чем current_date - 30 дней;
      'days'  - то же, но окно задаётся параметром days.
    Возвращает run_ts или None, если подходящего нет.
    """
    all_ts = [t for t in run_timestamps(project_id) if t < current_ts]
    if not all_ts:
        return None

    if mode == "prev":
        return all_ts[-1]

    window = {"week": 7, "month": 30}.get(mode, days or 7)
    cur_date = _ts_date(current_ts) or datetime.date.today()
    cutoff = cur_date - datetime.timedelta(days=window)
    eligible = [t for t in all_ts if (_ts_date(t) or datetime.date.max) <= cutoff]
    if eligible:
        return eligible[-1]
    # Нет прогона старше окна - берём самый ранний имеющийся (лучше, чем ничего).
    return all_ts[0]


def previous_aggregate(
    project_id: str,
    current_ts: str,
    mode: str = "prev",
    days: Optional[int] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """(run_ts предыдущего периода, его агрегат) или (None, None)."""
    prev_ts = pick_previous_run(project_id, current_ts, mode, days)
    if not prev_ts:
        return None, None
    return prev_ts, aggregate_for_run(project_id, prev_ts)


# ── Выгрузка / загрузка (для переноса истории) ───────────────────────────────
def export_csv(project_id: str) -> bytes:
    """Вся история проекта одним CSV (для кнопки «скачать историю»)."""
    path = history_path(project_id)
    if not path.exists():
        return ("﻿" + ",".join(COLUMNS) + "\r\n").encode("utf-8")
    return path.read_bytes()


def import_csv(project_id: str, data: bytes, mode: str = "merge") -> int:
    """Загрузить историю из CSV. mode='replace' - заменить целиком; 'merge' -
    добавить строки, которых ещё нет (по паре run_ts+url). Возвращает число
    добавленных строк."""
    text = data.decode("utf-8-sig", errors="replace")
    incoming = list(csv.DictReader(io.StringIO(text)))
    # оставляем только известные колонки, в правильном порядке
    incoming = [{c: (row.get(c, "") or "") for c in COLUMNS} for row in incoming]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = history_path(project_id)

    if mode == "replace":
        existing = []
    else:
        existing = load_rows(project_id)

    seen = {(r.get("run_ts"), r.get("url")) for r in existing}
    added = 0
    for row in incoming:
        key = (row.get("run_ts"), row.get("url"))
        if key in seen:
            continue
        existing.append(row)
        seen.add(key)
        added += 1

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for row in existing:
            w.writerow({c: row.get(c, "") for c in COLUMNS})
    return added
