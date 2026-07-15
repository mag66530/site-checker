"""Тесты metrika_api.py - «Цель на 404 в Метрике» (регулярный мониторинг):
распознавание цели без сети. counter_goals()/has_404_goal() (реальные
HTTP-запросы) юнит-тестом не покрываются - см. смоук-тест."""
from metrika_api import _goal_looks_like_404


def test_реальная_цель_смu_имп_по_названию():
    # Реальная форма цели, уже настроенной у СМУ/ИМП в Метрике.
    goal = {
        'id': 313053844, 'name': '404', 'type': 'action',
        'conditions': [{'type': 'exact', 'url': '404error'}],
    }
    assert _goal_looks_like_404(goal) is True


def test_название_содержит_404_среди_текста():
    goal = {'id': 1, 'name': 'Ошибка 404 (страница не найдена)', 'conditions': []}
    assert _goal_looks_like_404(goal) is True


def test_404_только_внутри_идентификатора_не_в_названии():
    # Название не содержит «404» - но JS-идентификатор события содержит,
    # где бы он ни лежал в структуре (схема ответа API может отличаться по
    # типу цели - функция не завязана на конкретный ключ).
    goal = {
        'id': 2, 'name': 'Клик по ошибке', 'type': 'action',
        'conditions': [{'type': 'exact', 'url': '404error'}],
    }
    assert _goal_looks_like_404(goal) is True


def test_обычная_цель_не_похожа_на_404():
    goal = {
        'id': 3, 'name': 'Клик по телефону', 'type': 'action',
        'conditions': [{'type': 'exact', 'url': 'tel'}],
    }
    assert _goal_looks_like_404(goal) is False


def test_автоцель_без_404_не_похожа():
    goal = {'id': 4, 'name': 'Автоцель: отправка формы', 'type': 'auto',
            'conditions': []}
    assert _goal_looks_like_404(goal) is False


def test_id_содержащий_404_не_даёт_ложное_срабатывание():
    # id - большое произвольное число, может случайно содержать «404» -
    # это НЕ должно считаться найденной целью (иначе отчёт соврёт «есть»,
    # когда цели на самом деле нет).
    goal = {'id': 4041178, 'name': 'Клик по WhatsApp', 'type': 'action',
            'conditions': [{'type': 'exact', 'url': 'whatsapp'}]}
    assert _goal_looks_like_404(goal) is False


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
    import sys
    sys.exit(0 if ok == len(fns) else 1)
