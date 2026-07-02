"""
forms_run.py – один фоновый процесс проверки форм для проекта.

Запускается страницей «Проверка форм» в фоне (как autoclick_run.py для
кликеров). Готовит рабочую папку cache/forms/<project>/, кладёт туда
config.py выбранного проекта и log_forms.xlsx, и гоняет движок форм-тестера
(forms_tester/test_all.py → run_test). Весь вывод идёт в stdout, который
вызывающая сторона перенаправляет в лог-файл.

Поддомены (города): если у проекта есть справочник forms_tester/projects/
<id>/cities.csv (город;url;почта), можно прогнать формы по выбранным городам.
Для каждого города подменяется поддомен в URL, а в отчёт пишутся колонки
«Город» и «Почта получателя» (куда должна прийти заявка).

Запуск:
    python forms_run.py --project smu
    python forms_run.py --project smu --cities "Москва,Санкт-Петербург,Казань"
"""
import argparse
import csv
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
ENGINE = ROOT / 'forms_tester'                  # пакет с движком (test_all, name_format, form_tester)
PROJECTS_ROOT = ENGINE / 'projects'             # forms_tester/projects/<id>/config.py
WORK_ROOT = ROOT / 'cache' / 'forms'            # рабочие папки прогонов (в .gitignore)

PROJECT_NAMES = {
    'smu': 'СМУ – Стальметурал',
    'imp': 'ИМП – Инметпром',
    'mpe': 'МПЭ – Мепэн',
    'mpe_cart': 'МПЭ – Корзина',
    'avia': 'АПС – Авиапромсталь',
}

# Проекты-варианты со своим config.py, но БЕЗ своего cities.csv – берут
# справочник городов у «родителя». Так «МПЭ – Корзина» гоняет те же города,
# что и Мепэн, без дублирования файла на 160 строк.
CITIES_FROM = {
    'mpe_cart': 'mpe',
}


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _load_cities(project: str):
    """Справочник городов проекта: [(город, url, почта), ...]. Пусто, если файла нет.
    Для проектов-вариантов (CITIES_FROM) берём справочник у родителя."""
    project = CITIES_FROM.get(project, project)
    f = PROJECTS_ROOT / project / 'cities.csv'
    if not f.is_file():
        return []
    out = []
    with open(f, encoding='utf-8', newline='') as fh:
        for row in csv.DictReader(fh):
            city = (row.get('город') or '').strip()
            url = (row.get('url') or '').strip().rstrip('/')
            mail = (row.get('почта') or '').strip()
            if city and url:
                out.append((city, url, mail))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='Прогон проверки форм для проекта.')
    ap.add_argument('--project', required=True, choices=list(PROJECT_NAMES),
                    help='Идентификатор проекта: smu / imp / mpe')
    ap.add_argument('--no-clear-excel', action='store_true',
                    help='Не очищать log_forms.xlsx перед прогоном')
    ap.add_argument('--show-browser', action='store_true',
                    help='Показывать окно браузера (по умолчанию скрыто, headless)')
    ap.add_argument('--cities', default='',
                    help='Список городов через запятую (из cities.csv). Пусто = основной сайт.')
    ap.add_argument('--forms-file', default='',
                    help='Путь к JSON-файлу со списком выбранных форм (имена). '
                         'Пусто = проверять все формы проекта.')
    a = ap.parse_args()

    name = PROJECT_NAMES[a.project]
    src_config = PROJECTS_ROOT / a.project / 'config.py'
    if not src_config.is_file():
        _stamp(f'✗ Нет файла конфигурации: {src_config}')
        return 2

    # Справочник городов и какие из них гнать
    cities_all = _load_cities(a.project)
    by_name = {c[0]: c for c in cities_all}
    main_host = urlparse(cities_all[0][1]).netloc if cities_all else ''   # домен основного сайта

    wanted = [c.strip() for c in a.cities.split(',') if c.strip()]
    if wanted and cities_all:
        run_cities = [by_name[c] for c in wanted if c in by_name]
    elif cities_all:
        run_cities = [cities_all[0]]                       # по умолчанию – основной город
    else:
        run_cities = [('', '', '')]                        # нет справочника – обычный прогон

    _stamp(f'ПРОВЕРКА ФОРМ СТАРТ – проект {name}'
           + (f' – городов: {len(run_cities)}' if run_cities and run_cities[0][0] else ''))

    work = WORK_ROOT / a.project
    work.mkdir(parents=True, exist_ok=True)
    base_config = src_config.read_text(encoding='utf-8')

    # Выбор форм из интерфейса: дописываем в конфиг список ТОЛЬКО_ФОРМЫ (движок
    # прогонит только формы с этими именами). Пустой/отсутствующий файл – гоним всё.
    forms_filter = []
    if a.forms_file:
        try:
            import json
            forms_filter = json.loads(Path(a.forms_file).read_text(encoding='utf-8')) or []
        except Exception as e:  # noqa: BLE001
            _stamp(f'⚠️ Не удалось прочитать список форм ({a.forms_file}): {e}')
            forms_filter = []
    if forms_filter:
        base_config = base_config.rstrip() + '\n\nТОЛЬКО_ФОРМЫ = ' + repr(list(forms_filter)) + '\n'
        _stamp(f'Выбрано форм: {len(forms_filter)} (остальные пропускаем).')

    # Базовый домен для подмены берём НЕ из первой строки cities.csv, а из самого
    # конфига: тот город-домен, что реально встречается в URL-ах СТРАНИЦ. Иначе если
    # первый город в справочнике – поддомен (напр. abakan.mepen.ru), а конфиг на
    # mepen.ru, замена не находила совпадения и все города шли на основной сайт (МПЭ).
    for _c in cities_all:
        _h = urlparse(_c[1]).netloc
        if _h and f'//{_h}' in base_config:
            main_host = _h
            break

    sys.path.insert(0, str(ENGINE))
    sys.path.insert(0, str(work))
    prev = os.getcwd()
    try:
        os.chdir(work)
    except OSError as e:
        _stamp(f'✗ Не удалось перейти в {work}: {e}')
        return 2

    rc = 0
    try:
        from form_tester.runner import run_test
        from form_tester.stop_signal import make_stop_check
        stop = make_stop_check()

        for i, (city, city_url, city_mail) in enumerate(run_cities):
            if stop():
                _stamp('⛔ Остановлено')
                break
            # Подменяем домен в конфиге под город (для Москвы/основного – без изменений)
            cfg = base_config
            if city and main_host:
                target = urlparse(city_url).netloc
                if target and target != main_host:
                    cfg = cfg.replace(f'//{main_host}', f'//{target}')
            (work / 'config.py').write_text(cfg, encoding='utf-8')

            if city:
                _stamp(f'── Город: {city}  ({city_url})  → заявка должна прийти на {city_mail or "?"} ──')

            run_test(
                ОЧИСТИТЬ_EXCEL=(not a.no_clear_excel and i == 0),   # чистим лог только перед первым
                stop_flag=stop,
                headless=not a.show_browser,
                город=city,
                почта_получателя=city_mail,
            )
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        import traceback
        _stamp(f'✗ Ошибка прогона: {e}')
        traceback.print_exc()
        rc = 1
    finally:
        try:
            os.chdir(prev)
        except OSError:
            pass

    if rc == 0:
        _stamp(f'Лог сохранён: {work / "log_forms.xlsx"}')
        _stamp('✅ ВСЁ ГОТОВО')
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
