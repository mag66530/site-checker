"""Тесты источника «Google (API)» для 404 в индексе: разбор ответа Search
Analytics, определение GSC-ресурса, группировка в стандартный формат index_404.
Без сети — сетевые вызовы (list_indexed_urls, _check_all) подменяются."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_gsc_api as G  # noqa: E402


def test_parse_search_analytics_rows():
    resp = {"rows": [
        {"keys": ["https://stalmetural.ru/catalog/armatura/"], "clicks": 5},
        {"keys": ["https://stalmetural.ru/catalog/truba/"], "impressions": 10},
        {"keys": ["not-a-url"]},          # без http — отбрасываем
        {"keys": []},                      # пустые ключи — отбрасываем
        {"clicks": 1},                     # без keys — отбрасываем
    ]}
    urls = G.parse_search_analytics_rows(resp)
    assert urls == ["https://stalmetural.ru/catalog/armatura/",
                    "https://stalmetural.ru/catalog/truba/"]


def test_parse_empty_response():
    assert G.parse_search_analytics_rows({}) == []
    assert G.parse_search_analytics_rows({"rows": []}) == []


def test_resolve_site_url_from_project_config():
    # smu.json содержит gsc_resource = sc-domain:stalmetural.ru
    assert G.resolve_site_url("smu") == "sc-domain:stalmetural.ru"


def test_resolve_site_url_fallback_to_root_domain():
    cfg = {"root_domain": "example.ru"}          # gsc_resource не задан
    assert G.resolve_site_url("x", cfg) == "sc-domain:example.ru"


def test_resolve_site_url_fallback_to_main_url_host():
    cfg = {"main_url": "https://sub.example.com/"}
    assert G.resolve_site_url("x", cfg) == "sc-domain:sub.example.com"


def _fake_check_all_factory(verdicts):
    async def _fake(pairs, proxy_url, progress=None):
        return {u: verdicts.get(u, {"verdict": "ok", "status": 200, "reason": ""})
                for _, u in pairs}
    return _fake


def test_check_gsc_api_404_grouping(monkeypatch):
    urls = [
        "https://stalmetural.ru/dead1/",
        "https://stalmetural.ru/ok1/",
        "https://stalmetural.ru/soft1/",
        "https://msk.stalmetural.ru/err1/",
    ]
    verdicts = {
        "https://stalmetural.ru/dead1/": {"verdict": "dead", "status": 404, "reason": "404"},
        "https://stalmetural.ru/soft1/": {"verdict": "soft", "status": 200, "reason": "soft-404"},
        "https://msk.stalmetural.ru/err1/": {"verdict": "server_error", "status": 500, "reason": "5xx"},
    }
    # подменяем сеть: список страниц и прозвон
    monkeypatch.setattr(G, "list_indexed_urls", lambda pid, sa, **k: list(urls))
    monkeypatch.setattr(G, "_check_all", _fake_check_all_factory(verdicts))

    res = G.check_gsc_api_404("smu", {"dummy": "sa"}, day_ordinal=0)

    assert res["available"] is True
    assert res["source"] == "gsc"
    assert res["error"] is None
    assert res["total_checked"] == 4
    assert res["total_dead"] == 1
    assert res["total_soft"] == 1

    hosts = {h["host"]: h for h in res["hosts"]}
    assert "stalmetural.ru" in hosts and "msk.stalmetural.ru" in hosts
    root = hosts["stalmetural.ru"]
    assert len(root["dead"]) == 1 and root["dead"][0]["url"] == "https://stalmetural.ru/dead1/"
    assert root["dead"][0]["source"] == "Google (API)"     # метка источника в записи
    assert len(root["soft"]) == 1
    assert root["ok"] == 1
    assert len(hosts["msk.stalmetural.ru"]["errors"]) == 1


def test_check_gsc_api_404_handles_list_error(monkeypatch):
    def _boom(pid, sa, **k):
        raise RuntimeError("HTTP 403: нет доступа")
    monkeypatch.setattr(G, "list_indexed_urls", _boom)
    res = G.check_gsc_api_404("smu", {"dummy": "sa"})
    assert res["available"] is False
    assert "403" in res["error"]
    assert res["hosts"] == []


def test_check_gsc_api_404_empty_index(monkeypatch):
    monkeypatch.setattr(G, "list_indexed_urls", lambda pid, sa, **k: [])
    res = G.check_gsc_api_404("smu", {"dummy": "sa"})
    assert res["available"] is True     # запрос прошёл, просто страниц нет
    assert res["total_checked"] == 0
    assert res["hosts"] == []
