"""
site_access.py - общий UI-блок для страниц чек-листов: поле прокси и
проверка доступности сайта (реальный IP / нужен ли прокси).

Ставится НАД кнопкой «Запустить проверку». Один компонент на все страницы
(кроме автокликеров) - чтобы не дублировать. render_proxy_access(...)
рисует:
  • поле ввода прокси + чекбокс «Вкл. Прокси» (мастер-выключатель:
    выключен - прокси не используется, даже если поле заполнено);
  • один свёрнутый блок «Доступ к сайту» с вердиктом в заголовке
    (напрямую / через прокси / прокси не меняет адрес), а внутри: какой
    прокси активен, два адреса выхода рядом (напрямую и через прокси),
    цветной итог и разовая проверка конкретного URL (статус/время/Server).

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
                        pid: str = "", default_on: bool | None = None) -> str | None:
    """Рисует блок (поле прокси + проверка доступа) НАД кнопкой запуска.
    Возвращает эффективный прокси (переопределение или из секретов, либо
    None, если чекбокс выключен) - страница может использовать его в прогоне.

    key_prefix - уникальный префикс ключей session_state на страницу.
    default_on - стартовое состояние галочки «Вкл. Прокси»: None (по умолчанию) -
    как раньше, включена при наличии секрета; True/False - явно задать (напр. на
    «Проверке форм» прокси нужен только части проектов, остальным - выключен)."""
    sec_proxy = secret_proxy(pid)

    # ── Текущий IP напрямую (кэшируем на сессию - не дёргаем сеть на каждый rerun) ──
    _ip_key = f"{key_prefix}_direct_ip"
    if _ip_key not in st.session_state:
        st.session_state[_ip_key] = outbound_ip(None)
    _ip, _ms, _err = st.session_state[_ip_key]

    # ── Поле прокси + чекбокс (управление прогоном - остаётся на виду) ──
    c1, c2 = st.columns([4, 1])
    proxy_field = c1.text_input(
        "Прокси", key=f"{key_prefix}_proxy_field",
        placeholder="http://user:pass@host:port (пусто = без прокси)",
        label_visibility="collapsed")
    _chk_default = bool(sec_proxy) if default_on is None else bool(default_on)
    use_proxy = c2.checkbox("Вкл. Прокси", key=f"{key_prefix}_proxy_on",
                            value=_chk_default)

    field = (proxy_field or "").strip()
    if use_proxy:
        effective = field or sec_proxy or None
    else:
        effective = None
    st.session_state[f"{key_prefix}_effective_proxy"] = effective

    # ── IP через прокси: кэш по значению эффективного прокси (один запрос на
    #    прокси за сессию). Позволяет увидеть, реально ли прокси подменяет адрес. ──
    _pip = _pms = _perr = None
    if effective:
        _pip_key = f"{key_prefix}_proxy_ip::{effective}"
        if _pip_key not in st.session_state:
            st.session_state[_pip_key] = outbound_ip(effective)
        _pip, _pms, _perr = st.session_state[_pip_key]

    # ── Единый вердикт: как сейчас идут запросы. Один текст на все места:
    #    заголовок свёрнутого блока, цветная плашка внутри. ──
    _same_ip = bool(effective and _pip and _ip and _pip == _ip)
    if not use_proxy:
        _tag, _kind = "напрямую", "off"
    elif not effective:
        _tag, _kind = "⚠ прокси не задан", "warn"
    elif _same_ip:
        _tag, _kind = "⚠ прокси не меняет адрес", "warn"
    elif _pip:
        _tag, _kind = "через прокси", "ok"
    else:
        _tag, _kind = "⚠ прокси не проверен", "warn"

    # ── Один свёрнутый блок «Доступ»: строка прокси, два адреса рядом,
    #    цветной итог и разовая проверка конкретного сайта. ──
    with st.expander(f"🔒 Доступ к сайту · {_tag}", expanded=False):
        # 1) Какой прокси активен (маскируем пароль).
        if not use_proxy:
            st.caption("Прокси выключен — все запросы идут напрямую.")
        elif not effective:
            st.caption("⚠ Прокси включён, но не задан (ни поле, ни секрет) — "
                       "запросы пойдут напрямую.")
        else:
            _src = "введён вручную" if field else "из секретов проекта"
            st.caption(f"Прокси: `{_mask(effective)}` · {_src}")

        # 2) Два адреса выхода рядом - сразу видно, подменяет ли прокси IP.
        ca, cb = st.columns(2)
        ca.markdown("🌐 **Напрямую**  \n"
                    + (f"`{_ip}` · {_ms} мс" if _ip else f"_не определён ({_err})_"))
        if not effective:
            _cell = "_прокси выключен_"
        elif _pip:
            _cell = f"`{_pip}` · {_pms} мс"
        else:
            _cell = f"_не определён ({_perr})_"
        cb.markdown(f"🛡 **Через прокси**  \n{_cell}")

        # 3) Цветной итог одной фразой.
        if _kind == "off":
            st.info("Запросы идут напрямую — прокси выключен.")
        elif not effective:
            st.warning("Прокси включён, но адрес не задан — фактически идём напрямую.")
        elif _same_ip:
            st.warning("Прокси включён, но адрес выхода не меняется — проверьте "
                       "строку прокси или секрет.")
        elif _pip:
            st.success(f"Прокси работает — выход подменяется на `{_pip}`.")
        else:
            st.warning(f"Прокси включён, но проверить адрес не удалось ({_perr}).")

        st.divider()

        # 4) Разовая проверка конкретного сайта по текущим настройкам.
        st.markdown("**Проверить конкретный сайт**")
        url = st.text_input("URL для проверки", value=default_url or "",
                            key=f"{key_prefix}_probe_url",
                            placeholder="https://example.ru/",
                            label_visibility="collapsed",
                            help="Один запрос к указанному адресу ТЕКУЩИМИ настройками "
                                 "прокси - тем же способом, каким пойдёт основная "
                                 "проверка. Показывает, доступен ли сайт (HTTP 200), "
                                 "какой сервер и сколько заняло, - чтобы заранее понять, "
                                 "не блокирует ли сайт наш IP/регион, не запуская "
                                 "полный прогон.")
        if st.button("Проверить доступ", key=f"{key_prefix}_probe_btn"):
            _u = url.strip()
            if not _u:
                st.caption("URL не задан — впишите адрес для проверки.")
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
                             + (" · возможно блок по IP/региону, включите прокси"
                                if effective is None else ""))

    return effective
