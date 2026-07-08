"""Тест пункта 2.7: детектор уведомления пользователю после отправки формы.
Проверяется чистая функция-маркер (без браузера)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

# test_all тянет bs4/playwright на уровне модуля - если их нет, тест пропускаем.
t = pytest.importorskip("test_all")


def test_маркеры_подтверждения_срабатывают():
    ok = [
        "Спасибо, ваша заявка принята!",
        "Заявка успешно отправлена",
        "Мы свяжемся с вами в ближайшее время",
        "Благодарим за обращение",
        "Ваша заявка получена",
        "заявка отправлена",           # текст сменившейся кнопки
        "ЗаяВка ПринЯта",              # регистр
        "Заявка принята в обработку",
    ]
    for s in ok:
        assert t._текст_подтверждает_отправку(s), s


def test_нет_ложных_срабатываний():
    no = [
        "Отправить",
        "Введите номер телефона",
        "Ошибка отправки формы",
        "Оставьте заявку",             # призыв, а не подтверждение
        "",
        None,
    ]
    for s in no:
        assert not t._текст_подтверждает_отправку(s), s


def test_извлечь_цели_из_запроса_get_и_post():
    # GET: goal:// закодирован в URL
    u = ("https://mc.yandex.ru/watch/123?page-url="
         "goal%3A%2F%2Fstalmetural.ru%2Ffindtome&x=1")
    assert t._извлечь_цели_из_запроса(u) == ["findtome"]
    # POST/sendBeacon: URL без goal, цель в ТЕЛЕ запроса (главный кейс фикса)
    body = "page-url=goal%3A%2F%2Fstalmetural.ru%2Ffindtome&site-info="
    assert t._извлечь_цели_из_запроса("https://mc.yandex.ru/watch/123", body) == ["findtome"]
    # не запрос Метрики - пусто
    assert t._извлечь_цели_из_запроса("https://stalmetural.ru/catalog/", body) == []
    # уже раскодированный goal:// в URL
    assert t._извлечь_цели_из_запроса(
        "https://mc.webvisor.com/watch/1?p=goal://x.ru/zakaz-proscheta") == ["zakaz-proscheta"]


def test_колонка_в_шапке_лога():
    # колонка 2.7 присутствует в заголовках и ключах лога, и они синхронны
    assert "Уведомление пользователю" in t.LOG_HEADERS
    assert "уведомление" in t.LOG_KEYS_ORDER
    assert len(t.LOG_HEADERS) == len(t.LOG_KEYS_ORDER)
    assert t.LOG_HEADERS.index("Уведомление пользователю") == \
        t.LOG_KEYS_ORDER.index("уведомление")


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
