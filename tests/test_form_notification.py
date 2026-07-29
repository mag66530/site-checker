"""Тест пункта 2.7: детектор уведомления пользователю после отправки формы.
Проверяется чистая функция-маркер (без браузера)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

# test_all тянет bs4/playwright на уровне модуля - если их нет, тест пропускаем.
t = pytest.importorskip("test_all")


class _FakeBtnTxt:
    """Мини-локатор для _текст_кнопки: inner_text + get_attribute."""
    def __init__(self, inner="", value="", aria=""):
        self._inner = inner
        self._attrs = {"value": value, "aria-label": aria}

    def inner_text(self, timeout=0):
        return self._inner

    def get_attribute(self, name):
        return self._attrs.get(name, "")


def test_текст_кнопки_читает_value_у_input_submit():
    # <button>Отправлено</button> - через inner_text.
    assert t._текст_кнопки(_FakeBtnTxt(inner="Отправлено")) == "Отправлено"
    # <input type=submit value="Отправлено"> - inner_text ПУСТОЙ, берём value.
    # Именно из-за этого тул раньше не видел смену кнопки на input-формах
    # (напр. «Срочный заказ») и ложно писал «нет подтверждения».
    assert t._текст_кнопки(_FakeBtnTxt(inner="", value="Отправлено")) == "Отправлено"
    # aria-label - последний фолбэк.
    assert t._текст_кнопки(_FakeBtnTxt(inner="", value="", aria="Отправить")) == "Отправить"
    # ничего нет → пустая строка (не падаем).
    assert t._текст_кнопки(_FakeBtnTxt()) == ""


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


# ── Разница «было/стало» вместо рубильника (ИМП: ложное «нет подтверждения») ──

def test_новый_текст_отсекает_то_что_было_до_отправки():
    до = "Оставить заявку\nМы свяжемся с вами в течение 15 минут"
    после = до + "\nСПАСИБО, ВАШ ЗАКАЗ ПРИНЯТ"
    новый = t._новый_текст(до, после)
    assert "спасибо" in новый.lower()
    # статический футер в «новый текст» не попал - ложного успеха он не даст
    assert "15 минут" not in новый
    assert not t._текст_подтверждает_отправку(t._новый_текст(до, до))


def test_живое_спасибо_не_теряется_из_за_статичного_футера():
    # Регрессия: на странице ЕЩЁ ДО отправки виден статический маркер
    # («мы свяжемся»), поэтому раньше ветка «текст успеха» глушилась целиком и
    # живое «Спасибо, заявка принята» после отправки уже не засчитывалось -
    # рабочая форма получала ✗ «НЕТ ПОДТВЕРЖДЕНИЯ» (весь отчёт ИМП был красный).
    до = "Мы свяжемся с вами в ближайшее время"
    page = _FakePage(body=до + "\nСпасибо, ваша заявка принята")
    res = t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=1000, текст_тела_до=до)
    assert res == "Да (текст)"


def test_статичный_маркер_без_новых_строк_не_даёт_ложный_успех():
    до = "Мы свяжемся с вами в ближайшее время"
    page = _FakePage(body=до)
    res = t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=500, текст_тела_до=до)
    assert res == "Нет"


def test_наблюдатель_даёт_да_даже_если_попап_уже_закрылся():
    # Наблюдатель зафиксировал подтверждение В МОМЕНТ появления; к опросу попап
    # уже закрыт (наши же повторные отправки его перебили) - вердикт «Да».
    page = _FakePage(body="Оставить заявку")
    res = t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=500,
        наблюдение="СПАСИБО, ВАШ ЗАКАЗ ПРИНЯТ")
    assert res == "Да (попап)"


def test_мусор_от_наблюдателя_не_считается_подтверждением():
    page = _FakePage(body="Оставить заявку")
    res = t.детект_уведомления_пользователю(
        page, "Отправить", "Отправить", таймаут_мс=500,
        наблюдение="Введите номер телефона")
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
