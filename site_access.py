"""
site_access.py - проверка доступности сайта (реальный IP / нужен ли прокси).

render_access_check(...) - блок «Доступ к сайту» на странице «Настройки
проекта». Показывает, какой прокси активен, и по кнопке сверяет адрес выхода
напрямую и через прокси + разово дёргает конкретный URL (статус/время/WAF).

Полей прокси здесь НЕТ: адрес задаётся в форме настроек проекта выше,
включается флагом use_proxy из projects/<pid>.json. Раньше такой блок вместе
с полем ввода висел над кнопкой «Запустить проверку» на четырёх страницах
чек-листов (render_proxy_access) - его убрали, чтобы настройки прокси были
ровно в одном месте, а не дублировались на каждой странице прогона.

Побочный эффект переезда: временно подставить другой прокси, не сохраняя его
в настройки, больше нельзя. Если понадобится - это отдельное поле на странице
настроек, а не возврат блока на страницы прогона.
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


def secret_proxy(pid: str = "") -> str | None:
    """Прокси: настройки проекта из кабинета (БД) → proxy_url_<pid> →
    proxy_url → env HTTP_PROXY. Логика вынесена в proxy_config.py - её же
    используют фоновые прогоны (runner_30min.py, run_scheduled.py и т.п.),
    которые исполняются отдельным процессом без Streamlit, - так адрес
    прокси проекта читается ОДНИМ и тем же способом везде."""
    from proxy_config import resolve_proxy
    return resolve_proxy(pid)


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


# Известные анти-DDoS/WAF: если блок (403) отдаёт ОН, а не origin сайта, то и
# whitelist IP надо ставить на нём, а не в nginx (и наоборот). Ключ - маркер в
# заголовках/теле ответа, значение - человекочитаемое имя.
_WAF_MARKERS = {
    "ddos-guard": "DDoS-Guard", "qrator": "Qrator", "cloudflare": "Cloudflare",
    "stormwall": "StormWall", "incapsula": "Imperva/Incapsula",
    "variti": "Variti", "servicepipe": "ServicePipe", "ochrana": "Ochrana",
}


def probe_site(url: str, proxy: str | None = None, timeout: int = 12) -> dict:
    """Прямой GET к сайту. Возвращает {status, ms, size, server, waf, snippet,
    error}. На не-200 waf/snippet помогают понять, КТО режет (origin/WAF)."""
    import requests
    proxies = {"http": proxy, "https": proxy} if proxy else None
    t0 = time.monotonic()
    try:
        r = requests.get(url, proxies=proxies, timeout=timeout,
                         allow_redirects=True, headers={"User-Agent": _UA})
        ms = int((time.monotonic() - t0) * 1000)
        srv = r.headers.get("Server", "")
        body = ""
        try:
            body = (r.text or "")[:3000]
        except Exception:  # noqa: BLE001
            body = ""
        hay = (srv + " " + " ".join(r.headers.keys()) + " " + body).lower()
        waf = next((name for key, name in _WAF_MARKERS.items() if key in hay), "")
        snippet = re.sub(r"<[^>]+>", " ", body)
        snippet = re.sub(r"\s+", " ", snippet).strip()[:160]
        return {"status": r.status_code, "ms": ms, "size": len(r.content),
                "server": srv, "waf": waf,
                "snippet": snippet if r.status_code != 200 else "", "error": None}
    except Exception as e:  # noqa: BLE001
        ms = int((time.monotonic() - t0) * 1000)
        kind = "таймаут" if "timeout" in str(e).lower() else "соединение не установлено"
        return {"status": None, "ms": ms, "size": 0, "server": "", "waf": "",
                "snippet": "", "error": f"{kind} ({e})"}


def render_access_check(pid: str, default_url: str = "",
                        known_proxy: str | None = None) -> None:
    """Проверка доступа к сайту для страницы «Настройки проекта».

    Здесь НЕТ поля прокси и мастер-выключателя - в отличие от старого блока,
    который стоял на страницах чек-листов (он убран: настройки прокси живут
    в одном месте). Адрес задаётся выше в форме настроек, включается флагом
    use_proxy проекта. Это только диагностика «что видно снаружи».

    known_proxy - адрес, уже прочитанный вызывающей страницей. Передавайте
    его: иначе на КАЖДУЮ отрисовку уйдёт лишний запрос в базу за той же
    настройкой, которую страница только что загрузила.

    Сеть дёргаем ТОЛЬКО по кнопке: страница настроек открывается часто, и
    ждать три сетевых запроса при каждом заходе незачем.
    """
    from proxy_config import project_use_proxy, resolve_proxy

    use_proxy = project_use_proxy(pid)
    # Адрес из настроек проекта знаем от вызывающего; лезем в общий механизм
    # (секреты/env) только если там пусто.
    proxy = (known_proxy or resolve_proxy(pid)) if use_proxy else None

    with st.container(border=True):
        st.markdown("**Доступ к сайту**")
        if not use_proxy:
            st.caption("У проекта прокси выключен (`use_proxy: false` в "
                       "`projects/%s.json`) - страницы качаются напрямую." % pid)
        elif proxy:
            st.caption(f"Прокси включён, активен: `{_mask(proxy)}`")
        else:
            st.caption("Прокси включён (`use_proxy: true`), но адрес не задан "
                       "ни здесь, ни в секретах - часть страниц не загрузится.")

        url = st.text_input("Адрес для проверки", value=default_url,
                            key=f"ac_url_{pid}",
                            placeholder="https://example.ru/")
        if not st.button("Проверить доступ", key=f"ac_go_{pid}"):
            return

        with st.spinner("Проверяю…"):
            direct_ip, direct_ms, direct_err = outbound_ip(None)
            proxy_ip = proxy_ms = proxy_err = None
            if proxy:
                proxy_ip, proxy_ms, proxy_err = outbound_ip(proxy)
            probe = probe_site(url, proxy) if url else None

        c1, c2 = st.columns(2)
        c1.metric("IP напрямую", direct_ip or "—",
                  help=direct_err or (f"{direct_ms} мс" if direct_ms else None))
        c2.metric("IP через прокси", proxy_ip or ("—" if proxy else "прокси нет"),
                  help=proxy_err or (f"{proxy_ms} мс" if proxy_ms else None))

        # Главный вопрос блока: реально ли прокси меняет адрес выхода. Если нет -
        # он настроен, но не работает, и это надо увидеть сразу.
        if proxy and direct_ip and proxy_ip:
            if direct_ip == proxy_ip:
                st.warning("Прокси не меняет адрес выхода - похоже, он не "
                           "применяется. Сайт увидит тот же IP.")
            else:
                st.success("Прокси работает: адрес выхода отличается от прямого.")

        if probe is None:
            return
        if probe["error"]:
            st.error(f"Сайт не ответил: {probe['error']}")
        elif probe["status"] == 200:
            st.success(f"200 OK · {probe['ms']} мс · {probe['size']} байт"
                       + (f" · Server: {probe['server']}" if probe["server"] else ""))
        else:
            st.error(f"HTTP {probe['status']} · {probe['ms']} мс"
                     + (f" · режет {probe['waf']}" if probe["waf"] else "")
                     + (f"\n\n{probe['snippet']}" if probe["snippet"] else ""))
