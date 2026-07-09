"""
kp_sheets.py - обновление базы «Карты присутствия» (КП) прямо из Google Таблиц.

Каждый проект держит КП в своей Google-таблице. Здесь мы скачиваем её как xlsx
(экспорт `.../export?format=xlsx`) и прогоняем СУЩЕСТВУЮЩИЙ парсер
convert_kp.convert() → catalogs/{proj}-kp.csv. Так проверки берут СВЕЖИЕ данные
из таблицы, а не зашитый снапшот (при обновлении КП в таблице оно подхватится).

Ссылка на таблицу проекта берётся из (в порядке приоритета):
  • секрет приложения  kp_sheet_url_<proj>  (или env),
  • projects/<proj>.json  →  "kp_sheet_url".
Если ссылки нет / таблица недоступна - остаётся прежний csv (fallback, без ошибок).

Доступ: таблица должна быть открыта «Всем, у кого есть ссылка - Читатель»
(тогда экспорт работает без ключей). Для закрытых таблиц нужен сервисный аккаунт
Google - тогда экспорт вернёт HTML логина, и мы честно скажем, что нет доступа.
"""
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def sheet_id(url: str) -> str:
    """ID таблицы из ссылки (или сам ID, если передан голым)."""
    if not url:
        return ''
    m = re.search(r'/spreadsheets/d/([A-Za-z0-9_\-]+)', url)
    if m:
        return m.group(1)
    return url.strip() if re.fullmatch(r'[A-Za-z0-9_\-]{20,}', url.strip() or '') else ''


def export_url(url: str) -> str:
    sid = sheet_id(url)
    return (f'https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx'
            if sid else '')


def kp_sheet_url(project: str) -> str:
    """Ссылка на КП-таблицу проекта. Приоритет: окружение kp_sheet_url_<proj>
    (прокидывает страница из секрета) → st.secrets (Streamlit-контекст) → поле
    kp_sheet_url в projects/<proj>.json. '' если не задана."""
    env = (os.environ.get(f'kp_sheet_url_{project}') or '').strip()
    if env:
        return env
    try:
        import streamlit as st
        v = st.secrets.get(f'kp_sheet_url_{project}')
        if v:
            return str(v).strip()
    except Exception:
        pass
    p = ROOT / 'projects' / f'{project}.json'
    try:
        return (json.loads(p.read_text(encoding='utf-8')).get('kp_sheet_url') or '').strip()
    except Exception:
        return ''


_XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _sa_from_b64(raw) -> dict:
    """base64-строка (весь файл-ключ) → dict. Пробелы/переносы игнорируем."""
    import base64
    cleaned = ''.join(str(raw).split())
    return json.loads(base64.b64decode(cleaned).decode('utf-8'))


def service_account_info() -> dict | None:
    """JSON-ключ сервисного аккаунта Google. Порядок источников:
      1) окружение GCP_SA_JSON (строка-JSON) или GCP_SA_B64 (base64) -
         их прокидывает страница из секрета в фоновый процесс;
      2) в Streamlit-контексте - секрет gcp_service_account (TOML-секция или
         строка-JSON) либо gcp_service_account_b64 (base64 всего файла - самый
         надёжный формат для TOML: одна строка, без кавычек и переносов внутри).
    None - ключ не задан / не разобрался (тогда работает публичный экспорт/снапшот)."""
    raw = os.environ.get('GCP_SA_JSON') or ''
    if raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return None
    b64env = os.environ.get('GCP_SA_B64') or ''
    if b64env.strip():
        try:
            return _sa_from_b64(b64env)
        except Exception:
            return None
    try:
        import streamlit as st  # доступно только когда код идёт из Streamlit
    except Exception:
        return None
    # Секрет-JSON: либо TOML-секция (dict), либо целый JSON одной строкой.
    try:
        v = st.secrets.get('gcp_service_account')
    except Exception:
        v = None
    if v is not None:
        try:
            return json.loads(v) if isinstance(v, str) else dict(v)
        except Exception:
            pass                        # битый - попробуем base64-вариант ниже
    # base64 всего файла-ключа - надёжнее всего вставлять в TOML.
    try:
        b = st.secrets.get('gcp_service_account_b64')
    except Exception:
        b = None
    if b:
        try:
            return _sa_from_b64(b)
        except Exception:
            pass
    return None


_GOOGLE_SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
# Читаем и Sheets (значения ячеек), и Drive (тип файла / прямое скачивание).
_SA_SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly',
              'https://www.googleapis.com/auth/drive.readonly']


def _sheets_values_to_xlsx(file_id: str, headers: dict) -> bytes:
    """Читает НАТИВНУЮ Google-таблицу через Google Sheets API (значения ячеек) и
    собирает из них xlsx в памяти. Ключевое: values.get - это чтение значений, а
    НЕ «скачивание файла», поэтому работает даже если у таблицы выключено
    скачивание/копирование для читателей (Drive-export в этом случае даёт 403
    «This file cannot be exported by the user»). Нужен включённый Google Sheets API."""
    import io
    import requests
    from openpyxl import Workbook

    base = f'https://sheets.googleapis.com/v4/spreadsheets/{file_id}'
    meta = requests.get(base, params={'fields': 'sheets.properties.title'},
                        headers=headers, timeout=30)
    if meta.status_code != 200:
        raise RuntimeError(
            f'Sheets API вернул HTTP {meta.status_code}: {meta.text[:160]} '
            '(включён ли Google Sheets API в проекте? расшарена ли таблица на '
            'сервисный аккаунт?)')
    titles = [s.get('properties', {}).get('title', '')
              for s in (meta.json() or {}).get('sheets', [])]
    titles = [t for t in titles if t]
    if not titles:
        raise RuntimeError('в таблице нет листов')

    wb = Workbook()
    wb.remove(wb.active)
    for title in titles:
        rng = requests.utils.quote(f"'{title}'")
        r = requests.get(base + f'/values/{rng}',
                         params={'majorDimension': 'ROWS',
                                 'valueRenderOption': 'FORMATTED_VALUE'},
                         headers=headers, timeout=60)
        ws = wb.create_sheet(title[:31])   # openpyxl: имя листа ≤ 31 символа
        if r.status_code == 200:
            for row in (r.json() or {}).get('values', []):
                ws.append(['' if v is None else v for v in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _download_xlsx_private(file_id: str, sa_info: dict) -> bytes:
    """Получает КП-таблицу проекта как xlsx от имени сервисного аккаунта (файл
    должен быть расшарен на его client_email - Читатель).

    По типу файла:
      • НАТИВНАЯ Google-таблица - читаем ЗНАЧЕНИЯ через Sheets API (работает даже
        при запрете скачивания для читателей);
      • ЗАГРУЖЕННЫЙ .xlsx (Office-режим) - качаем файл напрямую (files?alt=media),
        Sheets API его не читает."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as _GARequest
    import requests

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=_SA_SCOPES)
    creds.refresh(_GARequest())
    headers = {'Authorization': f'Bearer {creds.token}'}
    base = f'https://www.googleapis.com/drive/v3/files/{file_id}'

    meta = requests.get(base, params={'fields': 'mimeType,name',
                                      'supportsAllDrives': 'true'},
                        headers=headers, timeout=30)
    if meta.status_code != 200:
        raise RuntimeError(
            f'Drive API вернул HTTP {meta.status_code}: {meta.text[:160]} '
            '(расшарена ли таблица на сервисный аккаунт? включён ли Drive API?)')
    mime = (meta.json() or {}).get('mimeType', '')

    if mime == _GOOGLE_SHEET_MIME:
        return _sheets_values_to_xlsx(file_id, headers)
    # загруженный файл (xlsx и т.п.) - прямое скачивание, без экспорта
    r = requests.get(base, params={'alt': 'media', 'supportsAllDrives': 'true'},
                     headers=headers, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(
            f'Drive API вернул HTTP {r.status_code}: {r.text[:160]} '
            f'(тип файла: {mime or "неизвестен"})')
    return r.content


def _download_xlsx_public(url: str) -> bytes:
    """Публичный экспорт (для таблиц «по ссылке - Читатель»)."""
    import requests
    r = requests.get(export_url(url), timeout=60, allow_redirects=True,
                     headers={'User-Agent': 'Mozilla/5.0 (site-checker)'})
    if r.status_code != 200 or r.content[:2] != b'PK':
        raise RuntimeError('нет доступа к таблице по ссылке')
    return r.content


def refresh_project(project: str, log=print) -> tuple[bool, str]:
    """Скачивает КП-таблицу проекта и пересобирает catalogs/<proj>-kp.csv.
    Приоритет - сервисный аккаунт (приватные таблицы); если его нет - пробуем
    публичный экспорт. При любой проблеме csv не трогается."""
    url = kp_sheet_url(project)
    if not url:
        return False, 'ссылка на Google-таблицу КП не задана'
    fid = sheet_id(url)
    if not fid:
        return False, 'не удалось разобрать ссылку на Google-таблицу'

    sa = service_account_info()
    try:
        if sa:
            content = _download_xlsx_private(fid, sa)
        else:
            content = _download_xlsx_public(url)
    except Exception as e:  # noqa: BLE001
        return False, f'не удалось скачать таблицу: {e}'

    tmp = ''
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tf:
            tf.write(content)
            tmp = tf.name
        import convert_kp
        out = convert_kp.convert(project, tmp)
        log(f'КП {project}: обновлена из Google-таблицы → {out}')
        return True, 'обновлено из Google-таблицы'
    except Exception as e:  # noqa: BLE001
        return False, f'таблица скачана, но не разобралась: {e}'
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
