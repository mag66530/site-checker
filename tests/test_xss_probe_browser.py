"""Браузерный регресс: XSS-проба должна РЕАЛЬНО отправлять вторую заявку.

Случай ИМП: на всех формах с JS-маской телефона в отчёте стояло ⚠ «XSS не
проверен: поле telephone сайт считает заполненным неверно (Please fill out this
field)». Две причины, обе на нашей стороне:
  1) переоткрытая форма заполнялась сырым присваиванием value - маска телефона
     такое значение не принимает, поле остаётся пустым;
  2) payload лез во ВСЕ текстовые поля, включая телефон, и маска его выкидывала.
Теперь форма дозаполняется через .fill() (как человек), телефон и почта
остаются валидными, а payload идёт в имя/комментарий.

Тест пропускается, если браузер Playwright не установлен."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

t = pytest.importorskip("test_all")
sync_api = pytest.importorskip("playwright.sync_api")

HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>маска</title>
</head><body>
<form id="f">
  <input name="name" required>
  <input name="telephone" type="tel" required>
  <textarea name="comment" required></textarea>
  <button type="submit">Отправить</button>
</form>
<script>
// JS-маска: чужие символы из телефона выкидывает (как на ИМП)
const tel = document.querySelector('[name=telephone]');
tel.addEventListener('input', () => {
  tel.value = (tel.value.match(/[\\d+() -]/g)||[]).join('');
});
document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault(); window.__sent = (window.__sent||0)+1;
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


def _заполнить(page):
    f = page.locator("#f")
    f.locator("[name=name]").fill("Тест")
    f.locator("[name=telephone]").fill("71111111111")
    f.locator("[name=comment]").fill("Комментарий")
    return f


def test_дозаполнение_переживает_маску_телефона(страница):
    page = страница
    эталон = t._снять_поля_формы(_заполнить(page)).get("поля") or {}
    assert set(эталон) == {"name", "telephone", "comment"}

    page.goto("https://imp.test/")          # «переоткрыли» форму - она пустая
    f = page.locator("#f")
    assert t.заполнить_по_эталону(f, эталон) == 3
    assert f.locator("[name=telephone]").input_value() == "71111111111"
    # форма валидна - вторая заявка не упрётся в «заполните это поле»
    assert t._причина_невалидности(f) == ""


def test_повторный_вызов_не_перетирает_заполненное(страница):
    page = страница
    f = _заполнить(page)
    эталон = t._снять_поля_формы(f).get("поля") or {}
    assert t.заполнить_по_эталону(f, эталон) == 0


def test_страховка_называет_пробу_и_чинит_поля(страница, capsys):
    page = страница
    f = _заполнить(page)
    эталон = t._снять_поля_формы(f).get("поля") or {}
    page.evaluate("() => { f.name.value=''; f.comment.value=''; }")
    починено = t.страховка_формы(f, эталон, "Без согласия не отправить")
    assert set(починено) == {"name", "comment"}
    assert "Без согласия не отправить" in capsys.readouterr().out


def test_причина_невалидности_молчит_на_исправной_форме(страница):
    assert t._причина_невалидности(_заполнить(страница)) == ""
