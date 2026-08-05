"""Доступ к Postgres (Supabase). Соединение на операцию.

Взято из OpenGAR (auth/db.py), урезано до того, что нужно site-checker:
пользователи, проекты (M:N), инвайт-коды, сброс пароля, persistent-сессии,
seed-админ. Автосейвы/секреты/права-на-проект/статистика прогонов — вырезаны,
их в site-checker нет и они не нужны.

Транзакционный pooler (порт 6543) для коротких connect-per-op; Session pooler
(5432) рвал коннект на commit многооператорных транзакций. prepare_threshold=None
обязателен для pgbouncer transaction mode. @_retry переподнимает операцию при
транзиентном обрыве связи.

Enum-колонки (role/status) приводятся явно: %s::user_role, %s::user_status —
implicit cast text→enum в Postgres нет.
"""
from __future__ import annotations

import datetime as _dt
import functools
import re
import time
from contextlib import contextmanager
from typing import Optional
from urllib.parse import quote

import psycopg
import streamlit as st
from psycopg.rows import dict_row

from . import security


def _db_url() -> str:
    # Transaction pooler (6543): мультиплексит много клиентов через малый серверный
    # пул, idle-клиент НЕ держит серверный коннект. Session pooler (5432) в session
    # mode даёт мало клиентов (каждый держит backend всю жизнь) → лимит упирается
    # при переиспользовании коннекта и оборванных сессиях.
    raw = st.secrets["supabase"]["db_url"].strip().replace(":5432/", ":6543/")
    # Пароль в строке подключения часто содержит спецсимволы URI (?, [], @, #, /,
    # : и т.п.) — без экранирования драйвер не парсит URL ("missing key/value
    # separator" и подобное). Percent-энкодим ТОЛЬКО пароль (между первым ":"
    # после пользователя и последним "@"); заодно снимаем случайные скобки, если
    # пароль скопировали прямо из шаблона [YOUR-PASSWORD]. Простые пароли
    # (буквы/цифры) при этом не меняются.
    m = re.match(r"^(postgres(?:ql)?://[^:@/]+:)(.*)(@[^@]+)$", raw)
    if m:
        head, password, tail = m.groups()
        if len(password) >= 2 and password[0] == "[" and password[-1] == "]":
            password = password[1:-1]
        raw = head + quote(password, safe="") + tail
    return raw


# Транзиентные ошибки связи: обрыв pooler'а/сети/SSL.
_RETRY_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)


def _retry(fn):
    """Повтор операции при транзиентном обрыве связи (pooler/сеть/SSL). До 5 попыток.

    Экспоненциальный backoff (0.5→1→2→4с). Операции идемпотентны (autocommit +
    ON CONFLICT), повтор целиком безопасен даже если часть стейтментов уже прошла."""
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        last = None
        for attempt in range(5):
            try:
                return fn(*args, **kwargs)
            except _RETRY_ERRORS as e:
                last = e
                if attempt < 4:
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
        raise last
    return wrap


def _new_conn():
    # autocommit: каждый стейтмент коммитится сразу. Многооператорные операции
    # (set_user_projects) теряют атомарность, но идемпотентны и под @_retry.
    return psycopg.connect(
        _db_url(), connect_timeout=5, autocommit=True,
        prepare_threshold=None,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )


@contextmanager
def _conn():
    # Connect-per-operation: держаный коннект на transaction pooler рвётся заметно
    # чаще, чем свежий. Скорость берём сокращением ЧИСЛА запросов, не удержанием
    # коннекта.
    conn = _new_conn()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------- users ----------

@_retry
def get_user_by_email(email: str) -> Optional[dict]:
    email = security.normalize_email(email)
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        return cur.fetchone()


@_retry
def get_user_by_id(user_id: str) -> Optional[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


@_retry
def create_user(*, email: str, password: str, first_name: str, last_name: str,
                role: str, status: str, manager_id: Optional[str]) -> str:
    email = security.normalize_email(email)
    pw_hash = security.hash_password(password)
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """INSERT INTO users (email, password_hash, first_name, last_name,
                                  role, status, manager_id)
               VALUES (%s, %s, %s, %s, %s::user_role, %s::user_status, %s)
               RETURNING id""",
            (email, pw_hash, first_name, last_name, role, status, manager_id),
        )
        return str(cur.fetchone()["id"])


@_retry
def set_user_status(user_id: str, status: str) -> None:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE users SET status = %s::user_status, updated_at = now() WHERE id = %s",
            (status, user_id),
        )


@_retry
def set_user_role(user_id: str, role: str) -> None:
    """Меняет роль. У admin/manager нет руководителя → обнуляем manager_id."""
    with _conn() as c, c.cursor() as cur:
        if role in ("admin", "manager"):
            cur.execute(
                "UPDATE users SET role = %s::user_role, manager_id = NULL, "
                "updated_at = now() WHERE id = %s",
                (role, user_id),
            )
        else:
            cur.execute(
                "UPDATE users SET role = %s::user_role, updated_at = now() WHERE id = %s",
                (role, user_id),
            )


@_retry
def update_password(user_id: str, new_password: str) -> None:
    pw_hash = security.hash_password(new_password)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
            (pw_hash, user_id),
        )


@_retry
def list_pending_for_manager(manager_id: str) -> list[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM users WHERE manager_id = %s AND status = 'pending' "
            "ORDER BY created_at",
            (manager_id,),
        )
        return cur.fetchall()


@_retry
def list_team_for_manager(manager_id: str) -> list[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM users WHERE manager_id = %s AND status <> 'pending' "
            "ORDER BY last_name, first_name",
            (manager_id,),
        )
        return cur.fetchall()


@_retry
def get_team_with_projects(manager_id: str) -> list[dict]:
    """Все подчинённые (pending + active/disabled) с их проектами — ОДИН запрос
    (LEFT JOIN + array_agg). Каждая строка: поля users + ['projects']."""
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT u.*,
                      COALESCE(array_agg(up.project_key)
                               FILTER (WHERE up.project_key IS NOT NULL), '{}') AS projects
               FROM users u
               LEFT JOIN user_projects up ON up.user_id = u.id
               WHERE u.manager_id = %s
               GROUP BY u.id
               ORDER BY u.last_name, u.first_name""",
            (manager_id,),
        )
        return cur.fetchall()


@_retry
def get_all_users_with_projects() -> list[dict]:
    """Все юзеры + проекты одним запросом (для админ-панели)."""
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT u.*,
                      COALESCE(array_agg(up.project_key)
                               FILTER (WHERE up.project_key IS NOT NULL), '{}') AS projects
               FROM users u
               LEFT JOIN user_projects up ON up.user_id = u.id
               GROUP BY u.id
               ORDER BY u.role, u.last_name, u.first_name""",
        )
        return cur.fetchall()


@_retry
def list_active_invites(manager_id: str) -> list[dict]:
    """Живые коды (не истёкшие) — один запрос, без удаления в горячем пути."""
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM invite_codes WHERE manager_id = %s AND expires_at > now() "
            "ORDER BY created_at DESC",
            (manager_id,),
        )
        return cur.fetchall()


@_retry
def list_all_users() -> list[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM users ORDER BY role, last_name, first_name")
        return cur.fetchall()


@_retry
def delete_user(user_id: str) -> None:
    """Удаляет аккаунт одним запросом. FK настроены на ON DELETE:
    user_projects/password_resets/invite_codes(manager_id) → CASCADE,
    invite_codes(used_by) → SET NULL, users(manager_id) → SET NULL
    (сотрудники удалённого руководителя отвязываются)."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


# ---------- projects ----------

@_retry
def get_user_projects(user_id: str) -> list[str]:
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT project_key FROM user_projects WHERE user_id = %s", (user_id,))
        return [r[0] for r in cur.fetchall()]


@_retry
def get_projects_for_users(user_ids: list[str]) -> dict[str, list[str]]:
    """Проекты сразу для списка юзеров — один запрос вместо N (кабинет)."""
    if not user_ids:
        return {}
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT user_id, project_key FROM user_projects WHERE user_id = ANY(%s)",
            (list(user_ids),),
        )
        out: dict[str, list[str]] = {}
        for uid, pk in cur.fetchall():
            out.setdefault(str(uid), []).append(pk)
        return out


@_retry
def set_user_projects(user_id: str, project_keys: list[str]) -> None:
    # Один round-trip: DELETE в CTE + многострочный INSERT через unnest.
    keys = list(dict.fromkeys(project_keys or []))  # uniq, сохранив порядок
    with _conn() as c, c.cursor() as cur:
        if keys:
            cur.execute(
                """WITH d AS (
                       DELETE FROM user_projects
                       WHERE user_id = %s AND project_key <> ALL(%s::text[])
                   )
                   INSERT INTO user_projects (user_id, project_key)
                   SELECT %s, x FROM unnest(%s::text[]) AS x
                   ON CONFLICT DO NOTHING""",
                (user_id, keys, user_id, keys),
            )
        else:
            cur.execute("DELETE FROM user_projects WHERE user_id = %s", (user_id,))


# ---------- вкладки (доступ к разделам бокового меню) ----------
# Пустой набор = ВСЕ вкладки (дефолт: никого не ограничиваем). Таблицу создаёт
# сам код при первом обращении (как sessions) — ручной SQL не нужен.

_TABS_TABLE_READY = False


def _ensure_tabs_table() -> None:
    global _TABS_TABLE_READY
    if _TABS_TABLE_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_tabs (
                user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tab_key text NOT NULL,
                PRIMARY KEY (user_id, tab_key)
            );
            """
        )
    _TABS_TABLE_READY = True


@_retry
def get_user_tabs(user_id: str) -> list[str]:
    _ensure_tabs_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT tab_key FROM user_tabs WHERE user_id = %s", (user_id,))
        return [r[0] for r in cur.fetchall()]


@_retry
def get_all_user_tabs() -> dict[str, list[str]]:
    """Вкладки сразу по всем юзерам — один запрос (админка/кабинет)."""
    _ensure_tabs_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT user_id, tab_key FROM user_tabs")
        out: dict[str, list[str]] = {}
        for uid, tk in cur.fetchall():
            out.setdefault(str(uid), []).append(tk)
        return out


@_retry
def set_user_tabs(user_id: str, tab_keys: list[str]) -> None:
    _ensure_tabs_table()
    keys = list(dict.fromkeys(tab_keys or []))
    with _conn() as c, c.cursor() as cur:
        if keys:
            cur.execute(
                """WITH d AS (
                       DELETE FROM user_tabs
                       WHERE user_id = %s AND tab_key <> ALL(%s::text[])
                   )
                   INSERT INTO user_tabs (user_id, tab_key)
                   SELECT %s, x FROM unnest(%s::text[]) AS x
                   ON CONFLICT DO NOTHING""",
                (user_id, keys, user_id, keys),
            )
        else:
            cur.execute("DELETE FROM user_tabs WHERE user_id = %s", (user_id,))


# ---------- настройки проекта (ключи/прокси, общие на команду) ----------
# Значения храним ЗАШИФРОВАННЫМИ (Fernet, security.encrypt_secret) — утечка
# таблицы не раскрывает API-ключи. Таблицы создаёт код сам (как sessions).

_PROJ_SETTINGS_READY = False


def _ensure_proj_settings_table() -> None:
    global _PROJ_SETTINGS_READY
    if _PROJ_SETTINGS_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS project_settings (
                project_key text NOT NULL,
                name        text NOT NULL,
                value       text NOT NULL,
                updated_at  timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (project_key, name)
            );
            """
        )
    _PROJ_SETTINGS_READY = True


@_retry
def get_project_settings(project_key: str) -> dict:
    """{name: значение (расшифрованное)} настроек проекта."""
    _ensure_proj_settings_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT name, value FROM project_settings WHERE project_key = %s",
            (project_key,),
        )
        return {n: security.decrypt_secret(v) for n, v in cur.fetchall()}


@_retry
def set_project_settings(project_key: str, values: dict) -> None:
    """Upsert настроек проекта; пустое значение = удалить настройку.

    Форма шлёт ВСЕ поля разом (сейчас их 11), поэтому пишем двумя запросами:
    один пакетный upsert и один пакетный delete. Раньше был отдельный запрос
    на каждое поле - на удалённой базе это 11 сетевых round-trip'ов подряд,
    и кнопка «Сохранить» заметно подвисала (см. комментарий к _conn: скорость
    берём сокращением ЧИСЛА запросов).
    """
    _ensure_proj_settings_table()
    to_set, to_del = [], []
    for name, val in (values or {}).items():
        val = (val or "").strip()
        if val:
            to_set.append((project_key, name, security.encrypt_secret(val)))
        else:
            to_del.append(name)
    if not to_set and not to_del:
        return
    with _conn() as c, c.cursor() as cur:
        if to_set:
            # Плейсхолдеры генерим по КОЛИЧЕСТВУ строк, значения уходят
            # параметрами - подстановки пользовательских данных в SQL нет.
            rows = ",".join(["(%s, %s, %s)"] * len(to_set))
            cur.execute(
                f"""INSERT INTO project_settings (project_key, name, value)
                    VALUES {rows}
                    ON CONFLICT (project_key, name)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                [p for row in to_set for p in row],
            )
        if to_del:
            cur.execute(
                "DELETE FROM project_settings "
                "WHERE project_key = %s AND name = ANY(%s)",
                (project_key, to_del),
            )


# ---------- делегирование права менять настройки проекта ----------

_RIGHTS_TABLE_READY = False


def _ensure_rights_table() -> None:
    global _RIGHTS_TABLE_READY
    if _RIGHTS_TABLE_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings_rights (
                user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_key text NOT NULL,
                PRIMARY KEY (user_id, project_key)
            );
            """
        )
    _RIGHTS_TABLE_READY = True


@_retry
def get_settings_rights(user_id: str) -> list[str]:
    _ensure_rights_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT project_key FROM settings_rights WHERE user_id = %s",
                    (user_id,))
        return [r[0] for r in cur.fetchall()]


@_retry
def get_all_settings_rights() -> dict[str, list[str]]:
    _ensure_rights_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT user_id, project_key FROM settings_rights")
        out: dict[str, list[str]] = {}
        for uid, pk in cur.fetchall():
            out.setdefault(str(uid), []).append(pk)
        return out


@_retry
def set_settings_rights(user_id: str, project_keys: list[str]) -> None:
    _ensure_rights_table()
    keys = list(dict.fromkeys(project_keys or []))
    with _conn() as c, c.cursor() as cur:
        if keys:
            cur.execute(
                """WITH d AS (
                       DELETE FROM settings_rights
                       WHERE user_id = %s AND project_key <> ALL(%s::text[])
                   )
                   INSERT INTO settings_rights (user_id, project_key)
                   SELECT %s, x FROM unnest(%s::text[]) AS x
                   ON CONFLICT DO NOTHING""",
                (user_id, keys, user_id, keys),
            )
        else:
            cur.execute("DELETE FROM settings_rights WHERE user_id = %s", (user_id,))


# ---------- invite codes ----------

@_retry
def create_invite(manager_id: str, role: Optional[str] = None,
                  ttl_minutes: int = 10) -> str:
    code = security.gen_invite_code()
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=ttl_minutes)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """WITH d AS (
                   DELETE FROM invite_codes WHERE manager_id = %s AND expires_at < now()
               )
               INSERT INTO invite_codes (code, manager_id, role, expires_at)
               VALUES (%s, %s, %s::user_role, %s)""",
            (manager_id, code, manager_id, role, expires),
        )
    return code


@_retry
def get_invite(code: str) -> Optional[dict]:
    code = (code or "").strip().upper()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM invite_codes WHERE code = %s", (code,))
        return cur.fetchone()


def invite_is_valid(inv: Optional[dict]) -> tuple[bool, str]:
    if not inv:
        return False, "Код не найден"
    if inv.get("used_by"):
        return False, "Код уже использован"
    exp = inv.get("expires_at")
    if exp and exp < _dt.datetime.now(_dt.timezone.utc):
        return False, "Срок кода истёк"
    return True, ""


@_retry
def delete_invite(code: str) -> None:
    """Удаляет инвайт-код (после использования или вручную). Не храним."""
    code = (code or "").strip().upper()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM invite_codes WHERE code = %s", (code,))


@_retry
def delete_expired_invites() -> None:
    """Чистит истёкшие коды (TTL вышел)."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM invite_codes WHERE expires_at < now()")


@_retry
def list_invites_for_manager(manager_id: str) -> list[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM invite_codes WHERE manager_id = %s ORDER BY created_at DESC",
            (manager_id,),
        )
        return cur.fetchall()


# ---------- password resets ----------

@_retry
def create_reset(user_id: str, ttl_hours: int = 1) -> str:
    token = security.gen_reset_token()
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=ttl_hours)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO password_resets (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires),
        )
    return token


@_retry
def get_reset(token: str) -> Optional[dict]:
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM password_resets WHERE token = %s", (token,))
        return cur.fetchone()


def reset_is_valid(rec: Optional[dict]) -> tuple[bool, str]:
    if not rec:
        return False, "Ссылка недействительна"
    if rec.get("used"):
        return False, "Ссылка уже использована"
    if rec["expires_at"] < _dt.datetime.now(_dt.timezone.utc):
        return False, "Срок ссылки истёк"
    return True, ""


@_retry
def mark_reset_used(token: str) -> None:
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE password_resets SET used = true WHERE token = %s", (token,))


# ---------- session tokens (persistent login через cookie) ----------
# Сессия переживает refresh: на входе кладём случайный токен в cookie браузера,
# а в БД — только его SHA-256 + срок. На перезагрузке достаём токен из cookie,
# валидируем по хэшу. Утечка таблицы sessions не раскрывает живые токены.

_SESSIONS_TABLE_READY = False


def _ensure_sessions_table() -> None:
    global _SESSIONS_TABLE_READY
    if _SESSIONS_TABLE_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash text PRIMARY KEY,
                user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz NOT NULL
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions (user_id);"
        )
    _SESSIONS_TABLE_READY = True


@_retry
def session_create(user_id: str, token_hash: str, ttl_days: int = 30) -> None:
    """Создаёт сессию: хэш токена + срок. Заодно подчищает истёкшие этого юзера."""
    _ensure_sessions_table()
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=ttl_days)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """WITH d AS (
                   DELETE FROM sessions WHERE user_id = %s AND expires_at < now()
               )
               INSERT INTO sessions (token_hash, user_id, expires_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (token_hash) DO UPDATE SET expires_at = EXCLUDED.expires_at""",
            (user_id, token_hash, user_id, expires),
        )


@_retry
def session_get_user(token_hash: str) -> Optional[dict]:
    """Юзер по хэшу токена, если сессия жива (expires_at > now). Иначе None."""
    if not token_hash:
        return None
    _ensure_sessions_table()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT u.* FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = %s AND s.expires_at > now()""",
            (token_hash,),
        )
        return cur.fetchone()


@_retry
def session_delete(token_hash: str) -> None:
    """Удаляет одну сессию (logout)."""
    if not token_hash:
        return
    _ensure_sessions_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


@_retry
def session_delete_expired() -> None:
    """Чистка всех истёкших сессий (housekeeping)."""
    _ensure_sessions_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at < now()")


# ---------- привязка Telegram (уведомления о прогонах) ----------

_TG_READY = False


def _ensure_telegram_table() -> None:
    """Таблица привязок Telegram: одна строка на пользователя.

    code   - одноразовый код, который человек передаёт боту командой
             /start <code> (ссылка из личного кабинета);
    chat_id- заполняется, когда бот увидел этот /start (до тех пор пусто -
             привязка «ожидает подтверждения»);
    mode   - что присылать: 'own' - только свои запуски (так у сотрудников),
             'projects' - все прогоны по проектам человека (доступно
             руководителю и админу).
    """
    global _TG_READY
    if _TG_READY:
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_telegram (
                user_id    text PRIMARY KEY,
                code       text NOT NULL,
                chat_id    text,
                username   text,
                mode       text NOT NULL DEFAULT 'own',
                created_at timestamptz NOT NULL DEFAULT now(),
                linked_at  timestamptz
            );
            """
        )
        # Таблица могла быть создана ДО появления режима - добавляем колонку.
        cur.execute("ALTER TABLE user_telegram ADD COLUMN IF NOT EXISTS "
                    "mode text NOT NULL DEFAULT 'own';")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS user_telegram_code_idx "
            "ON user_telegram (code);"
        )
    _TG_READY = True


@_retry
def telegram_get(user_id: str) -> Optional[dict]:
    """Привязка пользователя: {user_id, code, chat_id, username, linked_at} или None."""
    if not user_id:
        return None
    _ensure_telegram_table()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM user_telegram WHERE user_id = %s", (str(user_id),))
        return cur.fetchone()


@_retry
def telegram_ensure_code(user_id: str, code: str) -> dict:
    """Вернуть существующую привязку или завести новую с этим кодом.
    Код меняем только если привязки ещё нет - иначе ссылка «прыгала» бы при
    каждом заходе на страницу."""
    _ensure_telegram_table()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """INSERT INTO user_telegram (user_id, code)
               VALUES (%s, %s)
               ON CONFLICT (user_id) DO NOTHING""",
            (str(user_id), code),
        )
        cur.execute("SELECT * FROM user_telegram WHERE user_id = %s", (str(user_id),))
        return cur.fetchone()


@_retry
def telegram_link_by_code(code: str, chat_id: str, username: str = "") -> Optional[str]:
    """Подтвердить привязку по коду из /start. → user_id или None (код не найден)."""
    if not code or not chat_id:
        return None
    _ensure_telegram_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """UPDATE user_telegram
                  SET chat_id = %s, username = %s, linked_at = now()
                WHERE code = %s
            RETURNING user_id""",
            (str(chat_id), (username or "")[:100], code),
        )
        row = cur.fetchone()
        return row[0] if row else None


@_retry
def telegram_set_mode(user_id: str, mode: str) -> None:
    """Режим уведомлений: 'own' (только свои запуски) или 'projects' (все
    прогоны по проектам человека). Чужое значение игнорируем - пишем 'own'."""
    if not user_id:
        return
    _ensure_telegram_table()
    mode = mode if mode in ("own", "projects") else "own"
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE user_telegram SET mode = %s WHERE user_id = %s",
                    (mode, str(user_id)))


@_retry
def telegram_project_subscribers(project_key: str) -> list[dict]:
    """Кому слать отчёт о ЛЮБОМ прогоне по этому проекту.

    Это те, кто в личном кабинете выбрал «все прогоны по моим проектам»
    (mode='projects') И у кого этот проект есть в списке. Админ получает по
    любому проекту - у него доступ ко всем.
    → [{'user_id', 'chat_id', 'role'}, …]
    """
    if not project_key:
        return []
    _ensure_telegram_table()
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT ut.user_id, ut.chat_id, u.role::text AS role
              FROM user_telegram ut
              JOIN users u ON u.id::text = ut.user_id
             WHERE ut.chat_id IS NOT NULL
               AND ut.mode = 'projects'
               AND u.status::text = 'active'
               AND (u.role::text = 'admin'
                    OR EXISTS (SELECT 1 FROM user_projects up
                                WHERE up.user_id::text = ut.user_id
                                  AND up.project_key = %s))
            """,
            (project_key,),
        )
        return list(cur.fetchall())


@_retry
def telegram_unlink(user_id: str) -> None:
    """Отключить уведомления: строку удаляем целиком, следующий заход выдаст
    новый код (старая ссылка перестаёт работать)."""
    if not user_id:
        return
    _ensure_telegram_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM user_telegram WHERE user_id = %s", (str(user_id),))


# ---------- seed admin ----------

def ensure_seed_admin() -> None:
    """Создаёт первого админа из st.secrets[seed_admin], если его ещё нет."""
    try:
        seed = st.secrets["seed_admin"]
        email = security.normalize_email(seed["email"])
        password = seed["password"]
    except Exception:
        return
    if not email or not password:
        return
    if get_user_by_email(email):
        return
    create_user(
        email=email, password=password, first_name="Admin", last_name="Seed",
        role="admin", status="active", manager_id=None,
    )
