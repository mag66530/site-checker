"""Браузерный регресс: пробы не должны срывать отправку формы с файлом.

Случай ИМП (29.07.2026): «Интересует вакансия?» и «Нужна консультация?
(товарная)» вручную отправляются и показывают попап, а тул писал по ним
«ОШИБКА» / «не удалось поймать сетевой POST».

Причина: перед штатной отправкой тул прикрепляет валидный PDF (форма с
обязательным файлом иначе не уйдёт), а проба показа ошибок валидации чистила
ВСЕ контролы, включая input[type=file]:
  • прикреплённый файл отваливался (JS вернуть его не может);
  • восстановление падало на этом поле с InvalidStateError, и все поля ПОСЛЕ
    него оставались пустыми.
В итоге боевая отправка уходила полупустой, сайт её не принимал - POST не
уходил вовсе.

Тест пропускается, если браузер Playwright не установлен."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

t = pytest.importorskip("test_all")
sync_api = pytest.importorskip("playwright.sync_api")

# Порядок полей как на форме вакансий: файл СТОИТ ПЕРЕД комментарием - именно
# поэтому падение восстановления на файле оставляло комментарий пустым.
HTML = """
<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>файл-форма</title>
</head><body>
<form id="f">
  <input name="name" required>
  <input name="telephone" type="tel" required>
  <input name="file" type="file" required>
  <textarea name="comment" required></textarea>
  <button type="submit">Отправить</button>
</form>
</body></html>
"""


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


def _состояние(page):
    return page.evaluate(
        "() => ({name: f.name.value, tel: f.telephone.value,"
        " comment: f.comment.value, files: f.file.files.length})")


def _заполнить(page):
    form = page.locator("#f")
    form.locator("[name=name]").fill("Тест")
    form.locator("[name=telephone]").fill("71111111111")
    form.locator("[name=comment]").fill("Комментарий")
    form.locator("[name=file]").set_input_files(t._безвредный_файл(".pdf"))
    return form


def test_проба_ошибок_валидации_не_отцепляет_файл_и_не_рвёт_восстановление(страница):
    page = страница
    form = _заполнить(page)
    assert _состояние(page)["files"] == 1

    form.evaluate(t._JS_VAL_NATIVE)          # проба чистит поля
    assert _состояние(page)["files"] == 1, "файл трогать нельзя - JS его не вернёт"

    form.evaluate(t._JS_VAL_RESTORE)         # и возвращает их обратно
    сост = _состояние(page)
    assert сост == {"name": "Тест", "tel": "71111111111",
                    "comment": "Комментарий", "files": 1}


def test_снимок_восстановление_залпа_тоже_переживает_файл(страница):
    page = страница
    form = _заполнить(page)
    снимок = form.evaluate(t._JS_RATELIMIT_SNAPSHOT)
    page.evaluate("() => { f.name.value=''; f.comment.value=''; }")
    form.evaluate(t._JS_RATELIMIT_RESTORE, снимок)
    сост = _состояние(page)
    assert сост["name"] == "Тест" and сост["comment"] == "Комментарий"
    assert сост["files"] == 1


def test_дозаполнение_чинит_поля_очищенные_пробой(страница):
    page = страница
    form = _заполнить(page)
    эталон = t._снять_поля_формы(form).get("поля") or {}
    assert эталон, "снимок эталона не должен быть пустым"

    # эмулируем пробу, которая не довосстановила форму
    page.evaluate("() => { f.name.value=''; f.comment.value=''; }")
    исправлено = form.evaluate(t._JS_ДОЗАПОЛНИТЬ, эталон)
    assert set(исправлено) == {"name", "comment"}
    сост = _состояние(page)
    assert сост["name"] == "Тест" and сост["comment"] == "Комментарий"
    # уже заполненное поле не перетираем (сайт мог применить свою маску)
    assert form.evaluate(t._JS_ДОЗАПОЛНИТЬ, эталон) == []
