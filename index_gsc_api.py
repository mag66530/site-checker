"""
index_gsc_api.py - источник «Google (API)» для проверки 404 в индексе.

В отличие от index_gsc_run.py (браузер + сохранённая сессия, только локально)
этот источник ходит в Google Search Console ЧЕРЕЗ API на сервисном аккаунте -
поэтому работает на Streamlit Cloud и в расписании, без браузера и без сессии.

Как:
  1) сервисным аккаунтом (из секретов) авторизуемся в Search Console API
     (webmasters v3, scope webmasters.readonly);
  2) через Search Analytics берём список страниц, которые Google показывал в
     поиске за последние N дней = проиндексированные «живые» страницы;
  3) прозваниваем порцию (ротация окна по дате, как у sitemap-источника) нашим
     же чекером index_pages_checker._check_all → 404/410/5xx/soft;
  4) отдаём результат в том же формате, что sitemap/Яндекс - merge_index_404
     сольёт всё в один лист «404 в индексе».

Ограничения (честно):
  • Search Analytics видит только страницы с показами за окно - индексные, но
    ни разу не показанные, не попадут (для регулярного мониторинга «живая
    страница вдруг 404» - это как раз то, что нужно);
  • это НЕ дословный отчёт «Не найдено (404)» из интерфейса GSC (тот только в
    браузере, см. index_gsc_run.py) - здесь код ответа проверяем сами.

Сервисный аккаунт должен быть добавлен пользователем в GSC-ресурс проекта
(Settings → Users). Ресурс берём из projects/<pid>.json (gsc_resource),
иначе sc-domain:<root_domain>.

Зависимости: google-auth (уже есть) + requests. googleapiclient не нужен -
ходим в REST через AuthorizedSession.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from index_pages_checker import _check_all           # прозвон + вердикт 404/…
from index_sitemap_checker import _host_of, _window  # нормализация хоста + ротация
from sources import load_project_config

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
SC_BASE = "https://www.googleapis.com/webmasters/v3"

DEFAULT_DAYS = 90
DEFAULT_MAX_URLS = 3000
_PAGE_ROWS = 25000       # строк за один запрос Search Analytics
_HARD_CAP = 200000       # предохранитель на общий объём


# ── Разбор ответа Search Analytics (без сети - тестируемо) ────────────────────
def parse_search_analytics_rows(resp: dict) -> list[str]:
    """Из ответа searchAnalytics.query достаём URL страниц (dimension=page)."""
    urls = []
    for row in (resp or {}).get("rows", []) or []:
        keys = row.get("keys") or []
        if keys and isinstance(keys[0], str) and keys[0].startswith("http"):
            urls.append(keys[0])
    return urls


def resolve_site_url(project_id: str, cfg: dict | None = None) -> str:
    """GSC-ресурс проекта: gsc_resource из конфига, иначе sc-domain:<root_domain>."""
    cfg = cfg or load_project_config(project_id) or {}
    res = (cfg.get("gsc_resource") or "").strip()
    if res:
        return res
    root = (cfg.get("root_domain") or "").strip()
    if root:
        return f"sc-domain:{root}"
    # последний фолбэк - хост из main_url
    host = urlsplit(cfg.get("main_url", "")).netloc
    return f"sc-domain:{host}" if host else ""


# ── Авторизация сервисным аккаунтом ──────────────────────────────────────────
def _authed_session(sa_info: dict):
    """AuthorizedSession на сервисном аккаунте (dict = разобранный JSON ключа).
    Ленивая загрузка google-auth - модуль импортируется и без него (для тестов
    парсинга/формата)."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=[SCOPE])
    return AuthorizedSession(creds)


# ── Список проиндексированных страниц из Search Analytics ─────────────────────
def list_indexed_urls(project_id: str, sa_info: dict, *, days: int = DEFAULT_DAYS,
                      cfg: dict | None = None, log=None) -> list[str]:
    """Страницы, которые Google показывал в поиске за последние `days` дней."""
    def _log(m):
        if log:
            try:
                log("info", m)
            except TypeError:
                log(m)

    cfg = cfg or load_project_config(project_id) or {}
    site_url = resolve_site_url(project_id, cfg)
    if not site_url:
        raise ValueError("не удалось определить GSC-ресурс проекта")

    session = _authed_session(sa_info)
    endpoint = f"{SC_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query"

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    urls: list[str] = []
    seen: set[str] = set()
    start_row = 0
    _log(f"GSC API: ресурс {site_url}, период {start}…{end}")
    while start_row < _HARD_CAP:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["page"],
            "rowLimit": _PAGE_ROWS,
            "startRow": start_row,
        }
        r = session.post(endpoint, json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(_api_error(r, site_url))
        rows = parse_search_analytics_rows(r.json())
        if not rows:
            break
        for u in rows:
            if u not in seen:
                seen.add(u)
                urls.append(u)
        if len(rows) < _PAGE_ROWS:
            break
        start_row += _PAGE_ROWS
        _log(f"GSC API: получено {len(urls)} страниц…")
    _log(f"GSC API: всего проиндексированных страниц с показами - {len(urls)}")
    return urls


def _api_error(r, site_url: str) -> str:
    try:
        msg = ((r.json().get("error") or {}).get("message") or "")[:200]
    except Exception:
        msg = (r.text or "")[:200]
    if r.status_code == 403:
        return (f"нет доступа к ресурсу {site_url} (HTTP 403): {msg}. "
                "Добавьте сервисный аккаунт пользователем в Search Console.")
    if r.status_code == 401:
        return f"сервисный аккаунт не авторизован (HTTP 401): {msg}"
    return f"Search Console API HTTP {r.status_code}: {msg}"


# ── Проверка 404 (тот же формат, что sitemap-источник) ───────────────────────
def check_gsc_api_404(project_id: str, sa_info: dict, *, proxy_url=None,
                      max_urls: int = DEFAULT_MAX_URLS, day_ordinal: int | None = None,
                      days: int = DEFAULT_DAYS, log=None) -> dict:
    """Список страниц из GSC (API) → прозвон порции на 404 → формат index_404.
    Возвращает dict с source='gsc' (записи source='Google (API)')."""
    def _log(m):
        if log:
            try:
                log("info", m)
            except TypeError:
                log(m)

    out = {"available": False, "source": "gsc", "hosts": [],
           "total_checked": 0, "total_dead": 0, "total_soft": 0, "error": None}

    try:
        all_urls = list_indexed_urls(project_id, sa_info, days=days, log=log)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"GSC API не отдал страницы: {e}"
        _log(f"⚠ {out['error']}")
        return out

    urls = sorted(u for u in set(all_urls) if u.startswith("http"))
    if not urls:
        out["available"] = True   # запрос прошёл, просто страниц нет
        return out

    if day_ordinal is None:
        day_ordinal = datetime.date.today().toordinal()
    window, total = _window(urls, max_urls, day_ordinal)
    _log(f"GSC API: всего {total}, прозваниваю в этот прогон {len(window)} "
         f"(ротация по дате)")

    async def _work():
        pairs = [(_host_of(u), u) for u in window]
        # Прогресс: прозвон медленный (боевой сайт отвечает секунды), показываем
        # ход и время, чтобы не выглядело зависанием.
        t0 = time.monotonic()

        def _prog(done, tot):
            if done % 50 == 0 or done == tot:
                _log(f"GSC API: прозвон {done}/{tot} "
                     f"({int(time.monotonic() - t0)}с)")
        return await _check_all(pairs, proxy_url, progress=_prog)

    try:
        checked = asyncio.run(_work())
    except Exception as e:  # noqa: BLE001
        out["error"] = f"прозвон GSC-страниц не удался: {e}"
        _log(f"⚠ {out['error']}")
        return out

    by_host: dict[str, dict] = {}
    for url, r in checked.items():
        host = _host_of(url)
        hb = by_host.setdefault(host, {
            "host": host, "dead": [], "soft": [], "errors": [],
            "in_index_total": 0, "checked": 0, "ok": 0, "redirects": 0})
        hb["checked"] += 1
        verdict = r.get("verdict")
        entry = {"url": url, "status": r.get("status"), "source": "Google (API)",
                 "reason": r.get("reason", "")}
        if verdict == "dead":
            hb["dead"].append(entry)
        elif verdict == "soft":
            hb["soft"].append(entry)
        elif verdict in ("server_error", "client_error", "no_response"):
            hb["errors"].append(entry)
        elif verdict == "redirect":
            hb["redirects"] += 1
        else:
            hb["ok"] += 1

    out["available"] = True
    for host, hb in sorted(by_host.items()):
        out["hosts"].append(hb)
        out["total_checked"] += hb["checked"]
        out["total_dead"] += len(hb["dead"])
        out["total_soft"] += len(hb["soft"])
    _log(f"GSC API: проверено {out['total_checked']}, битых 404/410 "
         f"{out['total_dead']}, soft {out['total_soft']}")
    return out


# ── Секрет: сервисный аккаунт из окружения (для CLI/фонового процесса) ────────
def sa_info_from_env() -> dict | None:
    """JSON сервисного аккаунта из GSC_SA_JSON (строка-JSON) - для CLI и
    фонового процесса. Страница/ранер кладут его туда из секретов."""
    raw = os.environ.get("GSC_SA_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _main():
    ap = argparse.ArgumentParser(description="404 в индексе из Google Search Console (API)")
    ap.add_argument("project")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--max-urls", type=int, default=DEFAULT_MAX_URLS)
    ap.add_argument("--proxy", default=os.environ.get("proxy_url"))
    ap.add_argument("--sa-file", default="", help="путь к JSON сервисного аккаунта "
                    "(иначе берётся из GSC_SA_JSON)")
    a = ap.parse_args()

    if a.sa_file:
        sa_info = json.loads(Path(a.sa_file).read_text(encoding="utf-8"))
    else:
        sa_info = sa_info_from_env()
    if not sa_info:
        print("Нет сервисного аккаунта: задайте --sa-file или GSC_SA_JSON", file=sys.stderr)
        sys.exit(2)

    res = check_gsc_api_404(a.project, sa_info, proxy_url=a.proxy,
                            max_urls=a.max_urls, days=a.days,
                            log=lambda lvl, m: print(m))
    if res.get("error"):
        print(f"Ошибка: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"\nПроверено {res['total_checked']}; битых 404/410: {res['total_dead']}, "
          f"soft: {res['total_soft']}")
    for h in res["hosts"]:
        for r in (h["dead"] + h["errors"])[:20]:
            print(f"   {r.get('status')}  {r['url']}")


if __name__ == "__main__":
    _main()
