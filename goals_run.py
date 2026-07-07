"""
goals_run.py - фоновый прогон «Проверки целей» (страница панели запускает его
как отдельный процесс, вывод пишется в лог-файл).

Запуск:
    python goals_run.py --projects smu,smu-uz [--with-forms] [--show-browser]
    python goals_run.py --project smu           # обратная совместимость (один)

Результат: cache/goals/<project>/goals_report.xlsx (листы «Сводка» и «Цели Метрики»)
для КАЖДОЙ выбранной страны.
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _прогнать_формы(base: str, show: bool) -> None:
    """Синхронно прогоняет «Проверку форм» для базового проекта (Москва) - чтобы
    цели отправки форм реально сработали и подтянулись в отчёт целей. Реальные
    заявки отправляются, поэтому делаем только по запросу (--with-forms)."""
    _stamp(f'ФОРМЫ: запускаю прогон форм для «{base}» (Москва) - поймать цели форм')
    args = [sys.executable, 'forms_run.py', '--project', base, '--no-admin']
    if show:
        args.append('--show-browser')
    try:
        p = subprocess.run(args, cwd=str(ROOT), timeout=1800)
        _stamp(f'ФОРМЫ: готово (код {p.returncode})')
    except Exception as e:  # noqa: BLE001
        _stamp(f'ФОРМЫ: не удалось прогнать ({e}) - продолжаю без них')


_МЕТКИ = {'': 'РФ', 'uz': 'УЗ', 'az': 'АЗ', 'az2': 'АЗ-перевод', 'am': 'АМ',
          'kg': 'КГ', 'kz': 'КЗ', 'rb': 'РБ'}


def _метка(pid: str) -> str:
    suf = pid.split('-', 1)[1] if '-' in pid else ''
    return _МЕТКИ.get(suf, suf.upper() or 'РФ')


def main() -> int:
    ap = argparse.ArgumentParser(description='Проверка целей Яндекс.Метрики.')
    # project(s) = коды каталогов: базовый (smu/imp/mpe) или страны (smu-uz…).
    ap.add_argument('--projects', help='несколько через запятую: smu,smu-uz,smu-az')
    ap.add_argument('--project', help='один проект (обратная совместимость)')
    ap.add_argument('--with-forms', action='store_true',
                    help='сначала прогнать формы, чтобы поймать цели отправки форм')
    ap.add_argument('--show-browser', action='store_true')
    a = ap.parse_args()

    projects = []
    for src in (a.projects or ''), (a.project or ''):
        for p in src.split(','):
            p = p.strip()
            if p and p not in projects:
                projects.append(p)
    if not projects:
        _stamp('✗ Не заданы проекты (--projects smu,smu-uz)')
        return 2

    import goals_tester as gt

    _stamp(f'ПРОВЕРКА ЦЕЛЕЙ СТАРТ - сайтов: {len(projects)} '
           f'({", ".join(projects)})')

    try:
        sys.path.insert(0, str(ROOT / 'forms_tester'))
        from form_tester.stop_signal import make_stop_check
        stop = make_stop_check()
    except Exception:
        stop = None

    # Формы прогоняем один раз на базовый проект (цели форм общие для всех стран).
    if a.with_forms:
        bases = []
        for p in projects:
            b = gt._базовый(p)
            if b not in bases:
                bases.append(b)
        for b in bases:
            if stop and stop():
                break
            _прогнать_формы(b, a.show_browser)

    результаты = []
    поймано = 0
    for i, pid in enumerate(projects, 1):
        if stop and stop():
            _stamp('⛔ Остановлено')
            break
        каталог = gt.загрузить_каталог(pid)
        if not каталог:
            _stamp(f'✗ Нет каталога целей catalogs/goals-{pid}.json')
            continue
        _stamp(f'СТРАНА {i}/{len(projects)}: {каталог.get("проект")} '
               f'(счётчик {каталог.get("счётчик")}, целей: {len(каталог.get("цели", []))})')
        прогон = gt.выполнить_прогон(pid, headless=not a.show_browser,
                                     log=_stamp, stop=stop)
        _stamp(f'  сработавших идентификаторов: {len(прогон["fired"])}')
        поймано += len(прогон['fired'])
        результаты.append((pid, каталог, прогон, _метка(pid)))

    # Один сводный отчёт: лист «Сводка» + по листу целей на каждый сайт.
    base = gt._базовый(projects[0])
    out = ROOT / 'cache' / 'goals' / base / 'goals_report.xlsx'
    if результаты:
        gt.построить_сводный_отчёт(результаты, out)
        _stamp(f'Отчёт (сводный, {len(результаты)} лист(ов) целей): {out}')

    _stamp(f'Всего сработавших идентификаторов по сайтам: {поймано}')
    _stamp('✅ ВСЁ ГОТОВО')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
