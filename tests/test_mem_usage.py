"""Замер памяти контейнера для лога прогона.

Когда контейнер убивают за превышение памяти, лог умирает вместе с ним -
посмертно смотреть нечего. Поэтому расход пишем ПО ХОДУ прогона: страница
тайлит лог вживую, и потолок видно до падения.
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

import mem_usage


def _cgroup_v2(корень: Path, занято: str, лимит: str):
    (корень / 'sys/fs/cgroup').mkdir(parents=True, exist_ok=True)
    (корень / 'sys/fs/cgroup/memory.current').write_text(занято)
    (корень / 'sys/fs/cgroup/memory.max').write_text(лимит)


def test_читает_cgroup_v2(tmp_path):
    _cgroup_v2(tmp_path, str(512 * 1024 * 1024), str(1024 * 1024 * 1024))

    занято, лимит = mem_usage.использование(tmp_path)

    assert (занято, лимит) == (512 * 1024 * 1024, 1024 * 1024 * 1024)
    assert mem_usage.строка(tmp_path) == (
        'Память контейнера: 512 МБ из 1024 МБ (50%)')
    print('✓ cgroup v2 читается, строка человеческая')


def test_читает_cgroup_v1(tmp_path):
    п = tmp_path / 'sys/fs/cgroup/memory'
    п.mkdir(parents=True)
    (п / 'memory.usage_in_bytes').write_text(str(300 * 1024 * 1024))
    (п / 'memory.limit_in_bytes').write_text(str(600 * 1024 * 1024))

    assert mem_usage.строка(tmp_path) == (
        'Память контейнера: 300 МБ из 600 МБ (50%)')
    print('✓ старый cgroup v1 тоже понимаем')


def test_предупреждение_у_потолка(tmp_path):
    _cgroup_v2(tmp_path, str(900 * 1024 * 1024), str(1024 * 1024 * 1024))

    строка = mem_usage.строка(tmp_path)

    assert '88%' in строка and 'близко к пределу' in строка
    print('✓ у потолка строка предупреждает')


def test_лимит_не_задан(tmp_path):
    _cgroup_v2(tmp_path, str(128 * 1024 * 1024), 'max')

    assert mem_usage.строка(tmp_path) == (
        'Память контейнера: 128 МБ (лимит не задан)')
    print('✓ «max» - не число, а «лимита нет»')


def test_локально_молчит(tmp_path):
    """Ни cgroup, ни файлов - строки в лог не будет (Windows/Mac)."""
    assert mem_usage.использование(tmp_path) == (None, None)
    assert mem_usage.строка(tmp_path) == ''
    print('✓ без cgroup лог не засоряется')


def test_битые_значения_не_ломают(tmp_path):
    _cgroup_v2(tmp_path, 'абв', 'тоже не число')

    assert mem_usage.строка(tmp_path) == ''
    print('✓ мусор в файлах cgroup не роняет прогон')
