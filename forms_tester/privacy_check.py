"""
Пункт чек-листа 2.12: на главной странице проверяем
  • cookie-уведомление для новых пользователей (всплывающая плашка «этот сайт
    использует файлы cookie …»),
  • ссылку на политику в этом уведомлении,
  • наличие живочата (виджет «Онлайн-консультант» в углу).

Заходим на главную каждого проверенного города СВЕЖИМ контекстом браузера (без
cookie - значит «новый пользователь», плашка должна показаться) и пишем 3 строки
в лист «Логи» лога форм. Детект - эвристический (как в 2.7): по видимому тексту и
характерным признакам виджетов. Вызывается из forms_run после прогона форм.
"""
import os
import re
from datetime import datetime


def _playwright_proxy_from_env():
    """Прокси для Playwright из env FORMS_PROXY (http://user:pass@host:port) или
    None. Тот же прокси, что и у основного прогона форм (см. test_all.py) - чтобы
    2.12 не падала на сайтах, режущих прямое подключение (напр. Метпромко)."""
    from urllib.parse import urlparse, unquote
    raw = (os.environ.get("FORMS_PROXY") or "").strip()
    if not raw:
        return None
    pr = urlparse(raw if "://" in raw else "http://" + raw)
    if not pr.hostname:
        return None
    server = f"{pr.scheme or 'http'}://{pr.hostname}"
    if pr.port:
        server += f":{pr.port}"
    conf = {"server": server}
    if pr.username:
        conf["username"] = unquote(pr.username)
    if pr.password:
        conf["password"] = unquote(pr.password)
    return conf


# ── Чистые функции (тестируются без браузера) ────────────────────────


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def текст_про_cookie(text: str) -> bool:
    """True, если текст похож на cookie-уведомление."""
    t = _norm(text)
    return ("cookie" in t) or ("куки" in t) or ("cookies" in t)


# Ссылка ведёт на политику/согласие (по href или тексту ссылки).
_ПОЛИТИКА_МАРКЕРЫ = (
    "politik", "policy", "privacy", "personal", "confiden", "soglas",
    "cookie", "полит", "конфиденциальн", "персональн", "обработк", "куки",
)


def ссылка_на_политику(href: str, text: str) -> bool:
    """True, если ссылка (по href или подписи) ведёт на политику/куки/согласие."""
    h = _norm(href)
    t = _norm(text)
    return any(m in h for m in _ПОЛИТИКА_МАРКЕРЫ) or any(m in t for m in _ПОЛИТИКА_МАРКЕРЫ)


# Признаки виджетов живого чата (скрипты/классы в html + видимые подписи).
_ЧАТ_ПРИЗНАКИ = (
    "jivo", "jdiv", "jivosite", "verbox", "redhelper", "talk-me", "talkme",
    "carrotquest", "chatra", "bitrix24", "b24-widget", "webim", "livechat",
    "onlineconsultant", "online-consultant", "онлайн-консультант",
    "онлайн консультант", "envybox", "chat2desk", "marquiz",
)


def html_содержит_живочат(html: str) -> bool:
    """True, если в html есть характерные признаки виджета онлайн-чата."""
    low = _norm(html)
    return any(s in low for s in _ЧАТ_ПРИЗНАКИ)


# ── Браузерные детекторы ─────────────────────────────────────────────


def детект_cookie_баннер(page):
    """(есть_баннер, есть_ссылка_на_политику) для видимого cookie-уведомления.
    Ищем по характерным контейнерам, затем фолбэк - по видимому тексту страницы."""
    есть_баннер = False
    есть_ссылка = False
    селекторы = ("[class*='cookie']", "[id*='cookie']", "[class*='Cookie']",
                 "[id*='Cookie']", "[class*='consent']", "[class*='gdpr']",
                 "[class*='cook']")
    for sel in селекторы:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                if not текст_про_cookie(el.inner_text(timeout=1000)):
                    continue
                есть_баннер = True
                links = el.locator("a")
                for j in range(min(links.count(), 10)):
                    a = links.nth(j)
                    if ссылка_на_политику(a.get_attribute("href") or "",
                                          a.inner_text(timeout=500) or ""):
                        есть_ссылка = True
                        break
                if есть_баннер:
                    return есть_баннер, есть_ссылка
        except Exception:  # noqa: BLE001
            pass
    # Фолбэк: видимый текст про cookie где-то на странице (плашка без класса cookie).
    try:
        loc = page.get_by_text(re.compile(r"файл[ыои]?\s+cookie|использ\w*\s+cookie|"
                                          r"использ\w*\s+куки|мы\s+используем\s+cookie",
                                          re.I))
        if loc.count() and loc.first.is_visible():
            есть_баннер = True
    except Exception:  # noqa: BLE001
        pass
    return есть_баннер, есть_ссылка


def детект_живочат(page, html: str) -> bool:
    """True, если на странице есть виджет живого чата (по html-признакам или
    видимой подписи «Онлайн-консультант / напишите нам / чат»)."""
    if html_содержит_живочат(html):
        return True
    try:
        loc = page.get_by_text(re.compile(
            r"онлайн[-\s]?консультант|напишите\s+нам|чат\s+с\s+|онлайн[-\s]?чат|"
            r"задать\s+вопрос\s+в\s+чат", re.I))
        if loc.count() and loc.first.is_visible():
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


# ── Запись и оркестрация ─────────────────────────────────────────────


def _записать(excel_path: str, city: str, url: str, название: str,
              есть: bool, коммент: str = "") -> None:
    import test_all as t
    now = datetime.now()
    t.append_log_row(excel_path, {
        "дата": now.strftime("%d.%m.%Y"),
        "время": now.strftime("%H:%M:%S"),
        "город": city,
        "страница": "Главная",
        "url": url,
        "название": название,
        "статус": "Есть" if есть else "Нет",
        "комментарий": "" if есть else коммент,
        "код": "privacy",
    })


def выполнить_проверку(города, excel_path: str = "log_forms.xlsx",
                       show: bool = False, log=print) -> bool:
    """По каждому городу открывает главную свежим контекстом и пишет в «Логи»
    результат по пункту 2.12. `города` - список (город, url_главной).
    Тихо пропускается без списка городов."""
    города = [(c, u) for (c, u) in (города or []) if u]
    if not города:
        return False

    from playwright.sync_api import sync_playwright
    log(f"🍪 Проверка 2.12 (cookie/политика/живочат) на {len(города)} главных …")
    есть_c = есть_ч = 0
    with sync_playwright() as pw:
        _kw = dict(headless=not show,
                   args=["--disable-blink-features=AutomationControlled"])
        _prx = _playwright_proxy_from_env()
        if _prx:
            _kw["proxy"] = _prx
        b = pw.chromium.launch(**_kw)
        # ВАЖНО: свежий контекст без cookie = «новый пользователь» → плашка покажется.
        ctx = b.new_context(locale="ru-RU")
        try:
            for city, url in города:
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)   # даём плашке/чату подгрузиться
                    html = page.content()
                    cookie_есть, политика_есть = детект_cookie_баннер(page)
                    чат_есть = детект_живочат(page, html)
                except Exception as e:  # noqa: BLE001
                    log(f"   ⚠️ {city or url}: не удалось проверить ({e})")
                    continue
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                _записать(excel_path, city, url,
                          "Cookie-уведомление новым пользователям (2.12)",
                          cookie_есть, "cookie-плашка не найдена на главной")
                _записать(excel_path, city, url,
                          "Ссылка на политику в cookie-уведомлении",
                          политика_есть,
                          "в cookie-плашке нет ссылки на политику" if cookie_есть
                          else "cookie-плашки нет")
                _записать(excel_path, city, url, "Живочат (онлайн-консультант)",
                          чат_есть, "виджет живого чата на главной не обнаружен")
                есть_c += int(cookie_есть)
                есть_ч += int(чат_есть)
                log(f"   {city or url}: cookie={'да' if cookie_есть else 'нет'}, "
                    f"политика={'да' if политика_есть else 'нет'}, "
                    f"живочат={'да' if чат_есть else 'нет'}")
        finally:
            b.close()
    log(f"✅ Проверка 2.12: cookie-плашка на {есть_c}/{len(города)} главных, "
        f"живочат на {есть_ч}/{len(города)}. Смотри строки «…(2.12)» в «Логах».")
    return True
