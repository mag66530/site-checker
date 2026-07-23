"""Хеширование паролей и генерация токенов. bcrypt — пароли необратимы.

Отдельно — обратимое шифрование (Fernet) для секретов, которые нужно ДОСТАВАТЬ
и использовать (API-ключи и т.п.). Хэш тут не годится: из него ключ не вернуть.
Шифр-ключ Fernet лежит в st.secrets["app"]["fernet_key"]."""
from __future__ import annotations

import hashlib
import secrets
import string

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def gen_reset_token() -> str:
    """URL-safe токен для ссылки сброса пароля."""
    return secrets.token_urlsafe(24)


def gen_session_token() -> str:
    """URL-safe токен сессии (cookie). Высокоэнтропийный — в БД храним только хэш."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 токена сессии. Токен высокоэнтропийный → bcrypt не нужен, sha256
    даёт быстрый индексируемый поиск по хэшу. В БД попадает только хэш — утечка
    таблицы sessions не раскрывает живые токены из cookie."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def gen_invite_code() -> str:
    """Человекочитаемый инвайт-код: 8 заглавных букв/цифр без похожих (0/O, 1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------- обратимое шифрование секретов (опционально, на будущее) ----------

_FERNET = None


def _get_fernet():
    """Fernet из st.secrets["app"]["fernet_key"]. Кэшируем инстанс.
    Бросает, если секрет не настроен — вызывающий ловит и работает без сохранения."""
    global _FERNET
    if _FERNET is None:
        import streamlit as st
        from cryptography.fernet import Fernet
        _key = st.secrets["app"]["fernet_key"]
        _FERNET = Fernet(_key.encode("utf-8") if isinstance(_key, str) else _key)
    return _FERNET


def encrypt_secret(plaintext: str) -> str:
    """Шифрует строку. Пустое → пустое."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Расшифровывает строку. Битый/чужой шифр или пусто → пустое (не падаем)."""
    if not token:
        return ""
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""
