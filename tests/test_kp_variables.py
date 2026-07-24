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
    """Прочерк «–» - ТОЛЬКО когда ни в КП, ни на сайте значения нет ВООБЩЕ
    (ячейка КП пустая И на сайте номера нет). Правило заказчика."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones)
    row.phone_seo = row.phone_ad = row.phone_common = ""    # КП реально ПУСТАЯ
    row.all_phones = ""
    try:
        html = "<header>Стальметурал</header><footer>© 2026</footer>"  # телефона нет
        r = kp.check_variables(html, "stalmetural.ru")
        by = {f["field"]: f for f in r["fields"]}
        for label in ("Тел. Общий Город", "Тел. Реклама Город", "Тел. SEO Город"):
            assert by[label]["status"] == "na", label       # ни там ни там → –
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones) = saved


def test_garbage_kp_value_is_info_always_bug():
    """«2»/мусор в ячейке КП - это ИНФА в КП (заказчик ставит её нарочно, чтобы
    проверить инструмент). Она заведомо не совпадает с сайтом → ВСЕГДА ✗, даже
    если на сайте городского номера нет. Прочерк для непустой ячейки запрещён.
    В «Сайт» показываем, что реально на сайте (городской, иначе сотовый)."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones,
             row.email, row.address)
    row.phone_seo = row.phone_ad = row.phone_common = "2"
    row.all_phones = ""
    row.email = row.address = "2"
    try:
        # Город, где на сайте ТОЛЬКО сотовый (кейс Севастополя/Владимира):
        html = '<header><a href="tel:+79030846889">+7 (903) 084-68-89</a></header>'
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        for label in ("Тел. Общий Город", "Тел. SEO Город"):
            assert by[label]["status"] == "bug", label          # «2» = инфа → ✗
            assert by[label]["expected"] == "2"
            assert "903" in by[label]["found"], label           # сотовый виден в «Сайт»
        # Почта «2», на сайте почты нет → всё равно ✗ (в КП инфа есть).
        assert by["Почта"]["status"] == "bug"
        # Адрес «2», на сайте адрес не вытащился → всё равно ✗.
        assert by["Адрес"]["status"] == "bug"

        # И совсем пустая страница: «2» в КП всё равно даёт ✗, не прочерк.
        by2 = {f["field"]: f for f in
               kp.check_variables("<p>пусто</p>", "stalmetural.ru")["fields"]}
        assert by2["Тел. Общий Город"]["status"] == "bug"
        assert by2["Почта"]["status"] == "bug"
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones,
         row.email, row.address) = saved


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
    """Переводная копия сайта (город «… (перевод)», напр. steelgroup.az): это
    реальный сайт, контакты сверяем как обычно (телефон/почта/WhatsApp), гасим
    ТОЛЬКО «Город» (это метка КП, не реальный город). Обычный город не трогаем."""
    import variables_run as vr
    fields = [
        {"field": "Город", "status": "bug", "found": "не найден на странице"},
        {"field": "Тел. SEO Город", "status": "bug", "found": "+7 (499) 130-07-86"},
        {"field": "Почта", "status": "bug", "found": "другая почта"},
        {"field": "WhatsApp", "status": "ok", "found": "есть"},
    ]
    out = vr._только_почта_для_перевода("Азербайджан (перевод)", [dict(f) for f in fields])
    by = {f["field"]: f for f in out}
    assert by["Город"]["status"] == "na" and by["Город"]["found"] == "–"  # город - метка
    assert by["Тел. SEO Город"]["status"] == "bug"   # контакты сверяем как обычно
    assert by["Почта"]["status"] == "bug"
    assert by["WhatsApp"]["status"] == "ok"

    # Обычный город (без «(перевод)») остаётся как есть.
    out2 = vr._только_почта_для_перевода("Баку", [dict(f) for f in fields])
    assert {f["field"]: f["status"] for f in out2} == \
        {"Город": "bug", "Тел. SEO Город": "bug", "Почта": "bug", "WhatsApp": "ok"}


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


def test_widget_url_digits_not_phone():
    """Цифры из URL виджетов/картинок (напр. рейтинг-бейдж Яндекса
    yandex.ru/sprav/widget/rating-badge/90492027885) - НЕ телефон. Раньше они
    попадали в «телефоны с сайта» и давали ложное «на сайте другой номер»
    (кейс Хабаровска: +7 (049) 202-78-85, которого на сайте нет). href="tel:…"
    при этом остаётся источником номера."""
    html = ('<footer>'
            '<a href="tel:+74212680556">+7 (421) 268-05-56</a>'
            '<iframe src="https://yandex.ru/sprav/widget/rating-badge/'
            '90492027885?type=rating" width="150"></iframe>'
            '</footer>')
    c = kp.extract_site_contacts(html)
    assert c["phones"] == ["4212680556"], c["phones"]   # только настоящий номер
    # И на уровне split_phones: 10 цифр с ведущим 0 - не номер (кодов на 0 нет).
    assert kp.split_phones("90492027885") == []


def test_merge_podmena_wording_and_format():
    """Живая проверка подмены: расхождение пишется ЕДИНООБРАЗНО «телефон на сайте
    не совпадает с КП» (как все остальные поля), а номера ЧИТАЕМО
    (+7 (800) 600-98-56, не голыми цифрами). Что в КП/на сайте - в колонках."""
    import variables_run as vr
    fld = {'field': 'Тел. Реклама Город', 'expected': '–', 'found': '–',
           'status': 'na', 'note': ''}
    vr._merge_подмена(fld, {'status': 'replaced_ok', 'shown': ['8006009856']},
                      False, '8006009856', "рекламный номер",
                      "с меткой ?utm_source=yandex", dial='7')
    assert fld['status'] == 'bug'
    assert fld['found'] == '+7 (800) 600-98-56'          # читаемый формат
    assert fld['note'] == 'телефон на сайте не совпадает с КП'

    fld2 = {'field': 'Тел. Реклама Город', 'expected': '–', 'found': '–',
            'status': 'na', 'note': ''}
    vr._merge_подмена(fld2, {'status': 'not_replaced', 'shown': ['4212680556']},
                      False, '8006009856', "рекламный номер",
                      "с меткой ?utm_source=yandex", dial='7')
    assert fld2['status'] == 'bug'
    assert fld2['found'] == '+7 (421) 268-05-56'
    assert fld2['note'] == 'телефон на сайте не совпадает с КП'


def test_mismatch_notes_are_uniform():
    """Все расхождения (✗) пишутся ЕДИНООБРАЗНО: «<поле> на сайте не совпадает с
    КП» - без разнобоя «в КП нет / на сайте нет / не распознан» (просьба
    заказчика). КП и фактическое значение сайта видны в колонках «КП»/«На сайте»."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones,
             row.email, row.address, row.telegram, row.whatsapp)
    # КП всё сломано («2»), на сайте - реальные данные, отличные от КП.
    row.phone_seo = row.phone_ad = row.phone_common = "2"
    row.all_phones = ""
    row.email = row.address = row.telegram = row.whatsapp = "2"
    try:
        html = (
            '<header>'
            '<a href="tel:+74991303669">+7 (499) 130-36-69</a> '
            '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a> '
            'Адрес: Москва, улица Полярная, 5 '
            '<a href="https://t.me/some_manager">TG</a>'
            '<a href="https://wa.me/79995553311">WA</a>'
            '</header>')
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. Общий Город"]["note"] == "телефон на сайте не совпадает с КП"
        assert by["Почта"]["note"] == "почта на сайте не совпадает с КП"
        assert by["Адрес"]["note"] == "адрес на сайте не совпадает с КП"
        assert by["Telegram"]["note"] == "Telegram на сайте не совпадает с КП"
        assert by["WhatsApp"]["note"] == "WhatsApp на сайте не совпадает с КП"
        # И везде видно фактическое значение с сайта.
        for f in ("Тел. Общий Город", "Почта", "Адрес", "Telegram", "WhatsApp"):
            assert by[f]["found"] not in ("–", ""), f
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones,
         row.email, row.address, row.telegram, row.whatsapp) = saved


def test_empty_slot_dash_but_garbage_slot_bug():
    """Пустая ячейка слота ≠ мусор «2» в ней (уточнение заказчика):
      • ПУСТО в КП + на сайте известный (общий) номер города → «–» - отдельного
        номера просто нет, это не ошибка;
      • «2» в КП (ИНФА, поставленная нарочно) → всегда ✗, что бы ни было на
        сайте - инфа в КП с сайтом не совпадает."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones)
    row.phone_common = "+7 (495) 111-22-33"
    row.phone_ad = ""
    row.all_phones = "4951112233"
    html = '<header><a href="tel:+74951112233">+7 (495) 111-22-33</a></header>'
    try:
        row.phone_seo = ""                      # пусто → прочерк
        by = {f["field"]: f for f in
              kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. SEO Город"]["status"] == "na"

        row.phone_seo = "2"                     # мусор-инфа → ✗
        by = {f["field"]: f for f in
              kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. SEO Город"]["status"] == "bug"
        assert by["Тел. SEO Город"]["expected"] == "2"
        assert "111-22-33" in by["Тел. SEO Город"]["found"]   # что на сайте - видно
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones) = saved


def test_address_tail_trimmed():
    """К адресу с сайта не приклеивается хвост карточки «Контактов»:
    «улица Руднева, 35Д Контакты: +7 (903)… krym@… Время работы: пн-пт» →
    в отчёте только «улица Руднева, 35Д» (просьба заказчика)."""
    assert kp._обрезать_хвост_адреса(
        "улица Руднева, 35Д Контакты: +7 (903) 084-68-89 "
        "krym@stalmetural.ru Время работы: пн-пт: с") == "улица Руднева, 35Д"
    # Чистый адрес не трогаем.
    assert kp._обрезать_хвост_адреса("улица Данилы Зверева, 31литS") == \
        "улица Данилы Зверева, 31литS"
    # И через _site_address_full (страница «Контакты»).
    html = ('<main>Адрес: Севастополь, улица Руднева, 35Д Контакты: '
            '+7 (903) 084-68-89 krym@stalmetural.ru Время работы: пн-пт</main>')
    got = kp._site_address_full(html)
    assert "Руднева, 35Д" in got and "Контакты" not in got and "работы" not in got


def test_any_nonempty_kp_value_is_value_shown_as_is():
    """Правило заказчика: ЛЮБОЕ непустое значение в КП («1», «.», «агркугш») -
    это значение. Выводим в колонку «КП» КАК ЕСТЬ и сверяем с сайтом → ✗
    (не совпадает). Пусто = нет значения (см. отдельные тесты). Проверяем все
    поля, включая «Реклама Город» (там колонка КП раньше показывала «–»)."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones,
             row.email, row.address, row.telegram, row.whatsapp)
    html = ('<header><a href="tel:+74991303669">+7 (499) 130-36-69</a> '
            '<a href="mailto:msk@stalmetural.ru">msk@stalmetural.ru</a></header>')
    try:
        for junk in ("1", ".", "агркугш"):
            (row.phone_seo, row.phone_ad, row.phone_common) = (junk, junk, junk)
            row.all_phones = ""
            row.email = row.address = row.telegram = row.whatsapp = junk
            by = {f["field"]: f for f in
                  kp.check_variables(html, "stalmetural.ru")["fields"]}
            for label in ("Тел. Общий Город", "Тел. Реклама Город",
                          "Тел. SEO Город", "Почта", "Адрес", "Telegram", "WhatsApp"):
                assert by[label]["status"] == "bug", (junk, label)
                assert by[label]["expected"] == junk, (junk, label)   # КП как есть
    finally:
        (row.phone_seo, row.phone_ad, row.phone_common, row.all_phones,
         row.email, row.address, row.telegram, row.whatsapp) = saved


def test_site_address_azerbaijani_translated():
    """Переводной сайт (steelgroup.az): адрес по метке «Ünvan:» латиницей/
    азербайджанскими буквами - «Bakı, 23 İzmir küçəsi». Хвост «İş saatları»
    обрезается. Раньше извлечение адреса было только под кириллицу → «Сайт: –»."""
    html = ('<main><h2>Əlaqə məlumatları</h2>'
            'Ünvan: Bakı, 23 İzmir küçəsi '
            'İş saatları: Bazar ertəsi-cümə: 09:00-18:00 '
            'Əlaqə: +994-50-5732867 info@steelgroup.az</main>')
    got = kp._site_address_full(html)
    assert got == "Bakı, 23 İzmir küçəsi", got
    # Кириллица по-прежнему работает, мусор отсеивается.
    assert kp._site_address_full('<main>Адрес: Самара, Ярмарочная, 55 '
                                 'График работы: пн-пт</main>') == "Самара, Ярмарочная, 55"
    assert kp._site_address_full('<main>адрес доставки. Уличные фонари, Урны</main>') == ""


def test_mobile_city_number_is_compared():
    """Города, где ОСНОВНОЙ номер - сотовый (Донецк/Севастополь: +7 903…):
    сотовые больше НЕ выкидываются из сверки. Берём номер КП и сравниваем с
    сайтом. КП 903… = сайт 903… → ✓ (раньше выходило ложное «нет ни в КП, ни
    на сайте» и прочерк)."""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_common, row.phone_ad, row.phone_seo, row.all_phones)
    row.phone_common = "7 (903) 411-80-66"       # основной номер города - сотовый
    row.phone_ad = row.phone_seo = ""
    row.all_phones = "9034118066"
    try:
        # На сайте тот же сотовый → ✓.
        html = '<header><a href="tel:+79034118066">+7 (903) 411-80-66</a></header>'
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. Общий Город"]["status"] == "ok"
        assert "903" in by["Тел. Общий Город"]["found"]

        # На сайте ДРУГОЙ сотовый (не из КП) → ✗ (сравнение работает и для сотовых).
        html2 = '<header><a href="tel:+79991112233">+7 (999) 111-22-33</a></header>'
        by2 = {f["field"]: f for f in kp.check_variables(html2, "stalmetural.ru")["fields"]}
        assert by2["Тел. Общий Город"]["status"] == "bug"
        assert "999" in by2["Тел. Общий Город"]["found"]
    finally:
        (row.phone_common, row.phone_ad, row.phone_seo, row.all_phones) = saved


def test_messenger_from_header_not_footer_global_channel():
    """Мессенджеры сверяем по ШАПКЕ (иконки контакта города), а ГЛОБАЛЬНЫЙ канал
    компании в <footer> (напр. t.me/inmetprom) НЕ считаем контактом города.
    Кейс ИМП/СНГ: у Минска/Астаны/Баку своих иконок в шапке нет → «на сайте нет»
    (а не ложный @inmetprom из подвала). Просьба заказчика: «проверяй по шапке»."""
    # Город с иконкой менеджера в шапке + глобальный канал в подвале.
    html = ('<header>Черемхово '
            '<a href="https://t.me/imp_manager2">TG</a> '
            '<a href="https://wa.me/79634523249">WA</a></header>'
            '<footer><a href="https://t.me/inmetprom">Наш канал</a></footer>')
    c = kp.extract_site_contacts(html)
    assert c["telegram"] == ["imp_manager2"], c["telegram"]   # без inmetprom из подвала
    assert "9634523249" in c["whatsapp"]

    # СНГ: в шапке иконок нет, в подвале только глобальный канал → на сайте пусто.
    sng = ('<header>Минск +375 (44) 588-81-48 minsk@inmetprom.by</header>'
           '<footer><a href="https://t.me/inmetprom">Наш канал</a></footer>')
    c2 = kp.extract_site_contacts(sng)
    assert c2["telegram"] == [], c2["telegram"]
    assert c2["whatsapp"] == []


def test_messenger_absent_says_otsutstvuet():
    """Если в КП мессенджер есть, а на сайте его НЕТ (нет значка в шапке) -
    пишем «Telegram на сайте отсутствует» / «WhatsApp на сайте отсутствует»
    (✗, значение КП показываем). Просьба заказчика. Если на сайте ДРУГОЙ -
    «не совпадает с КП»."""
    row = kp.KPRow(domain="inmetprom.by", city="Минск",
                   phone_common="375 (44) 588-81-48", all_phones="",
                   email="minsk@inmetprom.by", country="Беларусь",
                   telegram="imp_by", whatsapp="375 (44) 588-81-48")
    html = '<header>Минск <a href="tel:+375445888148">+375 (44) 588-81-48</a></header>'
    by = {f["field"]: f for f in kp.check_variables(html, "inmetprom.by", row=row)["fields"]}
    assert by["Telegram"]["status"] == "bug"
    assert by["Telegram"]["note"] == "Telegram на сайте отсутствует"
    assert by["Telegram"]["expected"] == "@imp_by"
    assert by["Telegram"]["found"] == "–"
    assert by["WhatsApp"]["status"] == "bug"
    assert by["WhatsApp"]["note"] == "WhatsApp на сайте отсутствует"
    assert by["WhatsApp"]["found"] == "–"

    # На сайте ДРУГОЙ телеграм → «не совпадает».
    row.telegram = "imp_by"
    html2 = ('<header>Минск <a href="https://t.me/other_acc">TG</a></header>')
    by2 = {f["field"]: f for f in kp.check_variables(html2, "inmetprom.by", row=row)["fields"]}
    assert by2["Telegram"]["note"] == "Telegram на сайте не совпадает с КП"


def test_site_address_without_trailing_marker():
    """Адрес по метке «Адрес», за которым НЕ идёт телефон/почта, а сразу
    «Реквизиты»/«Скачать» (кейс СПб: «набережная Обводного канала, 64к2
    Реквизиты…»). Раньше без стоп-маркера справа адрес вообще не находился."""
    html = ('<main>Адрес набережная Обводного канала, 64к2 '
            'Реквизиты Скачать реквизиты</main>')
    assert kp._site_address_full(html) == "набережная Обводного канала, 64к2"
    # И «Скачать» сразу после адреса.
    html2 = '<main>АДРЕС улица Мира, 5 Скачать прайс-лист</main>'
    assert kp._site_address_full(html2) == "улица Мира, 5"


def test_address_match_localized_not_fooled_by_page():
    """Адрес сверяется ЛОКАЛЬНО: номер дома из КП должен стоять РЯДОМ с названием
    улицы на странице. Иначе на длинном тексте «улица где-то» + «нужное число
    где-то ещё» ложно засчитывались как совпадение, и смена дома не ловилась."""
    # Адрес на сайте изменён на 999, а «151» встречается в другом месте страницы.
    page = 'В каталоге 151 позиция. Контакты: улица Люблинская, 999. © 2024'
    assert kp.address_match(page, 'улица Люблинская, 151') is False
    # Верный адрес на странице - совпадает.
    page_ok = 'Каталог. Адрес: улица Люблинская, 151. Телефон +7'
    assert kp.address_match(page_ok, 'улица Люблинская, 151') is True


def test_obshiy_phone_strict_one_digit_change_caught():
    """«Общий Город» виден на сайте напрямую и должен совпадать ТОЧНО. Смена
    одной цифры в КП (при том что старый номер остался в наборе all_phones) НЕ
    должна прятаться под «другой номер этого города» - это ✗. (Для SEO/Реклама
    подмену коллтрекинга по-прежнему засчитываем мягко.)"""
    m = kp.load_kp("smu")
    kp._KP_CACHE["smu"] = m
    row = m["stalmetural.ru"]
    saved = (row.phone_common, row.all_phones)
    # На сайте старый номер …69; в КП «Общий» изменён на …60, но all_phones старый.
    row.phone_common = "7 (499) 130-36-60"
    row.all_phones = "4991303669;9031303669"      # старый …69 ещё в наборе
    html = '<header><a href="tel:+74991303669">+7 (499) 130-36-69</a></header>'
    try:
        by = {f["field"]: f for f in kp.check_variables(html, "stalmetural.ru")["fields"]}
        assert by["Тел. Общий Город"]["status"] == "bug"
        assert by["Тел. Общий Город"]["note"] == "телефон на сайте не совпадает с КП"
    finally:
        (row.phone_common, row.all_phones) = saved
