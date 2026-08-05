"""
google_oauth.py - привязка ЛИЧНОГО Google-аккаунта проекта (обычный gmail).

Зачем. Отчёты на Диск пишет сервисный аккаунт, но у него нет своего места:
запись возможна только в Общий диск (Workspace). У обычного gmail Общих
дисков нет, поэтому для таких проектов подключаем сам аккаунт: человек один
раз разрешает доступ, мы храним refresh-токен и дальше создаём папки и файлы
ОТ ЕГО ИМЕНИ - место расходуется его, владелец файлов он же.

Область доступа - drive.file: приложение видит и меняет ТОЛЬКО то, что само
создало. К остальным файлам аккаунта доступа нет (важно: это личная почта).

Что нужно один раз завести в Google Cloud (та же организация, что и ключ
сервисного аккаунта):
  • OAuth client ID типа «Web application»;
  • в «Authorized redirect URIs» - адрес приложения (app.base_url), а для
    локального запуска ещё и http://localhost:8501;
  • секреты приложения: google_oauth_client_id, google_oauth_client_secret.
"""
from __future__ import annotations

from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Только свои файлы - не весь Диск (см. докстринг модуля).
SCOPES = ["https://www.googleapis.com/auth/drive.file",
          "https://www.googleapis.com/auth/userinfo.email"]

STATE_PREFIX = "gdrive:"


def auth_url(client_id: str, redirect_uri: str, project_id: str) -> str:
    """Ссылка «разрешить доступ». access_type=offline + prompt=consent -
    обязательны, иначе Google не выдаст refresh-токен на повторных заходах."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": f"{STATE_PREFIX}{project_id}",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def project_from_state(state: str) -> str:
    """id проекта из state ('' - state не наш)."""
    s = str(state or "")
    return s[len(STATE_PREFIX):] if s.startswith(STATE_PREFIX) else ""


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect_uri: str, proxy_url: str | None = None) -> dict:
    """Код авторизации → {'refresh_token', 'access_token', 'email'}.
    Бросает RuntimeError с текстом ответа Google - его показываем в интерфейсе."""
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    r = requests.post(TOKEN_URL, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, proxies=proxies, timeout=30)
    try:
        data = r.json() if r.content else {}
    except Exception:  # noqa: BLE001
        data = {}
    if r.status_code != 200 or not data.get("access_token"):
        код = str(data.get("error") or "")
        текст = str(data.get("error_description") or "")
        # Google на все частые ошибки отвечает скупым «Bad Request», поэтому
        # переводим их в понятные причины: почти всегда это либо повторное
        # использование кода, либо несовпадение адреса возврата, либо чужая
        # пара client_id/secret.
        подсказки = {
            "invalid_grant": ("код авторизации уже использован или просрочен - "
                              "нажмите «Подключить» ещё раз и не обновляйте "
                              "страницу после возврата"),
            "redirect_uri_mismatch": (
                f"адрес возврата не совпадает: в Google Cloud → Credentials → "
                f"ваш OAuth client → Authorized redirect URIs должна быть "
                f"строка ровно «{redirect_uri}» (без слеша на конце)"),
            "invalid_client": ("не подходит пара Client ID / Client secret - "
                               "проверьте, что скопированы из одного и того же "
                               "OAuth client"),
            "unauthorized_client": ("этот OAuth client не разрешён для такого "
                                    "входа - проверьте, что тип клиента "
                                    "«Web application»"),
        }
        причина = подсказки.get(код, "")
        куски = [x for x in (текст or код or f"HTTP {r.status_code}", причина) if x]
        raise RuntimeError("Google не выдал токен: " + ". ".join(куски))
    email = ""
    try:
        u = requests.get(USERINFO_URL,
                         headers={"Authorization": f"Bearer {data['access_token']}"},
                         proxies=proxies, timeout=30)
        if u.status_code == 200:
            email = str((u.json() or {}).get("email") or "")
    except Exception:  # noqa: BLE001
        pass
    # Какие права реально выдал человек. В окне согласия галочку доступа к
    # Диску можно СНЯТЬ - тогда токен выдаётся, подключение выглядит удачным, а
    # первая же запись падает с «Request had insufficient authentication
    # scopes». Поэтому проверяем сразу здесь.
    выдано = str(data.get("scope") or "")
    if "drive" not in выдано:
        raise RuntimeError(
            "доступ к Google Диску не выдан. В окне Google отметьте флажок "
            "про создание и изменение файлов на Диске (он снимается вручную) "
            "и повторите подключение.")
    return {"refresh_token": data.get("refresh_token", ""),
            "access_token": data["access_token"], "email": email,
            "scope": выдано}


def access_token(client_id: str, client_secret: str, refresh_token: str,
                 proxy_url: str | None = None) -> str:
    """Свежий access-token по сохранённому refresh-токену."""
    if not (client_id and client_secret and refresh_token):
        return ""
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, proxies=proxies, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code != 200 or not data.get("access_token"):
        raise RuntimeError(f"Google не обновил токен: "
                           f"{data.get('error_description') or data.get('error') or r.status_code}. "
                           f"Возможно, доступ отозван - подключите аккаунт заново.")
    return str(data["access_token"])
