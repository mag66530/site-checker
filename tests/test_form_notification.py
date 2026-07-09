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


class _FakeLoc:
    def __init__(self, count=0, text=""):
        self._count, self._text = count, text

    def count(self):
        return self._count

    def nth(self, i):
        return self

    def is_visible(self):
        return True

    def inner_text(self, timeout=0):
        return self._text


class _FakePage:
    """Мини-заглушка страницы: попапов нет, body отдаёт заданный текст."""
    def __init__(self, body=""):
        self._body, self.waits = body, 0

    def locator(self, sel):
        return _FakeLoc(1, self._body) if sel == "body" else _FakeLoc(0, "")

    def wait_for_timeout(self, ms):
        self.waits += 1


class _FakeBtn:
    """Кнопка, меняющая текст с задержкой: первые снимки - «Отправить»,
    затем «Отправлено» (как ajax-подтверждение через пару секунд)."""
    def __init__(self, seq):
        self.seq, self.i = seq, 0

    def inner_text(self, timeout=0):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v


def test_уведомление_ловится_при_поздней_смене_кнопки():
    # Кнопка становится «Отправлено» только на 3-м опросе - один снимок бы это
    # пропустил и записал «Нет». Опрос в окне времени ловит «Да (кнопка)».
    btn = _FakeBtn(["Отправить", "Отправить", "Отправлено"])
    page = _FakePage(body="")
    res = t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", кнопка=btn, таймаут_мс=3000)
    assert res == "Да (кнопка)"


def test_нет_уведомления_возвращает_нет():
    btn = _FakeBtn(["Отправить"])
    page = _FakePage(body="Введите номер телефона")
    res = t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", кнопка=btn, таймаут_мс=1000)
    assert res == "Нет"


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


def test_ссылка_ведёт_на_политику_2_13():
    assert t.ссылка_ведёт_на_политику("/politika-obrabotki-personalnyh-dannyh/", "")
    assert t.ссылка_ведёт_на_политику("#", "Политика обработки персональных данных")
    assert t.ссылка_ведёт_на_политику("/x", "даю согласие на обработку")
    assert t.ссылка_ведёт_на_политику("/privacy-policy", "подробнее")
    assert not t.ссылка_ведёт_на_политику("/catalog/", "Каталог")
    assert not t.ссылка_ведёт_на_политику("", "")


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
