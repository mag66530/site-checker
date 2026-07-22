"""
uniqueness_checker.py - проверка уникальности контента страниц через text.ru.

Движок без Streamlit: его зовёт фоновый прогон (uniqueness_run.py) и CLI.

Что делает:
  • для набора URL (обычно небольшая выборка ГЛАВНОГО домена: главная, каталог,
    N категорий, N товаров) скачивает страницу и достаёт «основной текст»
    (без сквозной шапки/подвала/меню, без скриптов/стилей);
  • отправляет текст в API text.ru (антиплагиат) и забирает результат:
    процент уникальности + список ЧУЖИХ сайтов, с которыми пересекается контент;
  • свои домены исключаются через exceptdomain - иначе города-поддомены (дубли
    по дизайну) занулят уникальность, и «источник пересечения» будет свой же сайт.

API text.ru асинхронный: сначала POST с текстом -> text_uid, потом POST с uid
опрашиваем готовность. Документация: https://text.ru/api-check/manual

Ключ (userkey) сюда передаётся явно вызывающим кодом (из секретов приложения);
модуль сам секреты не читает и в git ничего не кладёт.

requests импортируется лениво - парсинг/извлечение текста можно тестировать без
сети и без установленного requests (через подставной http_post/fetcher).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

API_ENDPOINT = "https://api.text.ru/post"

# Лимиты text.ru (документация): не чаще 10 текстов/сек, не более 3 млн симв./час.
# Держимся сильно ниже. Текст: 100..150000 символов.
MIN_CHARS = 100
MAX_CHARS = 150_000

# Коды ошибок text.ru, означающие «результат ещё не готов» - надо опрашивать
# дальше, это не фатально. (Полный список кодов - в мануале API.)
_NOT_READY_CODES = {181, 182, 183}


class TextRuError(Exception):
    """Ошибка API text.ru (неверный ключ, пустой текст, лимит и т.п.)."""
    def __init__(self, code, desc=""):
        self.code = code
        self.desc = desc or ""
        super().__init__(f"text.ru error {code}: {self.desc}".strip())


# ── Извлечение «основного текста» страницы ───────────────────────────────────
# Срезаем сквозные шапку/подвал/меню - оставляем контентную область (как в
# content_checker), затем берём видимый текст (как в text_checker).
_CHROME_RE = re.compile(r"<(header|footer|nav|aside)\b[^>]*>.*?</\1>", re.I | re.S)
_RE_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
_RE_TAG = re.compile(r"<[^>]+>")


def _visible_text(html: str) -> str:
    """Весь видимый текст фрагмента HTML (без скриптов/стилей/тегов)."""
    try:
        from text_checker import html_to_visible_text
        return html_to_visible_text(html)
    except Exception:
        h = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
        h = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", " ", h, flags=re.I)
        h = re.sub(r"<!--[\s\S]*?-->", " ", h)
        h = re.sub(r"<[^>]+>", " ", h)
        h = (h.replace("&nbsp;", " ").replace("&amp;", "&")
             .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
        return re.sub(r"\s+", " ", h).strip()


def extract_main_text(html: str, *, prefer_paragraphs: bool = True) -> str:
    """«Контентный» текст страницы для проверки уникальности.

    Сначала берём СВЯЗНЫЙ SEO-текст - абзацы <p> от 100 символов. Это ровно
    «текст, который лежит внизу категории», описание товара и т.п. - именно его
    надо проверять на уникальность, а НЕ карточки товаров / меню / фильтры
    (они короткие и повторяются между страницами, зашумляют проверку).

    Если связного текста нет (или он совсем короткий) - откатываемся на весь
    видимый текст контентной области (без сквозных шапки/подвала/меню)."""
    if not html:
        return ""
    body = _CHROME_RE.sub(" ", html)
    if prefer_paragraphs:
        paras = []
        for p_ in _RE_P.findall(body):
            t = re.sub(r"\s+", " ", _RE_TAG.sub(" ", p_)).strip()
            if len(t) >= 100:
                paras.append(t)
        seo = " ".join(paras)
        if len(seo) >= MIN_CHARS:
            return seo
    return _visible_text(body)


def _project_domains(urls) -> str:
    """exceptdomain-строка: все домены/поддомены из набора URL проекта (через
    пробел, с ведущим '*.' для поддоменов недостаточно - text.ru принимает точные
    хосты и wildcard'ы). Возвращаем уникальные хосты + wildcard основного домена,
    чтобы исключить и соседние города."""
    hosts, roots = [], set()
    seen = set()
    for u in urls:
        h = urlparse(u).netloc.lower()
        if not h or h in seen:
            continue
        seen.add(h)
        hosts.append(h)
        parts = h.split(".")
        if len(parts) >= 2:
            roots.add(".".join(parts[-2:]))
    # wildcard'ы корневых доменов ловят любые города-поддомены (spb.mepen.ru и т.п.)
    wild = [f"*.{r}" for r in sorted(roots)] + sorted(roots)
    out, seen2 = [], set()
    for x in hosts + wild:
        if x not in seen2:
            seen2.add(x)
            out.append(x)
    return " ".join(out)


# ── Клиент API text.ru ───────────────────────────────────────────────────────
class TextRuClient:
    """Тонкий клиент text.ru. http_post можно подменить для тестов (принимает
    (url, data: dict) -> dict распарсенного JSON)."""

    def __init__(self, userkey: str, *, http_post: Optional[Callable] = None,
                 timeout: int = 40):
        self.userkey = (userkey or "").strip()
        self.timeout = timeout
        self._http_post = http_post
        self._session = None

    def _post(self, data: dict) -> dict:
        if self._http_post is not None:
            return self._http_post(API_ENDPOINT, data)
        import requests
        if self._session is None:
            self._session = requests.Session()
        r = self._session.post(API_ENDPOINT, data=data, timeout=self.timeout)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            # text.ru изредка отдаёт JSON с префиксом/мусором - вытащим {...}
            m = re.search(r"\{.*\}", r.text, re.S)
            if m:
                return json.loads(m.group(0))
            raise

    def submit(self, text: str, *, exceptdomain: str = "", visible: bool = False) -> str:
        """Отправить текст на проверку. Возвращает text_uid или бросает TextRuError."""
        data = {"text": text, "userkey": self.userkey}
        if exceptdomain:
            data["exceptdomain"] = exceptdomain
        if visible:
            data["visible"] = "vis_on"
        resp = self._post(data) or {}
        uid = resp.get("text_uid")
        if uid:
            return uid
        raise TextRuError(resp.get("error_code"), resp.get("error_desc"))

    def result(self, uid: str) -> Optional[dict]:
        """Забрать результат по uid. Возвращает dict
        {'unique': float, 'urls': [{'url','plagiat'}], 'raw': {...}} когда готово,
        или None если проверка ещё идёт. Бросает TextRuError на фатальных кодах."""
        resp = self._post({"uid": uid, "userkey": self.userkey}) or {}
        tu = resp.get("text_unique")
        if tu is not None and tu != "":
            try:
                rj = json.loads(resp.get("result_json") or "{}")
            except Exception:
                rj = {}
            urls = []
            for it in (rj.get("urls") or []):
                try:
                    urls.append({"url": it.get("url", ""),
                                 "plagiat": float(str(it.get("plagiat", "0")).replace(",", "."))})
                except Exception:
                    urls.append({"url": it.get("url", ""), "plagiat": None})
            try:
                unique = float(str(tu).replace(",", "."))
            except Exception:
                unique = None
            return {"unique": unique, "urls": urls, "raw": rj}
        code = resp.get("error_code")
        if code in _NOT_READY_CODES or code is None:
            return None       # ещё считается - опрашиваем дальше
        raise TextRuError(code, resp.get("error_desc"))

    def check_account(self) -> dict:
        """Баланс аккаунта (символы). Полезно для проверки ключа/остатка."""
        import requests
        if self._session is None and self._http_post is None:
            self._session = requests.Session()
        endpoint = "https://api.text.ru/account"
        if self._http_post is not None:
            return self._http_post(endpoint, {"userkey": self.userkey, "method": "get_packages"})
        r = self._session.post(endpoint, data={"userkey": self.userkey,
                                               "method": "get_packages"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ── Прогон партии URL ────────────────────────────────────────────────────────
@dataclass
class UniqResult:
    url: str
    type_code: str = "other"
    unique: Optional[float] = None          # процент уникальности 0..100
    sources: list = field(default_factory=list)  # [{'url','plagiat'}] чужих совпадений
    chars: int = 0
    error: str = ""

    @property
    def has_data(self) -> bool:
        return self.unique is not None


def _default_fetch(url: str, timeout: int = 30) -> str:
    """Скачать HTML страницы (лениво requests, браузерный UA)."""
    import requests
    headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36")}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def run_batch(
    typed_urls,                          # список (url, type_code)
    client: TextRuClient,
    *,
    exceptdomain: str = "",
    fetcher: Optional[Callable[[str], str]] = None,
    log: Optional[Callable[[str], None]] = None,
    submit_pause: float = 1.5,           # пауза между отправками (лимит 10/сек, но щадим)
    poll_interval: float = 15.0,         # пауза между кругами опроса результата
    max_wait: float = 480.0,             # сколько всего ждать результаты, сек
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> list[UniqResult]:
    """Скачать страницы, извлечь текст, отправить в text.ru и собрать результаты.
    Возвращает UniqResult в исходном порядке URL."""
    def _log(m):
        if log:
            log(m)

    fetch = fetcher or _default_fetch

    # уникализируем URL, сохраняя порядок и тип первого вхождения
    order, types = [], {}
    for url, tc in typed_urls:
        if url and url not in types:
            types[url] = tc or "other"
            order.append(url)

    results: dict[str, UniqResult] = {u: UniqResult(url=u, type_code=types[u]) for u in order}
    pending: dict[str, str] = {}          # uid -> url
    total = len(order)

    # 1) Скачать + извлечь текст + отправить
    for i, url in enumerate(order, 1):
        res = results[url]
        try:
            html = fetch(url)
        except Exception as e:            # noqa: BLE001
            res.error = f"страница не скачалась: {str(e)[:120]}"
            _log(f"[{i}/{total}] ✗ {url} - {res.error}")
            continue
        text = extract_main_text(html)
        res.chars = len(text)
        if res.chars < MIN_CHARS:
            res.error = f"текста мало ({res.chars} симв.) - нечего проверять"
            _log(f"[{i}/{total}] — {url} - {res.error}")
            continue
        if res.chars > MAX_CHARS:
            text = text[:MAX_CHARS]
            res.chars = MAX_CHARS
        try:
            uid = client.submit(text, exceptdomain=exceptdomain)
            pending[uid] = url
            _log(f"[{i}/{total}] → отправлено {url} ({res.chars} симв.)")
        except TextRuError as e:
            res.error = f"не принято text.ru: {e.desc or e.code}"
            _log(f"[{i}/{total}] ✗ {url} - {res.error}")
        except Exception as e:            # noqa: BLE001
            res.error = f"ошибка отправки: {str(e)[:120]}"
            _log(f"[{i}/{total}] ✗ {url} - {res.error}")
        if i < total:
            sleeper(submit_pause)

    # 2) Опрашивать результаты, пока не готово всё или не выйдет время
    if pending:
        _log(f"Отправлено {len(pending)} текстов. Жду результаты…")
    deadline = now() + max_wait
    while pending and now() < deadline:
        sleeper(poll_interval)
        for uid in list(pending.keys()):
            url = pending[uid]
            try:
                r = client.result(uid)
            except TextRuError as e:
                results[url].error = f"ошибка результата: {e.desc or e.code}"
                pending.pop(uid, None)
                _log(f"✗ {url} - {results[url].error}")
                continue
            except Exception as e:        # noqa: BLE001
                # транзиентная сетевая ошибка - попробуем в следующем круге
                _log(f"… {url} - временная ошибка опроса: {str(e)[:80]}")
                continue
            if r is None:
                continue                  # ещё не готово
            res = results[url]
            res.unique = r["unique"]
            # чужие источники: отсортировать по проценту совпадения (по убыванию)
            res.sources = sorted(
                [s for s in r["urls"] if s.get("url")],
                key=lambda s: (s.get("plagiat") or 0), reverse=True)
            pending.pop(uid, None)
            _u = f"{res.unique:.1f}%" if res.unique is not None else "?"
            _log(f"✓ {url} - уникальность {_u}, источников: {len(res.sources)}")

    for uid, url in pending.items():
        if not results[url].error:
            results[url].error = "результат не пришёл за отведённое время"
            _log(f"⏳ {url} - {results[url].error}")

    return [results[u] for u in order]


def summarize(results: list[UniqResult], threshold: float = 95.0) -> dict:
    """Свод для отчёта/страницы."""
    checked = [r for r in results if r.has_data]
    below = [r for r in checked if (r.unique or 0) < threshold]
    uniques = [r.unique for r in checked if r.unique is not None]
    return {
        "total": len(results),
        "checked": len(checked),
        "below": len(below),
        "errors": len([r for r in results if r.error]),
        "avg_unique": round(sum(uniques) / len(uniques), 1) if uniques else None,
        "min_unique": round(min(uniques), 1) if uniques else None,
        "threshold": threshold,
    }
