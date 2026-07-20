"""Тесты пункта 1.4: расширение kp.py (страна/Telegram/WhatsApp + check_variables)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

kp = pytest.importorskip("kp")  # тянет content_checker/bs4 - если нет, пропускаем


def test_load_kp_new_columns():
    m = kp.load_kp("smu")
    assert m, "КП СМУ должна загружаться"
    row = m.get("stalmetural.ru")
    assert row is not None
    assert row.country == "Россия"
    assert row.telegram == "smu_manager2"
    assert row.whatsapp  # непусто


def test_normalize_tg():
    assert kp.normalize_tg("@smu_manager2") == "smu_manager2"
    assert kp.normalize_tg("https://t.me/smu_manager2") == "smu_manager2"
    assert kp.normalize_tg("tg://resolve?domain=imp_manager5") == "imp_manager5"
    assert kp.normalize_tg("telegram.me/Some_User") == "some_user"
    assert kp.normalize_tg("") == ""


def test_extract_messengers():
    html = ('<a href="https://t.me/smu_manager2">Telegram</a> '
            '<a href="https://wa.me/79031303669">WhatsApp</a> '
            '<a href="https://t.me/share/url?u=x">поделиться</a>')
    c = kp.extract_site_contacts(html)
    assert "smu_manager2" in c["telegram"]
    assert "share" not in c["telegram"]           # служебные t.me отфильтрованы
    assert "9031303669" in c["whatsapp"]          # нормализовано в 10 цифр


def test_check_variables_ok():
    html = (
        '<header>'
        '<a href="tel:+74991303669">+7 (499) 130-36-69</a> '
        '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a> '
        'г. Москва, улица Люблинская, 151'
        '</header>'
        '<a href="https://t.me/smu_manager2">TG</a>'
        '<a href="https://wa.me/79031303669">WA</a>')
    r = kp.check_variables(html, "stalmetural.ru")
    assert r["matched"] is True
    assert r["city"] == "Москва"
    assert r["country"] == "Россия"
    by = {f["field"]: f for f in r["fields"]}
    assert by["Почта"]["status"] == "ok"
    assert by["Telegram"]["status"] == "ok"
    assert by["WhatsApp"]["status"] == "ok"
    assert by["Тел. общий"]["status"] in ("ok", "ok_set")


def test_check_variables_bug_wrong_phone():
    # на сайте чужой номер (не из набора КП Москвы) → bug
    html = ('<header><a href="tel:+70000000000">+7 (000) 000-00-00</a>'
            '<a href="tel:+79990001122">+7 (999) 000-11-22</a></header>')
    r = kp.check_variables(html, "stalmetural.ru")
    by = {f["field"]: f for f in r["fields"]}
    # общий телефон Москвы точно есть в КП; на сайте его нет и номер чужой
    assert by["Тел. общий"]["status"] in ("bug", "warn")


def test_check_variables_garbage_kp_values_are_flagged():
    """Правка КП не должна проходить мимо проверки. Если в ячейке телефона или
    адреса лежит не номер/не адрес, а мусор («2»), это ошибка КП (✗), а не «нет
    в КП» (—). Раньше ловилась только почта - телефон показывал «—», адрес мог
    дать ложное ✓."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_common, row.email, row.address)
    row.phone_seo = row.phone_common = row.email = row.address = "2"
    try:
        html = (
            '<header>'
            '<a href="tel:+74991300786">+7 (499) 130-07-86</a> '   # рекл. номер реальный
            '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a> '
            'г. Москва, улица Люблинская, 151'
            '</header>')
        r = kp.check_variables(html, "stalmetural.ru")
        by = {f["field"]: f for f in r["fields"]}
        assert by["Тел. поиск"]["status"] == "bug"    # было «na» («—»)
        assert by["Тел. общий"]["status"] == "bug"    # было «na» («—»)
        assert by["Почта"]["status"] == "bug"         # ловилось и раньше
        assert by["Адрес"]["status"] == "bug"         # было ложное «ok» (✓)
        assert by["Тел. реклама"]["status"] == "ok"   # реальный номер по-прежнему ✓
    finally:
        row.phone_seo, row.phone_common, row.email, row.address = saved


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
