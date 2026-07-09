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


def service_account_info() -> dict | None:
    """JSON-ключ сервисного аккаунта Google: из окружения GCP_SA_JSON (строка
    JSON - её прокидывает страница из секрета gcp_service_account) или, в
    Streamlit-контексте, напрямую из st.secrets['gcp_service_account']. None - нет."""
    raw = os.environ.get('GCP_SA_JSON') or ''
    if raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return None
    try:
        import streamlit as st  # доступно только когда код идёт из Streamlit
        v = st.secrets.get('gcp_service_account')
        if v is None:
            return None
        # Секрет может быть либо TOML-секцией (dict), либо целым JSON одной строкой
        # (проще вставлять - весь файл-ключ в тройных кавычках).
        if isinstance(v, str):
            return json.loads(v)
        return dict(v)
    except Exception:
        pass
    return None


def _download_xlsx_private(file_id: str, sa_info: dict) -> bytes:
    """Скачивает Google-таблицу как xlsx через Drive API от имени сервисного
    аккаунта (таблица должна быть расшарена на его client_email - Читатель)."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as _GARequest
    import requests

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=['https://www.googleapis.com/auth/drive.readonly'])
    creds.refresh(_GARequest())
    r = requests.get(
        f'https://www.googleapis.com/drive/v3/files/{file_id}/export',
        params={'mimeType': _XLSX_MIME},
        headers={'Authorization': f'Bearer {creds.token}'}, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(
            f'Drive API вернул HTTP {r.status_code}: {r.text[:160]} '
            '(расшарена ли таблица на сервисный аккаунт? включён ли Drive API?)')
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
