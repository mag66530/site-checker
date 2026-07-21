"""
pagespeed_checker.py - проверка скорости страниц через Google PageSpeed Insights
(Lighthouse) с разбивкой по типам страниц. Движок без Streamlit - его зовёт и
страница приложения, и CLI.

Что делает:
  • шлёт каждый URL в PageSpeed Insights API v5 (desktop + mobile, locale=ru,
    Google сам возвращает рекомендации на русском);
  • достаёт оценку производительности (0-100) и метрики Lighthouse
    (FCP, LCP, CLS, TBT) + при наличии - «полевые» данные CrUX (реальные
    пользователи);
  • душит частоту запросов (QPS + пул потоков + семафор на домен), чтобы не
    упереться в лимиты Google и не завалить сам сайт параллельными прогонами;
  • классифицирует URL по типу (Главная/Категория/Товар/Фильтр/…);
  • считает средние по типам и топ-рекомендации.

Хранение истории и сравнение периодов - в pagespeed_history.py.
Отчёт (xlsx) - в pagespeed_report.py.

Ключ PageSpeed API берётся вызывающим кодом из секретов приложения и передаётся
сюда явно - модуль сам никакие секреты не читает и в git ничего не кладёт.

Провайдер сделан через интерфейс SpeedProvider, чтобы позже можно было добавить
GTmetrix / WebPageTest, не трогая остальной код.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

# requests импортируем лениво внутри провайдера - чтобы модуль (парсинг, агрегат,
# классификация) можно было тестировать без установленного requests и без сети.

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

STRATEGIES = ("desktop", "mobile")

# ── Троттлинг (безопасные дефолты, как у рабочего скрипта) ────────────────────
# Официальный лимит PSI ~ 400 запросов / 100 сек. Держимся сильно ниже.
DEFAULT_MAX_WORKERS = 10          # потоков: они долго ждут ответ Google
DEFAULT_MAX_QPS = 2               # запросов в секунду - с большим запасом
DEFAULT_MAX_PER_DOMAIN = 3        # одновременных Lighthouse-проверок одного домена


# ── Типы страниц ─────────────────────────────────────────────────────────────
# Совпадают с type_code в sources.py, плюс человеко-читаемые подписи.
TYPE_LABELS = {
    "main": "Главная",
    "catalog": "Каталог",
    "category": "Категория",
    "product": "Товар",
    "filter": "Фильтр",
    "tech": "Тех. страница",
    "custom": "URL",
    "other": "Прочее",
}
# Порядок для сводных таблиц/отчёта.
TYPE_ORDER = ("main", "catalog", "category", "product", "filter", "tech", "custom", "other")


# ── Пороги Google для оценки/цвета ───────────────────────────────────────────
# Оценка производительности (Lighthouse): 90-100 хорошо, 50-89 средне, 0-49 плохо.
def score_rating(score: Optional[float]) -> str:
    """'good' / 'ok' / 'poor' / 'na' по оценке производительности 0-100."""
    if score is None:
        return "na"
    if score >= 90:
        return "good"
    if score >= 50:
        return "ok"
    return "poor"


# Пороги Core Web Vitals (числовые значения: секунды / безразмерн. / мс).
# (good_max, ok_max): <=good_max -> good; <=ok_max -> ok; иначе poor.
METRIC_THRESHOLDS = {
    "fcp": (1.8, 3.0),      # секунды
    "lcp": (2.5, 4.0),      # секунды
    "cls": (0.1, 0.25),     # безразмерная
    "tbt": (200.0, 600.0),  # миллисекунды
}


def metric_rating(metric: str, value: Optional[float]) -> str:
    """'good' / 'ok' / 'poor' / 'na' для метрики по её числовому значению."""
    if value is None or metric not in METRIC_THRESHOLDS:
        return "na"
    good_max, ok_max = METRIC_THRESHOLDS[metric]
    if value <= good_max:
        return "good"
    if value <= ok_max:
        return "ok"
    return "poor"


# ── Классификация URL (эвристика для произвольных ссылок) ─────────────────────
# Когда выборку формируем из каталогов проекта (sources.py) - тип известен точно
# и передаётся явно. Эта функция - запасной вариант для вставленного вручную
# списка URL. Настраивается через catalog_prefixes/filter-признаки проекта.
_FILTER_MARKERS = ("/filter/", "/tag/", "/tags/")
_FILTER_PARAMS = ("filter", "price", "brand", "d", "gost", "material", "size")


def classify_url(url: str, root_domain: str = "", catalog_prefixes=("/catalog/", "/katalog/")) -> str:
    """Грубо определить тип страницы по URL. Возвращает type_code."""
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return "other"
    path = (p.path or "/").rstrip("/") or "/"
    low = path.lower()
    query = p.query or ""

    # Главная: пустой путь (учитываем и поддомены - у них свой «/»).
    if path == "/":
        return "main"

    # Фильтр: есть query-параметры-фильтры или маркеры фильтра/тега в пути.
    if any(m in low for m in _FILTER_MARKERS):
        return "filter"
    if query:
        keys = {kv.split("=", 1)[0].lower() for kv in query.split("&") if kv}
        if keys & set(_FILTER_PARAMS):
            return "filter"

    # Каталог и его глубина.
    for pref in catalog_prefixes:
        pref_l = pref.lower()
        if low == pref_l.rstrip("/"):
            return "catalog"
        if low.startswith(pref_l):
            tail = low[len(pref_l):].strip("/")
            depth = len([s for s in tail.split("/") if s])
            # 1 сегмент под каталогом - категория; 2+ - обычно карточка товара.
            return "category" if depth <= 1 else "product"

    return "other"


# ── Результат одной проверки (url × strategy) ────────────────────────────────
@dataclass
class MetricSet:
    """Метрики Lighthouse одного прогона. *_val - числовое значение для оценки,
    *_disp - как показывает Google (локализованная строка, напр. «2,6 с»)."""
    score: Optional[float] = None       # 0-100
    fcp_val: Optional[float] = None      # сек
    lcp_val: Optional[float] = None      # сек
    cls_val: Optional[float] = None      # безразмерн.
    tbt_val: Optional[float] = None      # мс
    fcp_disp: str = ""
    lcp_disp: str = ""
    cls_disp: str = ""
    tbt_disp: str = ""
    recs: list = field(default_factory=list)   # рекомендации (проваленные аудиты)
    crux: dict = field(default_factory=dict)   # полевые данные CrUX (если есть)
    elapsed: float = 0.0                 # время проверки, сек
    error: Optional[str] = None

    def as_row(self) -> dict:
        return {
            "score": self.score,
            "fcp": self.fcp_disp, "lcp": self.lcp_disp,
            "cls": self.cls_disp, "tbt": self.tbt_disp,
            "fcp_val": self.fcp_val, "lcp_val": self.lcp_val,
            "cls_val": self.cls_val, "tbt_val": self.tbt_val,
            "recs": self.recs, "crux": self.crux,
            "error": self.error,
        }


def _clean(s) -> str:
    if s is None:
        return ""
    return str(s).replace("\n", " ").replace("\r", " ").strip()


def _ms_to_s(v):
    return round(v / 1000.0, 2) if isinstance(v, (int, float)) else None


def _fmt_bytes(n) -> str:
    """Байты -> человекочитаемо (КБ/МБ)."""
    if not isinstance(n, (int, float)) or n <= 0:
        return ""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} МБ"
    return f"{n / 1024:.0f} КБ"


def _fmt_ms(n) -> str:
    """Миллисекунды -> человекочитаемо (мс/с)."""
    if not isinstance(n, (int, float)) or n <= 0:
        return ""
    return f"{n / 1000:.1f} с" if n >= 1000 else f"{int(n)} мс"


def _audit_items(audit: dict, limit: int = 3) -> list[dict]:
    """Конкретные проблемные ресурсы из details.items аудита: [{url, info}].

    info - что именно не так (экономия в КБ / задержка в мс / размер)."""
    det = (audit or {}).get("details") or {}
    items = det.get("items") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = it.get("url") or ""
        if not url and isinstance(it.get("source"), dict):
            url = it["source"].get("url", "")
        if not url:
            # сторонний код и т.п. - показываем название сущности
            url = it.get("entity") or it.get("groupLabel") or ""

        parts = []
        wb, tb, wm = it.get("wastedBytes"), it.get("totalBytes"), it.get("wastedMs")
        if isinstance(wb, (int, float)) and wb > 0:
            parts.append(f"экономия {_fmt_bytes(wb)}")
        elif isinstance(tb, (int, float)) and tb > 0:
            parts.append(_fmt_bytes(tb))
        if isinstance(wm, (int, float)) and wm > 0:
            parts.append(f"−{_fmt_ms(wm)}")

        if not url and not parts:
            continue
        out.append({"url": _clean(url), "info": ", ".join(p for p in parts if p)})
        if len(out) >= limit:
            break
    return out


def parse_psi_response(data: dict, elapsed: float = 0.0) -> MetricSet:
    """Разобрать JSON-ответ PageSpeed Insights в MetricSet.

    Вынесено отдельно от сети, чтобы тестировать на сохранённой фикстуре."""
    ms = MetricSet(elapsed=round(elapsed, 2))

    lr = (data or {}).get("lighthouseResult") or {}
    cats = lr.get("categories") or {}
    perf = cats.get("performance") or {}
    score = perf.get("score")
    ms.score = round(score * 100, 1) if isinstance(score, (int, float)) else None

    audits = lr.get("audits") or {}

    def _audit(name):
        a = audits.get(name) or {}
        return a.get("numericValue"), _clean(a.get("displayValue"))

    fcp_n, ms.fcp_disp = _audit("first-contentful-paint")
    lcp_n, ms.lcp_disp = _audit("largest-contentful-paint")
    cls_n, ms.cls_disp = _audit("cumulative-layout-shift")
    tbt_n, ms.tbt_disp = _audit("total-blocking-time")

    ms.fcp_val = _ms_to_s(fcp_n)
    ms.lcp_val = _ms_to_s(lcp_n)
    ms.cls_val = round(cls_n, 3) if isinstance(cls_n, (int, float)) else None
    ms.tbt_val = round(tbt_n, 0) if isinstance(tbt_n, (int, float)) else None

    # Рекомендации: аудиты с оценкой < 1 (проблемные), у которых есть заголовок.
    # score None у информационных аудитов - их не берём. Тащим и конкретику:
    # displayValue (сколько сэкономим) + items (какие именно ресурсы и где).
    recs = []
    for aid, a in audits.items():
        sc = a.get("score")
        if not (isinstance(sc, (int, float)) and sc < 1 and a.get("title")):
            continue
        recs.append({
            "id": aid,
            "title": _clean(a.get("title")),
            "savings": _clean(a.get("displayValue")),   # напр. «Экономия 320 КБ»
            "items": _audit_items(a),                    # конкретные ресурсы
        })
    ms.recs = recs

    # Полевые данные CrUX (реальные пользователи), если Google их отдал.
    le = (data or {}).get("loadingExperience") or {}
    metrics = le.get("metrics") or {}
    crux = {}
    _map = {
        "LARGEST_CONTENTFUL_PAINT_MS": "lcp",
        "FIRST_CONTENTFUL_PAINT_MS": "fcp",
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls",
        "INTERACTION_TO_NEXT_PAINT": "inp",
    }
    for key, short in _map.items():
        m = metrics.get(key)
        if m and "category" in m:
            crux[short] = {"percentile": m.get("percentile"),
                           "category": m.get("category")}
    if le.get("overall_category"):
        crux["overall"] = le["overall_category"]
    ms.crux = crux
    return ms


# ── Провайдеры скорости ──────────────────────────────────────────────────────
class SpeedProvider:
    """Интерфейс источника скорости. check() возвращает MetricSet."""
    name = "base"

    def check(self, url: str, strategy: str) -> MetricSet:  # pragma: no cover
        raise NotImplementedError


class PageSpeedInsightsProvider(SpeedProvider):
    """Google PageSpeed Insights API v5."""
    name = "PageSpeed Insights"

    def __init__(self, api_key: str, locale: str = "ru", timeout: int = 120,
                 retries: int = 2):
        self.api_key = api_key
        self.locale = locale
        self.timeout = timeout
        self.retries = retries
        import requests  # ленивый импорт - только когда реально ходим в сеть
        self._session = requests.Session()

    def check(self, url: str, strategy: str) -> MetricSet:
        import requests
        params = {
            "url": url,
            "strategy": strategy,
            "locale": self.locale,
            "category": "performance",
        }
        if self.api_key:
            params["key"] = self.api_key

        t0 = time.time()
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                r = self._session.get(PSI_ENDPOINT, params=params,
                                      timeout=self.timeout)
                if r.status_code == 200:
                    return parse_psi_response(r.json(), time.time() - t0)
                # 429/5xx - транзиентные, есть смысл повторить.
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2 + attempt * 3)
                    continue
                return MetricSet(error=_psi_http_error(r), elapsed=round(time.time() - t0, 2))
            except requests.exceptions.RequestException as e:
                last_err = _clean(str(e))
                if attempt < self.retries:
                    time.sleep(2 + attempt * 3)
                    continue
        return MetricSet(error=last_err or "сетевая ошибка",
                         elapsed=round(time.time() - t0, 2))


def _psi_http_error(r) -> str:
    """Понятное сообщение по ошибочному ответу PSI."""
    try:
        j = r.json()
        msg = (((j.get("error") or {}).get("message")) or "")[:200]
    except Exception:
        msg = (r.text or "")[:200]
    if r.status_code == 429:
        return f"лимит запросов PageSpeed (HTTP 429): {msg}"
    if r.status_code == 400:
        return f"страница недоступна для проверки (HTTP 400): {msg}"
    if r.status_code == 403:
        return f"ключ PageSpeed отклонён (HTTP 403): {msg}"
    return f"PageSpeed HTTP {r.status_code}: {msg}"


# ── Троттлинг ────────────────────────────────────────────────────────────────
class _Throttle:
    """Ограничитель: не чаще max_qps запросов в секунду + не больше
    max_per_domain одновременных проверок одного домена."""
    def __init__(self, max_qps: int, max_per_domain: int):
        self._min_interval = 1.0 / max_qps if max_qps > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0
        self._max_per_domain = max_per_domain
        self._dom_lock = threading.Lock()
        self._dom_sems: dict[str, threading.Semaphore] = {}

    def _domain_sem(self, url: str) -> threading.Semaphore:
        dom = urlparse(url).netloc
        with self._dom_lock:
            sem = self._dom_sems.get(dom)
            if sem is None:
                sem = threading.Semaphore(self._max_per_domain)
                self._dom_sems[dom] = sem
            return sem

    def pace(self):
        """Подождать, чтобы соблюсти QPS."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.time()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()


# ── Прогон партии URL ────────────────────────────────────────────────────────
@dataclass
class PageResult:
    url: str
    type_code: str
    desktop: MetricSet
    mobile: MetricSet

    @property
    def has_data(self) -> bool:
        return (self.desktop.score is not None) or (self.mobile.score is not None)


def run_batch(
    typed_urls,                       # список (url, type_code)
    provider: SpeedProvider,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_qps: int = DEFAULT_MAX_QPS,
    max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
    log: Optional[Callable[[str], None]] = None,
) -> list[PageResult]:
    """Прогнать все URL по desktop и mobile с троттлингом. Возвращает список
    PageResult в исходном порядке URL."""
    def _log(m):
        if log:
            log(m)

    # Уникализируем, сохраняя порядок и тип первого вхождения.
    order: list[str] = []
    types: dict[str, str] = {}
    for url, tc in typed_urls:
        if url and url not in types:
            types[url] = tc or "other"
            order.append(url)

    throttle = _Throttle(max_qps, max_per_domain)
    results: dict[str, dict[str, MetricSet]] = {u: {} for u in order}

    def _task(url: str, strategy: str) -> MetricSet:
        sem = throttle._domain_sem(url)
        with sem:
            throttle.pace()
            return provider.check(url, strategy)

    total = len(order) * len(STRATEGIES)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for url in order:
            for strat in STRATEGIES:
                futures[ex.submit(_task, url, strat)] = (url, strat)
        for fut in as_completed(futures):
            url, strat = futures[fut]
            try:
                ms = fut.result()
            except Exception as e:   # noqa: BLE001
                ms = MetricSet(error=_clean(str(e)))
            results[url][strat] = ms
            done += 1
            if ms.error:
                _log(f"[{done}/{total}] {strat:7} ✗ {url} – {ms.error}")
            else:
                _log(f"[{done}/{total}] {strat:7} → {url} – {ms.score}")

    out = []
    for url in order:
        out.append(PageResult(
            url=url,
            type_code=types[url],
            desktop=results[url].get("desktop", MetricSet(error="нет данных")),
            mobile=results[url].get("mobile", MetricSet(error="нет данных")),
        ))
    return out


# ── Агрегация по типам ───────────────────────────────────────────────────────
def _avg(nums):
    nums = [n for n in nums if isinstance(n, (int, float))]
    return round(sum(nums) / len(nums), 1) if nums else None


def aggregate(results: list[PageResult]) -> dict:
    """Средние оценки desktop/mobile по каждому типу страницы + всего.

    Возвращает:
      {
        'by_type': {type_code: {'count', 'desktop_avg', 'mobile_avg'}},
        'overall': {'count', 'desktop_avg', 'mobile_avg'},
      }
    """
    buckets: dict[str, dict] = {}
    for r in results:
        b = buckets.setdefault(r.type_code, {"desktop": [], "mobile": []})
        if r.desktop.score is not None:
            b["desktop"].append(r.desktop.score)
        if r.mobile.score is not None:
            b["mobile"].append(r.mobile.score)

    by_type = {}
    all_d, all_m = [], []
    for tc, b in buckets.items():
        by_type[tc] = {
            "count": max(len(b["desktop"]), len(b["mobile"])),
            "desktop_avg": _avg(b["desktop"]),
            "mobile_avg": _avg(b["mobile"]),
        }
        all_d += b["desktop"]
        all_m += b["mobile"]

    overall = {
        "count": len(results),
        "desktop_avg": _avg(all_d),
        "mobile_avg": _avg(all_m),
    }
    return {"by_type": by_type, "overall": overall}


def _delta(cur, prev):
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    return round(cur - prev, 1)


def compute_deltas(current_agg: dict, previous_agg: Optional[dict]) -> dict:
    """Дельты текущего агрегата к предыдущему периоду по типам и в целом.

    previous_agg - результат aggregate() прошлого прогона (или None)."""
    prev_by_type = (previous_agg or {}).get("by_type", {})
    prev_overall = (previous_agg or {}).get("overall", {})
    out = {"by_type": {}, "overall": {}}
    for tc, cur in current_agg.get("by_type", {}).items():
        prev = prev_by_type.get(tc, {})
        out["by_type"][tc] = {
            "desktop": _delta(cur.get("desktop_avg"), prev.get("desktop_avg")),
            "mobile": _delta(cur.get("mobile_avg"), prev.get("mobile_avg")),
        }
    co = current_agg.get("overall", {})
    out["overall"] = {
        "desktop": _delta(co.get("desktop_avg"), prev_overall.get("desktop_avg")),
        "mobile": _delta(co.get("mobile_avg"), prev_overall.get("mobile_avg")),
    }
    return out


def top_recommendations(results: list[PageResult], limit: int = 10) -> list[dict]:
    """Самые частые рекомендации Lighthouse с конкретикой.

    Каждый пункт: {title, pages (на скольких страницах), savings (пример
    экономии), items ([{url, info}] - какие ресурсы), example_pages ([url])}.
    Отсортированы по числу затронутых страниц."""
    agg: dict[str, dict] = {}
    for r in results:
        # одна и та же рекомендация на desktop+mobile одной страницы = +1 странице
        page_titles = set()
        for ms in (r.desktop, r.mobile):
            for rec in ms.recs:
                title = rec.get("title") if isinstance(rec, dict) else _clean(rec)
                if not title:
                    continue
                slot = agg.setdefault(title, {
                    "title": title, "pages": set(),
                    "savings": "", "items": {}, })
                page_titles.add(title)
                if isinstance(rec, dict):
                    if not slot["savings"] and rec.get("savings"):
                        slot["savings"] = rec["savings"]
                    for it in (rec.get("items") or []):
                        u = it.get("url")
                        if u and u not in slot["items"]:
                            slot["items"][u] = it.get("info", "")
        for title in page_titles:
            agg[title]["pages"].add(r.url)

    ranked = sorted(agg.values(), key=lambda s: (-len(s["pages"]), s["title"]))
    out = []
    for s in ranked[:limit]:
        pages = sorted(s["pages"])
        items = [{"url": u, "info": i} for u, i in list(s["items"].items())[:4]]
        out.append({
            "title": s["title"],
            "pages": len(pages),
            "savings": s["savings"],
            "items": items,
            "example_pages": pages[:3],
        })
    return out
