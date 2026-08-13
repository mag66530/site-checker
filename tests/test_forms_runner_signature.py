"""Обёртка form_tester.runner.run_test должна принимать то же, что движок.

Список аргументов в обёртке продублирован руками, и новый параметр движка
легко забыть: так «потоков» доехал до движка, но не до обёртки, и боевой
прогон МПЭ упал сразу после старта -
«run_test() got an unexpected keyword argument 'потоков'».
"""
import inspect
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / 'forms_tester'))

from form_tester.runner import run_test as обёртка
from test_all import run_test as движок


def test_подписи_совпадают():
    п_обёртки = list(inspect.signature(обёртка).parameters)
    п_движка = list(inspect.signature(движок).parameters)

    забыли = [p for p in п_движка if p not in п_обёртки]
    лишние = [p for p in п_обёртки if p not in п_движка]

    assert not забыли, (
        f'в обёртке form_tester/runner.py не хватает аргументов движка: '
        f'{забыли} - прогон упадёт на «unexpected keyword argument»')
    assert not лишние, f'обёртка принимает то, чего движок не знает: {лишние}'
    print(f'✓ обёртка принимает все {len(п_движка)} аргументов движка')


def test_потоки_доезжают_до_движка(monkeypatch):
    """Значение не просто принимается, а передаётся дальше."""
    поймали = {}

    def _подделка(**kw):
        поймали.update(kw)
        return 'ок'

    import test_all
    monkeypatch.setattr(test_all, 'run_test', _подделка)

    assert обёртка(потоков=4) == 'ок'
    assert поймали.get('потоков') == 4
    print('✓ «потоков» доходит до движка, а не теряется в обёртке')
