"""
goals_run.py - фоновый прогон «Проверки целей» (страница панели запускает его
как отдельный процесс, вывод пишется в лог-файл).

Запуск:
    python goals_run.py --project smu [--show-browser]

Результат: cache/goals/<project>/goals_report.xlsx (листы «Сводка» и «Цели Метрики»).
"""
import argparse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description='Проверка целей Яндекс.Метрики.')
    ap.add_argument('--project', required=True, choices=['smu', 'imp', 'mpe'])
    ap.add_argument('--show-browser', action='store_true')
    a = ap.parse_args()

    import goals_tester as gt
    каталог = gt.загрузить_каталог(a.project)
    if not каталог:
        _stamp(f'✗ Нет каталога целей catalogs/goals-{a.project}.json')
        return 2

    _stamp(f'ПРОВЕРКА ЦЕЛЕЙ СТАРТ - {каталог.get("проект")} '
           f'(счётчик {каталог.get("счётчик")}, целей: {len(каталог.get("цели", []))})')

    try:
        from form_tester.stop_signal import make_stop_check
        import sys
        sys.path.insert(0, str(ROOT / 'forms_tester'))
        stop = make_stop_check()
    except Exception:
        stop = None

    прогон = gt.выполнить_прогон(a.project, headless=not a.show_browser,
                                 log=_stamp, stop=stop)
    _stamp(f'Сработавших идентификаторов: {len(прогон["fired"])}')

    out = ROOT / 'cache' / 'goals' / a.project / 'goals_report.xlsx'
    gt.построить_отчёт(a.project, каталог, прогон, out)
    _stamp(f'Отчёт: {out}')
    _stamp('✅ ВСЁ ГОТОВО')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
