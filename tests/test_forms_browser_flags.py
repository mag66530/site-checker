"""Экономные флаги Chromium в проверке форм.

Каждый поток держит свой браузер, и в контейнере они делят память с самим
приложением. Флаги гасят фоновые службы БРАУЗЕРА, но ничего не меняют в
отрисовке страницы - иначе поедут вердикты вёрстки и форм.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))
sys.path.insert(0, str(КОРЕНЬ / 'forms_tester'))

import test_all as t

# То, что меняет саму страницу или поведение вкладок - в прогоне форм НЕЛЬЗЯ.
ЗАПРЕЩЁННЫЕ = (
    'imagesEnabled=false',      # картинки меняют высоту блоков → мобильная вёрстка
    'max-old-space-size',       # потолок JS-кучи → форма «ломается» не по вине сайта
    'renderer-process-limit',   # вкладки в одном процессе → падения тянут друг друга
    'single-process',
    'disable-javascript',
    'blink-settings=scriptEnabled=false',
)


def test_флаги_не_трогают_отрисовку():
    склеено = ' '.join(t._ЭКОНОМНЫЕ_ФЛАГИ)

    плохие = [ф for ф in ЗАПРЕЩЁННЫЕ if ф in склеено]

    assert not плохие, (
        f'эти флаги влияют на качество проверки, их тут быть не должно: '
        f'{плохие}')
    print('✓ ничего, что меняло бы страницу, в списке нет')


def test_гасим_именно_фоновые_службы():
    склеено = ' '.join(t._ЭКОНОМНЫЕ_ФЛАГИ)

    for ожидаем in ('--disable-extensions', '--disable-background-networking',
                    '--disable-sync', '--disable-component-update',
                    'BackForwardCache'):
        assert ожидаем in склеено, f'нет флага {ожидаем}'
    print('✓ фоновые службы браузера выключены')


def test_флаги_синтаксически_целы():
    for ф in t._ЭКОНОМНЫЕ_ФЛАГИ:
        assert ф.startswith('--'), ф
        assert ' ' not in ф, f'пробел внутри флага: {ф}'
    print(f'✓ все {len(t._ЭКОНОМНЫЕ_ФЛАГИ)} флагов оформлены верно')
