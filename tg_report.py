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


def runner_env(project_id: str) -> dict:
    """Env-переменные с Telegram-кредами для фонового прогона. Пустой словарь,
    если Telegram не настроен (тогда прогон просто не отправит отчёт)."""
    token = _secret('telegram_bot_token')
    recipients = _secret(_recipients_key(project_id))
    if not token or not recipients:
        return {}
    env = {'TG_BOT_TOKEN': token, 'TG_RECIPIENTS': recipients}
    proxy = _secret('proxy_url')
    if proxy:
        env['TG_PROXY'] = proxy
    return env
