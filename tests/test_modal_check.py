"""Тест «Модальные окна работают корректно (если есть)»: открывается/
закрывается. Проверяется логика через заглушки Playwright-объектов (по
образцу test_form_notification.py) - без реального браузера."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

# test_all тянет bs4/playwright на уровне модуля - если их нет, тест пропускаем.
t = pytest.importorskip("test_all")


class _FakeMissingLoc:
    """Локатор, которого нет (селектор не совпал ни с чем)."""
    def count(self):
        return 0

    def is_visible(self):
        return False

    def click(self, timeout=None, force=None):
        raise Exception("not found")  # noqa: BLE001

    @property
    def first(self):
        return self


class _CountingLoc:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self


class _FakeCloseBtn:
    def __init__(self, on_click):
        self._on_click = on_click

    def count(self):
        return 1

    def is_visible(self):
        return True

    def click(self, timeout=None, force=None):
        self._on_click()

    @property
    def first(self):
        return self


class _FakeModal:
    """Заглушка модалки: state['visible'] управляет _модалка_видна/_открылась
    и меняется «закрытием» (клик по кнопке / esc / клик вне - через page)."""
    def __init__(self, state, has_fields=True, close_selector=None,
                 box=(100, 100, 300, 200)):
        self._state = state
        self._has_fields = has_fields
        self._close_selector = close_selector
        self._box = box

    def count(self):
        return 1 if self._state["visible"] else 0

    def is_visible(self):
        return self._state["visible"]

    def locator(self, sel):
        if sel == "input, textarea, select":
            return _CountingLoc(1) if self._has_fields else _CountingLoc(0)
        if self._close_selector and sel == self._close_selector:
            return _FakeCloseBtn(lambda: self._state.__setitem__("visible", False))
        return _FakeMissingLoc()

    def bounding_box(self):
        x, y, w, h = self._box
        return {"x": x, "y": y, "width": w, "height": h}


class _FakeKeyboard:
    def __init__(self, closes):
        self._closes = closes

    def press(self, key):
        if key == "Escape" and self._closes:
            self._closes()


class _FakeMouse:
    def __init__(self, closes):
        self._closes = closes

    def click(self, x, y):
        if self._closes:
            self._closes()


class _FakePage:
    def __init__(self, state, esc_closes=False, outside_click_closes=False):
        self.viewport_size = {"width": 800, "height": 600}
        self.keyboard = _FakeKeyboard(
            (lambda: state.__setitem__("visible", False)) if esc_closes else None)
        self.mouse = _FakeMouse(
            (lambda: state.__setitem__("visible", False)) if outside_click_closes else None)

    def wait_for_timeout(self, ms):
        pass


# ── _модалка_видна / _модалка_открылась ───────────────────────────────────

def test_модалка_видна_с_полями():
    state = {"visible": True}
    modal = _FakeModal(state, has_fields=True)
    assert t._модалка_видна(modal) is True
    assert t._модалка_открылась(modal) is True


def test_модалка_видна_но_без_полей_не_открылась():
    # Видимый контейнер БЕЗ полей - типичный пустой fallback _find_modal_root().
    state = {"visible": True}
    modal = _FakeModal(state, has_fields=False)
    assert t._модалка_видна(modal) is True
    assert t._модалка_открылась(modal) is False


def test_модалка_невидима():
    state = {"visible": False}
    modal = _FakeModal(state, has_fields=True)
    assert t._модалка_видна(modal) is False
    assert t._модалка_открылась(modal) is False


# ── _проба_закрытия_модалки: три способа, жёсткий вердикт ─────────────────

def test_закрывается_крестиком():
    state = {"visible": True}
    modal = _FakeModal(state, close_selector=".modal-close")
    page = _FakePage(state)
    статус, способ = t._проба_закрытия_модалки(page, modal)
    assert статус == "Да"
    assert способ == "крестик/кнопка закрытия"


def test_закрывается_esc_когда_крестика_нет():
    state = {"visible": True}
    modal = _FakeModal(state, close_selector=None)
    page = _FakePage(state, esc_closes=True)
    статус, способ = t._проба_закрытия_модалки(page, modal)
    assert статус == "Да"
    assert способ == "клавиша Esc"


def test_закрывается_кликом_вне_когда_остальное_не_работает():
    state = {"visible": True}
    modal = _FakeModal(state, close_selector=None)
    page = _FakePage(state, esc_closes=False, outside_click_closes=True)
    статус, способ = t._проба_закрытия_модалки(page, modal)
    assert статус == "Да"
    assert способ == "клик вне модалки"


def test_не_закрывается_ничем_жёсткое_нет_без_проверить_вручную():
    # Ни один из трёх способов не сработал - вердикт «Нет», НЕ «проверить
    # вручную» (по явной просьбе - тул сам должен дойти до ответа).
    state = {"visible": True}
    modal = _FakeModal(state, close_selector=None)
    page = _FakePage(state, esc_closes=False, outside_click_closes=False)
    статус, способ = t._проба_закрытия_модалки(page, modal)
    assert статус == "Нет"
    assert "вручную" not in способ
    assert способ == "не закрылась ни крестиком, ни Esc, ни кликом вне модалки"


def test_уже_не_видна_до_проверки():
    # Окно исчезло само сразу после отправки (частый попап-кейс) - тестировать
    # закрытие было НЕ на чем. Это НЕ дефект «не закрывается»: раньше тут стоял
    # ложный ✗, теперь честное «Проверить» (⚠) с пояснением, что окно закрылось
    # после отправки. По ⚠ пользователь не примет рабочую модалку за сломанную.
    state = {"visible": False}
    modal = _FakeModal(state)
    page = _FakePage(state)
    статус, способ = t._проба_закрытия_модалки(page, modal)
    assert статус == "Проверить"
    assert "после отправки" in способ
    assert "закрытие работает" in способ


# ── _найти_модалку_вокруг ─────────────────────────────────────────────────
# XPath ancestor:: от локатора ФОРМЫ (не .filter(has=form) от page!) - см.
# комментарий у _MODAL_ANCESTOR_XPATH в test_all.py про то, почему именно так:
# .filter(has=form) переоценивает id-заякоренный селектор формы ВНУТРИ
# кандидата и часто не находит ничего, даже когда форма реально внутри.

class _FakeAncestorResult:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self


class _FakeForm:
    def __init__(self, n_found):
        self._n = n_found

    def locator(self, sel):
        return _FakeAncestorResult(self._n)


def test_форма_вне_модалки_none():
    form = _FakeForm(n_found=0)
    assert t._найти_модалку_вокруг(form) is None


def test_форма_внутри_модалки_найдена():
    form = _FakeForm(n_found=1)
    assert t._найти_модалку_вокруг(form) is not None


def test_детектор_модалки_ловит_fancybox_и_aria():
    # XPath расширен на типовые попап-библиотеки и aria-modal - иначе форма в
    # fancybox/magnific/lightbox шла прочерком «Модалка открывается».
    x = t._MODAL_ANCESTOR_XPATH.lower()
    for маркер in ("modal", "popup", "fancybox", "mfp", "lightbox", "aria-modal"):
        assert маркер in x, маркер


def test_детектор_модалки_учитывает_сам_элемент():
    # Локатор формы иногда указывает на САМ popup-контейнер (Bitrix div#txt-back
    # с class="popup"), а не на внутренний <form>. ancestor-OR-SELF ловит и этот
    # случай - иначе модалка шла прочерком «Модалка открывается».
    assert "ancestor-or-self" in t._MODAL_ANCESTOR_XPATH


# ── Колонки в шапке лога ───────────────────────────────────────────────

def test_колонки_модалок_в_шапке_лога():
    for заголовок, ключ in (
        ("Модалка открывается", "модалка_открылась"),
        ("Модалка закрывается", "модалка_закрывается"),
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
