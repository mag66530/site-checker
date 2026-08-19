"""Раздел каталога проекта: catalog_path в projects/<pid>.json.

У шести проектов каталог лежит по /catalog - там ничего меняться не должно.
У АПС разделы в корне (/chernyi-prokat), раздела /catalog нет вовсе, и запрос
к нему давал 404 по каждому городу выборки - ложная находка на ровном месте.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

from sources import Subdomain, Sources, build_plan, load_project_config


def _истчоники():
    return Sources(
        subdomains=[Subdomain(url='https://msk.site.ru/', city='Москва',
                              host='msk.site.ru', country='Россия')],
        categories=['/razdel/podrazdel'],
        filters=[],
    )


def _каталожные(plan):
    return [t.url for t in plan.tasks if t.type_code == 'catalog']


def test_по_умолчанию_как_раньше():
    """Ключа нет - поведение прежнее, со слешем."""
    plan = build_plan(_истчоники(), random_subdomains_count=0,
                      categories_per_subdomain=1)

    assert _каталожные(plan) == ['https://msk.site.ru/catalog/']
    print('✓ без ключа - прежний /catalog/')


def test_без_слеша_как_раньше():
    plan = build_plan(_истчоники(), random_subdomains_count=0,
                      categories_per_subdomain=1, trailing_slash=False)

    assert _каталожные(plan) == ['https://msk.site.ru/catalog']
    print('✓ trailing_slash=False - прежний /catalog')


def test_пустая_строка_убирает_задачу():
    plan = build_plan(_истчоники(), random_subdomains_count=0,
                      categories_per_subdomain=1, catalog_path='')

    assert _каталожные(plan) == []
    # Остальные проверки на месте - убрали ровно одну задачу.
    assert [t.type_code for t in plan.tasks] == ['main', 'category']
    print('✓ catalog_path="" - задачи «Каталог» нет, остальное цело')


def test_свой_путь_берётся_как_есть():
    plan = build_plan(_истчоники(), random_subdomains_count=0,
                      categories_per_subdomain=1, catalog_path='/produkciya/')

    assert _каталожные(plan) == ['https://msk.site.ru/produkciya/']
    print('✓ свой путь раздела уважается')


def test_раздела_нет_у_апс_и_мпк_а_у_остальных_есть():
    """Сверяем с реальными конфигами: правка адресная.

    МПК добавлен 18.08.2026 - у него разделы тоже в корне
    (/asbestovye-materialy), а /catalog отдаёт 302 на главную (проверено
    живьём), то есть задача «Каталог» ловила бы редирект вместо страницы."""
    for pid in ('avia', 'mpk'):
        assert load_project_config(pid).get('catalog_path') == '', pid
    for pid in ('imp', 'mpe', 'mpi', 'sm', 'smu'):
        cfg = load_project_config(pid)
        assert 'catalog_path' not in cfg, f'{pid}: ключ не должен был появиться'
    print('✓ ключ стоит только у АПС и МПК')


def test_каталоги_апс_и_мпк_действительно_не_под_catalog():
    """Причина правки, а не вкусовщина: в каталогах этих проектов нет ни одного
    адреса под /catalog - в отличие от остальных проектов."""
    import csv

    for pid in ('avia', 'mpk'):
        cfg = load_project_config(pid)
        путь = КОРЕНЬ / cfg['catalog_csv']
        with путь.open(encoding='utf-8') as f:
            адреса = [r['url'] for r in csv.DictReader(f)]

        assert адреса, f'каталог {pid} пуст'
        assert not [u for u in адреса if '/catalog/' in u], pid
        print(f'✓ ни один из {len(адреса)} адресов {pid} не лежит под /catalog')
