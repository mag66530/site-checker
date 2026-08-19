"""
tg_report.py - проброс Telegram-кредов из секретов в фоновый прогон.

«Проверка целей» и «Проверка форм» запускаются отдельным процессом
(goals_run.py / forms_run.py), а секреты Streamlit доступны только на странице.
Поэтому страница читает секреты и кладёт креды в окружение дочернего процесса, а
сам прогон после сборки отчёта зовёт telegram_notify.send_report_from_env.

Секреты - те же, что у еженедельной проверки (30-мин чек-лист):
  telegram_bot_token            - общий токен бота;
  telegram_recipients_<проект>  - chat_id получателей (список/строка);
  proxy_url                     - (необяз.) прокси для api.telegram.org.
"""
from __future__ import annotations


# Проекты-варианты, у которых нет своих получателей - берут список «родителя».
_RECIPIENTS_FROM = {
    'mpe_cart': 'mpe',      # «МПЭ - Корзина» шлёт тем же, кому МПЭ
}


def _secret(key: str) -> str:
    """Значение секрета Streamlit как строка ('' если нет). Списки (получатели
    заданы как массив) склеиваем через запятую."""
    try:
        import streamlit as st
        if hasattr(st, 'secrets') and key in st.secrets:
            val = st.secrets[key]
            if isinstance(val, (list, tuple)):
                return ','.join(str(x).strip() for x in val if str(x).strip())
            return str(val).strip()
    except Exception:
        pass
    return ''


def _recipients_key(project_id: str) -> str:
    base = _RECIPIENTS_FROM.get(project_id, project_id)
    return f'telegram_recipients_{base}'


def is_configured(project_id: str) -> bool:
    """True, если для проекта настроены и токен, и получатели."""
    return bool(_secret('telegram_bot_token') and _secret(_recipients_key(project_id)))


def _настройка(project_id: str, name: str) -> str:
    """Значение из «Настроек проекта» (личный кабинет) с откатом на секрет."""
    try:
        import auth
        v = auth.project_setting(project_id, name)
        if v:
            return str(v).strip()
    except Exception:
        pass
    return _secret(f'{name}_{project_id}') or _secret(name)


def кто_запустил() -> str:
    """«Имя Фамилия» текущего пользователя ('' - не определён).

    Подпись к отчёту: у руководителя в чате смешиваются свои и чужие прогоны -
    без имени не разобрать, чей отчёт пришёл. Одно определение на все прогоны
    (чек-лист, формы, цели, КП, скорость), чтобы подпись выглядела одинаково.
    """
    try:
        import auth
        u = auth.current_user() or {}
        имя = ' '.join(x for x in (u.get('first_name'), u.get('last_name')) if x)
        return имя or str(u.get('email') or '')
    except Exception:  # noqa: BLE001
        return ''


def _получатели(project_id: str) -> str:
    """chat_id получателей: тот, кто запустил (привязка в кабинете) + подписанные
    на проект руководители; иначе - старый список из секретов."""
    chats: list[str] = []

    def _add(v):
        v = str(v or '').strip()
        if v and v not in chats:
            chats.append(v)

    try:
        import auth
        import telegram_link
        u = auth.current_user()
        if u:
            _add(telegram_link.chat_id_for_user(u['id']))
    except Exception:
        pass
    try:
        from auth import db as _db
        for s in _db.telegram_project_subscribers(project_id):
            _add(s.get('chat_id'))
    except Exception:
        pass
    return ','.join(chats) or _secret(_recipients_key(project_id))


def drive_env(project_id: str, project_name: str = '') -> dict:
    """Env с настройками Google Диска для фонового прогона (формы/цели/КП).
    Пусто, если у проекта Диск не настроен - тогда выкладка просто не идёт."""
    env = {}
    # Общая привязка служебного аккаунта (один на все проекты) - приоритетна.
    refresh = ''
    try:
        import auth
        refresh = (auth.gdrive_account_settings(project_id) or {}).get(
            'refresh_token', '')
    except Exception:
        refresh = ''
    refresh = refresh or _настройка(project_id, 'gdrive_refresh_token')
    root = (_настройка(project_id, 'gdrive_folder_id')
            or _настройка(project_id, 'gdrive_shared_drive_id'))
    if not refresh and not root:
        return env
    if refresh:
        env['GDRIVE_REFRESH_TOKEN'] = refresh
        env['GDRIVE_CLIENT_ID'] = _настройка(project_id, 'google_oauth_client_id')
        env['GDRIVE_CLIENT_SECRET'] = _настройка(project_id, 'google_oauth_client_secret')
    if root:
        env['GDRIVE_ROOT_ID'] = root
    env['GDRIVE_PROJECT_NAME'] = project_name or project_id
    proxy = _secret('proxy_url')
    if proxy:
        env['GDRIVE_PROXY'] = proxy
    return env


def runner_env(project_id: str, project_name: str = '') -> dict:
    """Env-переменные для фонового прогона: Telegram + Google Диск.

    Telegram-часть пустая, если некому слать; Диск-часть - если он не настроен.
    Прогон в обоих случаях просто пропускает соответствующий шаг."""
    env = dict(drive_env(project_id, project_name))
    token = _secret('telegram_bot_token')
    recipients = _получатели(project_id)
    if token and recipients:
        env.update({'TG_BOT_TOKEN': token, 'TG_RECIPIENTS': recipients})
        proxy = _secret('proxy_url')
        if proxy:
            env['TG_PROXY'] = proxy
        # Кто запустил - подпись в конце сообщения. Читает
        # telegram_notify.send_report_from_env, то есть подпись появляется
        # сразу у всех фоновых прогонов (формы, цели, КП, скорость).
        кто = кто_запустил()
        if кто:
            env['TG_STARTED_BY'] = кто
    return env
