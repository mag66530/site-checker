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
    assert by["Тел. Общий Город"]["status"] in ("ok", "ok_set")


def test_check_variables_bug_wrong_phone():
    # на сайте чужой номер (не из набора КП Москвы) → bug
    html = ('<header><a href="tel:+70000000000">+7 (000) 000-00-00</a>'
            '<a href="tel:+79990001122">+7 (999) 000-11-22</a></header>')
    r = kp.check_variables(html, "stalmetural.ru")
    by = {f["field"]: f for f in r["fields"]}
    # общий телефон Москвы точно есть в КП; на сайте его нет и номер чужой
    assert by["Тел. Общий Город"]["status"] in ("bug", "warn")


def test_site_has_kp_empty_is_bug_and_shows_site():
    """ГЛАВНОЕ ПРАВИЛО заказчика: если на сайте значение ЕСТЬ, а в КП нет
    (пусто/«2»/мусор из-за съехавших столбцов) - это ОШИБКА ✗, и в отчёте
    ОБЯЗАТЕЛЬНО виден номер/значение с сайта. Восстанавливать номер из других
    колонок КП НЕЛЬЗЯ - иначе реальное расхождение прячется под ✓."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common,
             row.all_phones, row.email)
    # Все телефонные колонки и почта в КП = «2» (тест заказчика: намеренно ломает,
    # чтобы проверить, ловит ли инструмент ошибку). all_phones тоже пуст.
    row.phone_seo = row.phone_ad = row.phone_common = "2"
    row.all_phones = ""
    row.email = "2"
    try:
        html = (
            '<header>'
            '<a href="tel:+74991300786">+7 (499) 130-07-86</a> '
            '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a>'
            '</header>')
        r = kp.check_variables(html, "stalmetural.ru")
        by = {f["field"]: f for f in r["fields"]}
        for label in ("Тел. Общий Город", "Тел. Реклама Город", "Тел. SEO Город"):
            assert by[label]["status"] == "bug", label      # на сайте есть → ✗
            assert by[label]["found"] not in ("–", ""), label  # номер сайта ВИДЕН
        assert by["Почта"]["status"] == "bug"               # почта на сайте есть → ✗
        assert by["Почта"]["found"] not in ("–", "")        # почта сайта видна
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common,
         row.all_phones, row.email) = saved


def test_kp_empty_and_site_empty_is_dash():
    """Если НИ в КП, ни на сайте значения нет (КП = «2»/пусто, на сайте номера
    тоже нет) - это прочерк «–», а не ошибка (правило заказчика: «ни там, ни
    там → прочерк»)."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones)
    row.phone_seo = row.phone_ad = row.phone_common = "2"
    row.all_phones = ""
    try:
        html = "<header>Стальметурал</header><footer>© 2026</footer>"  # телефона нет
        r = kp.check_variables(html, "stalmetural.ru")
        by = {f["field"]: f for f in r["fields"]}
        for label in ("Тел. Общий Город", "Тел. Реклама Город", "Тел. SEO Город"):
            assert by[label]["status"] == "na", label       # ни там ни там → –
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones) = saved


def test_empty_slot_site_shows_common_is_dash_not_bug():
    """Правило заказчика: если в КП для слота (SEO/Реклама) номера НЕТ, а на сайте
    стоит ОБЩИЙ номер города (он же есть в КП) - это НЕ баг, а прочерк «–»:
    отдельного SEO/рекламного номера просто нет, сайт показывает общий. Баг
    (✗) - только если на сайте НОВЫЙ номер, которого в КП города нет вообще."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones)
    row.phone_common = "+7 (495) 111-22-33"      # в КП только общий
    row.phone_seo = row.phone_ad = ""            # SEO/Реклама пусто
    row.all_phones = "4951112233"
    try:
        # На сайте стоит общий номер → SEO/Реклама прочерк, Общий ✓.
        html = '<header><a href="tel:+74951112233">+7 (495) 111-22-33</a></header>'
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. Общий Город"]["status"] == "ok"
        assert by["Тел. SEO Город"]["status"] == "na"       # пусто в КП + общий на сайте → –
        assert by["Тел. Реклама Город"]["status"] == "na"

        # На сайте НОВЫЙ номер, которого в КП нет → все слоты ✗ и виден номер сайта.
        html2 = '<header><a href="tel:+74959998877">+7 (495) 999-88-77</a></header>'
        by2 = {f["field"]: f for f in kp.check_variables(html2, "stalmetural.ru")["fields"]}
        assert by2["Тел. SEO Город"]["status"] == "bug"
        assert "999" in (by2["Тел. SEO Город"]["found"] or "")
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones) = saved


def test_foreign_phone_formatted_with_country_code():
    """Иностранные нац. номера (9 цифр) в отчёте показываем с кодом страны
    (+375/+996/+994/+998), а не «голыми» цифрами (выглядело как мусор:
    447666258, 221318882, 123110138). Просьба заказчика."""
    assert kp._fmt("447666258", "375") == "+375 (44) 766-62-58"
    assert kp._fmt("221318882", "996") == "+996 (221) 31-88-82"
    assert kp._fmt("123110138", "994") == "+994 (12) 311-01-38"
    assert kp._fmt("900112688", "998") == "+998 (90) 011-26-88"
    assert kp._fmt("4991306028", "7") == "+7 (499) 130-60-28"
    # Код страны определяется по стране КП и по домену (.by/.kg/.uz/.az).
    assert kp._dial_for(kp.KPRow(domain="stalmetural.by", city="Минск",
                                 country="Беларусь")) == "375"
    assert kp._dial_for(kp.KPRow(domain="stalmetural.kg", city="Бишкек",
                                 country="")) == "996"
    assert kp._dial_for(kp.KPRow(domain="smg.az", city="Баку",
                                 country="Азербайджан")) == "994"


def test_foreign_phone_formatted_with_country_code():
    """Иностранные нац. номера (9 цифр) в отчёте показываем с кодом страны
    (+375/+996/+994/+998), а не «голыми» цифрами (выглядело как мусор:
    447666258, 221318882, 123110138). Просьба заказчика."""
    assert kp._fmt("447666258", "375") == "+375 (44) 766-62-58"
    assert kp._fmt("221318882", "996") == "+996 (221) 31-88-82"
    assert kp._fmt("123110138", "994") == "+994 (12) 311-01-38"
    assert kp._fmt("900112688", "998") == "+998 (90) 011-26-88"
    assert kp._fmt("4991306028", "7") == "+7 (499) 130-60-28"
    # Код страны определяется по стране КП и по домену (.by/.kg/.uz/.az).
    assert kp._dial_for(kp.KPRow(domain="stalmetural.by", city="Минск",
                                 country="Беларусь")) == "375"
    assert kp._dial_for(kp.KPRow(domain="stalmetural.kg", city="Бишкек",
                                 country="")) == "996"
    assert kp._dial_for(kp.KPRow(domain="smg.az", city="Баку",
                                 country="Азербайджан")) == "994"


def test_garbage_kp_address_still_shows_site_address():
    """Если в КП адрес — мусор («1.0»), но на сайте по метке «Адрес:» есть
    нормальный адрес, показываем его в «На сайте» (а не «–»): видно, что адрес на
    странице ЕСТЬ (кейс «Не нашёл адрес на сайте»). Баг «проверьте КП» остаётся."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = row.address
    row.address = "1.0"          # сломанное значение из КП (как в реальном прогоне)
    try:
        html = ('<main><div class="card"><h3>Стальметурал в Калуге</h3>'
                'Адрес: Калуга, улица Ленина, 102а '
                'Телефон 8 (484) 259-58-86</div></main>')
        r = kp.check_variables(html, "stalmetural.ru")
        by = {f["field"]: f for f in r["fields"]}
        assert by["Адрес"]["status"] == "bug"                  # КП сломана → ✗
        assert "Ленина, 102" in (by["Адрес"]["found"] or "")   # адрес с сайта показан
    finally:
        row.address = saved


def test_not_found_on_site_is_bug():
    """«В КП есть, на сайте нет» = расхождение ✗ (не ⚠), единообразно для
    телефона/почты/Telegram (по просьбе заказчика: не совпадение - красное)."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_common, row.phone_ad, row.phone_seo,
             row.email, row.telegram)
    row.phone_common = "7 (495) 123-45-67"
    row.phone_ad = row.phone_seo = ""
    row.email = "msk@stalmetural.ru"
    row.telegram = "smu_manager"
    try:
        # Пустая страница - ни телефона, ни почты, ни Telegram на сайте нет.
        html = "<header>Стальметурал</header><footer>© 2026</footer>"
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. Общий Город"]["status"] == "bug"   # телефона нет → ✗
        assert by["Почта"]["status"] == "bug"              # почты нет → ✗
        assert by["Telegram"]["status"] == "bug"           # Telegram нет → ✗
    finally:
        (row.phone_common, row.phone_ad, row.phone_seo,
         row.email, row.telegram) = saved


def test_phone_equals_whatsapp_not_dropped():
    """Город, где телефон = номер WhatsApp (напр. Бишкек): номер показан и как
    tel:, и как wa.me. Раньше исключение WhatsApp роняло телефон в «не найден» -
    теперь номер остаётся (из tel:) и сходится с КП → ✓."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_common, row.phone_ad, row.phone_seo, row.whatsapp)
    row.phone_common = "996 (221) 31-88-82"
    row.phone_ad = row.phone_seo = ""
    row.whatsapp = "996 221 31 88 82"
    try:
        html = ('<header>'
                '<a class="ct_phone" href="tel:+996221318882">+996 221 31 88 82</a> '
                '<a href="https://wa.me/996221318882">WhatsApp</a>'
                '</header>')
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. Общий Город"]["status"] == "ok"   # телефон НЕ потерян
        assert by["Тел. Общий Город"]["found"] != "–"
        assert by["WhatsApp"]["status"] == "ok"           # и WhatsApp сходится
    finally:
        (row.phone_common, row.phone_ad, row.phone_seo, row.whatsapp) = saved


def test_check_variables_address_from_contacts():
    """Адрес на «Контактах», а не в подвале главной (кейс МПЭ/mepen): по одной
    главной адрес не находится (✗ «не найден»), с переданным html «Контактов» -
    находится (✓). «Не найден» = ✗ (в КП есть, на сайте нет), а не ⚠."""
    m = kp.load_kp("smu")
    row = m.get("stalmetural.ru")
    assert row and row.address, "нужна строка КП с адресом"

    # Главная: телефон/почта в шапке ЕСТЬ, адреса НЕТ (как у mepen).
    home = ('<header><a href="tel:+74991303669">+7 (499) 130-36-69</a> '
            '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a></header>'
            '<footer>Стальметурал политика конфиденциальности</footer>')
    r0 = kp.check_variables(home, "stalmetural.ru")
    by0 = {f["field"]: f for f in r0["fields"]}
    assert by0["Адрес"]["status"] == "bug"       # без «Контактов» - не найден = ✗
    assert by0["Адрес"]["found"] in ("–", "")    # на сайте не нашли
    assert by0["Почта"]["status"] == "ok"        # почта из шапки не пострадала

    # Страница «Контакты» с карточкой «Адрес: …» (формат mepen: «улица …, дом N»).
    contacts = ('<main><div class="card"><h3>Стальметурал в Москве</h3>'
                f'Адрес: 115477, г. Москва, {row.address} '
                'Телефон 8 (499) 130-36-69 Email msk@stalmetural.ru</div></main>')
    r1 = kp.check_variables(home, "stalmetural.ru", contacts_html=contacts)
    by1 = {f["field"]: f for f in r1["fields"]}
    assert by1["Адрес"]["status"] == "ok"        # с «Контактами» - найден и совпал
    assert by1["Почта"]["status"] == "ok"


def test_check_variables_different_address_is_bug():
    """На сайте найден ДРУГОЙ адрес (с номером дома), не совпадающий с КП →
    расхождение ✗ (bug), а не ⚠ (по просьбе заказчика: адрес крестом).
    «Не найден вовсе» остаётся ⚠ (см. test_check_variables_address_from_contacts)."""
    m = kp.load_kp("smu")
    row = m.get("stalmetural.ru")
    assert row and row.address, "нужна строка КП с адресом"
    saved = row.address
    try:
        row.address = "проспект Богдана Хмельницкого, 102"
        html = ('<header><a href="tel:+74991303669">+7 (499) 130-36-69</a> '
                '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a></header>'
                '<footer>Адрес: проспект Мира, 17 '
                'Телефон 8 (499) 130-36-69</footer>')
        r = kp.check_variables(html, "stalmetural.ru", row=row)
        by = {f["field"]: f for f in r["fields"]}
        assert by["Адрес"]["status"] == "bug"         # другой адрес = ✗
        assert "Мира" in (by["Адрес"]["found"] or "")  # показываем реальный адрес сайта
    finally:
        row.address = saved


def test_только_почта_для_перевода():
    """Переводная копия сайта (город «… (перевод)», напр. steelgroup.az): в отчёте
    проверяем ТОЛЬКО «Почту», остальные колонки → «–». Обычный город не трогаем."""
    import variables_run as vr
    fields = [
        {"field": "Город", "status": "bug", "found": "не найден на странице"},
        {"field": "Тел. SEO Город", "status": "ok", "found": "есть"},
        {"field": "Почта", "status": "bug", "found": "другая почта"},
        {"field": "WhatsApp", "status": "ok", "found": "есть"},
    ]
    out = vr._только_почта_для_перевода("Азербайджан (перевод)", [dict(f) for f in fields])
    by = {f["field"]: f for f in out}
    assert by["Город"]["status"] == "na" and by["Город"]["found"] == "–"
    assert by["Тел. SEO Город"]["status"] == "na"
    assert by["WhatsApp"]["status"] == "na"
    assert by["Почта"]["status"] == "bug"            # почту проверяем как обычно

    # Обычный город (без «(перевод)») остаётся как есть.
    out2 = vr._только_почта_для_перевода("Баку", [dict(f) for f in fields])
    assert {f["field"]: f["status"] for f in out2} == \
        {"Город": "bug", "Тел. SEO Город": "ok", "Почта": "bug", "WhatsApp": "ok"}


def test_find_contacts_path():
    """variables_run находит ссылку «Контакты» на том же хосте (для догрузки)."""
    import variables_run as vr
    home = ('<nav><a href="/catalog/">Каталог</a>'
            '<a href="/kontakty/">Контакты</a></nav>')
    assert vr._find_contacts_path(home, "minsk.mepen.by") == "/kontakty/"
    # href с /contacts/ ловится, даже если текст не «Контакты»
    assert vr._find_contacts_path('<a href="/contacts/">Contact</a>', "x.ru") == "/contacts/"
    # чужой хост игнорируем (ссылка на соцсеть с текстом «Контакты»)
    assert vr._find_contacts_path('<a href="https://vk.com/x">Контакты</a>', "x.ru") == ""
    # ссылки нет - пусто
    assert vr._find_contacts_path("<a href='/about/'>О нас</a>", "x.ru") == ""


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
