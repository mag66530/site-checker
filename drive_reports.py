"""
drive_reports.py - выкладка отчётов о прогонах на Google Диск.

Структура (создаётся сама, по мере надобности):

    <Диск>/<Проект>/<Год>/Сайт чекер/<Месяц>/<Дата>/<Вид прогона>/<файл>
    например:  SHOPMET / 2026 / Сайт чекер / 08 - август / 17.08.2026 /
               Чек-лист / Чек-лист · SHOPMET · 17.08.2026 13-40.xlsx

Каждый уровень заводится автоматически при первом прогоне в этом периоде:
на следующий день появится своя папка даты, в 2027-м - папка «2027», руками
создавать не нужно ничего. Папка проекта - только на общем диске, где рядом
лежат отчёты нескольких проектов (см. folder_parts).

ВАЖНО про доступ. Пишем от СЕРВИСНОГО АККАУНТА (тот же ключ, что читает
КП-таблицы: gcp_service_account_b64). У сервисного аккаунта нет собственного
места на Диске, поэтому складывать файлы можно только в ОБЩИЙ ДИСК (Shared
Drive) Google Workspace, где этот аккаунт добавлен участником с правом записи.
Загрузка в обычный «Мой диск» упрётся в нулевую квоту аккаунта.

Настройка (секрет или настройка проекта):
    gdrive_shared_drive_id       - ID общего диска (общий для всех проектов);
    gdrive_folder_id_<проект>    - (необяз.) готовая папка проекта, если нужно
                                   своё место вместо папки по имени проекта.
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime
from typing import Optional

import requests

_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
_FILES = "https://www.googleapis.com/drive/v3/files"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
              ".spreadsheetml.sheet")
# Записываем файлы - readonly-скоупов (как в kp_sheets) здесь мало.
_SCOPES = ["https://www.googleapis.com/auth/drive"]

_МЕСЯЦЫ = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
           "август", "сентябрь", "октябрь", "ноябрь", "декабрь")

# Свой уровень для отчётов чекера: на диске проекта рядом лежит и остальная
# работа по проекту, вываливать годовые папки прогонов в корень нельзя.
CHECKER_FOLDER = "Сайт чекер"


def month_folder_name(dt: datetime) -> str:
    """«08 - август»: цифра впереди, чтобы папки сортировались по порядку, а не
    по алфавиту. ЧИСТАЯ функция - проверяется юнит-тестом."""
    return f"{dt.month:02d} - {_МЕСЯЦЫ[dt.month - 1]}"


def day_folder_name(dt: datetime) -> str:
    """«17.08.2026» - папка одного дня прогонов. Формат тот же, что в именах
    файлов (report_file_name), чтобы дата в папке и в файле читалась одинаково;
    внутри папки месяца дни всё равно идут по порядку.
    ЧИСТАЯ функция - проверяется юнит-тестом."""
    return dt.strftime("%d.%m.%Y")


def folder_id(value: str) -> str:
    """ID папки/диска из чего угодно: можно вставить и голый ID, и ссылку
    целиком («…/drive/folders/<ID>?hl=ru», «…/drive/u/0/folders/<ID>»,
    «…/file/d/<ID>/view», «…open?id=<ID>»). Лишний хвост с параметрами
    отбрасываем. ЧИСТАЯ функция - проверяется юнит-тестом."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = re.search(r"/(?:folders|d)/([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    # Голый ID: срезаем случайные параметры/слеши, если их дописали руками.
    return s.split("?")[0].strip().strip("/")


def folder_parts(project_name: str, dt: datetime, run_type: str = "",
                 свой_диск: bool = False) -> list[str]:
    """Цепочка папок отчёта: <Год>/Сайт чекер/<Месяц>/<Дата>/<Вид прогона>.

    Папку проекта добавляем ТОЛЬКО когда пишем в общий диск, где могут лежать
    отчёты нескольких проектов. Если диск принадлежит самому проекту (аккаунт
    подключён этим проектом), лишний уровень не нужен - иначе получалось бы
    «Диск проекта X / X / 2026 / …».

    Месяц И дата - оба уровня: без даты все прогоны месяца валились в одну
    папку «08 - август», а без месяца в году копилось бы до 365 папок подряд.

    ЧИСТАЯ функция - проверяется юнит-тестом.
    """
    parts = [] if свой_диск else [project_name or "Проект"]
    parts += [str(dt.year), CHECKER_FOLDER, month_folder_name(dt),
              day_folder_name(dt)]
    if run_type:
        parts.append(run_type)
    return parts


def report_file_name(run_type: str, project_name: str, dt: datetime,
                     ext: str = ".xlsx") -> str:
    """Имя файла отчёта: «Проверка форм · SHOPMET · 05.08.2026 14-30.xlsx».
    Дата в имени - чтобы в папке месяца прогоны различались по датам."""
    stamp = dt.strftime("%d.%m.%Y %H-%M")
    куски = [x for x in (run_type, project_name, stamp) if x]
    return " · ".join(куски) + ext


def _token(sa_info: dict, proxy_url: str | None = None) -> str:
    """Access-token сервисного аккаунта под scope drive."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as _GReq

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=_SCOPES)
    sess = requests.Session()
    if proxy_url:
        sess.proxies.update({"https": proxy_url, "http": proxy_url})
    creds.refresh(_GReq(session=sess))
    return creds.token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _find_child(token: str, parent_id: str, name: str, *, folder: bool,
                drive_id: str | None, proxy_url: str | None) -> Optional[str]:
    """ID дочернего элемента по имени ('' - нет). Ищем только в этом родителе."""
    q = (f"'{parent_id}' in parents and name = '{name}' and trashed = false")
    if folder:
        q += f" and mimeType = '{_FOLDER_MIME}'"
    params = {
        "q": q, "fields": "files(id,name)", "pageSize": 10,
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
    }
    if drive_id:
        params.update({"corpora": "drive", "driveId": drive_id})
    r = requests.get(_FILES, params=params, headers=_headers(token),
                     proxies=_proxies(proxy_url), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Drive files.list: HTTP {r.status_code} {r.text[:180]}")
    files = (r.json() or {}).get("files") or []
    return files[0]["id"] if files else None


def _proxies(proxy_url: str | None):
    return {"https": proxy_url, "http": proxy_url} if proxy_url else None


def _create_folder(token: str, parent_id: str, name: str, *,
                   proxy_url: str | None) -> str:
    body = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
    r = requests.post(_FILES, params={"supportsAllDrives": "true",
                                      "fields": "id"},
                      json=body, headers=_headers(token),
                      proxies=_proxies(proxy_url), timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Drive создание папки «{name}»: "
                           f"HTTP {r.status_code} {r.text[:180]}")
    return (r.json() or {})["id"]


def ensure_path(token: str, root_id: str, parts: list[str], *,
                drive_id: str | None = None,
                proxy_url: str | None = None) -> str:
    """Пройти/создать цепочку папок от root_id. → ID последней папки."""
    cur = root_id
    for name in parts:
        found = _find_child(token, cur, name, folder=True, drive_id=drive_id,
                            proxy_url=proxy_url)
        cur = found or _create_folder(token, cur, name, proxy_url=proxy_url)
    return cur


def upload_report(file_path: str, *, project_name: str, sa_info: dict = None,
                  root_id: str = "", drive_id: str | None = None,
                  when: datetime | None = None,
                  file_name: str | None = None,
                  proxy_url: str | None = None,
                  oauth_token: str | None = None,
                  run_type: str = "",
                  share_link: bool = True,
                  share_role: str = "writer") -> dict:
    """Залить отчёт в <root>/…/<год>/Сайт чекер/<месяц>/<дата>/<вид> и вернуть ссылку.

    Два режима записи:
      • sa_info - сервисный аккаунт, пишет в ОБЩИЙ диск (Workspace);
      • oauth_token - готовый access-token аккаунта проекта (обычный gmail),
        тогда root_id по умолчанию «root» = «Мой диск» этого аккаунта.

    → {'ok': True, 'id': …, 'link': …,
       'path': 'Проект/2026/Сайт чекер/08 - август/17.08.2026/Чек-лист'}
      либо {'ok': False, 'error': 'текст'} - выкладка НЕ должна ронять прогон.
    """
    if not os.path.isfile(file_path):
        return {"ok": False, "error": f"файла нет: {file_path}"}
    if not oauth_token:
        if not sa_info:
            return {"ok": False, "error": "сервисный аккаунт Google не задан "
                                          "(gcp_service_account_b64)"}
        if not root_id:
            return {"ok": False, "error": "не задан общий диск/папка "
                                          "(gdrive_shared_drive_id)"}
    else:
        root_id = root_id or "root"      # «Мой диск» подключённого аккаунта
    dt = when or datetime.now()
    name = file_name or os.path.basename(file_path)
    try:
        token = oauth_token or _token(sa_info, proxy_url)
        parts = folder_parts(project_name, dt, run_type,
                             свой_диск=bool(oauth_token))
        folder_id = ensure_path(token, root_id, parts, drive_id=drive_id,
                                proxy_url=proxy_url)
        with open(file_path, "rb") as f:
            data = f.read()
        meta = {"name": name, "parents": [folder_id]}
        files = {
            "metadata": ("metadata", _json_bytes(meta), "application/json"),
            "file": (name, io.BytesIO(data), _XLSX_MIME),
        }
        r = requests.post(
            _UPLOAD,
            params={"uploadType": "multipart", "supportsAllDrives": "true",
                    "fields": "id,webViewLink"},
            files=files, headers=_headers(token),
            proxies=_proxies(proxy_url), timeout=180)
        if r.status_code not in (200, 201):
            return {"ok": False,
                    "error": f"загрузка: HTTP {r.status_code} {r.text[:200]}"}
        res = r.json() or {}
        # Открываем доступ по ссылке: отчёт лежит на Диске одного аккаунта, а
        # ссылку в Telegram получает вся команда. Если организация запрещает
        # публичные ссылки, файл всё равно загружен - просто пишем это в ответ.
        _share_err = ""
        if share_link and res.get("id"):
            _share_err = share_with_link(token, res["id"], role=share_role,
                                         proxy_url=proxy_url)
        return {"ok": True, "id": res.get("id"),
                "shared": (not _share_err) if share_link else False,
                "share_error": _share_err,
                "link": res.get("webViewLink") or
                        f"https://drive.google.com/file/d/{res.get('id')}/view",
                "path": "/".join(parts)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _json_bytes(obj) -> bytes:
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def share_with_link(token: str, file_id: str, *,
                    role: str = "writer",
                    proxy_url: str | None = None) -> str:
    """Открыть файл «всем, у кого есть ссылка».

    Роль по умолчанию - редактор (writer), а не читатель: в режиме чтения
    Google не даёт даже отфильтровать таблицу, а отчёт как раз для того и
    нужен, чтобы в нём копались. Без этой выдачи отчёт виден только аккаунту,
    на чьём Диске он лежит, - а ссылка в Telegram приходит всей команде.
    → '' при успехе, иначе текст ошибки (загрузку это не отменяет).
    """
    try:
        r = requests.post(
            f"{_FILES}/{file_id}/permissions",
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"role": role, "type": "anyone"},
            headers=_headers(token), proxies=_proxies(proxy_url), timeout=30)
        if r.status_code not in (200, 201):
            return f"HTTP {r.status_code} {r.text[:180]}"
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)


def upload_from_env(file_path: str, run_type: str, *, log=None) -> dict:
    """Выложить отчёт ФОНОВОГО прогона (формы/цели/КП/скорость) на Диск.

    Фоновые прогоны - отдельные процессы без Streamlit, секретов и базы, им
    настройки прокидывает страница через окружение (см. tg_report.runner_env):

        GDRIVE_PROJECT_NAME     - название проекта (папка на общем диске);
        GDRIVE_REFRESH_TOKEN    - ключ подключённого аккаунта проекта;
        GDRIVE_CLIENT_ID/SECRET - ключи OAuth-приложения;
        GDRIVE_ROOT_ID          - ID общего диска/папки (режим сервисного аккаунта);
        GDRIVE_PROXY            - (необяз.) прокси.

    Ничего не настроено - тихо возвращаем {} (прогон не должен падать из-за
    выкладки). → {'link', 'path'} при успехе.
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:  # noqa: BLE001
                pass

    refresh = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()
    root_id = folder_id(os.environ.get("GDRIVE_ROOT_ID", ""))
    if not refresh and not root_id:
        return {}
    proxy = os.environ.get("GDRIVE_PROXY", "").strip() or None
    project = os.environ.get("GDRIVE_PROJECT_NAME", "").strip() or "Проект"
    dt = datetime.now()
    try:
        oauth = None
        if refresh:
            import google_oauth
            oauth = google_oauth.access_token(
                os.environ.get("GDRIVE_CLIENT_ID", ""),
                os.environ.get("GDRIVE_CLIENT_SECRET", ""),
                refresh, proxy_url=proxy)
        sa = None
        if not oauth:
            from kp_sheets import service_account_info
            sa = service_account_info()
        res = upload_report(
            file_path, project_name=project, sa_info=sa, root_id=root_id,
            oauth_token=oauth, run_type=run_type, when=dt,
            file_name=report_file_name(run_type, project, dt),
            proxy_url=proxy)
    except Exception as e:  # noqa: BLE001
        _log(f"⚠ Google Диск: выкладка не выполнена ({e})")
        return {}
    if not res.get("ok"):
        _log(f"⚠ Google Диск: {res.get('error')}")
        return {}
    _log(f"✓ Google Диск: отчёт в «{res['path']}» - {res['link']}")
    if res.get("share_error"):
        _log(f"⚠ Google Диск: доступ по ссылке не открылся ({res['share_error']}) "
             f"- файл увидит только владелец Диска.")
    return res


def _пробная_запись(token: str, parent_id: str, *,
                    proxy_url: str | None = None) -> str:
    """Создать во временной папке КРОШЕЧНЫЙ ФАЙЛ и всё убрать. '' - успех,
    иначе текст ошибки.

    Почему именно файл: папка на Диске занимает 0 байт, поэтому она создаётся
    даже там, где записать отчёт нельзя (у сервисного аккаунта нет своей квоты,
    и файл, созданный им в чужой папке, списывается с его нулевого места).
    Проверка «создали папку - значит всё хорошо» давала ложное «доступно», а
    падало уже на первом прогоне.
    """
    tmp_folder = None
    try:
        tmp_folder = _create_folder(token, parent_id, ".проверка-доступа",
                                    proxy_url=proxy_url)
        meta = {"name": "проверка.txt", "parents": [tmp_folder]}
        files = {
            "metadata": ("metadata", _json_bytes(meta), "application/json"),
            "file": ("проверка.txt", io.BytesIO(b"site-checker"), "text/plain"),
        }
        r = requests.post(
            _UPLOAD,
            params={"uploadType": "multipart", "supportsAllDrives": "true",
                    "fields": "id"},
            files=files, headers=_headers(token),
            proxies=_proxies(proxy_url), timeout=60)
        if r.status_code not in (200, 201):
            return f"HTTP {r.status_code} {r.text[:200]}"
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)
    finally:
        if tmp_folder:
            try:
                requests.delete(f"{_FILES}/{tmp_folder}",
                                params={"supportsAllDrives": "true"},
                                headers=_headers(token),
                                proxies=_proxies(proxy_url), timeout=30)
            except Exception:  # noqa: BLE001
                pass


def check_write_as_user(access_token: str, root_id: str = "root", *,
                        proxy_url: str | None = None) -> dict:
    """Проверка записи ОТ ИМЕНИ подключённого аккаунта проекта (обычный gmail).

    Отличается от check_access тем, что не трогает сервисный аккаунт: здесь
    файлы создаёт сам пользователь, поэтому и проверять надо его токеном.
    Пишем временную папку в «Мой диск» и сразу убираем.
    """
    if not access_token:
        return {"ok": False, "kind": "", "name": "",
                "error": "аккаунт проекта не подключён (нет токена доступа)"}
    ошибка = _пробная_запись(access_token, root_id, proxy_url=proxy_url)
    if ошибка:
        подсказка = ""
        if "insufficient" in ошибка.lower() or "scope" in ошибка.lower():
            подсказка = (" Похоже, при входе не был отмечен флажок доступа к "
                         "Google Диску. Отключите аккаунт и подключите заново, "
                         "отметив в окне Google все флажки.")
        return {"ok": False, "kind": "", "name": "",
                "error": f"нет прав на запись: {ошибка}{подсказка}"}
    # Куда именно попадут отчёты: имя указанной папки, иначе корень «Мой диск».
    имя = "Мой диск (корень)"
    вид = "Диск аккаунта проекта"
    if root_id and root_id != "root":
        try:
            r = requests.get(f"{_FILES}/{root_id}",
                             params={"fields": "name", "supportsAllDrives": "true"},
                             headers=_headers(access_token),
                             proxies=_proxies(proxy_url), timeout=30)
            if r.status_code == 200:
                имя = (r.json() or {}).get("name") or имя
                вид = "Папка"
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "kind": вид, "name": имя, "error": ""}


def check_access(sa_info: dict, root_id: str, *,
                 proxy_url: str | None = None) -> dict:
    """Проверить, годится ли указанный диск/папка для отчётов ДО прогона.

    Отвечает на три вопроса разом: виден ли объект сервисному аккаунту, это
    Общий диск или обычная папка, и можно ли туда писать (пробуем создать
    временную папку и сразу удалить). Обычная папка на личном Google-аккаунте
    видна и «доступна», но запись упирается в нулевую квоту сервисного
    аккаунта - эту разницу и ловим пробной записью.

    → {'ok': bool, 'kind': 'Общий диск'|'папка'|'', 'name': str, 'error': str}
    """
    if not sa_info:
        return {"ok": False, "kind": "", "name": "",
                "error": "сервисный аккаунт Google не задан (gcp_service_account_b64)"}
    if not root_id:
        return {"ok": False, "kind": "", "name": "",
                "error": "не указан ID диска или папки"}
    try:
        token = _token(sa_info, proxy_url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": "", "name": "",
                "error": f"ключ сервисного аккаунта не принят: {e}"}

    kind, name, drive_id = "", "", None
    # Сначала пробуем как ОБЩИЙ ДИСК (у него отдельный эндпоинт), потом как файл-папку.
    try:
        r = requests.get(f"https://www.googleapis.com/drive/v3/drives/{root_id}",
                         headers=_headers(token), proxies=_proxies(proxy_url),
                         timeout=30)
        if r.status_code == 200:
            kind, name = "Общий диск", (r.json() or {}).get("name", "")
            drive_id = root_id
    except Exception:  # noqa: BLE001
        pass
    if not kind:
        try:
            r = requests.get(
                f"{_FILES}/{root_id}",
                params={"fields": "id,name,mimeType,driveId",
                        "supportsAllDrives": "true"},
                headers=_headers(token), proxies=_proxies(proxy_url), timeout=30)
            if r.status_code != 200:
                return {"ok": False, "kind": "", "name": "",
                        "error": f"объект не найден или нет доступа "
                                 f"(HTTP {r.status_code}). Проверьте ID и то, что "
                                 f"сервисный аккаунт добавлен участником."}
            info = r.json() or {}
            if info.get("mimeType") != _FOLDER_MIME:
                return {"ok": False, "kind": "", "name": info.get("name", ""),
                        "error": "это не папка и не диск"}
            kind = "папка"
            name = info.get("name", "")
            drive_id = info.get("driveId")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "kind": "", "name": "", "error": str(e)}

    # Пробная запись НАСТОЯЩИМ файлом (папка занимает 0 байт и создаётся даже
    # там, где записать отчёт нельзя) - и убираем за собой.
    ошибка = _пробная_запись(token, root_id, proxy_url=proxy_url)
    if ошибка:
        e = ошибка
        подсказка = ""
        if "storageQuotaExceeded" in str(e) or "quota" in str(e).lower():
            подсказка = (" Папка на ЛИЧНОМ Google-диске: файл, созданный "
                         "сервисным аккаунтом, принадлежит ему же, а места у "
                         "него нет вовсе. Варианта два: Общий диск Workspace "
                         "или подключить один служебный Google-аккаунт "
                         "(кнопка ниже) и расшарить папки проектов на него.")
        return {"ok": False, "kind": kind, "name": name,
                "error": f"нет прав на запись: {e}.{подсказка}"}
    return {"ok": True, "kind": kind, "name": name, "error": "",
            "drive_id": drive_id}
