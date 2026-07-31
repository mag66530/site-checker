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
    """Итоговый прокси для фонового прогона проекта: None, если use_proxy=
    false у проекта (главный выключатель - приоритет над любым настроенным
    адресом), иначе resolve_proxy(pid). Это то, что должны звать
    runner_30min.py/run_scheduled.py/variables_run.py/collect_products.py и
    т.п. вместо своих копий этой логики."""
    if not project_use_proxy(pid):
        return None
    return resolve_proxy(pid)
