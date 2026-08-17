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


def test_folder_id_принимает_и_ссылку_и_голый_id():
    ид = "1jjwU_AAxtLVzRtInkY_OLo2IelmZa8RQ"
    assert drive_reports.folder_id(ид) == ид
    assert drive_reports.folder_id(
        f"https://drive.google.com/drive/folders/{ид}?hl=ru") == ид
    assert drive_reports.folder_id(
        f"https://drive.google.com/drive/u/0/folders/{ид}") == ид
    assert drive_reports.folder_id(
        f"https://drive.google.com/file/d/{ид}/view?usp=sharing") == ид
    assert drive_reports.folder_id(
        f"https://drive.google.com/open?id={ид}") == ид
    assert drive_reports.folder_id("") == ""


def test_day_folder_name_отдельная_папка_на_каждый_день():
    assert drive_reports.day_folder_name(datetime(2026, 8, 5, 14, 30)) == \
        "05.08.2026"
    # разные дни одного месяца → РАЗНЫЕ папки (иначе весь месяц в одной)
    assert drive_reports.day_folder_name(datetime(2026, 8, 17)) != \
        drive_reports.day_folder_name(datetime(2026, 8, 18))


def test_folder_parts_общий_диск_и_свой_диск():
    dt = datetime(2026, 8, 5)
    # общий диск: несколько проектов рядом → папка проекта нужна
    assert drive_reports.folder_parts("SHOPMET", dt, "Проверка форм") == [
        "SHOPMET", "2026", "Сайт чекер", "08 - август", "05.08.2026",
        "Проверка форм"]
    # диск самого проекта → лишний уровень с именем проекта не нужен
    assert drive_reports.folder_parts("SHOPMET", dt, "Чек-лист",
                                      свой_диск=True) == [
        "2026", "Сайт чекер", "08 - август", "05.08.2026", "Чек-лист"]
    # вид прогона не задан - папку не создаём
    assert drive_reports.folder_parts("X", dt, "", свой_диск=True) == [
        "2026", "Сайт чекер", "08 - август", "05.08.2026"]


def test_folder_parts_новый_день_даёт_новую_папку():
    """Баг, из-за которого всё сваливалось в «08 - август»: у прогонов разных
    дней цепочка папок была одна и та же."""
    a = drive_reports.folder_parts("X", datetime(2026, 8, 17, 10, 0),
                                   "Чек-лист", свой_диск=True)
    b = drive_reports.folder_parts("X", datetime(2026, 8, 18, 10, 0),
                                   "Чек-лист", свой_диск=True)
    assert a != b
    assert a[:-2] == b[:-2]     # год/чекер/месяц общие, различие - в дате


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


class _Ответ:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload or {}
        self.text = text

    def json(self):
        return self._p


def _сеть(monkeypatch, вызовы, permissions_status=200):
    """Подменяем Drive API: папки создаются, файл грузится, права выдаются."""
    def post(url, **kw):
        вызовы.append((url, kw.get("json")))
        if url.endswith("/permissions"):
            return _Ответ(permissions_status, {"id": "p1"}, "нет прав")
        if "upload" in url:
            return _Ответ(200, {"id": "file-1", "webViewLink": "https://drive/f1"})
        return _Ответ(200, {"id": "folder-1"})

    monkeypatch.setattr(drive_reports.requests, "post", post)
    monkeypatch.setattr(drive_reports.requests, "get",
                        lambda *a, **k: _Ответ(200, {"files": []}))


def test_отчёт_открывается_по_ссылке_на_редактирование(tmp_path, monkeypatch):
    """Ссылка уходит в Telegram всей команде - файл должен открываться у всех.
    Именно редактором: в режиме чтения Google не даёт даже отфильтровать."""
    вызовы = []
    _сеть(monkeypatch, вызовы)
    f = tmp_path / "отчёт.xlsx"
    f.write_bytes(b"PK\x03\x04")
    res = drive_reports.upload_report(str(f), project_name="X",
                                      oauth_token="tok", root_id="папка",
                                      run_type="Проверка форм")
    assert res["ok"] and res["shared"] is True
    assert (f"{drive_reports._FILES}/file-1/permissions",
            {"role": "writer", "type": "anyone"}) in вызовы


def test_запрет_публичных_ссылок_не_ломает_выкладку(tmp_path, monkeypatch):
    _сеть(monkeypatch, [], permissions_status=403)
    f = tmp_path / "отчёт.xlsx"
    f.write_bytes(b"PK\x03\x04")
    res = drive_reports.upload_report(str(f), project_name="X",
                                      oauth_token="tok", root_id="папка",
                                      run_type="Чек-лист")
    assert res["ok"] is True and res["shared"] is False
    assert "403" in res["share_error"]


def test_q_escape_апостроф_не_ломает_запрос():
    # без экранирования кавычка закрывала строку запроса и поиск падал/врал
    assert drive_reports._q_escape("Отчёты 'МПЭ'") == r"Отчёты \'МПЭ\'"
    assert drive_reports._q_escape("2026") == "2026"


def _поиск(monkeypatch, файлы, статус=200):
    """Подменяем files.list заданным ответом; возвращаем список запросов."""
    запросы = []

    def get(url, **kw):
        запросы.append(kw.get("params") or {})
        return _Ответ(статус, {"files": файлы}, "не видно")

    monkeypatch.setattr(drive_reports.requests, "get", get)
    return запросы


def test_find_child_берёт_самую_старую_из_дублей(monkeypatch):
    """Дубли уже могли завестись прошлыми прогонами - все новые прогоны должны
    сходиться в изначальную папку, а не в последний дубль."""
    запросы = _поиск(monkeypatch, [
        {"id": "старая", "name": "2026", "mimeType": drive_reports._FOLDER_MIME,
         "createdTime": "2026-01-01T00:00:00Z"},
        {"id": "дубль", "name": "2026", "mimeType": drive_reports._FOLDER_MIME,
         "createdTime": "2026-08-17T00:00:00Z"},
    ])
    assert drive_reports._find_child("tok", "корень", "2026", folder=True,
                                    drive_id=None, proxy_url=None) == "старая"
    # порядок задаём запросом, а не надеждой на порядок ответа
    assert запросы[0]["orderBy"] == "createdTime"


def test_find_child_ярлык_на_папку_ведёт_в_саму_папку(monkeypatch):
    _поиск(monkeypatch, [{
        "id": "ярлык", "name": "2026",
        "mimeType": drive_reports._SHORTCUT_MIME,
        "createdTime": "2026-01-01T00:00:00Z",
        "shortcutDetails": {"targetId": "настоящая",
                            "targetMimeType": drive_reports._FOLDER_MIME},
    }])
    assert drive_reports._find_child("tok", "корень", "2026", folder=True,
                                    drive_id=None, proxy_url=None) == "настоящая"


def test_невидимая_папка_даёт_ошибку_а_не_дубль(tmp_path, monkeypatch):
    """Корень бага МПЭ: узкий доступ к Диску - files.get отвечает 404, а
    files.list по той же папке отвечает 200 и пустым списком. Писать Диск
    разрешает, поэтому прогон молча заводил свою «2026» рядом с существующей."""
    созданные = []

    def get(url, **kw):
        if url.startswith(f"{drive_reports._FILES}/"):
            return _Ответ(404, {}, "File not found")
        return _Ответ(200, {"files": []})       # «в папке пусто» - но это ложь

    def post(url, **kw):
        созданные.append((kw.get("json") or {}).get("name"))
        return _Ответ(200, {"id": "новая"})

    monkeypatch.setattr(drive_reports.requests, "get", get)
    monkeypatch.setattr(drive_reports.requests, "post", post)
    f = tmp_path / "отчёт.xlsx"
    f.write_bytes(b"PK\x03\x04")
    res = drive_reports.upload_report(str(f), project_name="МПЭ",
                                      oauth_token="tok", root_id="папка-мпэ",
                                      run_type="Чек-лист")
    assert res["ok"] is False
    assert "переподключите" in res["error"].lower()
    assert созданные == []          # ни одной папки вслепую не создано


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
