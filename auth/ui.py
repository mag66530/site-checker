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


def _invalidate() -> None:
    """Сбросить кеши после мутации (одобрение/проекты/статус/удаление/инвайт)."""
    _c_team.clear()
    _c_invites.clear()
    _c_mgr_projects.clear()
    _c_all_users.clear()
    _c_user_projects_live.clear()
    _c_user_tabs_live.clear()
    _c_all_tabs.clear()


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
            st.caption("Проекты: " + ", ".join(project_label(p) for p in user["projects"]))
        if st.button("Выйти", key="logout_btn", use_container_width=True):
            logout()
            st.rerun()


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
