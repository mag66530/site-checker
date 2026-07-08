"""Тесты privacy_check.py (пункт 2.12) - чистые детекторы cookie/политики/чата."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

import privacy_check as pc  # noqa: E402  (чистый импорт: только re/datetime)


def test_текст_про_cookie():
    assert pc.текст_про_cookie("Этот сайт использует файлы cookie")
    assert pc.текст_про_cookie("Мы используем куки")
    assert pc.текст_про_cookie("We use cookies")
    assert not pc.текст_про_cookie("Оставьте заявку")
    assert not pc.текст_про_cookie("")


def test_ссылка_на_политику():
    # по href
    assert pc.ссылка_на_политику("/politika-konfidencialnosti/", "файлы cookie")
    assert pc.ссылка_на_политику("https://x.ru/privacy", "подробнее")
    assert pc.ссылка_на_политику("/personal-data/", "")
    # по тексту ссылки
    assert pc.ссылка_на_политику("#", "Политика конфиденциальности")
    assert pc.ссылка_на_политику("/x", "обработка персональных данных")
    # не политика
    assert not pc.ссылка_на_политику("/catalog/", "Каталог")
    assert not pc.ссылка_на_политику("", "")


def test_html_содержит_живочат():
    assert pc.html_содержит_живочат('<script src="//code.jivo.ru/w.js"></script>')
    assert pc.html_содержит_живочат('<div class="jdiv">...</div>')
    assert pc.html_содержит_живочат('<div>Онлайн-консультант</div>')
    assert pc.html_содержит_живочат('<script>verbox</script>')
    assert not pc.html_содержит_живочат('<div>Обычная страница без чата</div>')
    assert not pc.html_содержит_живочат('')


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
