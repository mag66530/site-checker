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


def test_folder_parts_общий_диск_и_свой_диск():
    dt = datetime(2026, 8, 5)
    # общий диск: несколько проектов рядом → папка проекта нужна
    assert drive_reports.folder_parts("SHOPMET", dt, "Проверка форм") == [
        "SHOPMET", "2026", "08 - август", "Проверка форм"]
    # диск самого проекта → лишний уровень с именем проекта не нужен
    assert drive_reports.folder_parts("SHOPMET", dt, "Чек-лист",
                                      свой_диск=True) == [
        "2026", "08 - август", "Чек-лист"]
    # вид прогона не задан - папку не создаём
    assert drive_reports.folder_parts("X", dt, "", свой_диск=True) == [
        "2026", "08 - август"]


def test_report_file_name_с_датой():
    имя = drive_reports.report_file_name("Проверка КП", "SHOPMET",
                                         datetime(2026, 8, 5, 14, 30))
    assert имя == "Проверка КП · SHOPMET · 05.08.2026 14-30.xlsx"


def test_upload_from_env_без_настроек_молчит(monkeypatch, tmp_path):
    for k in ("GDRIVE_REFRESH_TOKEN", "GDRIVE_ROOT_ID"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / "отчёт.xlsx"
    f.write_bytes(b"PK\x03\x04")
    assert drive_reports.upload_from_env(str(f), "Проверка форм") == {}


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
