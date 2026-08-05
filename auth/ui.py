"""Экраны авторизации и кабинеты (site-checker). Точка входа — require_login().

Урезано из OpenGAR (auth/ui.py): нет gated-вкладок (tab_access), нет статистики
прогонов (run_stats — понятие GAR-конвейера), нет вложенного аккордеон-меню —
навигация по страницам в site-checker уже даёт st.navigation(), этот модуль
рисует только блок аккаунта в сайдбаре (кто я / выйти / кабинет руководителя /
админ-панель).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Optional

import extra_streamlit_components as stx
import streamlit as st

from . import db, email_utils, security

# Persistent-login через cookie: токен в браузере переживает refresh.
SESSION_TTL_DAYS = 30          # срок cookie и серверной сессии
_SESSION_COOKIE = "sc_sid"     # своё имя куки, не путать с gar_sid у OpenGAR

ROLE_LABELS = {
    "admin": "Администратор",
    "manager": "Руководитель",
    "specialist": "Специалист",
}
STATUS_LABELS = {
    "pending": "⏳ ждёт одобрения",
    "active": "✅ активен",
    "disabled": "⛔ отключён",
}
ALL_ROLES = ["admin", "manager", "specialist"]  # для смены роли в админке

# Вкладки панели проверок (ключ → название в меню). app.py строит st.Page по
# этим ключам; здесь — реестр для настраиваемого доступа (как с проектами).
# Пустой набор у юзера в БД = ВСЕ вкладки (по умолчанию не ограничиваем).
APP_TABS = [
    ("checklist", "Чек-лист"),
    ("autoclickers", "Автокликеры"),
    ("forms", "Проверка форм"),
    ("goals", "Проверка целей"),
    ("kp", "Проверка КП"),
    ("pagespeed", "Скорость страниц"),
]
APP_TAB_KEYS = [k for k, _ in APP_TABS]
_TAB_LABELS = dict(APP_TABS)


def tab_label(key: str) -> str:
    return _TAB_LABELS.get(key, key)


# Настройки проекта (ключи/прокси, общие на команду): (имя, подпись, вид поля).
# Имена совпадают с ключами секретов, которые читают проверки (_secret_pid):
# значение из БД имеет ПРИОРИТЕТ над st.secrets.
PROJECT_SETTING_FIELDS = [
    ("kp_sheet_url", "Ссылка на Google-таблицу КП (Карта присутствия)", "text"),
    ("proxy_url", "Прокси (http://user:pass@host:port)", "text"),
    ("textru_key", "Ключ Text.ru (антиплагиат)", "password"),
    ("arsenkin_token", "Токен Арсенкина", "password"),
    ("pagespeed_api_key", "Ключ PageSpeed API", "password"),
    ("metrika_counter", "Номер счётчика Метрики", "text"),
    ("metrika_oauth", "OAuth-токен Метрики", "password"),
    ("webmaster_oauth", "OAuth-токен Вебмастера", "password"),
    ("yandex_oauth", "OAuth-токен Яндекса", "password"),
    ("autoclick_session", "Сессия Яндекса (base64, автокликеры/Я.Бизнес)", "textarea"),
    ("gsc_service_account", "Сервис-аккаунт GSC (JSON)", "textarea"),
    # Google Диск для отчётов о прогонах: у каждого проекта может быть СВОЙ
    # диск/папка. Внутри инструмент сам заводит <Проект>/<Год>/<Месяц>/.
    ("gdrive_shared_drive_id", "ID общего диска Google (отчёты проекта)", "text"),
    ("gdrive_folder_id", "ID папки Google (если вместо диска - готовая папка)", "text"),
    # Ключи OAuth-приложения Google. Технически они общие для всех проектов, но
    # держим их ЗДЕСЬ, а не в секретах приложения: секреты видит только владелец
    # хостинга, а настройки проекта - любой руководитель со своими правами.
    # Вписываются один раз (одни и те же значения можно продублировать в каждый
    # проект); секрет остаётся запасным источником.
    ("google_oauth_client_id", "Google OAuth: Client ID (для подключения Диска)", "text"),
    ("google_oauth_client_secret", "Google OAuth: Client secret", "password"),
]


# ---------- проекты (JSON-файлы в projects/*.json, ключ = "id" внутри файла) ----------

def list_projects() -> list[dict]:
    """[{"id": "avia", "name": "АПС - Авиапромсталь"}, ...] по имени файла."""
    d = "projects"
    out: list[dict] = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            key = data.get("id") or fn[:-5]
            name = data.get("name") or key
            out.append({"id": key, "name": name})
        except Exception:
            continue
    return out


def project_keys() -> list[str]:
    return [p["id"] for p in list_projects()]


def project_label(key: str) -> str:
    for p in list_projects():
        if p["id"] == key:
            return p["name"]
    return key


def project_main_url(key: str) -> str:
    """Главная страница проекта - подставляем в проверку доступа к сайту,
    чтобы не заставлять вбивать адрес руками."""
    try:
        with open(os.path.join("projects", f"{key}.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ""
    if data.get("main_url"):
        return data["main_url"]
    return f"https://{data['root_domain']}/" if data.get("root_domain") else ""


def _app_base_url() -> str:
    try:
        return str(st.secrets["app"]["base_url"]).rstrip("/")
    except Exception:
        return ""


def _seed_admin_email() -> str:
    try:
        return security.normalize_email(st.secrets["seed_admin"]["email"])
    except Exception:
        return ""


# ---------- кеш чтений (reruns от виджетов не бьют в БД) ----------

@st.cache_data(ttl=30, show_spinner=False)
def _c_team(mid: str) -> list:
    return db.get_team_with_projects(mid)


@st.cache_data(ttl=30, show_spinner=False)
def _c_invites(mid: str) -> list:
    return db.list_active_invites(mid)


@st.cache_data(ttl=30, show_spinner=False)
def _c_mgr_projects(mid: str) -> list:
    return db.get_user_projects(mid)


@st.cache_data(ttl=30, show_spinner=False)
def _c_all_users() -> list:
    return db.get_all_users_with_projects()


@st.cache_data(ttl=20, show_spinner=False)
def _c_user_projects_live(uid: str) -> list:
    return db.get_user_projects(uid)


def live_user_projects(user_id: str) -> list[str]:
    """Актуальные проекты юзера из БД (кеш ~20с), а не из снимка сессии —
    выданный руководителем проект виден без перелогина."""
    return _c_user_projects_live(str(user_id))


@st.cache_data(ttl=20, show_spinner=False)
def _c_user_tabs_live(uid: str) -> list:
    return db.get_user_tabs(uid)


@st.cache_data(ttl=30, show_spinner=False)
def _c_all_tabs() -> dict:
    return db.get_all_user_tabs()


def live_allowed_tabs(user: dict) -> list[str]:
    """Ключи вкладок панели, доступных юзеру (в порядке APP_TABS). Пустой набор
    в БД = все вкладки; админ всегда видит всё. Кеш ~20с — смена прав
    подхватывается без перелогина."""
    if not user or user.get("role") == "admin":
        return list(APP_TAB_KEYS)
    rows = set(_c_user_tabs_live(str(user["id"])))
    allowed = [k for k in APP_TAB_KEYS if k in rows]
    return allowed or list(APP_TAB_KEYS)


@st.cache_data(ttl=20, show_spinner=False)
def _c_settings_rights_live(uid: str) -> list:
    return db.get_settings_rights(uid)


@st.cache_data(ttl=30, show_spinner=False)
def _c_all_rights() -> dict:
    return db.get_all_settings_rights()


@st.cache_data(ttl=20, show_spinner=False)
def _c_proj_settings(pk: str) -> dict:
    return db.get_project_settings(pk)


def live_settings_projects(user: dict) -> list[str]:
    """Проекты, чьи НАСТРОЙКИ юзер вправе менять: админ — все, руководитель —
    свои проекты, специалист — делегированные (settings_rights)."""
    if not user:
        return []
    if user.get("role") == "admin":
        return project_keys()
    valid = set(project_keys())
    if user.get("role") == "manager":
        return [p for p in live_user_projects(user["id"]) if p in valid]
    return [p for p in _c_settings_rights_live(str(user["id"])) if p in valid]


def project_setting(project_id: str, name: str):
    """Значение настройки проекта из БД (кеш ~20с) или None. Никогда не бросает
    — проверки используют это как приоритетный источник ПЕРЕД st.secrets."""
    try:
        return _c_proj_settings(str(project_id)).get(name) or None
    except Exception:
        return None


def _invalidate() -> None:
    """Сбросить кеши после мутации (одобрение/проекты/статус/удаление/инвайт)."""
    _c_team.clear()
    _c_invites.clear()
    _c_mgr_projects.clear()
    _c_all_users.clear()
    _c_user_projects_live.clear()
    _c_user_tabs_live.clear()
    _c_all_tabs.clear()
    _c_settings_rights_live.clear()
    _c_all_rights.clear()
    _c_proj_settings.clear()


# ---------- session ----------

def _set_session(user: dict) -> None:
    projects = db.get_user_projects(str(user["id"]))
    st.session_state["auth_user"] = {
        "id": str(user["id"]),
        "email": user["email"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "role": user["role"],
        "manager_id": str(user["manager_id"]) if user.get("manager_id") else None,
        "projects": projects,
    }


def current_user() -> Optional[dict]:
    return st.session_state.get("auth_user")


# ---------- persistent login (cookie) ----------

def _cookie_manager() -> stx.CookieManager:
    """Один инстанс на сессию: компонент читает cookie браузера и отдаёт их Python."""
    if "_cookie_mgr" not in st.session_state:
        st.session_state["_cookie_mgr"] = stx.CookieManager()
    return st.session_state["_cookie_mgr"]


def _start_persistent_session(user: dict) -> None:
    """Создаёт серверную сессию (запись в БД). Сам cookie пишет _ensure_cookie()
    на каждом залогиненном ране — разовая запись через stx-компонент терялась,
    если ран обрывался rerun'ом до монтирования компонента (из-за этого F5
    выкидывал на вход)."""
    token = security.gen_session_token()
    try:
        db.session_create(str(user["id"]), security.hash_token(token), ttl_days=SESSION_TTL_DAYS)
    except Exception as e:
        print(f"[auth] session_create failed: {e}")
        return
    st.session_state["_auth_token"] = token


def _ensure_cookie() -> None:
    """Держит cookie сессии записанным: пишем на КАЖДОМ залогиненном ране.
    Идемпотентно (тот же токен), гарантирует запись даже если какой-то ран
    оборвался до монтирования компонента, и заодно продлевает срок — скользящие
    SESSION_TTL_DAYS (30) дней от последнего визита, как в GAR.

    ВАЖНО: срок квантуем до ДНЯ. С «сырым» datetime.now() аргументы компонента
    менялись каждый ран → компонент бесконечно перемонтировался и своими
    ответами провоцировал каскад rerun'ов (подвисания). Стабильные аргументы =
    компонент монтируется один раз и живёт."""
    token = st.session_state.get("_auth_token")
    if not token:
        return
    _day = datetime.now().date() + timedelta(days=SESSION_TTL_DAYS)
    try:
        _cookie_manager().set(
            _SESSION_COOKIE, token,
            expires_at=datetime.combine(_day, datetime.min.time()),
        )
    except Exception as e:
        print(f"[auth] cookie set failed: {e}")


def _request_cookies():
    """Cookie из HTTP-запроса браузера (st.context) — читаются мгновенно на
    первом же ране, без компонентов и пустых экранов. dict (возможно пустой) =
    ответ получен; None = st.context недоступен (очень старый Streamlit)."""
    try:
        return dict(st.context.cookies)
    except Exception:
        return None


def _restore_session_from_cookie() -> bool:
    """Живой токен в cookie → восстанавливаем сессию без формы входа.

    Два источника, по очереди:
      1) st.context.cookies — из HTTP-запроса, мгновенно. Но на Streamlit Cloud
         прокси может НЕ пробрасывать cookie в запрос (контекст пуст, хотя в
         браузере cookie есть!) — поэтому пустой контекст НЕ значит «нет сессии»;
      2) stx-компонент (document.cookie из браузера) — источник истины;
         на первом ране его ответа ещё нет → probe: st.stop() и ждём ответа
         компонента (он сам перезапустит скрипт со значениями)."""
    token = (_request_cookies() or {}).get(_SESSION_COOKIE)
    if not token:
        mgr = _cookie_manager()
        cookies = mgr.get_all()
        if (cookies is None or cookies == {}) and not st.session_state.get("_cookie_probed"):
            st.session_state["_cookie_probed"] = True
            st.stop()
        token = (cookies or {}).get(_SESSION_COOKIE)
    if not token:
        return False
    try:
        user = db.session_get_user(security.hash_token(token))
    except Exception as e:
        print(f"[auth] session_get_user failed: {e}")
        return False
    if not user or user.get("status") != "active":
        try:
            _cookie_manager().delete(_SESSION_COOKIE)
        except Exception:
            pass
        return False
    _set_session(user)
    st.session_state["_auth_token"] = token
    return True


def logout() -> None:
    token = st.session_state.get("_auth_token")
    if token:
        try:
            db.session_delete(security.hash_token(token))
        except Exception as e:
            print(f"[auth] session_delete failed: {e}")
        try:
            _cookie_manager().delete(_SESSION_COOKIE)
        except Exception:
            pass
    for _k in list(st.session_state.keys()):
        del st.session_state[_k]


# ---------- forms ----------

def _login_form() -> None:
    st.subheader("Вход")
    email = st.text_input("Email", key="login_email")
    password = st.text_input("Пароль", type="password", key="login_pw")
    if st.button("ВОЙТИ", type="primary", use_container_width=True, key="login_btn"):
        user = db.get_user_by_email(email)
        if not user or not security.verify_password(password, user["password_hash"]):
            st.error("❌ Неверный email или пароль")
            return
        if user["status"] == "pending":
            st.warning("⏳ Заявка ещё не одобрена руководителем.")
            return
        if user["status"] == "disabled":
            st.error("⛔ Аккаунт отключён. Обратитесь к руководителю.")
            return
        for _k in list(st.session_state.keys()):
            del st.session_state[_k]
        _set_session(user)
        _start_persistent_session(user)  # cookie + серверная сессия → переживёт refresh
        st.rerun()

    with st.expander("Забыли пароль?"):
        _forgot_form()


def _smtp_configured() -> bool:
    try:
        return bool(st.secrets["smtp"]["user"]) and bool(st.secrets["smtp"]["app_password"])
    except Exception:
        return False


def _forgot_form() -> None:
    if not _smtp_configured():
        st.info("📮 Отправка писем ещё не настроена (блок [smtp] в секретах). "
                "Пока пароль сбрасывает руководитель или админ из своего "
                "кабинета — кнопка «Сбросить пароль» покажет ссылку на экране.")
    email = st.text_input("Ваш email", key="forgot_email")
    if st.button("Прислать ссылку для сброса", key="forgot_btn"):
        user = db.get_user_by_email(email)
        # Не раскрываем существование аккаунта — сообщение всегда одинаковое.
        if user and user["status"] != "disabled":
            token = db.create_reset(str(user["id"]))
            base = _app_base_url()
            link = f"{base}/?reset={token}" if base else f"?reset={token}"
            ok, err = email_utils.send_reset_email(user["email"], link)
            if not ok:
                st.error(f"Не удалось отправить письмо: {err}")
                return
        st.success("Если email зарегистрирован — письмо со ссылкой отправлено.")


def _register_form() -> None:
    st.subheader("Регистрация")
    mode = st.radio("Кто вы?", ["Сотрудник (по инвайт-коду)", "Руководитель"],
                    key="reg_mode", horizontal=True)
    is_manager = mode == "Руководитель"

    if is_manager:
        st.caption("Регистрация руководителя — заявка уйдёт администратору на одобрение.")
        code = ""
        inv = None
        role = "manager"
    else:
        st.caption("Нужен инвайт-код от вашего руководителя.")
        code = st.text_input("Инвайт-код", key="reg_code").strip().upper()
        inv = db.get_invite(code) if code else None

    email = st.text_input("Email", key="reg_email")
    col1, col2 = st.columns(2)
    first = col1.text_input("Имя", key="reg_first")
    last = col2.text_input("Фамилия", key="reg_last")
    pw1 = col1.text_input("Пароль", type="password", key="reg_pw1")
    pw2 = col2.text_input("Повтор пароля", type="password", key="reg_pw2")

    if not is_manager:
        role = (inv.get("role") if inv else None) or "specialist"
        st.caption(f"Должность: **{ROLE_LABELS.get(role, role)}**")
    proj_label = "Желаемые проекты (админ подтвердит)" if is_manager else "Проекты"
    _proj_opts = project_keys()
    projects = st.multiselect(
        proj_label, _proj_opts, key="reg_projects",
        format_func=project_label,
        placeholder="Выберите проекты",
    )

    if st.button("ЗАРЕГИСТРИРОВАТЬСЯ", type="primary", use_container_width=True, key="reg_btn"):
        if not email or "@" not in email:
            st.error("❌ Укажите корректный email")
            return
        if not first or not last:
            st.error("❌ Укажите имя и фамилию")
            return
        if len(pw1) < 6:
            st.error("❌ Пароль минимум 6 символов")
            return
        if pw1 != pw2:
            st.error("❌ Пароли не совпадают")
            return
        if db.get_user_by_email(email):
            st.error("❌ Email уже зарегистрирован")
            return

        if is_manager:
            user_id = db.create_user(
                email=email, password=pw1, first_name=first, last_name=last,
                role="manager", status="pending", manager_id=None,
            )
            if projects:
                db.set_user_projects(user_id, projects)  # желаемые, админ скорректирует
            _invalidate()
            st.success("✅ Заявка отправлена администратору. После одобрения сможете войти.")
            return

        ok, msg = db.invite_is_valid(inv)
        if not ok:
            st.error(f"❌ Инвайт-код: {msg}")
            return
        manager_id = str(inv["manager_id"])
        user_id = db.create_user(
            email=email, password=pw1, first_name=first, last_name=last,
            role=role, status="active", manager_id=manager_id,
        )
        # Выдаём только проекты в рамках доступа руководителя (сверх — нельзя).
        mgr_projects = set(db.get_user_projects(manager_id))
        granted = [p for p in projects if p in mgr_projects]
        if granted:
            db.set_user_projects(user_id, granted)
        db.delete_invite(code)
        _invalidate()
        st.success("✅ Регистрация завершена! Можно войти.")


def _reset_password_view(token: str) -> None:
    st.subheader("Новый пароль")
    rec = db.get_reset(token)
    ok, msg = db.reset_is_valid(rec)
    if not ok:
        st.error(f"❌ {msg}")
        if st.button("На страницу входа"):
            st.query_params.clear()
            st.rerun()
        return
    pw1 = st.text_input("Новый пароль", type="password", key="rst_pw1")
    pw2 = st.text_input("Повтор пароля", type="password", key="rst_pw2")
    if st.button("СОХРАНИТЬ", type="primary", use_container_width=True, key="rst_btn"):
        if len(pw1) < 6:
            st.error("❌ Пароль минимум 6 символов")
            return
        if pw1 != pw2:
            st.error("❌ Пароли не совпадают")
            return
        db.update_password(str(rec["user_id"]), pw1)
        db.mark_reset_used(token)
        st.success("✅ Пароль изменён. Войдите с новым паролем.")
        st.query_params.clear()


# ---------- gate ----------

def _capture_return_slug() -> None:
    """Запоминает slug ТЕКУЩЕЙ страницы до cookie-probe. Probe (_restore_session_
    from_cookie) делает st.stop() без вызова st.navigation — из-за этого Streamlit
    сбрасывает URL на корень, и после восстановления сессии открывается стартовая
    страница вместо той, где пользователь обновил вкладку. Запомненный slug
    возвращает app.py через take_return_slug() (st.switch_page)."""
    if "_return_slug" in st.session_state:
        return
    try:
        from urllib.parse import urlsplit
        slug = urlsplit(st.context.url).path.strip("/").split("/")[-1]
    except Exception:
        slug = ""
    st.session_state["_return_slug"] = slug or ""


def take_return_slug() -> str:
    """Отдаёт и очищает slug страницы, на которую надо вернуться после логина."""
    return st.session_state.pop("_return_slug", "") or ""


def require_login() -> bool:
    """Гейт. Рисует экран входа/регистрации/сброса. True = пускаем в приложение."""
    if not st.session_state.get("_seed_admin_done"):
        try:
            db.ensure_seed_admin()
        except Exception as e:
            st.error(f"Ошибка инициализации БД: {e}")
            st.stop()
        st.session_state["_seed_admin_done"] = True

    reset_token = st.query_params.get("reset")
    if reset_token:
        _center_logo()
        _reset_password_view(reset_token)
        return False

    if current_user():
        _ensure_cookie()   # cookie пишется каждый ран: надёжно + скользящие 30 дней
        return True

    _capture_return_slug()          # до probe: запомнить страницу для возврата
    if _restore_session_from_cookie():
        _ensure_cookie()
        return True

    _center_logo()
    tab_login, tab_reg = st.tabs(["Войти", "Зарегистрироваться"])
    with tab_login:
        _login_form()
    with tab_reg:
        _register_form()
    return False


def _center_logo() -> None:
    st.markdown(
        "<div style='text-align:center;margin-bottom:1rem'><h2>🔎 Site-Checker</h2></div>",
        unsafe_allow_html=True,
    )


# ---------- account / dashboards (вызывать из app после require_login) ----------

def render_account_ui() -> None:
    """Блок аккаунта в сайдбаре: кто я + выход.

    Кабинет руководителя и админ-панель — ОБЫЧНЫЕ страницы st.navigation()
    (manager_cabinet_page / admin_panel_page добавляет app.py по роли), а не
    полноэкранный перехват со st.stop(): раньше из-за него клики по боковому
    меню не работали («не могу уйти с админ-панели») и после выхода оставалось
    меню страниц."""
    user = current_user()
    if not user:
        return
    with st.sidebar:
        st.markdown(f"👤 **{user['first_name']} {user['last_name']}**")
        st.caption(f"{user['email']} · {ROLE_LABELS.get(user['role'], user['role'])}")
        if user["projects"]:
            # Только аббревиатура («СМУ»), не полное «СМУ - Стальметурал» -
            # здесь это просто список для ориентировки, длинное имя не нужно.
            st.caption("Проекты: " + ", ".join(
                project_label(p).split(" - ")[0].strip() for p in user["projects"]))

        # Прокси - здесь же, как часть настроек аккаунта (не на каждой
        # странице по отдельности). render_account_ui выполняется РАНЬШЕ
        # скрипта конкретной страницы (app.py зовёт её до st.navigation().run()),
        # поэтому в этот момент ещё не известно, какой проект выбран НА
        # СТРАНИЦЕ - показывать чек-бокс прямо тут означало бы всегда видеть
        # проект с ПРЕДЫДУЩЕГО захода. Вместо этого оставляем пустое место
        # (st.empty) и заполняем его позже, когда сама страница вызовет
        # fill_proxy_slot(pid) с уже известным pid - тогда и позиция (здесь,
        # над «Выйти»), и содержимое (актуальный проект) верные одновременно.
        global _proxy_slot
        _proxy_slot = st.empty()

        render_telegram_block(user)

        if st.button("Выйти", key="logout_btn", use_container_width=True):
            logout()
            st.rerun()


def _tg_bot_token() -> str:
    """Токен бота уведомлений (тот же, что шлёт отчёты)."""
    try:
        return str(st.secrets.get("telegram_bot_token") or "").strip()
    except Exception:
        return ""


@st.cache_data(ttl=3600, show_spinner=False)
def _tg_bot_name(token: str) -> tuple[str, str]:
    """(@имя бота, текст ошибки). Ошибку возвращаем, а не глотаем: «бот не
    отвечает» без причины не даёт понять, дело в токене, сети или вебхуке."""
    import telegram_link
    try:
        return telegram_link.bot_username(token), ""
    except Exception as e:  # noqa: BLE001
        return "", str(e)


def render_telegram_block(user: dict) -> None:
    """«Уведомления в Telegram» в личном кабинете: ссылка-подключение и статус.

    Человек жмёт ссылку, Telegram открывает бота и шлёт ему «/start <код>»;
    кнопка «Проверить подключение» вычитывает апдейты и записывает chat_id.
    Дальше отчёты о ЕГО прогонах приходят ему в личку - без ручного вписывания
    chat_id в секреты."""
    import telegram_link
    from . import db

    token = _tg_bot_token()
    with st.expander("🔔 Уведомления в Telegram", expanded=False):
        if not token:
            st.caption("Бот не настроен: нет секрета `telegram_bot_token`.")
            return
        try:
            row = db.telegram_get(user["id"])
        except Exception as e:  # noqa: BLE001
            st.caption(f"Не удалось прочитать привязку: {e}")
            return

        if row and row.get("chat_id"):
            кто = row.get("username") or row["chat_id"]
            st.success(f"Подключено: {кто}")
            # Руководитель (и админ) может следить за всеми прогонами своих
            # проектов - в том числе за чужими. Сотруднику выбор не нужен:
            # ему приходят только его собственные запуски.
            if user["role"] in ("manager", "admin"):
                _варианты = ["own", "projects"]
                _подписи = {
                    "own": "Только мои запуски",
                    "projects": ("Все прогоны по моим проектам"
                                 if user["role"] == "manager"
                                 else "Все прогоны по всем проектам"),
                }
                _тек = row.get("mode") if row.get("mode") in _варианты else "own"
                _новый = st.radio(
                    "Что присылать", _варианты, index=_варианты.index(_тек),
                    format_func=lambda k: _подписи[k], key="tg_mode")
                if _новый != _тек:
                    db.telegram_set_mode(user["id"], _новый)
                    st.rerun()
            else:
                st.caption("Отчёты о ваших прогонах приходят в этот чат.")
            if st.button("Отключить", key="tg_unlink", use_container_width=True):
                db.telegram_unlink(user["id"])
                st.rerun()
            return

        bot, ошибка = _tg_bot_name(token)
        if not bot:
            st.caption(f"Бот не отвечает: {ошибка or 'причина неизвестна'}. "
                       "Проверьте токен `telegram_bot_token` и доступ к "
                       "api.telegram.org (при блокировке - через прокси).")
            if st.button("Повторить", key="tg_retry", use_container_width=True):
                _tg_bot_name.clear()
                st.rerun()
            return
        code = (row or {}).get("code") or telegram_link.make_code(user["id"])
        try:
            row = db.telegram_ensure_code(user["id"], code)
            code = row["code"]
        except Exception as e:  # noqa: BLE001
            st.caption(f"Не удалось создать код привязки: {e}")
            return

        st.caption("1. Откройте бота и нажмите «Start» 2. Вернитесь и нажмите "
                   "«Проверить подключение».")
        st.link_button("Открыть бота", telegram_link.link_url(bot, code),
                       use_container_width=True)
        if st.button("Проверить подключение", key="tg_check",
                     use_container_width=True):
            try:
                _linked = telegram_link.try_link_all(token)
            except Exception as e:  # noqa: BLE001
                _hook = telegram_link.webhook_set(token)
                if _hook:
                    st.error("У бота задан вебхук - привязка кодом не работает. "
                             "Снимите вебхук (deleteWebhook) или подключайте "
                             "chat_id вручную.")
                else:
                    st.error(f"Telegram не ответил: {e}")
            else:
                # Подтвердиться могла привязка ДРУГОГО человека (его /start
                # лежал в общей очереди апдейтов) - тогда для нажавшего ничего
                # не изменилось, и делать вид, что всё готово, нельзя.
                if str(user["id"]) in _linked:
                    st.rerun()
                else:
                    st.info("Пока не вижу вашего «Start». Откройте бота по "
                            "кнопке выше, нажмите «Start» и повторите проверку.")


_proxy_slot = None


def fill_proxy_slot(pid: str | None) -> str | None:
    """Вызывается со страницы прогона, когда её собственный pid уже известен:
    дорисовывает чек-бокс прокси в место, оставленное render_account_ui (см.
    её докстринг). Если render_account_ui почему-то не вызывалась в этом
    запуске (страница открыта отдельно/до входа) - тихо возвращает None,
    ничего не рисуя."""
    if _proxy_slot is None:
        return None
    import site_access
    with _proxy_slot.container():
        return site_access.render_proxy_toggle(pid)


def render_manager_team(user: dict) -> None:
    """Управление командой: инвайты, проекты сотрудников."""
    mid = user["id"]

    all_projects = project_keys()
    mgr_projects = set(all_projects) if user["role"] == "admin" \
        else set(_c_mgr_projects(mid))

    def _grey(text: str) -> None:
        st.markdown(f"<span style='color:#999'>{text}</span>", unsafe_allow_html=True)

    if user["role"] == "manager":
        if mgr_projects:
            st.caption("Ваши проекты: " + ", ".join(project_label(p) for p in sorted(mgr_projects)))
        else:
            st.warning("У вас не назначены проекты. Попросите администратора назначить "
                       "их вам (Админ-панель → ваш аккаунт → Проекты).")

    team_all = _c_team(mid)
    invites = _c_invites(mid)
    tabsmap = _c_all_tabs()
    rightsmap = _c_all_rights()
    # Руководитель может выдавать только вкладки, доступные ему самому.
    mgr_tabs = live_allowed_tabs(user)

    st.markdown("### 🎟 Инвайт-коды")
    st.caption("Код действует 10 минут, потом сбрасывается. Использованный — пропадает.")
    if st.button("Сгенерировать код", key="gen_inv"):
        code = db.create_invite(mid)
        _invalidate()
        st.success(f"Код: **{code}** (действует 10 минут)")
        st.rerun()
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    for inv in invites:
        left = inv["expires_at"] - now
        mins = max(0, int(left.total_seconds() // 60))
        secs = max(0, int(left.total_seconds() % 60))
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        c1.caption(f"`{inv['code']}` · осталось {mins}м {secs:02d}с")
        if c2.button("🗑", key=f"delinv_{inv['code']}", use_container_width=True):
            db.delete_invite(inv["code"])
            _invalidate()
            st.rerun()
    st.divider()

    team = [u for u in team_all if u["status"] != "pending"]

    st.markdown("### Мои сотрудники")
    if not team:
        st.caption("Пока никого.")
    for u in team:
        uid = str(u["id"])
        cur = list(u.get("projects") or [])
        cur_managed = [p for p in cur if p in mgr_projects]
        cur_foreign = [p for p in cur if p not in mgr_projects]
        st.markdown(f"👤 **{u['first_name']} {u['last_name']}** · {u['email']} · "
                    f"{ROLE_LABELS.get(u['role'], u['role'])} · "
                    f"{STATUS_LABELS.get(u['status'], u['status'])}")
        tc = st.columns([3, 1.6, 1.6, 1.6, 1.6, 1.5], vertical_alignment="bottom")
        sel = tc[0].multiselect("Проекты", sorted(mgr_projects), default=cur_managed,
                                format_func=project_label,
                                key=f"team_pj_{uid}", label_visibility="collapsed",
                                placeholder="проекты")
        if tc[1].button("Сохранить проекты", key=f"team_save_{uid}",
                        use_container_width=True):
            db.set_user_projects(uid, sel + cur_foreign)
            _invalidate()
            st.rerun()
        if tc[2].button("Сбросить пароль", key=f"rst_{uid}", use_container_width=True):
            token = db.create_reset(uid)
            base = _app_base_url()
            link = f"{base}/?reset={token}" if base else f"?reset={token}"
            if not _smtp_configured():
                st.info(f"📮 Почта не настроена — передайте сотруднику ссылку "
                        f"сами (действует 1 час): {link}")
            else:
                ok, err = email_utils.send_reset_email(u["email"], link)
                if ok:
                    st.success(f"Письмо со ссылкой отправлено на {u['email']}")
                else:
                    st.error(f"Письмо не ушло: {err}. Ссылка: {link}")
        if u["status"] == "active":
            if tc[3].button("Отключить аккаунт", key=f"dis_{uid}",
                            use_container_width=True):
                db.set_user_status(uid, "disabled")
                _invalidate()
                st.rerun()
        else:
            if tc[3].button("Включить аккаунт", key=f"ena_{uid}",
                            use_container_width=True):
                db.set_user_status(uid, "active")
                _invalidate()
                st.rerun()
        del_confirm = tc[4].checkbox("Подтвердить удаление", key=f"team_delchk_{uid}")
        if tc[5].button("Удалить сотрудника", key=f"team_del_{uid}",
                        disabled=not del_confirm, use_container_width=True):
            db.delete_user(uid)
            _invalidate()
            st.rerun()
        # Вкладки панели для сотрудника (пусто = все доступные). Чужие вкладки
        # (выданные админом вне ваших) сохраняем нетронутыми - как с проектами.
        cur_tabs = tabsmap.get(uid, [])
        tab_managed = [k for k in cur_tabs if k in mgr_tabs]
        tab_foreign = [k for k in cur_tabs if k not in mgr_tabs]
        tt = st.columns([3, 1.6, 4.6], vertical_alignment="bottom")
        tsel = tt[0].multiselect("Вкладки", mgr_tabs, default=tab_managed,
                                 format_func=tab_label,
                                 key=f"team_tb_{uid}", label_visibility="collapsed",
                                 placeholder="все вкладки")
        if tt[1].button("Сохранить вкладки", key=f"team_tbsave_{uid}",
                        use_container_width=True):
            db.set_user_tabs(uid, tsel + tab_foreign)
            _invalidate()
            st.rerun()
        tt[2].caption("какие разделы меню видит · пусто = все вкладки")
        # Делегирование права менять настройки проекта — в рамках проектов
        # самого руководителя; выданное админом вне их не трогаем.
        cur_rights = rightsmap.get(uid, [])
        rt_managed = [p for p in cur_rights if p in mgr_projects]
        rt_foreign = [p for p in cur_rights if p not in mgr_projects]
        rr = st.columns([3, 1.6, 4.6], vertical_alignment="bottom")
        rsel = rr[0].multiselect("Право настроек", sorted(mgr_projects),
                                 default=rt_managed, format_func=project_label,
                                 key=f"team_rt_{uid}", label_visibility="collapsed",
                                 placeholder="нет права настроек")
        if rr[1].button("Сохранить права", key=f"team_rtsave_{uid}",
                        use_container_width=True):
            db.set_settings_rights(uid, rsel + rt_foreign)
            _invalidate()
            st.rerun()
        rr[2].caption("⚙ право менять настройки проекта (ключи, прокси)")
        if cur_foreign:
            _grey("🔒 вне вашего управления (выдано админом): "
                  + ", ".join(project_label(p) for p in cur_foreign))
        st.markdown("<div style='height:1.2rem'></div>", unsafe_allow_html=True)


def manager_cabinet_page() -> None:
    """Страница «Кабинет руководителя» (st.Page в навигации app.py)."""
    user = current_user()
    if not user or user["role"] not in ("manager", "admin"):
        st.error("Доступ только для руководителей.")
        return
    st.markdown("## 🗂 Кабинет руководителя")
    render_manager_team(user)


def project_settings_page() -> None:
    """Страница «Настройки проекта»: ключи/прокси, общие на команду проекта.
    Доступ: админ (все проекты), руководитель (свои), специалист — если
    руководитель/админ делегировал право («Сохранить права» в кабинете)."""
    user = current_user()
    allowed = live_settings_projects(user) if user else []
    if not allowed:
        st.error("Нет прав на настройки проектов. Право выдаёт руководитель "
                 "или администратор (кабинет → «Право менять настройки»).")
        return
    st.markdown("## 🔑 Настройки проекта")
    st.caption("Ключи общие для проекта — их использует вся команда проекта. "
               "Значение из настроек имеет приоритет над секретами приложения; "
               "пустое поле = настройка удалена (используется секрет, если есть). "
               "Хранится в базе в зашифрованном виде.")
    pid = st.selectbox("Проект", allowed, format_func=project_label,
                       key="ps_project")
    cur = dict(_c_proj_settings(pid))

    def _effective_hint(name: str, pid: str):
        """Что реально используется СЕЙЧАС, если поле в БД пустое (секрет/
        projects.json/переменные окружения). Есть только для полей, у которых
        есть отдельная функция чтения «эффективного» значения - остальные
        читаются точечно внутри своих модулей, обобщать не на чем."""
        try:
            if name == "proxy_url":
                from proxy_config import resolve_proxy
                v = resolve_proxy(pid)
                if not v:
                    return None
                from site_access import _mask
                return _mask(v)
            if name == "kp_sheet_url":
                from kp_sheets import kp_sheet_url
                return kp_sheet_url(pid) or None
        except Exception:
            return None
        return None

    with st.form(key=f"ps_form_{pid}"):
        vals = {}
        for name, label, kind in PROJECT_SETTING_FIELDS:
            if kind == "textarea":
                vals[name] = st.text_area(label, value=cur.get(name, ""),
                                          key=f"ps_{pid}_{name}", height=90)
            elif kind == "password":
                vals[name] = st.text_input(label, value=cur.get(name, ""),
                                           type="password",
                                           key=f"ps_{pid}_{name}")
            else:
                vals[name] = st.text_input(label, value=cur.get(name, ""),
                                           key=f"ps_{pid}_{name}")
            # Поле в БД пустое - значит, вопреки виду, оно НЕ «ничего не
            # настроено»: приложение всё ещё работает через секрет/конфиг.
            # Показываем это явно, иначе (просьба заказчика) непонятно, откуда
            # данные вообще берутся, если тут пусто.
            if not cur.get(name):
                _eff = _effective_hint(name, pid)
                if _eff:
                    st.caption(f"↳ сейчас используется (из секретов/конфига), "
                              f"поле здесь пустое: `{_eff}`")
        if st.form_submit_button("💾 Сохранить ключи"):
            try:
                db.set_project_settings(pid, vals)
                _invalidate()
                st.success(f"✅ Настройки проекта «{project_label(pid)}» сохранены")
            except Exception as e:
                st.error(f"❌ Не удалось сохранить: {e}")

    render_gdrive_account(pid, cur)

    # Google Диск для отчётов: сразу говорим, годится ли указанный ID. Личный
    # gmail и Общий диск снаружи выглядят одинаково, а пишет туда сервисный
    # аккаунт - разницу видно только пробной записью (см. drive_reports).
    def _поле(name: str) -> str:
        """Значение поля: сперва то, что человек ввёл ПРЯМО СЕЙЧАС (виджет), потом
        сохранённое. Иначе проверка ругалась «укажите ID и сохраните» на уже
        вставленный, но ещё не сохранённый ID - и выглядело как поломка."""
        v = st.session_state.get(f"ps_{pid}_{name}")
        return str(v if v is not None else cur.get(name, "") or "").strip()

    # Из поля принимаем и голый ID, и ссылку на папку целиком.
    try:
        from drive_reports import folder_id as _folder_id
    except Exception:  # noqa: BLE001
        def _folder_id(v):
            return v
    _drive_id = _folder_id(_поле("gdrive_folder_id")
                           or _поле("gdrive_shared_drive_id"))
    if st.button("🔍 Проверить доступ к Google Диску", key=f"gd_check_{pid}"):
        _refresh = (cur.get("gdrive_refresh_token") or "").strip()
        if not _drive_id and not _refresh:
            st.warning("Укажите ID папки или общего диска в полях выше (либо "
                       "подключите аккаунт проекта) - и повторите проверку. "
                       "Проверять можно сразу после ввода, до сохранения.")
        else:
            try:
                import drive_reports
                from kp_sheets import service_account_info
                if _refresh:
                    # Подключённый аккаунт проекта: проверяем запись ЕГО
                    # токеном - сервисный аккаунт тут вообще ни при чём.
                    import google_oauth
                    cid, csec = _google_oauth_creds(pid, cur)
                    tok = google_oauth.access_token(cid, csec, _refresh)
                    res = drive_reports.check_write_as_user(
                        tok, _drive_id or "root")
                else:
                    res = drive_reports.check_access(service_account_info(),
                                                     _drive_id)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e), "kind": "", "name": ""}
            if res.get("ok"):
                _где = f"{res.get('kind') or 'Диск'} «{res.get('name') or ''}»".strip()
                st.success(f"✅ {_где} доступен на запись - отчёты будут "
                           f"складываться сюда: год / месяц / вид проверки.")
                if _drive_id and _drive_id != (cur.get("gdrive_folder_id") or
                                               cur.get("gdrive_shared_drive_id")):
                    st.info("Не забудьте нажать «💾 Сохранить ключи» - "
                            "проверка прошла по введённому значению, но в "
                            "настройках оно ещё не сохранено.")
            else:
                st.error(f"❌ {res.get('error')}")

    # Проверка доступа к сайту - здесь, рядом с полем прокси. Раньше этот блок
    # висел на каждой странице чек-листов; убрали, чтобы настройки прокси были
    # ровно в одном месте.
    try:
        from site_access import render_access_check
        # cur уже загружен выше - передаём готовый адрес, чтобы блок не ходил
        # в базу второй раз за той же настройкой.
        render_access_check(pid, default_url=project_main_url(pid),
                            known_proxy=cur.get("proxy_url") or None)
    except Exception as e:  # noqa: BLE001
        st.caption(f"⚠ Блок проверки доступа не загрузился: {e}")


def _google_oauth_creds(pid: str | None = None,
                        settings: dict | None = None) -> tuple[str, str]:
    """(client_id, client_secret) для привязки Google-аккаунтов.

    Порядок: настройки проекта (их видит и правит руководитель прямо в
    интерфейсе) → секреты приложения (доступны только владельцу хостинга,
    остаются как запасной вариант для уже настроенных установок)."""
    cid = csec = ""
    src = settings
    if src is None and pid:
        try:
            src = _c_proj_settings(pid)
        except Exception:  # noqa: BLE001
            src = None
    if src:
        cid = str(src.get("google_oauth_client_id") or "").strip()
        csec = str(src.get("google_oauth_client_secret") or "").strip()
    if cid and csec:
        return cid, csec
    # Ключи OAuth-приложения ОДНИ на все проекты, поэтому вписывать их в каждый
    # проект не нужно: если у этого пусто - берём у любого, где заполнено.
    try:
        for _p in project_keys():
            if _p == pid:
                continue
            s = _c_proj_settings(_p)
            _cid = str(s.get("google_oauth_client_id") or "").strip()
            _csec = str(s.get("google_oauth_client_secret") or "").strip()
            if _cid and _csec:
                return _cid, _csec
    except Exception:  # noqa: BLE001
        pass
    try:
        return (cid or str(st.secrets.get("google_oauth_client_id") or "").strip(),
                csec or str(st.secrets.get("google_oauth_client_secret") or "").strip())
    except Exception:
        return cid, csec


APP_GDRIVE_PID = "_app"          # «проект» для общей привязки Google (state OAuth)


@st.cache_data(ttl=20, show_spinner=False)
def _c_app_settings() -> dict:
    return db.get_app_settings()


def gdrive_account_settings(pid: str | None = None) -> dict:
    """Данные подключённого Google-аккаунта для выкладки отчётов.

    Сначала ОБЩАЯ привязка (один служебный аккаунт на все проекты - его
    подключают один раз, а владельцы просто расшаривают ему папки), потом -
    привязка конкретного проекта, если её успели сделать раньше."""
    try:
        app = _c_app_settings()
    except Exception:  # noqa: BLE001
        app = {}
    if app.get("gdrive_refresh_token"):
        return {"refresh_token": app["gdrive_refresh_token"],
                "account": app.get("gdrive_account", ""), "scope": "общий"}
    if pid:
        try:
            proj = _c_proj_settings(pid)
        except Exception:  # noqa: BLE001
            proj = {}
        if proj.get("gdrive_refresh_token"):
            return {"refresh_token": proj["gdrive_refresh_token"],
                    "account": proj.get("gdrive_account", ""), "scope": "проект"}
    return {}


def render_gdrive_account(pid: str, cur: dict) -> None:
    """Блок «Google Диск»: подключение СЛУЖЕБНОГО Google-аккаунта - одного на все
    проекты.

    Почему не сервисный аккаунт: Google прямо отвечает «Service Accounts do not
    have storage quota» - файл, созданный им в чужой папке, писать некуда.
    Почему один аккаунт, а не по одному на проект: вход через Google нужен
    ровно раз, а владельцы проектов просто расшаривают свои папки на этот
    обычный адрес почты - без паролей и подтверждений с их стороны."""
    import google_oauth

    st.markdown("**Google Диск: аккаунт для выкладки отчётов**")
    привязка = gdrive_account_settings(pid)
    if привязка:
        _чей = ("общий для всех проектов" if привязка["scope"] == "общий"
                else "привязан к этому проекту")
        st.success(f"Подключён аккаунт: {привязка['account'] or 'Google'} "
                   f"({_чей}). Отчёты складываются в папку, указанную выше; "
                   f"если поле пустое - в «Мой диск» этого аккаунта.")
        st.caption("Владельцу проекта достаточно расшарить свою папку на этот "
                   "адрес с правом «Редактор».")
        _к = st.columns(2)
        if привязка["scope"] == "общий":
            if _к[0].button("Отключить общий аккаунт", key="gd_unlink_app"):
                db.set_app_settings({"gdrive_refresh_token": "",
                                     "gdrive_account": ""})
                _c_app_settings.clear()
                st.rerun()
        else:
            if _к[0].button("Отключить аккаунт проекта", key=f"gd_unlink_{pid}"):
                db.set_project_settings(pid, {**cur, "gdrive_refresh_token": "",
                                              "gdrive_account": ""})
                _invalidate()
                st.rerun()
        return

    cid, csec = _google_oauth_creds(pid, cur)
    base = _app_base_url()
    if not (cid and csec):
        st.caption("Заполните выше поля «Google OAuth: Client ID» и «Client "
                   "secret» (берутся в Google Cloud → Credentials → OAuth "
                   "client ID, тип Web application) и сохраните - тогда "
                   "появится кнопка подключения. Если у проекта есть Общий "
                   "диск (Workspace), подключать аккаунт не нужно: хватит "
                   "поля с ID диска.")
        return
    if not base:
        st.caption("Не задан `app.base_url` - без него Google некуда вернуть "
                   "ответ авторизации.")
        return
    st.caption("Два способа - выберите удобный.")
    _c1, _c2 = st.columns(2)
    with _c1:
        st.link_button(f"Подключить аккаунт «{project_label(pid)}»",
                       google_oauth.auth_url(cid, base, pid),
                       use_container_width=True)
        st.caption("Входите почтой САМОГО проекта. Отчёты лягут на его Диск и "
                   "займут его место. Почта должна быть в списке Test users "
                   "экрана согласия (Google Cloud → OAuth consent screen).")
    with _c2:
        st.link_button("Подключить один служебный аккаунт",
                       google_oauth.auth_url(cid, base, APP_GDRIVE_PID),
                       use_container_width=True)
        st.caption("Один вход на ВСЕ проекты. Владельцы просто расшаривают "
                   "свои папки на его адрес («Редактор»); файлы лежат в папке "
                   "проекта, место - служебного аккаунта.")


def handle_gdrive_oauth_redirect() -> None:
    """Приём ответа Google после согласия: ?code=…&state=gdrive:<проект>.

    Зовётся из app.py на КАЖДОЙ странице - Google возвращает человека на
    базовый адрес приложения, а не на страницу настроек."""
    import google_oauth
    try:
        params = st.query_params
        code = params.get("code")
        state = params.get("state")
    except Exception:  # noqa: BLE001
        return
    pid = google_oauth.project_from_state(state or "")
    if not (code and pid):
        return
    # Ключи берём ПО ПРОЕКТУ из state: обработчик срабатывает на любой
    # странице, до того как человек снова выбрал проект в интерфейсе.
    # Для общей привязки (state = gdrive:_app) ключи берутся из любого проекта,
    # где они заполнены - client_id/secret у OAuth-приложения одни на всех.
    cid, csec = _google_oauth_creds(None if pid == APP_GDRIVE_PID else pid)
    if pid == APP_GDRIVE_PID and not (cid and csec):
        for _p in project_keys():
            cid, csec = _google_oauth_creds(_p)
            if cid and csec:
                break
    base = _app_base_url()
    if not (cid and csec and base):
        return
    try:
        res = google_oauth.exchange_code(cid, csec, code, base)
        if pid == APP_GDRIVE_PID:
            # Общий служебный аккаунт: одна привязка на всё приложение.
            vals = {"gdrive_account": res.get("email", "")}
            if res.get("refresh_token"):
                vals["gdrive_refresh_token"] = res["refresh_token"]
            db.set_app_settings(vals)
            _c_app_settings.clear()
            st.success(f"✅ Служебный Google-аккаунт {res.get('email') or ''} "
                       f"подключён. Расшарьте на него папки проектов.")
        else:
            vals = dict(db.get_project_settings(pid))
            if res.get("refresh_token"):
                vals["gdrive_refresh_token"] = res["refresh_token"]
            vals["gdrive_account"] = res.get("email", "")
            db.set_project_settings(pid, vals)
            _invalidate()
            st.success(f"✅ Google-аккаунт {res.get('email') or ''} подключён к "
                       f"проекту «{project_label(pid)}»")
    except Exception as e:  # noqa: BLE001
        st.error(f"❌ Подключить Google не вышло: {e}")
    finally:
        # Код одноразовый - убираем из адреса, иначе обновление страницы
        # пыталось бы обменять его повторно и падало.
        try:
            st.query_params.clear()
        except Exception:  # noqa: BLE001
            pass


def admin_panel_page() -> None:
    """Страница «Админ-панель» (st.Page в навигации app.py)."""
    user = current_user()
    if not user or user["role"] != "admin":
        st.error("Доступ только для администраторов.")
        return
    st.markdown("## ⚙️ Админ-панель")

    all_projects = project_keys()
    me = str(user["id"])
    users = _c_all_users()
    projmap = {str(u["id"]): list(u.get("projects") or []) for u in users}
    tabsmap = _c_all_tabs()
    rightsmap = _c_all_rights()

    def _prune_ms(key: str, options) -> None:
        cur = st.session_state.get(key)
        if isinstance(cur, list):
            valid = set(options)
            pruned = [v for v in cur if v in valid]
            if len(pruned) != len(cur):
                st.session_state[key] = pruned

    def _status_btn(u, col):
        uid = str(u["id"])
        if u["status"] == "active":
            if col.button("Отключить аккаунт", key=f"adis_{uid}", use_container_width=True):
                db.set_user_status(uid, "disabled")
                _invalidate()
                st.rerun()
        else:
            if col.button("Включить аккаунт", key=f"aena_{uid}", use_container_width=True):
                db.set_user_status(uid, "active")
                _invalidate()
                st.rerun()

    def _delete_ctrl(u, col_conf, col_btn):
        uid = str(u["id"])
        if uid == me:
            col_conf.caption("это вы")
            return
        conf = col_conf.checkbox("Подтвердить удаление", key=f"delchk_{uid}")
        if col_btn.button("Удалить аккаунт", key=f"del_{uid}", disabled=not conf,
                          use_container_width=True):
            db.delete_user(uid)
            _invalidate()
            st.rerun()

    def _role_ctrl(u, col_sel, col_btn):
        uid = str(u["id"])
        cur_role = u["role"] if u["role"] in ALL_ROLES else "specialist"
        new_role = col_sel.selectbox(
            "Роль", ALL_ROLES, index=ALL_ROLES.index(cur_role),
            format_func=lambda r: ROLE_LABELS.get(r, r),
            key=f"role_{uid}", label_visibility="collapsed")
        if col_btn.button("Сменить роль", key=f"rolesave_{uid}", use_container_width=True):
            if new_role != u["role"]:
                db.set_user_role(uid, new_role)
                _invalidate()
                st.rerun()

    def _controls(u: dict) -> None:
        uid = str(u["id"])
        if u["role"] == "admin":
            if uid != me:
                c = st.columns([2, 1.5, 2, 1.6, 1.4], vertical_alignment="bottom")
                _role_ctrl(u, c[0], c[1])
                _status_btn(u, c[2])
                _delete_ctrl(u, c[3], c[4])
            else:
                c = st.columns([3, 2], vertical_alignment="bottom")
                _status_btn(u, c[0])
                c[1].caption("это вы")
            st.caption("👁 видит все проекты")
        else:
            if uid != me:
                rc = st.columns([3, 1.6, 4.6], vertical_alignment="bottom")
                _role_ctrl(u, rc[0], rc[1])
                rc[2].caption("роль = права доступа")
            c = st.columns([3, 1.6, 1.6, 1.6, 1.4], vertical_alignment="bottom")
            _pj_default = [p for p in projmap.get(uid, []) if p in all_projects]
            _prune_ms(f"pjedit_{uid}", all_projects)
            sel = c[0].multiselect("Проекты", all_projects, default=_pj_default,
                                   format_func=project_label,
                                   key=f"pjedit_{uid}", label_visibility="collapsed",
                                   placeholder="проекты")
            if c[1].button("Сохранить проекты", key=f"pjsave_{uid}",
                           use_container_width=True):
                db.set_user_projects(uid, sel)
                _invalidate()
                st.rerun()
            _status_btn(u, c[2])
            _delete_ctrl(u, c[3], c[4])
            # Доступ к вкладкам панели (разделам бокового меню). Пусто = все.
            t = st.columns([3, 1.6, 4.6], vertical_alignment="bottom")
            _tb_default = [k for k in tabsmap.get(uid, []) if k in APP_TAB_KEYS]
            _prune_ms(f"tbedit_{uid}", APP_TAB_KEYS)
            tsel = t[0].multiselect("Вкладки", APP_TAB_KEYS, default=_tb_default,
                                    format_func=tab_label,
                                    key=f"tbedit_{uid}", label_visibility="collapsed",
                                    placeholder="все вкладки")
            if t[1].button("Сохранить вкладки", key=f"tbsave_{uid}",
                           use_container_width=True):
                db.set_user_tabs(uid, tsel)
                _invalidate()
                st.rerun()
            t[2].caption("какие разделы меню видит · пусто = все вкладки")
            # Делегирование: право менять НАСТРОЙКИ выбранных проектов
            # (ключи/прокси на странице «Настройки проекта»).
            if u["role"] != "manager":   # у руководителя право уже есть на свои
                r = st.columns([3, 1.6, 4.6], vertical_alignment="bottom")
                _rt_default = [p for p in rightsmap.get(uid, []) if p in all_projects]
                _prune_ms(f"rtedit_{uid}", all_projects)
                rsel = r[0].multiselect("Право настроек", all_projects,
                                        default=_rt_default,
                                        format_func=project_label,
                                        key=f"rtedit_{uid}",
                                        label_visibility="collapsed",
                                        placeholder="нет права настроек")
                if r[1].button("Сохранить права", key=f"rtsave_{uid}",
                               use_container_width=True):
                    db.set_settings_rights(uid, rsel)
                    _invalidate()
                    st.rerun()
                r[2].caption("⚙ право менять настройки проекта (ключи, прокси)")

    emps_by_mgr: dict[str, list] = {}
    for u in users:
        if u["role"] not in ("admin", "manager"):
            key = str(u["manager_id"]) if u["manager_id"] else "_none"
            emps_by_mgr.setdefault(key, []).append(u)
    admins = [u for u in users if u["role"] == "admin"]
    pending_mgrs = [u for u in users if u["role"] == "manager" and u["status"] == "pending"]
    managers = [u for u in users if u["role"] == "manager" and u["status"] != "pending"]

    tab_create, tab_pending, tab_team = st.tabs(
        ["➕ Создать пользователя", "📨 Заявки руководителей", "👥 Сотрудники"]
    )

    with tab_create:
        _adm_msg = st.session_state.pop("_adm_create_msg", None)
        if _adm_msg:
            _kind, _text = _adm_msg
            (st.success if _kind == "ok" else st.error)(_text)
        email = st.text_input("Email", key="adm_email")
        c1, c2 = st.columns(2)
        first = c1.text_input("Имя", key="adm_first")
        last = c2.text_input("Фамилия", key="adm_last")
        pw = c1.text_input("Пароль", type="password", key="adm_pw")
        new_role = c2.selectbox("Роль", ["manager", "admin"],
                                format_func=lambda r: ROLE_LABELS.get(r, r), key="adm_role")
        _prune_ms("adm_projects", all_projects)
        projects = st.multiselect("Проекты", all_projects, key="adm_projects",
                                  format_func=project_label,
                                  placeholder="Выберите проекты",
                                  disabled=(new_role == "admin"),
                                  help="Админ видит все проекты — выбор не нужен.")
        if st.button("Создать", key="adm_create"):
            if not email or "@" not in email or len(pw) < 6 or not first or not last:
                st.error("❌ Заполните все поля, пароль ≥6 символов")
            elif db.get_user_by_email(email):
                st.error("❌ Email уже занят")
            else:
                try:
                    uid = db.create_user(
                        email=email, password=pw, first_name=first, last_name=last,
                        role=new_role, status="active", manager_id=None,
                    )
                    if projects and new_role != "admin":
                        db.set_user_projects(uid, projects)
                    _invalidate()
                    _ok = f"✅ {ROLE_LABELS.get(new_role, new_role)} {email} успешно создан"
                    st.session_state["_adm_create_msg"] = ("ok", _ok)
                    st.toast(_ok, icon="✅")
                    for _k in ("adm_email", "adm_first", "adm_last", "adm_pw"):
                        st.session_state.pop(_k, None)
                    st.rerun()
                except Exception as _e:
                    _err = f"❌ Не удалось создать: {_e}"
                    st.session_state["_adm_create_msg"] = ("err", _err)
                    st.toast(_err, icon="⚠️")
                    st.rerun()

    with tab_pending:
        if not pending_mgrs:
            st.caption("Нет новых заявок.")
        for m in pending_mgrs:
            uid = str(m["id"])
            want = projmap.get(uid, [])
            st.markdown(f"**{m['first_name']} {m['last_name']}** · {m['email']}")
            if want:
                st.caption("Желает проекты: " + ", ".join(project_label(p) for p in want))
            _want_default = [p for p in want if p in all_projects]
            sel = st.multiselect("Выдать доступ к проектам", all_projects, default=_want_default,
                                 format_func=project_label, key=f"mgr_pj_{uid}",
                                 placeholder="Выберите проекты")
            pc = st.columns([1, 1, 4])
            if pc[0].button("Одобрить", key=f"mgr_appr_{uid}", type="primary",
                            use_container_width=True):
                db.set_user_projects(uid, sel)
                db.set_user_status(uid, "active")
                _invalidate()
                st.rerun()
            if pc[1].button("Отклонить", key=f"mgr_rej_{uid}", use_container_width=True):
                db.delete_user(uid)
                _invalidate()
                st.rerun()

    with tab_team:
        st.markdown("#### ⚙️ Администраторы")
        seed_email = _seed_admin_email()
        my_email = security.normalize_email(user["email"])
        for a in admins:
            a_email = security.normalize_email(a["email"])
            protected = seed_email and a_email == seed_email and my_email != seed_email
            if protected:
                st.markdown(f"🔒 **{a['first_name']} {a['last_name']}** · {a['email']} · "
                            "главный администратор (защищён)")
                continue
            with st.expander(f"{a['first_name']} {a['last_name']} · {a['email']}"):
                _controls(a)

        st.markdown("#### 🗂 Руководители и команды")
        if not managers:
            st.caption("Руководителей нет.")
        for m in managers:
            mid2 = str(m["id"])
            emps = emps_by_mgr.get(mid2, [])
            title = (f"{m['first_name']} {m['last_name']} · {m['email']} · "
                     f"{STATUS_LABELS.get(m['status'], m['status'])} · Сотрудников: {len(emps)}")
            with st.expander(title):
                st.markdown("**Руководитель**")
                _controls(m)
                st.divider()
                st.markdown(f"**Сотрудники ({len(emps)})**")
                if not emps:
                    st.caption("Нет сотрудников.")
                for e in emps:
                    st.markdown(
                        f"👤 **{e['first_name']} {e['last_name']}** · {e['email']} · "
                        f"{ROLE_LABELS.get(e['role'], e['role'])} · "
                        f"{STATUS_LABELS.get(e['status'], e['status'])}")
                    _controls(e)
                    st.markdown("<div style='height:1.2rem'></div>", unsafe_allow_html=True)

        orphans = emps_by_mgr.get("_none", [])
        if orphans:
            st.markdown("#### 🚫 Без руководителя")
            for e in orphans:
                with st.expander(f"{e['first_name']} {e['last_name']} · {e['email']} · "
                                 f"{ROLE_LABELS.get(e['role'], e['role'])}"):
                    _controls(e)
