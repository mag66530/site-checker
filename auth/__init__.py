"""Система авторизации site-checker (перенесено из OpenGAR, урезано).

Использование в app.py:
    import auth
    if not auth.require_login():
        st.stop()
    auth.render_account_ui()   # сайдбар: кто я, выход, кабинеты
    user = auth.current_user()
"""
from .ui import (current_user, live_user_projects, logout,
                 render_account_ui, require_login)

__all__ = ["require_login", "current_user", "logout", "render_account_ui",
           "live_user_projects"]
