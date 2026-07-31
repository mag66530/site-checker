"""Тест variables_run.py: сайт (потоки/HTTP, fetch_all) и карты (asyncio/
Playwright, _проверить_карты) должны выполняться ОДНОВРЕМЕННО, не по очереди -
раньше main() ждал sайт целиком, а потом ЕЩЁ ждал карты, хотя ничего не
мешает гонять их параллельно (просьба заказчика: если проверяется КП, сразу
одновременно проверять и карты, не последовательно).

main() тяжёлый - мокаем всё внешнее (та же подложка, что в
test_variables_run_check_site_flag.py), проверяем ГЛАВНОЕ: реальным замером
времени, что сайт и карты не складываются друг с другом."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import variables_run as vr
import kp as kp_module
import region_checker
from kp import KPRow


def _make_row(city='Москва', domain='stalmetural.ru'):
    return KPRow(domain=domain, city=city, country='Россия',
                phone_common='+7 499 130-36-69', all_phones='4991303669',
                address='Люблинская ул., 151')


def _patch_common(monkeypatch, tmp_path, fetch_all_spy):
    monkeypatch.setattr(vr, 'WORK_ROOT', tmp_path)

    class _FakeKPS:
        @staticmethod
        def kp_sheet_url(pid):
            return ''

    monkeypatch.setitem(sys.modules, 'kp_sheets', _FakeKPS)

    rows = [_make_row()]
    monkeypatch.setattr(kp_module, 'load_kp_rows', lambda pid: rows)
    monkeypatch.setattr(kp_module, 'load_kp', lambda pid, refresh=True: {r.domain: r for r in rows})
    monkeypatch.setattr(vr, 'project_use_proxy', lambda pid: False)
    monkeypatch.setattr(vr, 'fetch_all', fetch_all_spy)
    monkeypatch.setattr(region_checker, 'build_region_context', lambda kp, subs: SimpleNamespace())
    monkeypatch.setattr(vr, '_per_city', lambda pid: False)
    monkeypatch.setattr(vr, '_записать_xlsx', lambda *a, **kw: None)

    class _FakeTN:
        @staticmethod
        def send_report_from_env(**kw):
            return {'skipped': True}
    monkeypatch.setitem(sys.modules, 'telegram_notify', _FakeTN)


def test_site_and_maps_run_in_parallel(monkeypatch, tmp_path):
    delay = 0.3

    def _slow_fetch_all(*a, **kw):
        time.sleep(delay)
        return {}
    _patch_common(monkeypatch, tmp_path, _slow_fetch_all)

    async def _slow_maps(*a, **kw):
        await asyncio.sleep(delay)
        return []
    monkeypatch.setattr(vr, '_проверить_карты', _slow_maps)

    monkeypatch.setattr(sys, 'argv',
                        ['variables_run.py', '--project', 'smu', '--check-yandex-maps'])
    t0 = time.monotonic()
    rc = vr.main()
    elapsed = time.monotonic() - t0

    assert rc == 0
    # Последовательно было бы >= 2×delay (0.6с); параллельно - около delay
    # (0.3с) + накладные. Щедрый запас, чтобы тест не был хрупким.
    assert elapsed < delay * 1.8, \
        f'сайт и карты шли последовательно, не параллельно: {elapsed:.2f} сек'
    print(f'✓ сайт и карты выполнились параллельно за {elapsed:.2f} сек '
          f'(последовательно было бы {delay * 2:.2f})')


def test_maps_failure_does_not_lose_site_results(monkeypatch, tmp_path):
    """Карты сломались - результаты сайта (из параллельного потока) всё равно
    должны дойти до отчёта, а не потеряться."""
    calls = []

    def _fetch_all(*a, **kw):
        calls.append(1)
        return {}
    _patch_common(monkeypatch, tmp_path, _fetch_all)

    async def _boom(*a, **kw):
        raise RuntimeError('карты недоступны')
    monkeypatch.setattr(vr, '_проверить_карты', _boom)

    monkeypatch.setattr(sys, 'argv',
                        ['variables_run.py', '--project', 'smu', '--check-yandex-maps'])
    rc = vr.main()
    assert rc == 0
    assert calls == [1], 'сайт должен был отработать, даже если карты упали'
    print('✓ падение карт не мешает сайту довести проверку до конца')


def test_only_site_no_maps_still_works(monkeypatch, tmp_path):
    """Без карт (--check-site, без флагов карт) - поведение как раньше,
    без фонового потока не ломается."""
    calls = []

    def _fetch_all(*a, **kw):
        calls.append(1)
        return {}
    _patch_common(monkeypatch, tmp_path, _fetch_all)

    monkeypatch.setattr(sys, 'argv', ['variables_run.py', '--project', 'smu'])
    rc = vr.main()
    assert rc == 0
    assert calls == [1]
    print('✓ без карт - сайт по-прежнему проверяется штатно')
