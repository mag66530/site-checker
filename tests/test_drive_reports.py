# -*- coding: utf-8 -*-
"""Чистые функции выкладки отчётов на Google Диск (без сети)."""
from datetime import datetime

import drive_reports


def test_month_folder_name_сортируется_по_порядку():
    имена = [drive_reports.month_folder_name(datetime(2026, m, 1))
             for m in range(1, 13)]
    assert имена[0] == "01 - январь"
    assert имена[7] == "08 - август"
    assert имена[11] == "12 - декабрь"
    # цифра впереди => алфавитная сортировка совпадает с календарной
    assert имена == sorted(имена)


def test_upload_report_без_файла_не_падает():
    res = drive_reports.upload_report(
        "нет-такого-файла.xlsx", project_name="X", sa_info={"a": 1},
        root_id="root")
    assert res["ok"] is False and "файла нет" in res["error"]


def test_upload_report_без_сервисного_аккаунта(tmp_path):
    f = tmp_path / "отчёт.xlsx"
    f.write_bytes(b"PK\x03\x04")
    res = drive_reports.upload_report(
        str(f), project_name="X", sa_info=None, root_id="root")
    assert res["ok"] is False and "сервисный аккаунт" in res["error"]


def test_upload_report_без_диска(tmp_path):
    f = tmp_path / "отчёт.xlsx"
    f.write_bytes(b"PK\x03\x04")
    res = drive_reports.upload_report(
        str(f), project_name="X", sa_info={"a": 1}, root_id="")
    assert res["ok"] is False and "общий диск" in res["error"]
