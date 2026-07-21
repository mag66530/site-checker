"""Тесты сбора URL для проверки скорости (pagespeed_run).

Главное - что ТОВАРЫ попадают в выборку. Регрессия: загрузка товаров вызывалась
с неверной сигнатурой (load_product_pathnames(pid, sitemap_url, force=False)) и
всегда падала с TypeError - тип «Товар» молча пропадал из каждого прогона.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pagespeed_run as R  # noqa: E402


class _Src:
    """Минимальный заменитель Sources для _load_products."""
    categories = ["/catalog/cat-a/"]
    filters = ["/catalog/cat-a/filter/x-is-1/"]
    products: list = []


def test_load_products_prefers_listings_db(monkeypatch):
    """Есть локальная база листингов - берём её, в sitemap не ходим."""
    import product_links
    monkeypatch.setattr(product_links, "load_product_links",
                        lambda pid: {"pathnames": ["/catalog/c/p1/", "/catalog/c/p2/"]})

    def _boom(*a, **k):
        raise AssertionError("sitemap не должен вызываться, если есть база листингов")
    import sitemap
    monkeypatch.setattr(sitemap, "load_product_pathnames", _boom)

    got = R._load_products("smu", {"id": "smu"}, _Src())
    assert got == ["/catalog/c/p1/", "/catalog/c/p2/"]


def test_load_products_falls_back_to_sitemap(monkeypatch):
    """Базы листингов нет - идём в sitemap с ПРАВИЛЬНОЙ сигнатурой (project-dict)."""
    import product_links
    import sitemap
    monkeypatch.setattr(product_links, "load_product_links", lambda pid: None)

    async def fake_load(project, cats, filters, **kw):
        # регрессия: раньше сюда прилетала строка pid и падало TypeError
        assert isinstance(project, dict)
        assert cats == _Src.categories and filters == _Src.filters
        return {"pathnames": ["/catalog/c/p9/"]}

    monkeypatch.setattr(sitemap, "load_product_pathnames", fake_load)
    got = R._load_products("smu", {"id": "smu", "sitemap_url": "x"}, _Src())
    assert got == ["/catalog/c/p9/"]


def test_load_products_all_fail_returns_empty(monkeypatch):
    """Обе неудачи - не падаем, а возвращаем пустой список (прогон без товаров)."""
    import product_links
    import sitemap
    monkeypatch.setattr(product_links, "load_product_links", lambda pid: None)

    async def boom(*a, **k):
        raise RuntimeError("нет сети")

    monkeypatch.setattr(sitemap, "load_product_pathnames", boom)
    assert R._load_products("smu", {"id": "smu"}, _Src()) == []


def test_sample_urls_includes_products_from_repo_db():
    """Сквозной тест на реальной базе листингов проекта: в выборке есть товары."""
    typed = R._sample_urls("smu", 3, want_products=True)
    types = {tc for _, tc in typed}
    assert "product" in types, f"товары не попали в выборку: {sorted(types)}"
    # товарные URL - абсолютные, ведут в /catalog/<категория>/<товар>/
    prods = [u for u, tc in typed if tc == "product"]
    assert all(u.startswith("https://") and "/catalog/" in u for u in prods)


def test_sample_urls_can_skip_products():
    """Без товаров тип «Товар» отсутствует, остальные типы на месте."""
    typed = R._sample_urls("smu", 3, want_products=False)
    types = {tc for _, tc in typed}
    assert "product" not in types
    assert "main" in types and "category" in types
