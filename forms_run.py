"""
forms_run.py – один фоновый процесс проверки форм для проекта.

Запускается страницей «Проверка форм» в фоне (как autoclick_run.py для
кликеров). Готовит рабочую папку cache/forms/<project>/, кладёт туда
config.py выбранного проекта и log_forms.xlsx, и гоняет движок форм-тестера
(forms_tester/test_all.py → run_test). Весь вывод идёт в stdout, который
вызывающая сторона перенаправляет в лог-файл.

Запуск (обычно из страницы в фоне):
    python forms_run.py --project smu
"""
import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
ENGINE = ROOT / 'forms_tester'                  # пакет с движком (test_all, name_format, form_tester)
PROJECTS_ROOT = ENGINE / 'projects'             # forms_tester/projects/<id>/config.py
WORK_ROOT = ROOT / 'cache' / 'forms'            # рабочие папки прогонов (в .gitignore)

PROJECT_NAMES = {
    'smu': 'СМУ – Сталметурал',
    'imp': 'ИМП – Инметпром',
    'mpe': 'МПЭ – Мепэн',
}


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description='Прогон проверки форм для проекта.')
    ap.add_argument('--project', required=True, choices=list(PROJECT_NAMES),
                    help='Идентификатор проекта: smu / imp / mpe')
    ap.add_argument('--no-clear-excel', action='store_true',
                    help='Не очищать log_forms.xlsx перед прогоном')
    ap.add_argument('--show-browser', action='store_true',
                    help='Показывать окно браузера (по умолчанию скрыто, headless)')
    a = ap.parse_args()

    name = PROJECT_NAMES[a.project]
    _stamp(f'ПРОВЕРКА ФОРМ СТАРТ – проект {name}')

    src_config = PROJECTS_ROOT / a.project / 'config.py'
    if not src_config.is_file():
        _stamp(f'✗ Нет файла конфигурации: {src_config}')
        return 2

    work = WORK_ROOT / a.project
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_config, work / 'config.py')

    # Движок: test_all / name_format / form_tester ищутся как модули верхнего
    # уровня → кладём ENGINE в путь. config.py берётся из рабочей папки → её в путь.
    sys.path.insert(0, str(ENGINE))
    sys.path.insert(0, str(work))

    prev = os.getcwd()
    try:
        os.chdir(work)            # log_forms.xlsx пишется здесь, config.py отсюда же
    except OSError as e:
        _stamp(f'✗ Не удалось перейти в {work}: {e}')
        return 2

    try:
        from form_tester.runner import run_test
        from form_tester.stop_signal import make_stop_check

        stop = make_stop_check()  # отмена – через kill процесса со стороны страницы
        run_test(ОЧИСТИТЬ_EXCEL=not a.no_clear_excel, stop_flag=stop,
                 headless=not a.show_browser)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        import traceback
        _stamp(f'✗ Ошибка прогона: {e}')
        traceback.print_exc()
        return 1
    finally:
        try:
            os.chdir(prev)
        except OSError:
            pass

    _stamp(f'Лог сохранён: {work / "log_forms.xlsx"}')
    _stamp('✅ ВСЁ ГОТОВО')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
