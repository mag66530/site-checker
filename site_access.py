"""
site_access.py - общий UI-блок для страниц чек-листов: поле прокси и
проверка доступности сайта (реальный IP / нужен ли прокси).

Ставится НАД кнопкой «Запустить проверку». Один компонент на все страницы
(кроме автокликеров) - чтобы не дублировать. render_proxy_access(...)
рисует:
  • текущий IP сервера напрямую;
  • поле ввода прокси + чекбокс «Вкл. Прокси» (мастер-выключатель:
    выключен - прокси не используется, даже если поле заполнено);
  • сворачиваемый блок проверки: IP напрямую, IP с настройками приложения,
    прямой запрос к сайту (статус/время/размер/Server) с цветовой пометкой.

Прокси по умолчанию берётся из секретов проекта; поле позволяет временно
переопределить/протестировать другой.
"""
import re
import time

import streamlit as st

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Сервисы «какой у меня внешний IP» - пробуем по очереди.
_IP_SERVICES = (
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://ifconfig.me/ip",
)


def _secret(key):
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def secret_proxy(pid: str = "") -> str | None:
    """Прокси из секретов: proxy_url_<pid> → proxy_url → env HTTP_PROXY."""
    import os
    if pid:
        v = _secret(f"proxy_url_{pid}")
        if v:
            return v
    return (_secret("proxy_url") or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy"))


def _mask(proxy: str) -> str:
    """Прячем пароль в строке прокси для показа."""
    return re.sub(r"//([^:@/]+):[^@/]+@", r"//\1:***@", proxy or "")


def outbound_ip(proxy: str | None = None, timeout: int = 7):
    """Внешний IP этого сервера (опц. через прокси). (ip, ms, error)."""
    import requests
    proxies = {"http": proxy, "https": proxy} if proxy else None
    last_err = "нет ответа"
    for svc in _IP_SERVICES:
        t0 = time.monotonic()
        try:
            r = requests.get(svc, proxies=proxies, timeout=timeout,
                             headers={"User-Agent": _UA})
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                txt = (r.text or "").strip()
                ip = None
                if txt.startswith("{"):
                    j = r.json()
                    ip = j.get("ip") or j.get("origin")
                else:
                    ip = txt.split(",")[0].strip()
                if ip:
                    return ip, ms, None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return None, None, last_err


def probe_site(url: str, proxy: str | None = None, timeout: int = 12) -> dict:
    """Прямой GET к сайту. Возвращает {status, ms, size, server, error}."""
    import requests
    proxies = {"http": proxy, "https": proxy} if proxy else None
    t0 = time.monotonic()
    try:
        r = requests.get(url, proxies=proxies, timeout=timeout,
                         allow_redirects=True, headers={"User-Agent": _UA})
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": r.status_code, "ms": ms, "size": len(r.content),
                "server": r.headers.get("Server", ""), "error": None}
    except Exception as e:  # noqa: BLE001
        ms = int((time.monotonic() - t0) * 1000)
        kind = "таймаут" if "timeout" in str(e).lower() else "соединение не установлено"
        return {"status": None, "ms": ms, "size": 0, "server": "",
                "error": f"{kind} ({e})"}


def render_proxy_access(key_prefix: str, default_url: str = "",
                        pid: str = "") -> str | None:
    """Рисует блок (поле прокси + проверка доступа) НАД кнопкой запуска.
    Возвращает эффективный прокси (переопределение или из секретов, либо
    None, если чекбокс выключен) - страница может использовать его в прогоне.

    key_prefix - уникальный префикс ключей session_state на страницу."""
    sec_proxy = secret_proxy(pid)

    # ── Текущий IP напрямую (кэшируем на сессию - не дёргаем сеть на каждый rerun) ──
    _ip_key = f"{key_prefix}_direct_ip"
    if _ip_key not in st.session_state:
        st.session_state[_ip_key] = outbound_ip(None)
    _ip, _ms, _err = st.session_state[_ip_key]
    if _ip:
        st.markdown(f"🌐 **ВАШ IP (НАПРЯМУЮ):** `{_ip}`  ·  {_ms} мс")
    else:
        st.caption(f"🌐 IP напрямую определить не удалось ({_err})")

    # ── Поле прокси + чекбокс ──
    c1, c2 = st.columns([4, 1])
    proxy_field = c1.text_input(
        "Прокси", key=f"{key_prefix}_proxy_field",
        placeholder="http://user:pass@host:port (пусто = без прокси)",
        label_visibility="collapsed")
    use_proxy = c2.checkbox("Вкл. Прокси", key=f"{key_prefix}_proxy_on",
                            value=bool(sec_proxy))

    field = (proxy_field or "").strip()
    if use_proxy:
        effective = field or sec_proxy or None
    else:
        effective = None
    st.session_state[f"{key_prefix}_effective_proxy"] = effective

    if use_proxy and effective:
        _src = "введён вручную" if field else "из секретов проекта"
        st.caption(f"Прокси включён (`{_mask(effective)}`, {_src}).")
    elif use_proxy and not effective:
        st.caption("⚠ Прокси включён, но не задан (ни поле, ни секрет) - "
                   "запросы пойдут напрямую.")
    else:
        st.caption("Прокси выключен - запросы напрямую.")

    # ── Блок проверки доступа ──
    with st.expander("🔒 Проверка доступа к сайту (по текущим настройкам "
                     "прокси)", expanded=False):
        url = st.text_input("URL для проверки", value=default_url or "",
                            key=f"{key_prefix}_probe_url",
                            placeholder="https://example.ru/")
        if st.button("Запустить проверку", key=f"{key_prefix}_probe_btn"):
            _u = url.strip()
            if not _u:
                st.caption("URL не задан - вписать адрес для проверки.")
            else:
                with st.spinner("Проверяю доступ…"):
                    site = probe_site(_u, effective)
                _pm = "через прокси" if effective else "напрямую"
                if site["error"]:
                    st.error(f"❌ Не доступен ({_pm}) — {site['error']} · "
                             f"{site['ms']} мс")
                elif site["status"] == 200:
                    st.success(f"✅ Доступен — HTTP 200 ({_pm}) · "
                               f"Server {site['server'] or '—'} · {site['ms']} мс")
                else:
                    st.error(f"❌ Не доступен — HTTP {site['status']} ({_pm}) · "
                             f"{site['ms']} мс"
                             + (" · возможно блок по IP/региону, включи прокси"
                                if effective is None else ""))

    return effective
