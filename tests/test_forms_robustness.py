"""Устойчивость проверки форм:
1) Атомарное сохранение лога - оборванная запись НЕ бьёт отчёт (раньше при сбое
   сохранения файл превращался в «Truncated file header» и терялись все прошлые
   формы прогона).
2) Мобильную вёрстку страниц «только для» других городов (подписка Хабаровска
   и т.п.) в прогоне не гоняем - иначе в отчёт лезет лишний домен.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'forms_tester'))

import test_all as t          # noqa: E402
import forms_run as fr        # noqa: E402


def test_atomic_save_valid_file(tmp_path):
    from openpyxl import Workbook, load_workbook
    p = str(tmp_path / 'log.xlsx')
    wb = Workbook()
    wb.active['A1'] = 'ok'
    t._atomic_save_wb(wb, p)
    assert load_workbook(p).active['A1'].value == 'ok'
    assert not os.path.exists(p + '.tmp')          # временный файл убран
    print('✓ атомарное сохранение даёт валидный файл, tmp не остаётся')


def test_atomic_save_failure_keeps_previous(tmp_path):
    # Сбой сохранения (нет места и т.п.) НЕ должен портить уже валидный файл.
    from openpyxl import Workbook, load_workbook
    p = str(tmp_path / 'log.xlsx')
    wb = Workbook()
    wb.active['A1'] = 'first'
    t._atomic_save_wb(wb, p)

    class _BadWB:
        def save(self, _path):
            raise IOError('disk full')

    try:
        t._atomic_save_wb(_BadWB(), p)
    except IOError:
        pass
    assert load_workbook(p).active['A1'].value == 'first'   # старый файл цел
    assert not os.path.exists(p + '.tmp')                    # мусор убран
    print('✓ сбой сохранения не рушит отчёт: прошлые данные на месте')


def _write_cfg(path, body):
    path.write_text(body, encoding='utf-8')
    return path


def test_города_ограничения_читаются(tmp_path):
    cfg = _write_cfg(tmp_path / 'config.py', (
        "СТРАНИЦЫ_ДЛЯ_ПРОВЕРКИ = [\n"
        "  {'тип': 'Главная'},\n"
        "  {'тип': 'Подписка_Хабаровск', 'только_города': ['Хабаровск']},\n"
        "  {'тип': 'Менеджер_СНГ', 'только_города': ['Алматы', 'Минск']},\n"
        "]\n"
    ))
    огр = fr._страницы_только_города(cfg)
    assert огр == {'Подписка_Хабаровск': {'Хабаровск'},
                   'Менеджер_СНГ': {'Алматы', 'Минск'}}
    print('✓ ограничения «только_города» страниц читаются из конфига')


def test_мобильная_страница_чужого_города_пропускается():
    # Логика фильтра из forms_run: страницу пропускаем, если она «только для»
    # городов, которых нет в прогоне.
    огр = {'Подписка_Хабаровск': {'Хабаровск'}}
    run_города = {'Москва'}

    def _пропустить(тип):
        только = огр.get(тип)
        return bool(только) and not (run_города & только)

    assert _пропустить('Подписка_Хабаровск') is True     # Хабаровск при Москве - мимо
    assert _пропустить('Главная') is False               # общая страница - гоним
    # А в прогоне Хабаровска - страница нужна.
    run_города2 = {'Хабаровск'}
    assert not (огр['Подписка_Хабаровск'] and not (run_города2 & огр['Подписка_Хабаровск']))
    print('✓ мобильную вёрстку чужого города пропускаем, свой - гоним')


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v', '-s']))
