"""Отправка писем через SMTP (Gmail). Сейчас один сценарий — ссылка сброса пароля."""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

import streamlit as st


def _smtp_cfg() -> dict:
    return dict(st.secrets["smtp"])


def send_reset_email(to_email: str, reset_link: str) -> tuple[bool, str]:
    """Шлёт письмо со ссылкой сброса. Возвращает (ok, error)."""
    try:
        cfg = _smtp_cfg()
        host = cfg["host"]
        port = int(cfg["port"])
        user = cfg["user"]
        app_password = str(cfg["app_password"]).replace(" ", "")
        from_name = cfg.get("from_name", "Site-Checker")
    except Exception as e:
        return False, f"SMTP не настроен: {e}"

    body = (
        f"Запрошен сброс пароля в Site-Checker.\n\n"
        f"Перейдите по ссылке, чтобы задать новый пароль (действует 1 час):\n"
        f"{reset_link}\n\n"
        f"Если вы не запрашивали сброс — проигнорируйте письмо."
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "Сброс пароля — Site-Checker"
    msg["From"] = formataddr((from_name, user))
    msg["To"] = to_email

    try:
        with smtplib.SMTP(host, port, timeout=20) as srv:
            srv.starttls()
            srv.login(user, app_password)
            srv.send_message(msg)
        return True, ""
    except Exception as e:
        return False, str(e)
