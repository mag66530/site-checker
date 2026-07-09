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


def _прогнать_формы(base: str, show: bool, only_orders: bool = False) -> None:
    """Синхронно прогоняет «Проверку форм» для базового проекта (Москва) - чтобы
    цели отправки форм реально сработали и подтянулись в отчёт целей.

    only_orders=True - гоним ТОЛЬКО сквозной заказ (корзина → оформление): так
    «Проверка целей» сама подтверждает заказ-цели, не отправляя лишние формы
    (по умолчанию для проверки целей). only_orders=False - полный прогон форм.

    ВСЕ сработавшие при формах цели (в т.ч. те, что форм-движок пишет только в
    лог, а не в лист «Цели») вылавливаем прямо из вывода и сохраняем в
    cache/forms/<base>/fired_goals.json - отчёт целей их подхватит."""
    import re
    import json
    _что = 'заказ (корзина → оформление)' if only_orders else 'все формы'
    _stamp(f'ФОРМЫ: запускаю прогон ({_что}) для «{base}» (Москва) - поймать цели')
    args = [sys.executable, 'forms_run.py', '--project', base, '--no-admin']
    if only_orders:
        args.append('--only-orders')
    if show:
        args.append('--show-browser')
    _pat1 = re.compile(r'зафиксирована цель [«"]([\w\-.]+)[»"]')
    _pat2 = re.compile(r'Сработала цель:\s*([\w\-.]+)')
    # URL, до которых дошёл прогон форм (переходы + итоговый URL сценария): по ним
    # «Проверка целей» подтверждает url-цели (оформленный заказ / «спасибо»).
    _patu = re.compile(r'(?:URL сценария:|переход →)\s*(https?://\S+)')
    fired: set = set()
    urls: set = set()
    try:
        proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:            # стримим в общий лог И ловим цели/URL
            print(line, end='', flush=True)
            for m in _pat1.finditer(line):
                fired.add(m.group(1))
            for m in _pat2.finditer(line):
                fired.add(m.group(1))
            for m in _patu.finditer(line):
                urls.add(m.group(1).rstrip('.,;'))
        proc.wait(timeout=1800)
        d = ROOT / 'cache' / 'forms' / base
        d.mkdir(parents=True, exist_ok=True)
        (d / 'fired_goals.json').write_text(
            json.dumps(sorted(fired), ensure_ascii=False), encoding='utf-8')
        (d / 'fired_urls.json').write_text(
            json.dumps(sorted(urls), ensure_ascii=False), encoding='utf-8')
        _stamp(f'ФОРМЫ: готово (код {proc.returncode}); поймано целей форм: '
               f'{len(fired)}, URL прогона: {len(urls)}')
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
                    help='прогнать ВСЕ формы (а не только заказ), чтобы поймать все цели форм')
    ap.add_argument('--no-orders', action='store_true',
                    help='не прогонять заказ; заказ-цели брать из последнего прогона «Проверки форм»')
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

    # Внешний IP прогона: некоторые сайты (напр. inmetprom.ru) отдают 403
    # запросам из дата-центра. Чтобы добавить нас в белый список, админу сайта
    # нужен именно этот IP - выводим его в лог, чтобы можно было прочитать и
    # передать. Мягко: короткий таймаут, любая ошибка не мешает прогону.
    try:
        import urllib.request as _u
        _ip = _u.urlopen('https://api.ipify.org', timeout=8).read().decode().strip()
        _stamp(f'Внешний IP прогона (для белого списка сайта): {_ip}')
    except Exception:
        _stamp('Внешний IP прогона: определить не удалось')

    try:
        sys.path.insert(0, str(ROOT / 'forms_tester'))
        from form_tester.stop_signal import make_stop_check
        stop = make_stop_check()
    except Exception:
        stop = None

    # Заказ/формы прогоняем один раз на базовый проект (цели общие для всех стран).
    # По умолчанию - ТОЛЬКО сквозной заказ (корзина → оформление): «Проверка целей»
    # сама подтверждает заказ-цели, не гоняя лишние формы и не требуя отдельного
    # запуска «Проверки форм». --with-forms - полный прогон форм; --no-orders -
    # вообще не трогать формы (взять из последнего прогона «Проверки форм»).
    if not a.no_orders:
        bases = []
        for p in projects:
            b = gt._базовый(p)
            if b not in bases:
                bases.append(b)
        for b in bases:
            if stop and stop():
                break
            _прогнать_формы(b, a.show_browser, only_orders=not a.with_forms)

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
    # УНИКАЛЬНЫЙ финал именно для целей: форм-прогон (--with-forms) пишет своё
    # «✅ ВСЁ ГОТОВО» в тот же лог, и страница не должна принять его за конец целей.
    _stamp('🏁 ПРОВЕРКА ЦЕЛЕЙ ЗАВЕРШЕНА')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
