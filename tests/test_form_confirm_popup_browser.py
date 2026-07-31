"""Браузерный регресс-тест детекта подтверждения отправки (ИМП, 29.07.2026).

Воспроизводит разметку inmetprom.ru, на которой тул писал ✗ «НЕТ
ПОДТВЕРЖДЕНИЯ» по всем AJAX-формам, хотя сайт работал:
  • модалок в DOM много, окно «Спасибо» - ПОСЛЕДНЕЕ (старый поиск смотрел
    только первые 3 узла на селектор и до него не доходил);
  • перед POST-ом формы уходит POST reCAPTCHA (его ответ брался за «ответ
    формы», поэтому настоящий success сайта не читался никогда);
  • попап благодарности сам закрывается через пару секунд (а вердикт
    снимался ПОСЛЕ разрушающих проб - к тому моменту его уже нет).

Тест пропускается, если браузер Playwright не установлен - остальные тесты
пакета браузера не требуют."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

t = pytest.importorskip("test_all")
sync_api = pytest.importorskip("playwright.sync_api")

HTML = """
<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>макет</title>
<style>.my-modal{display:none}.my-modal.open{display:block}</style></head><body>
<h1>Инметпром</h1>
<p>Оставьте заявку - мы свяжемся с вами в течение 15 минут</p>
<div class="my-modal" id="modal-city"><div class="my-modal__body">Ваш город Москва?</div></div>
<div class="my-modal open" id="modal-callback"><div class="my-modal__body">
  <div class="my-modal__body_form">
    <form id="form-callback">
      <input name="name" value=""><input name="telephone" type="tel" value="">
      <input name="email" type="email" value="">
      <button type="submit">Отправить</button>
    </form></div></div></div>
<div class="my-modal" id="modal-making-order"><div class="my-modal__body">Оформление</div></div>
<div class="my-modal" id="modal-cart"><div class="my-modal__body">Корзина пуста</div></div>
<div class="my-modal" id="modal-thanks"><div class="my-modal__body">
  <h2>СПАСИБО, ВАШ ЗАКАЗ ПРИНЯТ</h2>
  <p>Наш менеджер свяжется с вами в ближайшее время</p></div></div>
<script>
document.getElementById('form-callback').addEventListener('submit', async e => {
  e.preventDefault(); const f = e.target;
  await fetch('https://www.google.com/recaptcha/api2/reload?k=6Le',
              {method: 'POST', body: 'v=abc&k=6Le'});
  const body = new URLSearchParams({name: f.name.value, telephone: f.telephone.value,
                                    email: f.email.value}).toString();
  const r = await fetch('/ajax/form.php', {method: 'POST', body,
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}});
  if ((await r.json()).success) {
    document.getElementById('modal-callback').classList.remove('open');
    const th = document.getElementById('modal-thanks'); th.classList.add('open');
    setTimeout(() => th.classList.remove('open'), 1500);   // попап сам закрылся
  }});
</script></body></html>
"""


def _маршрут(route):
    u = route.request.url
    if "/ajax/form.php" in u:
        route.fulfill(status=200, content_type="application/json",
                      body='{"success":true}')
    elif "recaptcha" in u:
        route.fulfill(status=200, content_type="text/plain",
                      body=')]}\'\n["rresp","03AF"]')
    else:
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML)


@pytest.fixture(scope="module")
def страница():
    """Страница-макет в реальном браузере. Нет браузера - тест пропускаем."""
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
        page.route("**/*", _маршрут)
        page.goto("https://inmetprom.test/")
        yield page
        браузер.close()


def _старый_скан(page):
    """Как детект попапа работал ДО фикса: первые 3 узла на селектор."""
    for sel in ("[class*='popup']", "[class*='modal']", "[role='dialog']",
                "[class*='thank']", "[class*='success']", "[class*='spasibo']"):
        loc = page.locator(sel)
        for i in range(min(loc.count(), 3)):
            el = loc.nth(i)
            if el.is_visible() and t._текст_подтверждает_отправку(
                    el.inner_text(timeout=500)):
                return True
    return False


def test_молчащая_форма_не_даёт_ложного_подтверждения(страница):
    """Обратная сторона фикса: форма, которая НИЧЕГО не показала, обязана
    остаться «Нет». Расширенный поиск попапа и разница текста не должны
    цепляться за статический «мы свяжемся с вами» на самой странице."""
    page = страница
    page.goto("https://inmetprom.test/")
    текст_до = page.locator("body").inner_text()
    t._наблюдатель_подтверждения_старт(page)
    # сабмит без обработчика успеха: попапа нет, текст страницы не меняется
    page.evaluate("() => { const th = document.getElementById('modal-thanks');"
                  " th.classList.remove('open'); }")
    page.wait_for_timeout(400)
    набл = t._наблюдатель_подтверждения_итог(page)
    assert набл == ""
    assert t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=600,
        текст_тела_до=текст_до, наблюдение=набл) == "Нет"


def test_рабочая_форма_даёт_подтверждение_и_ответ_сервера(страница):
    page = страница
    page.goto("https://inmetprom.test/")   # чистое состояние, не зависим от порядка
    form = page.locator("#form-callback")
    form.locator("[name=name]").fill("Тест")
    form.locator("[name=telephone]").fill("71111111111")
    form.locator("[name=email]").fill("test111@yandex.ru")

    поля = t._снять_поля_формы(form).get("поля") or {}
    текст_до = page.locator("body").inner_text()

    кандидаты = []

    def on_resp(resp):
        rq = resp.request
        if (rq.method or "").upper() != "POST" or t._ds_это_трекер(resp.url):
            return
        тело = t._тело_запроса_для_поиска(
            rq.headers.get("content-type", ""), rq.post_data or "")
        кандидаты.append({"url": resp.url, "статус": resp.status,
                          "текст": resp.text(), "локация": "", "тело_запроса": тело})

    page.on("response", on_resp)
    t._наблюдатель_подтверждения_старт(page)
    page.locator("#form-callback button[type=submit]").click()
    page.wait_for_timeout(800)

    # 1) Попап «Спасибо» - последний из пяти модалок в DOM. Новый поиск (только
    # видимые узлы, лимит 20) его находит; старый (первые 3 подряд) - нет.
    assert t._найти_видимый_попап_успеха(page) is not None
    assert _старый_скан(page) is False

    # 2) Попап сам закрылся - подтверждение всё равно засчитано (наблюдатель).
    page.wait_for_timeout(1500)
    assert t._найти_видимый_попап_успеха(page) is None
    набл = t._наблюдатель_подтверждения_итог(page)
    assert t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=800,
        текст_тела_до=текст_до, наблюдение=набл) == "Да (попап)"

    # 3) Ответ формы, а не reCAPTCHA: капча отфильтрована как трекер, выбранный
    # ответ - наш POST с success:true.
    page.remove_listener("response", on_resp)
    assert all("recaptcha" not in к["url"] for к in кандидаты)
    отв = t._выбрать_ответ_формы(кандидаты, поля, "inmetprom.test")
    assert "/ajax/form.php" in отв["url"]
    assert t._ответ_формы_вердикт(отв["текст"], отв["статус"]) == "успешно"
    assert "HTTP 200" in t.описание_ответа_формы(отв)
