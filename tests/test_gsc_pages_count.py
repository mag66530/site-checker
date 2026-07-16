"""Тесты разбора чисел «Количество страниц в ГСК» (без браузера): парсинг
локализованных чисел, таблица причин, отделение «Проиндексировано» от «Не
проиндексировано», сумма, история/дельты."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gsc_pages_count as G  # noqa: E402

NBSP = " "
NNBSP = " "


def test_parse_int_localized():
    assert G.parse_int("45 678") == 45678
    assert G.parse_int(f"1{NBSP}234") == 1234
    assert G.parse_int(f"12{NNBSP}345") == 12345
    assert G.parse_int("1,234") == 1234
    assert G.parse_int("12") == 12
    assert G.parse_int("нет данных") is None
    assert G.parse_int(None) is None


def test_last_number_takes_rightmost():
    row = "Просканировано, но пока не проиндексировано  Системы Google  Не начата  12 345"
    assert G.last_number(row) == 12345


def test_is_crawled_not_indexed():
    assert G.is_crawled_not_indexed("Просканировано, но пока не проиндексировано")
    assert G.is_crawled_not_indexed("Crawled - currently not indexed")
    assert not G.is_crawled_not_indexed("Страница с переадресацией")
    assert not G.is_crawled_not_indexed("Обнаружена, сейчас не проиндексирована")


REASON_ROWS = [
    "Просканировано, но пока не проиндексировано  Системы Google  Не начата  12 345",
    "Обнаружена, сейчас не проиндексирована  Системы Google  Не начата  3 210",
    "Страница с переадресацией  Системы Google  Не начата  1 024",
    "Альтернативная страница с тегом canonical  Системы Google  Не начата  8 765",
]


def test_parse_reasons():
    reasons = G.parse_reasons(REASON_ROWS)
    assert len(reasons) == 4
    by_name = {r["name"]: r["count"] for r in reasons}
    assert by_name["Просканировано, но пока не проиндексировано"] == 12345
    assert by_name["Страница с переадресацией"] == 1024


def test_find_indexed_distinguishes_not_indexed():
    # число только для «Не проиндексировано» → indexed не должен взяться
    assert G.find_indexed(["Не проиндексировано 20 000"]) is None
    # чистое «Проиндексировано»
    assert G.find_indexed(["Проиндексировано 45 678"]) == 45678
    # оба рядом → берём именно «Проиндексировано»
    assert G.find_indexed(["Не проиндексировано 20 000", "Проиндексировано 45 678"]) == 45678
    assert G.find_indexed(["Не проиндексировано 20 000 Проиндексировано 45 678"]) == 45678
    # число перед словом
    assert G.find_indexed(["45 678 Проиндексировано"]) == 45678


def test_summarize_computes_total_and_sum():
    reasons = G.parse_reasons(REASON_ROWS)
    nums = G.summarize(45678, reasons)
    assert nums["indexed"] == 45678
    assert nums["crawled_not_indexed"] == 12345
    assert nums["not_indexed_total"] == 12345 + 3210 + 1024 + 8765   # 25344
    assert nums["total"] == 45678 + 25344                            # 71022


def test_summarize_without_indexed():
    reasons = G.parse_reasons(REASON_ROWS)
    nums = G.summarize(None, reasons)
    assert nums["indexed"] is None
    assert nums["not_indexed_total"] == 25344
    assert nums["total"] is None       # без indexed сумму не считаем


def test_summarize_empty_reasons():
    nums = G.summarize(100, [])
    assert nums["not_indexed_total"] is None
    assert nums["crawled_not_indexed"] is None
    assert nums["total"] is None


def test_history_roundtrip_and_deltas(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    G.append_history("smu", {"indexed": 45000, "crawled_not_indexed": 12000,
                             "not_indexed_total": 25000, "total": 70000},
                     when="2026-07-01")
    prev = G.previous_row("smu", before="2026-07-16")
    assert prev["indexed"] == "45000"

    cur = {"indexed": 45678, "crawled_not_indexed": 12345,
           "not_indexed_total": 25344, "total": 71022}
    d = G.deltas(cur, prev)
    assert d["indexed"] == 678          # 45678 - 45000
    assert d["total"] == 1022           # 71022 - 70000
    assert d["crawled_not_indexed"] == 345


def test_deltas_no_previous():
    cur = {"indexed": 100, "crawled_not_indexed": 5, "not_indexed_total": 50, "total": 150}
    d = G.deltas(cur, None)
    assert all(v is None for v in d.values())
