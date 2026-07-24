"""Система авторизации site-checker (перенесено из OpenGAR, урезано).

Использование в app.py:
    import auth
    if not auth.require_login():
        st.stop()
    auth.render_account_ui()   # сайдбар: кто я, выход, кабинеты
    user = auth.current_user()
"""
from .ui import (APP_TAB_KEYS, APP_TABS, admin_panel_page, current_user,
                 live_allowed_tabs, live_settings_projects, live_user_projects,
                 logout, manager_cabinet_page, project_setting,
                 project_settings_page, render_account_ui, require_login,
                 tab_label, take_return_slug)

__all__ = ["require_login", "current_user", "logout", "render_account_ui",
           "live_user_projects", "live_allowed_tabs", "APP_TABS",
           "APP_TAB_KEYS", "tab_label", "manager_cabinet_page",
           "admin_panel_page", "project_settings_page", "project_setting",
           "live_settings_projects", "take_return_slug"]
