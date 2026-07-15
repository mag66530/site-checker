"""Тест CSRF-проверки (наличие токена/поля защиты сессии, если требуется).
Проверяется чистая функция-вердикт (без браузера)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

# test_all тянет bs4/playwright на уровне модуля - если их нет, тест пропускаем.
t = pytest.importorskip("test_all")


def test_токен_найден_и_заполнен_есть():
    статус, деталь = t.csrf_вердикт(найдено=True, заполнено=True)
    assert статус == "Есть"
    assert деталь


def test_токен_есть_но_пустой_проверить():
    статус, деталь = t.csrf_вердикт(найдено=True, заполнено=False)
    assert статус == "Проверить"
    assert деталь


def test_токен_не_найден_нет():
    статус, деталь = t.csrf_вердикт(найдено=False, заполнено=False)
    assert статус == "Нет"
    assert деталь


def test_ошибка_чтения_не_путается_с_отсутствием_токена():
    # Техническая ошибка (DOM не прочитать) - это НЕ доказательство, что
    # токена нет. Должно уходить в «Проверить», а не в «Нет».
    статус, деталь = t.csrf_вердикт(найдено=False, заполнено=False, ошибка=True)
    assert статус == "Проверить"
    статус2, _ = t.csrf_вердикт(найдено=True, заполнено=True, ошибка=True)
    assert статус2 == "Проверить"


class _FakeForm:
    """Мини-заглушка Playwright-локатора формы: evaluate() возвращает заданный
    результат или бросает исключение (эмуляция сбоя чтения DOM)."""
    def __init__(self, result=None, raise_error=False):
        self._result, self._raise = result, raise_error

    def evaluate(self, js):
        if self._raise:
            raise RuntimeError("страница уже закрыта")
        return self._result


def test_найти_csrf_поле_возвращает_результат_evaluate():
    form = _FakeForm({"найдено": True, "заполнено": True, "имя": "sessid"})
    r = t._найти_csrf_поле(form)
    assert r["найдено"] is True
    assert r["заполнено"] is True
    assert r["имя"] == "sessid"
    assert r["ошибка"] is False


def test_найти_csrf_поле_гасит_исключение_и_ставит_ошибку():
    form = _FakeForm(raise_error=True)
    r = t._найти_csrf_поле(form)
    assert r["найдено"] is False
    assert r["ошибка"] is True


# ── SameSite-cookie как второй механизм защиты (устраняет ложную «Нет») ──
def test_нет_токена_но_session_cookie_samesite_lax_есть():
    куки = t.csrf_куки_инфо([{"name": "PHPSESSID", "sameSite": "Lax"}])
    статус, деталь = t.csrf_вердикт(False, False, куки=куки)
    assert статус == "Есть"
    assert "SameSite" in деталь


def test_нет_токена_session_cookie_samesite_none_нет():
    # Явная SameSite=None + нет токена = реальная уязвимость.
    куки = t.csrf_куки_инфо([{"name": "BITRIX_SM_LOGIN", "sameSite": "None"}])
    статус, деталь = t.csrf_вердикт(False, False, куки=куки)
    assert статус == "Нет"
    assert "BITRIX_SM_LOGIN" in деталь


def test_samesite_не_задан_считается_защищённым():
    # Пустой/отсутствующий SameSite браузер трактует как Lax - не «Нет».
    куки = t.csrf_куки_инфо([{"name": "sessid", "sameSite": ""}])
    assert t.csrf_вердикт(False, False, куки=куки)[0] == "Есть"


def test_нет_сессионных_cookie_csrf_неприменим_есть():
    # Аналитические cookie (ga/ym) - не сессионные; подделывать нечего.
    куки = t.csrf_куки_инфо([{"name": "_ga", "sameSite": "None"},
                             {"name": "_ym_uid", "sameSite": "None"}])
    assert куки["сессионные_есть"] is False
    assert t.csrf_вердикт(False, False, куки=куки)[0] == "Есть"


def test_токен_приоритетнее_кук():
    # Заполненный токен - защита есть независимо от SameSite.
    куки = t.csrf_куки_инфо([{"name": "sessid", "sameSite": "None"}])
    assert t.csrf_вердикт(True, True, куки=куки)[0] == "Есть"


def test_куки_не_переданы_старое_поведение():
    # Без данных о cookie (чтение не удалось) - прежний вердикт по токену.
    assert t.csrf_вердикт(False, False, куки=None)[0] == "Нет"
    assert t.csrf_вердикт(True, True, куки=None)[0] == "Есть"


def test_несколько_сессионных_одна_none_нет():
    куки = t.csrf_куки_инфо([{"name": "PHPSESSID", "sameSite": "Lax"},
                             {"name": "auth_token", "sameSite": "None"}])
    assert куки["все_защищены_samesite"] is False
    assert t.csrf_вердикт(False, False, куки=куки)[0] == "Нет"


def test_колонка_csrf_в_шапке_лога():
    # колонка CSRF присутствует в заголовках и ключах лога, и они синхронны
    assert "CSRF-защита" in t.LOG_HEADERS
    assert "csrf_защита" in t.LOG_KEYS_ORDER
    assert len(t.LOG_HEADERS) == len(t.LOG_KEYS_ORDER)
    assert t.LOG_HEADERS.index("CSRF-защита") == t.LOG_KEYS_ORDER.index("csrf_защита")


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
