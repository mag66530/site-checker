"""
telegram_link.py - привязка Telegram-аккаунта к пользователю личного кабинета.

Зачем: чтобы отчёт о прогоне приходил человеку в Telegram, боту нужен его
chat_id. Раньше chat_id вписывали руками в секрет telegram_recipients_<проект>.
Здесь человек делает это сам из интерфейса: жмёт ссылку вида
    https://t.me/<бот>?start=<код>
Telegram открывает диалог с ботом и шлёт ему «/start <код>». Мы забираем этот
апдейт (getUpdates) и по коду находим, чей это чат.

Почему getUpdates, а не webhook: приложение живёт на Streamlit и постоянного
HTTP-эндпоинта под вебхук у него нет. getUpdates дёргается по кнопке
«Проверить подключение» - этого достаточно: апдейт лежит на серверах Telegram
до 24 часов. ВАЖНО: getUpdates и webhook взаимоисключающи - если у бота задан
вебхук, привязка работать не будет (об этом сообщаем текстом ошибки).

Токен бота - тот же, что и для отправки отчётов (секрет telegram_bot_token).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Optional

import requests

API = "https://api.telegram.org/bot"
_TIMEOUT = 20

# Смещение getUpdates: апдейты Telegram отдаёт по одному разу «до подтверждения»,
# поэтому подтверждаем прочитанное, храня offset в памяти процесса. Потеря
# offset не критична - Telegram отдаст те же апдейты заново (до 24 часов).
_offset: dict[str, int] = {}


def _api(token: str, method: str, params: dict | None = None,
         proxy_url: str | None = None) -> dict:
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    r = requests.get(f"{API}{token}/{method}", params=params or {},
                     proxies=proxies, timeout=_TIMEOUT)
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"Telegram API {method}: ответ не разобрался "
                           f"(HTTP {r.status_code})")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: "
                           f"{data.get('description') or r.status_code}")
    return data.get("result") or {}


def bot_username(token: str, proxy_url: str | None = None) -> str:
    """@username бота (без «@») - из него собираем ссылку привязки."""
    if not token:
        return ""
    return str(_api(token, "getMe", proxy_url=proxy_url).get("username") or "")


def webhook_set(token: str, proxy_url: str | None = None) -> str:
    """URL вебхука, если он задан у бота ('' - не задан). При заданном вебхуке
    getUpdates всегда возвращает 409 - привязка кодом работать не будет."""
    if not token:
        return ""
    try:
        return str(_api(token, "getWebhookInfo", proxy_url=proxy_url).get("url") or "")
    except Exception:  # noqa: BLE001
        return ""


def make_code(user_id: str) -> str:
    """Код привязки: короткий, но неугадываемый. Привязан к пользователю, чтобы
    два человека не получили одинаковый код даже при совпадении случайности."""
    seed = f"{user_id}:{secrets.token_hex(8)}:{time.time()}"
    return "u" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def link_url(bot: str, code: str) -> str:
    """Ссылка «подключить уведомления». Telegram передаст код боту в /start."""
    return f"https://t.me/{bot}?start={code}" if bot and code else ""


_START_RE = re.compile(r"^/start(?:@\w+)?\s+(\S+)")


def collect_starts(token: str, proxy_url: str | None = None) -> dict[str, dict]:
    """Забрать новые апдейты и вернуть {код: {chat_id, username}} по всем
    полученным «/start <код>».

    Возвращаем ВСЕ найденные коды разом (а не только «свой»): апдейты выдаются
    один раз, и если чужой /start попал в ту же пачку, его нельзя терять -
    иначе привязка того человека не сработает никогда.
    """
    if not token:
        return {}
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    off = _offset.get(token)
    if off:
        params["offset"] = off
    updates = _api(token, "getUpdates", params, proxy_url=proxy_url) or []
    out: dict[str, dict] = {}
    for upd in updates:
        try:
            _offset[token] = int(upd.get("update_id", 0)) + 1
        except Exception:  # noqa: BLE001
            pass
        msg = upd.get("message") or {}
        text = str(msg.get("text") or "").strip()
        m = _START_RE.match(text)
        if not m:
            continue
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        name = (chat.get("username") or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x) or "")
        out[m.group(1)] = {"chat_id": str(chat_id), "username": str(name)}
    return out


def send_hello(token: str, chat_id: str, text: str,
               proxy_url: str | None = None) -> bool:
    """Короткое подтверждение в чат после успешной привязки."""
    try:
        _api(token, "sendMessage",
             {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
             proxy_url=proxy_url)
        return True
    except Exception:  # noqa: BLE001
        return False


def try_link_all(token: str, proxy_url: str | None = None) -> int:
    """Вычитать апдейты и записать все найденные привязки в БД.
    → сколько привязок подтверждено. Ошибки БД не глушим - их видно в UI."""
    from auth import db
    starts = collect_starts(token, proxy_url=proxy_url)
    n = 0
    for code, info in starts.items():
        uid = db.telegram_link_by_code(code, info["chat_id"], info.get("username", ""))
        if uid:
            n += 1
            send_hello(token, info["chat_id"],
                       "✅ Уведомления подключены. Сюда будут приходить отчёты "
                       "о ваших прогонах.", proxy_url=proxy_url)
    return n


def chat_id_for_user(user_id: str) -> Optional[str]:
    """chat_id пользователя или None, если Telegram не подключён."""
    from auth import db
    row = db.telegram_get(user_id)
    return (row or {}).get("chat_id") or None
