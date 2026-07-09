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
    """Ссылка на КП-таблицу проекта: секрет/окружение kp_sheet_url_<proj>, иначе
    поле kp_sheet_url в projects/<proj>.json. '' если не задана."""
    env = (os.environ.get(f'kp_sheet_url_{project}') or '').strip()
    if env:
        return env
    p = ROOT / 'projects' / f'{project}.json'
    try:
        return (json.loads(p.read_text(encoding='utf-8')).get('kp_sheet_url') or '').strip()
    except Exception:
        return ''


def refresh_project(project: str, log=print) -> tuple[bool, str]:
    """Скачивает КП-таблицу проекта и пересобирает catalogs/<proj>-kp.csv.
    Возвращает (успех, сообщение). При любой проблеме csv не трогается."""
    url = kp_sheet_url(project)
    if not url:
        return False, 'ссылка на Google-таблицу КП не задана'
    exp = export_url(url)
    if not exp:
        return False, 'не удалось разобрать ссылку на Google-таблицу'
    try:
        import requests
        r = requests.get(exp, timeout=60, allow_redirects=True,
                         headers={'User-Agent': 'Mozilla/5.0 (site-checker)'})
    except Exception as e:  # noqa: BLE001
        return False, f'не удалось скачать таблицу: {e}'
    if r.status_code != 200:
        return False, f'таблица вернула HTTP {r.status_code}'
    # xlsx - это zip, начинается с 'PK'. HTML логина Google начинается с '<'.
    if r.content[:2] != b'PK':
        return False, ('нет доступа к таблице по ссылке - открой её «Всем, у кого '
                       'есть ссылка - Читатель» (или нужен сервисный аккаунт)')
    tmp = ''
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tf:
            tf.write(r.content)
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
