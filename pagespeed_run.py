"""
pagespeed_run.py - фоновый прогон проверки скорости страниц (PageSpeed Insights).

Запускается страницей приложения отдельным процессом (как variables_run.py) и
может использоваться по расписанию для регулярного накопления истории.

Что делает:
  1) собирает список URL по выбранному охвату:
       --scope sample   - выборка по типам из каталогов проекта (main/catalog/
                          categories/filters [+ products из sitemap]);
       --scope list     - свой список из файла (--urls-file), типы по адресу;
  2) гоняет каждый URL через PageSpeed Insights (desktop+mobile) c троттлингом;
  3) считает средние по типам и Δ к прошлому периоду (история берётся ДО записи
     текущего прогона);
  4) дописывает прогон в локальную историю pagespeed_data/{pid}.csv;
  5) строит Excel-отчёт и пишет last_run.json для страницы.

Ключ PageSpeed берётся из переменной окружения PAGESPEED_API_KEY (её проставляет
страница из секретов). В аргументах ключ не передаём - чтобы не светился.

Прогресс печатается в stdout строками «[i/N] …» - страница тайлит лог.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pagespeed_checker as PC
import pagespeed_history as PH
import pagespeed_report as PR
import sources as S

CACHE_DIR = ROOT / "cache" / "pagespeed"


def _out_dir(pid: str) -> Path:
    d = CACHE_DIR / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log(msg: str) -> None:
    print(msg, flush=True)


# ── Сбор URL ─────────────────────────────────────────────────────────────────
def _sample_urls(pid: str, per_type: int, want_products: bool) -> list[tuple[str, str]]:
    """Выборка по типам из каталогов проекта (только главный домен - Москва)."""
    project = S.load_project_config(pid)
    sources = S.load_sources(project)

    # Товары - из sitemap (кэш или разовая загрузка). Не фатально при ошибке.
    if want_products:
        try:
            info = None
            try:
                info = __import__("sitemap").get_cached_products_info(pid)
            except Exception:
                info = None
            paths = (info or {}).get("pathnames") if info else None
            if not paths:
                import asyncio
                import sitemap
                data = asyncio.run(sitemap.load_product_pathnames(
                    pid, project.get("sitemap_url", ""), force=False))
                paths = (data or {}).get("pathnames") or []
            sources.products = list(paths)
            _log(f"Sitemap: товаров доступно {len(sources.products)}")
        except Exception as e:  # noqa: BLE001
            _log(f"⚠ Товары из sitemap недоступны ({e}) - пропускаю этот тип.")
            sources.products = []

    plan = S.build_plan(
        sources,
        random_subdomains_count=0,                       # только главный домен
        mandatory_city=project.get("mandatory_city", "Москва"),
        mandatory_hosts=[],
        categories_per_subdomain=per_type,
        filters_per_subdomain=per_type,
        products_per_subdomain=per_type,
        check_products=want_products and bool(sources.products),
        seed=None,
    )
    return [(t.url, t.type_code) for t in plan.tasks]


def _list_urls(pid: str, urls_file: str) -> list[tuple[str, str]]:
    """Свой список URL из файла: типы определяем по адресу в контексте проекта."""
    raw = Path(urls_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    try:
        project = S.load_project_config(pid)
        sources = S.load_sources(project)
    except Exception:
        sources = None
    tasks = S.build_custom_tasks_typed(raw, sources)
    return [(t.url, t.type_code) for t in tasks]


# ── Главная логика ───────────────────────────────────────────────────────────
def run(args) -> int:
    pid = args.project
    try:
        project_name = S.load_project_config(pid).get("name", pid)
    except Exception:
        project_name = pid

    _log(f"Проект: {project_name} · охват: {args.scope}")

    if args.scope == "list":
        if not args.urls_file:
            _log("✗ Для охвата 'list' нужен --urls-file")
            return 2
        typed = _list_urls(pid, args.urls_file)
    else:
        typed = _sample_urls(pid, args.per_type, want_products=not args.no_products)

    if not typed:
        _log("✗ Нет URL для проверки.")
        return 2
    _log(f"К проверке: {len(typed)} страниц × (desktop+mobile)")

    api_key = os.environ.get("PAGESPEED_API_KEY", "").strip()
    if not api_key:
        _log("⚠ PAGESPEED_API_KEY не задан - PageSpeed вернёт очень жёсткий лимит "
             "(часть/все страницы упадут с ошибкой лимита).")
    provider = PC.PageSpeedInsightsProvider(api_key=api_key, locale=args.locale)

    results = PC.run_batch(
        typed, provider,
        max_workers=args.max_workers, max_qps=args.max_qps,
        max_per_domain=args.per_domain, log=_log,
    )

    # Агрегат текущего + предыдущий период (ДО записи текущего в историю).
    run_ts = PH.now_ts()
    cur_agg = PC.aggregate(results)
    prev_ts, prev_agg = PH.previous_aggregate(pid, run_ts, mode=args.compare)
    deltas = PC.compute_deltas(cur_agg, prev_agg)
    top_recs = PC.top_recommendations(results, limit=12)

    PH.append_run(pid, run_ts, results)
    _log(f"История: прогон {run_ts} записан ({PH.history_path(pid)})")
    if prev_ts:
        _log(f"Сравнение с прошлым снятием: {prev_ts}")
    else:
        _log("Прошлых снятий нет - первый прогон, сравнивать не с чем.")

    out = _out_dir(pid)
    date_disp = datetime.now().strftime("%d.%m.%Y")
    xlsx_name = f"{pid.upper()}-скорость-{date_disp}.xlsx"
    xlsx_path = out / xlsx_name
    PR.save_report(
        xlsx_path,
        project_name=project_name, run_ts=run_ts, prev_ts=prev_ts,
        results=results, agg=cur_agg, deltas=deltas, top_recs=top_recs,
        provider_name=provider.name,
    )
    _log(f"Отчёт: {xlsx_path}")

    _write_last_run(out, pid, project_name, provider.name, run_ts, prev_ts,
                    results, cur_agg, deltas, top_recs, xlsx_name)

    _log("✅ ГОТОВО")
    return 0


def _write_last_run(out, pid, project_name, provider_name, run_ts, prev_ts,
                    results, agg, deltas, top_recs, xlsx_name):
    """Компактный JSON для отрисовки на странице (таблицы + Δ)."""
    by_type = []
    d_by = deltas.get("by_type", {})
    bt = agg.get("by_type", {})
    ordered = [tc for tc in PC.TYPE_ORDER if tc in bt] + \
              [tc for tc in bt if tc not in PC.TYPE_ORDER]
    for tc in ordered:
        b = bt[tc]
        by_type.append({
            "type": tc, "label": PC.TYPE_LABELS.get(tc, tc),
            "count": b.get("count"),
            "desktop_avg": b.get("desktop_avg"), "mobile_avg": b.get("mobile_avg"),
            "d_desktop": d_by.get(tc, {}).get("desktop"),
            "d_mobile": d_by.get(tc, {}).get("mobile"),
        })

    rows = []
    for r in results:
        d, m = r.desktop, r.mobile
        rows.append({
            "url": r.url, "type": r.type_code, "label": PC.TYPE_LABELS.get(r.type_code, r.type_code),
            "d_score": d.score, "d_fcp": d.fcp_disp, "d_lcp": d.lcp_disp,
            "d_cls": d.cls_disp, "d_tbt": d.tbt_disp,
            "m_score": m.score, "m_fcp": m.fcp_disp, "m_lcp": m.lcp_disp,
            "m_cls": m.cls_disp, "m_tbt": m.tbt_disp,
            "error": " ".join(x for x in (d.error, m.error) if x).strip(),
        })

    payload = {
        "project": pid, "project_name": project_name, "provider": provider_name,
        "run_ts": run_ts, "prev_ts": prev_ts,
        "overall": agg.get("overall", {}), "deltas_overall": deltas.get("overall", {}),
        "by_type": by_type, "rows": rows,
        "top_recs": top_recs,   # список словарей: title/pages/savings/items/example_pages
        "xlsx_name": xlsx_name,
    }
    (out / "last_run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Проверка скорости страниц (PageSpeed Insights)")
    ap.add_argument("--project", required=True, help="id проекта (smu/imp/mpe/…)")
    ap.add_argument("--scope", choices=["sample", "list"], default="sample")
    ap.add_argument("--per-type", type=int, default=5, help="сколько страниц каждого типа в выборке")
    ap.add_argument("--no-products", action="store_true", help="не тянуть товары из sitemap")
    ap.add_argument("--urls-file", default="", help="файл со списком URL (для --scope list)")
    ap.add_argument("--compare", choices=["prev", "week", "month"], default="prev")
    ap.add_argument("--locale", default="ru")
    ap.add_argument("--max-workers", type=int, default=PC.DEFAULT_MAX_WORKERS)
    ap.add_argument("--max-qps", type=int, default=PC.DEFAULT_MAX_QPS)
    ap.add_argument("--per-domain", type=int, default=PC.DEFAULT_MAX_PER_DOMAIN)
    args = ap.parse_args()

    try:
        sys.exit(run(args))
    except Exception as e:  # noqa: BLE001
        _log(f"✗ ОШИБКА: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
