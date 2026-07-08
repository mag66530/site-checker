"""Тесты order_mail_check.py - чистая логика поиска письма о заказе (без сети)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from order_mail_check import (  # noqa: E402
    извлечь_адрес, декодировать_заголовок, дата_имап, разобрать_дату_письма,
    похоже_на_письмо_заказа, _хост_бренд, выбрать_подтверждение, _parse_ts,
)


def test_извлечь_адрес():
    assert извлечь_адрес("Магазин <no-reply@stalmetural.ru>") == "no-reply@stalmetural.ru"
    assert извлечь_адрес("test111@yandex.ru") == "test111@yandex.ru"
    assert извлечь_адрес("") == ""


def test_декодировать_заголовок_mime():
    # =?UTF-8?B?...?= - «Ваш заказ»
    raw = "=?UTF-8?B?0JLQsNGIINC30LDQutCw0Lc=?="
    assert декодировать_заголовок(raw) == "Ваш заказ"
    assert декодировать_заголовок("Plain subject") == "Plain subject"
    assert декодировать_заголовок(None) == ""


def test_дата_имап_английский_месяц():
    # берём день раньше и всегда английский месяц (не зависит от локали)
    assert дата_имап(datetime(2026, 7, 8, 12, 0, 0)) == "07-Jul-2026"
    assert дата_имап(datetime(2026, 1, 1, 0, 0, 0)) == "31-Dec-2025"
    assert дата_имап(datetime(2026, 3, 1, 9, 0, 0)) == "28-Feb-2026"


def test_разобрать_дату_письма():
    dt = разобрать_дату_письма("Wed, 08 Jul 2026 12:34:56 +0000")
    assert dt is not None
    # tz снят - сравниваем как naive
    assert dt.tzinfo is None
    assert разобрать_дату_письма("") is None
    assert разобрать_дату_письма("мусор") is None


def test_похоже_на_письмо_заказа():
    assert похоже_на_письмо_заказа("Ваш заказ №123 оформлен", "shop@x.ru")
    assert похоже_на_письмо_заказа("Спасибо за покупку", "no-reply@x.ru")
    assert похоже_на_письмо_заказа("Заказ принят", "")
    # ё → е в маркере/теме
    assert похоже_на_письмо_заказа("Заказ офоРМлЕн", "")
    # постороннее письмо - не заказ
    assert not похоже_на_письмо_заказа("Новости компании", "news@x.ru")
    assert not похоже_на_письмо_заказа("Скидка 20%", "promo@x.ru")


def test_хост_бренд():
    assert _хост_бренд("https://stalmetural.uz/catalog/x") == "stalmetural"
    assert _хост_бренд("https://osh.stalmetural.kg/") == "stalmetural"
    assert _хост_бренд("inmetprom.ru") == "inmetprom"
    assert _хост_бренд("https://mepen.ru:443/basket/") == "mepen"


def _письмо(subj, when, frm="no-reply@stalmetural.ru"):
    return {"subject": subj, "from": frm, "date": when}


def test_выбрать_подтверждение_в_окне():
    момент = datetime(2026, 7, 8, 12, 0, 0)
    письма = [
        _письмо("Рассылка", момент + timedelta(minutes=1)),           # не заказ
        _письмо("Ваш заказ №777 оформлен", момент + timedelta(minutes=3)),  # ← оно
        _письмо("Заказ №1 оформлен", момент - timedelta(hours=2)),     # вне окна (рано)
    ]
    res = выбрать_подтверждение(письма, момент, домен="https://stalmetural.ru/x")
    assert res is not None
    assert "777" in res["subject"]


def test_выбрать_подтверждение_предпочитает_бренд():
    момент = datetime(2026, 7, 8, 12, 0, 0)
    письма = [
        _письмо("Заказ оформлен", момент + timedelta(minutes=10), frm="no-reply@othershop.com"),
        _письмо("Заказ оформлен на mepen", момент + timedelta(minutes=12), frm="shop@mepen.ru"),
    ]
    res = выбрать_подтверждение(письма, момент, домен="https://mepen.ru/basket/")
    assert res is not None
    # предпочли письмо с брендом mepen, хоть оно и позже по времени
    assert "mepen" in res["subject"].lower() or "mepen" in res["from"].lower()


def test_выбрать_подтверждение_ничего():
    момент = datetime(2026, 7, 8, 12, 0, 0)
    письма = [_письмо("Обычное письмо", момент + timedelta(minutes=5))]
    assert выбрать_подтверждение(письма, момент, домен="") is None
    # заказ, но далеко за окном (через 2 часа)
    поздно = [_письмо("Заказ оформлен", момент + timedelta(hours=2))]
    assert выбрать_подтверждение(поздно, момент, домен="") is None


def test_parse_ts():
    assert _parse_ts("2026-07-08T12:00:00") == datetime(2026, 7, 8, 12, 0, 0)
    assert _parse_ts("") is None
    assert _parse_ts(None) is None


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn()
            print(f"✓ {fn.__name__}")
            ok += 1
        except Exception:
            print(f"✗ {fn.__name__}")
            traceback.print_exc()
    print(f"\n{ok}/{len(fns)} прошло")
    sys.exit(0 if ok == len(fns) else 1)
