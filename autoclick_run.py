"""
autoclick_run.py - один фоновый процесс, последовательно гоняет выбранные
автокликеры (ГСК и/или Вебмастер) для проекта. Весь вывод идёт в stdout,
который вызывающая сторона (страница «Автокликеры») перенаправляет в лог-файл.

Запуск (обычно из страницы в фоне):
    python autoclick_run.py --project mpe --gsc --wm
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
PY = sys.executable

# ЛОКАЛЬНЫЙ режим: подключение к Chrome (CDP 127.0.0.1:9222) не должно
# ходить через внешний прокси. Если в консоли, из которой запущен Streamlit,
# остался HTTP(S)_PROXY (например, задавали для git push) - Node-драйвер
# Playwright наследует его ПРИ СТАРТЕ и шлёт CDP-запрос на прокси (407).
# Чистить надо ЗДЕСЬ, до запуска подпроцессов-кликеров (чистка внутри
# уже запущенного Playwright не действует на его Node-процесс).
if os.environ.get('AUTOCLICK_MODE') != 'cloud':
    for _v in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
               'ALL_PROXY', 'all_proxy'):
        os.environ.pop(_v, None)
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _run(script_args, title):
    _stamp(f'▶▶ {title}')
    proc = subprocess.Popen(
        [PY, *script_args], cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace',
    )
    for line in proc.stdout:
        print(line.rstrip(), flush=True)
    proc.wait()
    _stamp(f'■■ {title} - код {proc.returncode}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--gsc', action='store_true')
    ap.add_argument('--wm', action='store_true')
    a = ap.parse_args()

    _stamp(f'АВТОКЛИКЕР СТАРТ - проект {a.project} '
           f'(ГСК={a.gsc}, Вебмастер={a.wm})')

    if a.gsc:
        _run(['gsc_validate_fixes.py', '--project', a.project], 'ГСК: проверка исправлений')
    if a.wm:
        _run(['webmaster_recheck.py', '--project', a.project], 'Вебмастер: проверка ошибок')

    _stamp('✅ ВСЁ ГОТОВО')


if __name__ == '__main__':
    main()
