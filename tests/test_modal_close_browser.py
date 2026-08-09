"""Браузерный регресс: модалка, закрытая через opacity, должна считаться закрытой.

Тот же корень, что и у попапа «Спасибо»: Playwright считает элемент с
`opacity: 0` ВИДИМЫМ. А модалки сплошь закрываются именно так (снимают класс
`open`, остаётся opacity:0 + pointer-events:none). Из-за этого проба закрытия
писала ✗ «не закрылась ни крестиком, ни Esc, ни кликом вне модалки» на окнах,
которые на экране исчезают, - ложный дефект (ИМП: «Нужна консультация?
(товарная)», «Интересует вакансия?»).

Тест пропускается, если браузер Playwright не установлен."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

t = pytest.importorskip("test_all")
sync_api = pytest.importorskip("playwright.sync_api")

HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>модалка</title>
<style>
  .my-modal { opacity: 0; pointer-events: none; position: fixed; inset: 20% 30%; }
  .my-modal.open { opacity: 1; pointer-events: auto; }
</style></head><body>
<h1>Страница</h1>
<div class="my-modal open" id="m">
  <button class="modal-close" id="x">×</button>
  <form id="f"><input name="name"><button type="submit">Отправить</button></form>
</div>
<script>
document.getElementById('x').addEventListener('click',
  () => document.getElementById('m').classList.remove('open'));
</script></body></html>"""


@pytest.fixture()
def страница():
    with sync_api.sync_playwright() as p:
        браузер = None
        for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
            try:
                браузер = p.chromium.launch(**kw)
                break
            except Exception:  # noqa: BLE001
                continue
        if браузер is None:
            pytest.skip("браузер Playwright не установлен")
        page = браузер.new_page()
        page.route("**/*", lambda r: r.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=HTML))
        page.goto("https://imp.test/")
        yield page
        браузер.close()


def test_открытая_модалка_видна(страница):
    m = страница.locator("#m")
    assert t._модалка_видна(m)
    assert t._модалка_открылась(m)          # внутри есть поля


def test_закрытая_через_opacity_модалка_считается_закрытой(страница):
    page = страница
    m = page.locator("#m")
    ст, способ = t._проба_закрытия_модалки(page, m)
    assert ст == "Да", способ
    # ключевое: Playwright всё ещё считает её видимой, а тул - уже нет
    assert m.is_visible() is True
    assert t._модалка_видна(m) is False


def test_крестик_закрытой_модалки_не_считается_видимым(страница):
    page = страница
    page.evaluate("() => document.getElementById('m').classList.remove('open')")
    # Крестик внутри прозрачной модалки: клик по нему ничего не закроет, и
    # вердикт «окно закрывается крестиком» был бы выдуман.
    assert t._видимый_крестик_закрытия(page) is None
