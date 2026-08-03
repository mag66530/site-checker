"""Тесты _run_subprocess_with_timeout из runner_30min.py.

2026-08-03: «404 в индексе» на проде завис навсегда - подпроцесс с headless-
браузером подвис (не упал), а построчное чтение stdout без тайм-аута ждало
вывода вечно и остановило весь чек-лист. Проверяем, что теперь зависший
подпроцесс принудительно убивается по дедлайну, а нормальный - отрабатывает
как раньше (построчный лог, код возврата).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runner_30min as m


def test_normal_process_streams_lines_and_returns_code():
    lines = []
    args = [sys.executable, '-c',
            "import sys; print('one'); print('two'); sys.exit(3)"]
    rc, timed_out = m._run_subprocess_with_timeout(
        args, cwd=str(Path(__file__).resolve().parent.parent),
        env=dict(os.environ), log=lines.append, prefix='[t] ',
        timeout_sec=30)
    assert timed_out is False
    assert rc == 3
    assert lines == ['[t] one', '[t] two']


def test_hung_process_is_killed_on_timeout():
    """Подпроцесс, который никогда не пишет в stdout и не завершается сам -
    должен быть убит в течение тайм-аута, а не висеть вечно."""
    args = [sys.executable, '-c', 'import time; time.sleep(120)']
    started = time.monotonic()
    rc, timed_out = m._run_subprocess_with_timeout(
        args, cwd=str(Path(__file__).resolve().parent.parent),
        env=dict(os.environ), log=lambda s: None, prefix='[t] ',
        timeout_sec=1.5)
    elapsed = time.monotonic() - started
    assert timed_out is True
    assert rc is None
    # Убили быстро, а не ждали все 120с сна дочернего процесса.
    assert elapsed < 20
