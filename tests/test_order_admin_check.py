"""Тесты order_admin_check.py - разбор списка «Заказы» Bitrix и сопоставление."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "forms_tester"))

import order_admin_check as oac  # noqa: E402

# Мини-таблица «Заказы» в формате Bitrix (строки adm-list-table-row; в реальной
# вёрстке дата-время и «№…» лежат во вложенных ячейках - берём регулярками).
BITRIX = """
<table class="adm-list-table">
  <tr class="adm-list-table-header"><td>Дата создания</td><td>ID</td></tr>
  <tr class="adm-list-table-row">
     <td><input type="checkbox"></td><td></td>
     <td>08.07.2026 16:16:40</td><td></td><td><a href="sale_order_view.php?ID=2691&lang=ru">№2691</a></td>
  </tr>
  <tr class="adm-list-table-row">
     <td></td><td>07.07.2026 09:00:00</td><td><a>№2690</a></td>
  </tr>
  <tr class="adm-list-table-row"><td>строка без даты - пропускаем</td></tr>
</table>
"""


def test_построить_url():
    u = oac.построить_url_списка("https://stalmetural.ru/")
    assert u == "https://stalmetural.ru/bitrix/admin/sale_order.php?lang=ru&PAGEN_1=1&SIZEN_1=100"
    assert "SIZEN_1=50" in oac.построить_url_списка("https://x.ru", размер=50)


def test_разобрать_заказы():
    z = oac.разобрать_заказы(BITRIX)
    assert len(z) == 2, z
    assert z[0]["id"] == "2691"
    assert z[0]["дата_время"] == "08.07.2026 16:16:40"
    assert z[1]["id"] == "2690"
    assert z[1]["время"] == "09:00:00"


def test_сопоставить_в_окне():
    # наш заказ оформлен 08.07.2026 16:16:38, в админке 16:16:40 (через 2 сек) → найден
    заказы = oac.разобрать_заказы(BITRIX)
    оформленные = [{"город": "Москва", "название": "Оформление заказа",
                    "ts": "2026-07-08T16:16:38"}]
    res, свободные = oac.сопоставить(заказы, оформленные)
    assert res[0]["статус"] == "Есть в админке"
    assert res[0]["заказ"]["id"] == "2691"


def test_сопоставить_вне_окна():
    # заказ оформлен на час раньше любого в списке → НЕ найдено
    заказы = oac.разобрать_заказы(BITRIX)
    оформленные = [{"город": "Москва", "название": "Оформление заказа",
                    "ts": "2026-07-08T10:00:00"}]
    res, _ = oac.сопоставить(заказы, оформленные)
    assert res[0]["статус"] == "НЕ найдено"


def test_сопоставить_каждый_заказ_один_раз():
    заказы = oac.разобрать_заказы(BITRIX)
    # два наших заказа почти в одно время - не должны занять один и тот же заказ админки
    оформленные = [
        {"город": "Москва", "название": "A", "ts": "2026-07-08T16:16:39"},
        {"город": "Москва", "название": "B", "ts": "2026-07-08T16:16:41"},
    ]
    res, _ = oac.сопоставить(заказы, оформленные)
    найдены = [r for r in res if r["статус"].startswith("Есть")]
    ids = {r["заказ"]["id"] for r in найдены}
    assert len(ids) == len(найдены)  # без повторного использования одного заказа


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
