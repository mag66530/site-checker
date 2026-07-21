"""Тест фильтра загрузки файлов (правило «только PDF/DOC/DOCX ≤20 КБ»).
Чистая функция-вердикт, генератор файла заданного размера и наличие .doc в
списке проб - всё без браузера."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))
t = pytest.importorskip("test_all")


def test_правило_pdf_doc_docx_20kb():
    assert set(t._ФАЙЛ_РАЗРЕШЁННЫЕ) == {".pdf", ".doc", ".docx"}
    assert t._ФАЙЛ_МАКС_КБ == 20


def test_doc_и_docx_и_pdf_в_списке_проб():
    exts = {e for e, _ in t._ПРОБА_ТИПЫ}
    assert {".pdf", ".doc", ".docx"} <= exts, exts


def _v(**res):
    base = {"принятые_опасные": [], "принятые_обычные": [], "большой_принят": None}
    base.update(res)
    return t.фильтр_файлов_вердикт(base)


def _sym(кол):
    return t._матрица_классифицировать("Типы файлов формы", кол)[0]


def test_опасный_тип_крест():
    кол, _ = _v(принятые_опасные=[".php"])
    assert кол.startswith("✗")
    assert _sym(кол) == "✗"


def test_посторонний_формат_крест():
    кол, дет = _v(принятые_обычные=[".pdf", ".jpg", ".zip"])
    assert кол.startswith("✗") and "посторонн" in кол.lower()
    assert ".jpg" in дет and ".zip" in дет and ".pdf" not in дет  # pdf разрешён
    assert _sym(кол) == "✗"


def test_большой_файл_принят_крест():
    кол, _ = _v(принятые_обычные=[".pdf"], большой_принят=True)
    assert кол.startswith("✗") and "больше" in кол.lower()
    assert _sym(кол) == "✗"


def test_только_разрешённые_и_лимит_есть_галочка():
    # Приняты только PDF/DOCX, большой (>20 КБ) отклонён → корректно (✓).
    кол, _ = _v(принятые_обычные=[".pdf", ".docx"], большой_принят=False)
    assert кол.startswith("корректно")
    assert _sym(кол) == "✓"


def test_формат_ок_но_размер_не_проверен_внимание():
    # Формат правильный, но лимит автоматически не проверен (None) → ⚠.
    кол, _ = _v(принятые_обычные=[".pdf"], большой_принят=None)
    assert "проверить" in кол.lower()
    assert _sym(кол) == "⚠"


def test_ничего_не_принято_внимание():
    кол, _ = _v()
    assert "проверить" in кол.lower()
    assert _sym(кол) == "⚠"


def test_генератор_файла_нужного_размера():
    big = t._безвредный_файл(".pdf", 25 * 1024)
    assert os.path.getsize(big) >= 25 * 1024
    small = t._безвредный_файл(".pdf")            # без размера - крошечный
    assert os.path.getsize(small) < 1024
    assert big != small                            # кэш отдельный по размеру


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f"✓ {fn.__name__}"); ok += 1
        except Exception:
            print(f"✗ {fn.__name__}"); traceback.print_exc()
    print(f"\n{ok}/{len(fns)} прошло")
    sys.exit(0 if ok == len(fns) else 1)
