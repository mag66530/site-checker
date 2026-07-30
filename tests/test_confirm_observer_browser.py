"""Браузерный регресс: попап «Спасибо», спрятанный через opacity, должен ловиться.

Случай ИМП (форма отзыва): статус ✓ УСПЕШНО, а «Уведомление пользователю» = ✗,
хотя окно «СПАСИБО, ВАШ ЗАКАЗ ПРИНЯТ» на экране есть.

Опыт в браузере показал корень: текст элемента, скрытого через `opacity: 0`
(самый частый способ анимировать модалку), ПОПАДАЕТ в document.body.innerText.
Значит базовый снимок наблюдателя уже содержит «спасибо», и когда попап реально
открывается, НОВОГО текста не появляется - подтверждение не ловилось никогда.
`display:none` и `visibility:hidden` в innerText не попадают.

Теперь главный сигнал наблюдателя - элемент с благодарностью, который СТАЛ
видимым (в layout, без visibility:hidden/display:none/opacity≈0), а не разница
текста. Плюс подтверждение снимается ДО повторных отправок.

Тест пропускается, если браузер Playwright не установлен."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

t = pytest.importorskip("test_all")
sync_api = pytest.importorskip("playwright.sync_api")

# Модалка спрятана opacity:0 (её текст ВИДЕН в innerText ещё до отправки),
# открывается добавлением класса open. Ровно эта схема и ломала детект.
HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>попап</title>
<style>
  .my-modal { opacity: 0; pointer-events: none; position: fixed; inset: 20% 30%; }
  .my-modal.open { opacity: 1; pointer-events: auto; }
</style></head><body>
<h1>Товар</h1>
<form id="f"><input name="name" required><button type="submit">Отправить</button></form>
<div class="my-modal" id="modal-thanks">
  <h2>СПАСИБО, ВАШ ЗАКАЗ ПРИНЯТ</h2>
  <p>Наш менеджер свяжется с вами в ближайшее время</p>
</div>
<script>
document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault();
  setTimeout(() => document.getElementById('modal-thanks').classList.add('open'), 300);
});
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


def test_текст_скрытой_opacity_модалки_действительно_в_innertext(страница):
    # Фиксируем причину бага: до отправки «спасибо» уже в тексте страницы,
    # поэтому детект «по новому тексту» тут бесполезен.
    assert "СПАСИБО" in страница.evaluate("() => document.body.innerText")


def test_наблюдатель_ловит_попап_несмотря_на_текст_в_базе(страница):
    page = страница
    assert t._наблюдатель_подтверждения_старт(page)
    # до отправки подтверждения нет - статичный (невидимый) блок не считается
    assert t._наблюдатель_подтверждения_итог(page, стоп=False) == ""
    page.locator("[name=name]").fill("Тест")
    page.locator("#f button").click()
    найдено = t.ждать_подтверждения(page, таймаут_мс=3000)
    assert "спасибо" in найдено.lower(), найдено


def test_вердикт_уведомления_да_попап(страница):
    page = страница
    t._наблюдатель_подтверждения_старт(page)
    текст_до = page.locator("body").inner_text()
    page.locator("[name=name]").fill("Тест")
    page.locator("#f button").click()
    набл = t.ждать_подтверждения(page, таймаут_мс=3000)
    assert t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=800,
        текст_тела_до=текст_до, наблюдение=набл) == "Да (попап)"


def test_без_отправки_подтверждения_нет(страница):
    page = страница
    t._наблюдатель_подтверждения_старт(page)
    assert t.ждать_подтверждения(page, таймаут_мс=600) == ""
