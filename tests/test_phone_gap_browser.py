"""Пробел валидации телефона ищем ПОВЕДЕНИЕМ, а не чтением атрибутов.

Регресс ИМП: тул писал ✗ «форма принимает заявки с неполным номером телефона
(+7 (1)», а вручную такой номер не отправляется - поле краснеет. Причина: маску
искали по классу/data-атрибуту/плейсхолдеру, а её вешает скрипт - по атрибутам
её не видно. Теперь тул вписывает неполный номер и смотрит, вмешался ли сайт.

Тест пропускается, если браузер Playwright не установлен."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

t = pytest.importorskip("test_all")
sync_api = pytest.importorskip("playwright.sync_api")

HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>телефон</title>
</head><body>
<!-- f1: маску вешает СКРИПТ (как на ИМП) - в атрибутах о ней ни слова -->
<form id="f1"><input name="telephone" type="tel" placeholder="Ваш номер телефона"></form>
<!-- f2: голое поле - настоящий пробел валидации -->
<form id="f2"><input name="telephone" type="tel" placeholder="Ваш номер телефона"></form>
<!-- f3: ограничение объявлено атрибутами -->
<form id="f3"><input name="telephone" type="tel" minlength="11" required></form>
<script>
const el = document.querySelector('#f1 [name=telephone]');
el.addEventListener('input', () => {
  const d = (el.value.match(/\\d/g)||[]).join('').slice(0,11);
  el.value = d.length >= 11 ? '+7 (' + d.slice(1,4) + ') ' + d.slice(4) : d;
});
</script></body></html>"""


@pytest.fixture(scope="module")
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


def test_js_маска_не_даёт_ложного_пробела(страница):
    f = страница.locator("#f1")
    f.locator("[name=telephone]").fill("71111111111")
    assert t._статичные_пробелы_валидации(f) == []


def test_голое_поле_телефона_это_настоящий_пробел(страница):
    f = страница.locator("#f2")
    f.locator("[name=telephone]").fill("71111111111")
    assert t._статичные_пробелы_валидации(f) == ["phone"]


def test_ограничение_атрибутами_тоже_считается_защитой(страница):
    f = страница.locator("#f3")
    f.locator("[name=telephone]").fill("71111111111")
    assert t._статичные_пробелы_валидации(f) == []


def test_проба_возвращает_поле_в_исходное_состояние(страница):
    f = страница.locator("#f2")
    поле = f.locator("[name=telephone]")
    поле.fill("71111111111")
    до = поле.input_value()
    t._статичные_пробелы_валидации(f)
    # Проба идёт ПЕРЕД боевой отправкой - если она затрёт номер, заявка уйдёт
    # с испорченным телефоном (ровно так пробы уже ломали отправку раньше).
    assert поле.input_value() == до
