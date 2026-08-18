"""
proxy_config.py - единая точка чтения прокси проекта (адрес + флаг use_proxy).

Раньше настройка прокси была размазана по 4 независимым источникам (флаг
use_proxy в projects/<pid>.json, общий секрет proxy_url, БД project_settings +
страница «Настройки проекта», отдельная константа ИСПОЛЬЗОВАТЬ_ПРОКСИ в
forms_tester/projects/<id>/config.py) и разные страницы/прогоны читали их
по-разному - где-то use_proxy игнорировался, где-то не было доступа к
адресу из личного кабинета. Теперь один источник правды - этот модуль,
которым пользуются и Streamlit-страницы (site_access.secret_proxy), и
фоновые прогоны (runner_30min.py, run_scheduled.py, variables_run.py,
collect_products.py, goals_run.py, forms_check.py), запускаемые отдельным
процессом БЕЗ Streamlit-рантайма.

Приоритет АДРЕСА (resolve_proxy):
  1. БД project_settings (личный кабинет → «Настройки проекта», свой прокси
     на каждый проект) - через auth.project_setting(pid, "proxy_url").
     Импорт auth/Streamlit - ЛЕНИВЫЙ и в try/except: если секреты/БД
     недоступны (обычный случай для GitHub Actions или голого CLI-запуска
     без .streamlit/secrets.toml) - просто получаем None и идём дальше по
     цепочке, модуль не падает и не требует Streamlit-рантайма;
  2. секрет/переменная окружения proxy_url_<pid> (свой адрес на проект -
     старый способ, оставлен для обратной совместимости);
  3. общий секрет/переменная окружения proxy_url (один адрес на все
     проекты - как было исторически, фоллбэк для проектов без личного
     адреса);
  4. переменная окружения HTTP_PROXY / http_proxy.

Флаг ВКЛ/ВЫКЛ (project_use_proxy): читается из projects/<pid>.json.
use_proxy=false - прокси не используется ВООБЩЕ, что бы ни было настроено
адресом выше (proxy_for_project возвращает None) - это главный выключатель,
и он должен соблюдаться везде одинаково.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).parent

# forms_tester/projects/<id> не совпадает один-в-один с projects/<id>.json
# (там есть варианты вроде metpromko → mpk, mpe_cart без собственного
# соответствия). Отображение forms_tester-id → канонический id проекта
# (projects/*.json, БД project_settings), чтобы use_proxy и адрес прокси
# читались из ОДНОГО места, под каким бы именем проект ни ходил в
# форм-тестере/«Проверке целей».
FORMS_PROJECT_ALIASES = {
    "metpromko": "mpk",
    "shopmet": "sm",
}


def canonical_project_id(forms_pid: str) -> str:
    """id проекта в projects/*.json/БД по id форм-тестера (алиас или тот же
    id, если явного соответствия нет - напр. у mpe_cart единого проекта нет,
    вернётся 'mpe_cart' как есть, и project_use_proxy на нём просто не
    найдёт файл → False, как у любого проекта без флага)."""
    return FORMS_PROJECT_ALIASES.get(forms_pid, forms_pid)


def _secret(key: str):
    """st.secrets[key], если Streamlit доступен и секрет задан - иначе None.
    Ленивый импорт: subprocess без Streamlit-рантайма просто получит None
    (см. докстринг модуля), а не упадёт."""
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def _db_proxy(pid: str):
    """Прокси проекта из БД (личный кабинет, страница «Настройки проекта»).
    None, если auth/БД недоступны - тогда просто пропускаем эту ступень."""
    if not pid:
        return None
    try:
        import auth
        return auth.project_setting(pid, "proxy_url") or None
    except Exception:
        return None


# Явное решение пользователя на ЭТОТ прогон: чек-бокс «Прокси» в сайдбаре.
# Кладётся страницей в окружение фонового процесса.
#
# Зачем отдельная переменная, а не «нет адреса - значит выключено»: адрес
# прогон умеет находить сам (БД/секреты), поэтому «страница не положила
# proxy_url» читалось как «адрес не передали», и прогон брал его сам. У
# проекта с use_proxy=true выключенная галочка не выключала ничего - прогон
# всё равно шёл через прокси (поймано на АПС: локально сайт открывается, а
# через прокси отдаёт 401, и прогон КП падал с 4xx).
USE_PROXY_ENV = 'USE_PROXY'


def proxy_env(effective_proxy: str | None) -> dict:
    """Env для фонового прогона по решению галочки на странице.

    effective_proxy - то, что вернул чек-бокс «Прокси» (адрес, если включён,
    иначе None). Кладём И адрес, И само решение: без явного «выключено»
    прогон достаёт адрес сам и галочка ничего не выключает.

    При выключенной галочке ГАСИМ и системные HTTP_PROXY/HTTPS_PROXY: requests
    по умолчанию читает их сам (trust_env=True) и идёт через прокси, даже если
    мы явно передали «без прокси». На машине разработчика такие переменные
    обычно выставлены - и прогон «без прокси» всё равно шёл через него.
    ЧИСТАЯ функция - есть юнит-тест."""
    if effective_proxy:
        return {'proxy_url': effective_proxy, USE_PROXY_ENV: '1'}
    return {USE_PROXY_ENV: '0', 'proxy_url': '',
            'HTTP_PROXY': '', 'HTTPS_PROXY': '',
            'http_proxy': '', 'https_proxy': '',
            # NO_PROXY=* - для библиотек, которые смотрят на наличие
            # переменной, а не на её пустоту.
            'NO_PROXY': '*', 'no_proxy': '*'}


def env_use_proxy():
    """Решение по галочке из окружения: True / False / None (не задано).
    ЧИСТАЯ функция - есть юнит-тест."""
    v = (os.environ.get(USE_PROXY_ENV) or '').strip().lower()
    if not v:
        return None
    if v in ('0', 'false', 'no', 'off'):
        return False
    if v in ('1', 'true', 'yes', 'on'):
        return True
    return None


def project_use_proxy(pid: str) -> bool:
    """use_proxy проекта из projects/<pid>.json. False - если pid пуст,
    файла нет или JSON битый (безопасный дефолт: без прокси, как у проекта,
    для которого флаг явно не настроен)."""
    if not pid:
        return False
    p = ROOT / "projects" / f"{pid}.json"
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("use_proxy"))
    except Exception:
        return False


def resolve_proxy(pid: str = "") -> str | None:
    """Адрес прокси проекта по приоритету источников (см. докстринг модуля).
    НЕ проверяет use_proxy - вызывающий код сам решает, применять ли адрес
    (страницы - через чекбокс «Вкл. Прокси»; фоновые прогоны - см.
    proxy_for_project, где use_proxy уже учтён)."""
    if pid:
        v = _db_proxy(pid)
        if v:
            return v
        v = _secret(f"proxy_url_{pid}") or os.environ.get(f"proxy_url_{pid}")
        if v:
            return v
    return (_secret("proxy_url") or os.environ.get("proxy_url")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))


def proxy_for_project(pid: str) -> str | None:
    """Итоговый прокси для фонового прогона проекта.

    Порядок решений:
      1. галочка «Прокси» на странице, если она передана в окружении
         (USE_PROXY=0/1) - у неё ПРИОРИТЕТ: человек выключил её осознанно,
         именно чтобы прогнать без прокси;
      2. иначе use_proxy проекта из projects/<pid>.json;
      3. адрес - resolve_proxy(pid).

    Это то, что должны звать runner_30min.py/run_scheduled.py/
    variables_run.py/collect_products.py и т.п. вместо своих копий логики."""
    выбор = env_use_proxy()
    if выбор is False:
        return None
    if выбор is None and not project_use_proxy(pid):
        return None
    return resolve_proxy(pid)
