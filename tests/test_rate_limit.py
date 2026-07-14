"""Тест «Ограничено количество запросов» (защита от спама/ботов): пассивный
слой (капча/honeypot, всегда включён) и активный залп (галочка, выключена по
умолчанию). Проверяются чистые функции-вердикты и защита от прогона на форме
заказа - без браузера."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

# test_all тянет bs4/playwright на уровне модуля - если их нет, тест пропускаем.
t = pytest.importorskip("test_all")


# ── Пассивный слой ────────────────────────────────────────────────────────

def test_обнаруживает_recaptcha():
    r = t.защита_от_спама_из_html('<div class="g-recaptcha" data-sitekey="x"></div>')
    assert r["капча"] is True
    assert r["какая"] == "reCAPTCHA"


def test_обнаруживает_hcaptcha():
    r = t.защита_от_спама_из_html('<script src="https://hcaptcha.com/1/api.js"></script>')
    assert r["капча"] is True
    assert r["какая"] == "hCaptcha"


def test_обнаруживает_yandex_smartcaptcha():
    r = t.защита_от_спама_из_html('<div id="smartcaptcha"></div>')
    assert r["капча"] is True
    assert r["какая"] == "Яндекс SmartCaptcha"


def test_без_капчи_ничего_не_находит():
    r = t.защита_от_спама_из_html('<form><input name="phone"></form>')
    assert r["капча"] is False
    assert r["какая"] == ""


def test_пустой_html_не_падает():
    r = t.защита_от_спама_из_html("")
    assert r["капча"] is False
    r2 = t.защита_от_спама_из_html(None)
    assert r2["капча"] is False


def test_пассивный_вердикт_капча_есть_защита():
    статус, деталь = t.лимит_пассивно_вердикт(
        {"капча": True, "капча_какая": "reCAPTCHA", "honeypot": False, "honeypot_имя": ""})
    assert статус == "Есть защита"
    assert "reCAPTCHA" in деталь


def test_пассивный_вердикт_honeypot_есть_защита():
    статус, деталь = t.лимит_пассивно_вердикт(
        {"капча": False, "капча_какая": "", "honeypot": True, "honeypot_имя": "hideit"})
    assert статус == "Есть защита"
    assert "hideit" in деталь


def test_пассивный_вердикт_ничего_не_обнаружено():
    статус, деталь = t.лимит_пассивно_вердикт(
        {"капча": False, "капча_какая": "", "honeypot": False, "honeypot_имя": ""})
    assert статус == "Не обнаружено"
    assert деталь


def test_пассивный_вердикт_пустой_словарь():
    статус, _ = t.лимит_пассивно_вердикт({})
    assert статус == "Не обнаружено"


# ── Активный залп ─────────────────────────────────────────────────────────

def test_блок_по_капча_маркеру():
    assert t._текст_похож_на_блок_лимита("Капча не пройдена, попробуйте снова")


def test_блок_по_фразе_слишком_часто():
    assert t._текст_похож_на_блок_лимита("Вы отправляете слишком часто, попробуйте позже")


def test_блок_по_английской_фразе():
    assert t._текст_похож_на_блок_лимита("Error: too many requests")


def test_обычный_текст_не_блок():
    assert not t._текст_похож_на_блок_лимита("Спасибо, ваша заявка принята!")
    assert not t._текст_похож_на_блок_лимита("")
    assert not t._текст_похож_на_блок_лимита(None)


def test_активный_вердикт_блок_на_третьей_попытке():
    статус, деталь = t.лимит_активно_вердикт([
        {"n": 1, "успех": True, "блок": False},
        {"n": 2, "успех": True, "блок": False},
        {"n": 3, "успех": False, "блок": True},
    ])
    assert статус == "Сработала защита"
    assert "№3" in деталь


def test_активный_вердикт_все_прошли_не_сработала():
    статус, деталь = t.лимит_активно_вердикт([
        {"n": 1, "успех": True, "блок": False},
        {"n": 2, "успех": True, "блок": False},
        {"n": 3, "успех": True, "блок": False},
    ])
    assert статус == "Не сработала за 3 попытки"
    assert деталь


def test_активный_вердикт_пустой_список_проверить():
    статус, _ = t.лимит_активно_вердикт([])
    assert статус == "Проверить"


def test_активный_вердикт_неоднозначно_проверить():
    статус, _ = t.лимит_активно_вердикт([
        {"n": 1, "успех": False, "блок": False},
        {"n": 2, "успех": True, "блок": False},
    ])
    assert статус == "Проверить"


def test_форма_заказа_пропускается_без_браузера():
    рез = t.активная_проба_лимита(None, None, None, is_order=True)
    assert рез["попытки"] == []
    assert "заказ" in рез["детали"]


def test_число_попыток_фиксировано():
    # Сознательно не настраивается через UI - защита от «покрутить побольше».
    assert t._RATELIMIT_ПОПЫТОК == 3


# ── Колонки в шапке лога ────────────────────────────────────────────────

def test_колонки_лимита_в_шапке_лога():
    for заголовок, ключ in (
        ("Защита от спама (пассивно)", "защита_от_спама_пассивно"),
        ("Защита от спама (активно)", "защита_от_спама_активно"),
    ):
        assert заголовок in t.LOG_HEADERS
        assert ключ in t.LOG_KEYS_ORDER
        assert t.LOG_HEADERS.index(заголовок) == t.LOG_KEYS_ORDER.index(ключ)
    assert len(t.LOG_HEADERS) == len(t.LOG_KEYS_ORDER)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f"✓ {fn.__name__}"); ok += 1
        except Exception:
            print(f"✗ {fn.__name__}"); traceback.print_exc()
    print(f"\n{ok}/{len(fns)} прошло")
    sys.exit(0 if ok == len(fns) else 1)
