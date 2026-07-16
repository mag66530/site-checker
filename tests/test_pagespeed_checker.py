"""Тесты движка pagespeed_checker: разбор ответа PSI, классификация URL,
агрегат и дельты. Без сети - на сохранённой фикстуре ответа PageSpeed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pagespeed_checker import (  # noqa: E402
    MetricSet, PageResult,
    parse_psi_response, classify_url, aggregate, compute_deltas,
    top_recommendations, score_rating, metric_rating,
)

# ── Фикстура: урезанный, но реалистичный ответ PageSpeed Insights v5 ─────────
PSI_FIXTURE = {
    "loadingExperience": {
        "overall_category": "SLOW",
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4200, "category": "SLOW"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 12, "category": "AVERAGE"},
        },
    },
    "lighthouseResult": {
        "categories": {"performance": {"score": 0.63}},
        "audits": {
            "first-contentful-paint": {"numericValue": 1700, "displayValue": "1,7 с", "score": 0.8},
            "largest-contentful-paint": {"numericValue": 3100, "displayValue": "3,1 с", "score": 0.5},
            "cumulative-layout-shift": {"numericValue": 0.11, "displayValue": "0,11", "score": 0.9},
            "total-blocking-time": {"numericValue": 260, "displayValue": "260 мс", "score": 0.7},
            "uses-optimized-images": {
                "score": 0,
                "title": "Показывайте изображения в форматах следующего поколения",
                "displayValue": "Потенциальная экономия: 320 КБ",
                "details": {"items": [
                    {"url": "https://stalmetural.ru/upload/big-banner.jpg",
                     "totalBytes": 410000, "wastedBytes": 320000},
                ]},
            },
            "render-blocking-resources": {
                "score": 0,
                "title": "Устраните ресурсы, блокирующие отображение",
                "displayValue": "Потенциальная экономия: 0,45 с",
                "details": {"items": [
                    {"url": "https://stalmetural.ru/css/main.css", "wastedMs": 450},
                ]},
            },
            "server-response-time": {"score": 1, "title": "Время ответа сервера небольшое"},
            "final-screenshot": {"score": None, "title": "Final Screenshot"},
        },
    },
}


def test_parse_score_and_metrics():
    ms = parse_psi_response(PSI_FIXTURE, elapsed=12.3)
    assert ms.score == 63.0
    assert ms.fcp_val == 1.7 and ms.fcp_disp == "1,7 с"
    assert ms.lcp_val == 3.1 and ms.lcp_disp == "3,1 с"
    assert ms.cls_val == 0.11
    assert ms.tbt_val == 260
    assert ms.elapsed == 12.3
    assert ms.error is None


def test_parse_recommendations_only_failing():
    ms = parse_psi_response(PSI_FIXTURE)
    titles = [r["title"] for r in ms.recs]
    # Берём только аудиты со score < 1 и заголовком.
    assert "Показывайте изображения в форматах следующего поколения" in titles
    assert "Устраните ресурсы, блокирующие отображение" in titles
    # score == 1 (успешный) и score == None (информационный) - не рекомендации.
    assert "Время ответа сервера небольшое" not in titles
    assert "Final Screenshot" not in titles
    assert len(ms.recs) == 2


def test_parse_recommendations_carry_concrete_data():
    ms = parse_psi_response(PSI_FIXTURE)
    by_title = {r["title"]: r for r in ms.recs}
    img = by_title["Показывайте изображения в форматах следующего поколения"]
    # сколько сэкономим (displayValue)
    assert "320 КБ" in img["savings"]
    # какой именно ресурс и на сколько (details.items)
    assert img["items"]
    assert img["items"][0]["url"] == "https://stalmetural.ru/upload/big-banner.jpg"
    assert "КБ" in img["items"][0]["info"]
    # render-blocking - конкретный css и задержка в мс/с
    css = by_title["Устраните ресурсы, блокирующие отображение"]
    assert css["items"][0]["url"] == "https://stalmetural.ru/css/main.css"
    assert css["items"][0]["info"]   # непустая инфа про задержку


def test_parse_crux_field_data():
    ms = parse_psi_response(PSI_FIXTURE)
    assert ms.crux.get("overall") == "SLOW"
    assert ms.crux.get("lcp", {}).get("category") == "SLOW"
    assert ms.crux.get("cls", {}).get("category") == "AVERAGE"


def test_parse_empty_response_is_safe():
    ms = parse_psi_response({})
    assert ms.score is None
    assert ms.recs == []
    assert ms.crux == {}


def test_score_rating_bands():
    assert score_rating(95) == "good"
    assert score_rating(63) == "ok"
    assert score_rating(40) == "poor"
    assert score_rating(None) == "na"


def test_metric_rating_thresholds():
    assert metric_rating("lcp", 2.4) == "good"
    assert metric_rating("lcp", 3.1) == "ok"
    assert metric_rating("lcp", 5.0) == "poor"
    assert metric_rating("cls", 0.05) == "good"
    assert metric_rating("tbt", 700) == "poor"
    assert metric_rating("fcp", None) == "na"


def test_classify_url_types():
    root = "stalmetural.ru"
    assert classify_url("https://stalmetural.ru/", root) == "main"
    assert classify_url("https://stalmetural.ru/catalog/", root) == "catalog"
    assert classify_url("https://stalmetural.ru/catalog/armatura/", root) == "category"
    assert classify_url("https://stalmetural.ru/catalog/armatura/a500c-12/", root) == "product"
    # query-параметр фильтра -> фильтр
    assert classify_url("https://stalmetural.ru/catalog/truba/?d=57", root) == "filter"
    # непонятный раздел -> прочее
    assert classify_url("https://stalmetural.ru/about/", root) == "other"


def _mk(url, tc, d_score, m_score):
    return PageResult(url=url, type_code=tc,
                      desktop=MetricSet(score=d_score),
                      mobile=MetricSet(score=m_score))


def test_aggregate_averages_by_type():
    results = [
        _mk("u1", "category", 60, 30),
        _mk("u2", "category", 70, 40),
        _mk("u3", "product", 50, 20),
    ]
    agg = aggregate(results)
    assert agg["by_type"]["category"]["count"] == 2
    assert agg["by_type"]["category"]["desktop_avg"] == 65.0
    assert agg["by_type"]["category"]["mobile_avg"] == 35.0
    assert agg["by_type"]["product"]["desktop_avg"] == 50.0
    assert agg["overall"]["count"] == 3
    assert agg["overall"]["desktop_avg"] == 60.0   # (60+70+50)/3


def test_aggregate_ignores_missing_scores():
    results = [
        _mk("u1", "main", 80, None),   # у mobile нет оценки
        _mk("u2", "main", None, 50),   # у desktop нет оценки
    ]
    agg = aggregate(results)
    assert agg["by_type"]["main"]["desktop_avg"] == 80.0
    assert agg["by_type"]["main"]["mobile_avg"] == 50.0


def test_compute_deltas_vs_previous():
    cur = aggregate([_mk("u1", "category", 68, 36)])
    prev = aggregate([_mk("u1", "category", 63, 38)])
    d = compute_deltas(cur, prev)
    assert d["by_type"]["category"]["desktop"] == 5.0    # 68 - 63
    assert d["by_type"]["category"]["mobile"] == -2.0    # 36 - 38
    assert d["overall"]["desktop"] == 5.0


def test_compute_deltas_no_previous_is_none():
    cur = aggregate([_mk("u1", "category", 68, 36)])
    d = compute_deltas(cur, None)
    assert d["by_type"]["category"]["desktop"] is None
    assert d["overall"]["mobile"] is None


def test_top_recommendations_counts_pages():
    r1 = PageResult("u1", "category",
                    desktop=MetricSet(recs=[
                        {"title": "A", "savings": "Экономия 10 КБ",
                         "items": [{"url": "a.js", "info": "экономия 10 КБ"}]},
                        {"title": "B"}]),
                    mobile=MetricSet(recs=[{"title": "A"}]))   # A на стр.1 - один раз
    r2 = PageResult("u2", "product",
                    desktop=MetricSet(recs=[{"title": "A"}]),
                    mobile=MetricSet(recs=[{"title": "C"}]))
    top = top_recommendations([r1, r2], limit=5)
    by_title = {t["title"]: t for t in top}
    assert by_title["A"]["pages"] == 2     # на двух страницах
    assert by_title["B"]["pages"] == 1
    assert by_title["C"]["pages"] == 1
    assert top[0]["title"] == "A"          # самая частая - первой
    # конкретика подтянулась в агрегат
    assert by_title["A"]["items"][0]["url"] == "a.js"
    assert "10 КБ" in by_title["A"]["savings"]
