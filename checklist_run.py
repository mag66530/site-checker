"""
checklist_run.py — фоновый подпроцесс прогона 30-мин чек-листа.

Запускается страницей чек-листа. Читает параметры+секреты из JSON, гоняет
проверку (runner_30min.run_check), пишет прогресс в status-файл, лог — в stdout
(родитель перенаправляет в файл), результат пиклит в out-файл.

Запуск:
    python checklist_run.py --params <params.json> --out <result.pkl> --status <status.json>
"""
import argparse
import json
import pickle
import sys
from datetime import datetime

from runner_30min import run_check

DONE_MARK = '✅ ВСЁ ГОТОВО'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--params', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--status', required=True)
    a = ap.parse_args()

    with open(a.params, encoding='utf-8') as f:
        data = json.load(f)
    pid = data['pid']
    params = data['params']
    creds = data['creds']

    def log(msg):
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

    def progress(frac, text):
        try:
            with open(a.status, 'w', encoding='utf-8') as sf:
                json.dump({'progress': max(0.0, min(1.0, frac)), 'text': text},
                          sf, ensure_ascii=False)
        except Exception:
            pass

    log(f'СТАРТ прогона — проект {pid}')
    progress(0.0, 'Подготовка…')

    result = run_check(pid, params, creds, log, progress)

    try:
        with open(a.out, 'wb') as of:
            pickle.dump(result, of)
        log('Результат сохранён.')
    except Exception as e:
        log(f'⚠ Не удалось сохранить результат (pickle): {e}')

    if result.get('error'):
        log(f'Завершено с ошибкой: {result["error"]}')
    log(DONE_MARK)


if __name__ == '__main__':
    main()
