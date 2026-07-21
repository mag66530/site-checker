"""Тесты локальной истории pagespeed_history: запись/чтение прогонов, агрегат
прошлого прогона, выбор периода сравнения, экспорт/импорт. Пишем во временный
каталог (DATA_DIR подменяется), настоящую pagespeed_data/ не трогаем."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pagespeed_history as H  # noqa: E402
from pagespeed_checker import MetricSet, PageResult  # noqa: E402


def _mk(url, tc, d_score, m_score):
    return PageResult(url=url, type_code=tc,
                      desktop=MetricSet(score=d_score, lcp_val=3.1),
                      mobile=MetricSet(score=m_score, lcp_val=5.2))


def _use_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "DATA_DIR", tmp_path)


def test_append_and_load_roundtrip(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    results = [_mk("https://s.ru/", "main", 71, 42),
               _mk("https://s.ru/catalog/a/", "category", 63, 36)]
    H.append_run("smu", "2026-07-16 14:20:00", results)

    rows = H.load_rows("smu")
    assert len(rows) == 2
    assert rows[0]["url"] == "https://s.ru/"
    assert rows[0]["desktop_score"] == "71"
    assert rows[0]["type_code"] == "main"
    assert (tmp_path / "smu.csv").exists()


def test_two_runs_are_separate_timestamps(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-09 10:00:00", [_mk("https://s.ru/", "main", 60, 30)])
    H.append_run("smu", "2026-07-16 10:00:00", [_mk("https://s.ru/", "main", 66, 33)])
    ts = H.run_timestamps("smu")
    assert ts == ["2026-07-09 10:00:00", "2026-07-16 10:00:00"]


def test_aggregate_for_run(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-16 10:00:00",
                 [_mk("u1", "category", 60, 30), _mk("u2", "category", 70, 40)])
    agg = H.aggregate_for_run("smu", "2026-07-16 10:00:00")
    assert agg["by_type"]["category"]["desktop_avg"] == 65.0
    assert agg["by_type"]["category"]["mobile_avg"] == 35.0


def test_all_run_aggregates_chronological(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    # прогоны заносим НЕ по порядку - функция должна вернуть их хронологически
    H.append_run("smu", "2026-07-16 10:00:00",
                 [_mk("u1", "main", 80, 40), _mk("u2", "category", 60, 30)])
    H.append_run("smu", "2026-07-02 10:00:00", [_mk("u1", "main", 70, 35)])

    runs = H.all_run_aggregates("smu")
    assert [r["run_ts"] for r in runs] == ["2026-07-02 10:00:00", "2026-07-16 10:00:00"]

    # overall второго (позднего) прогона: desktop (80+60)/2, mobile (40+30)/2, count 2
    ov = runs[1]["agg"]["overall"]
    assert ov["count"] == 2
    assert ov["desktop_avg"] == 70.0
    assert ov["mobile_avg"] == 35.0
    # per-run агрегат совпадает с aggregate_for_run
    assert runs[0]["agg"] == H.aggregate_for_run("smu", "2026-07-02 10:00:00")


def test_all_run_aggregates_empty_history(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert H.all_run_aggregates("smu") == []


def test_pick_previous_run_prev(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-02 10:00:00", [_mk("u", "main", 55, 25)])
    H.append_run("smu", "2026-07-09 10:00:00", [_mk("u", "main", 60, 30)])
    prev = H.pick_previous_run("smu", "2026-07-16 10:00:00", mode="prev")
    assert prev == "2026-07-09 10:00:00"


def test_pick_previous_run_week_window(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-02 10:00:00", [_mk("u", "main", 55, 25)])
    H.append_run("smu", "2026-07-15 10:00:00", [_mk("u", "main", 62, 31)])  # 1 день назад
    # неделя: нужен прогон не позже, чем 16-7=09.07 -> подходит только 02.07
    prev = H.pick_previous_run("smu", "2026-07-16 10:00:00", mode="week")
    assert prev == "2026-07-02 10:00:00"


def test_previous_aggregate_returns_ts_and_agg(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-09 10:00:00", [_mk("u", "category", 63, 38)])
    prev_ts, agg = H.previous_aggregate("smu", "2026-07-16 10:00:00", mode="prev")
    assert prev_ts == "2026-07-09 10:00:00"
    assert agg["by_type"]["category"]["desktop_avg"] == 63.0


def test_no_previous_run(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-16 10:00:00", [_mk("u", "main", 60, 30)])
    prev_ts, agg = H.previous_aggregate("smu", "2026-07-16 10:00:00", mode="prev")
    assert prev_ts is None and agg is None


def test_export_import_merge(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    H.append_run("smu", "2026-07-16 10:00:00", [_mk("u1", "main", 60, 30)])
    blob = H.export_csv("smu")
    assert b"run_ts" in blob and b"u1" in blob

    # импорт в другой проект того же CSV - строки должны добавиться
    added = H.import_csv("mpe", blob, mode="replace")
    assert added == 1
    assert H.run_timestamps("mpe") == ["2026-07-16 10:00:00"]

    # повторный merge того же - дублей нет
    again = H.import_csv("mpe", blob, mode="merge")
    assert again == 0
    assert len(H.load_rows("mpe")) == 1
