"""Тесты variables_run.py: --check-site/--no-check-site.

Раньше сверка сайта с КП выполнялась ВСЕГДА, без вариантов - страница
скачивалась в любом случае, даже если интерес был только к картам. Теперь
это отдельная галочка: --no-check-site должен пропустить ВЕСЬ сайт-этап
(ни одного HTTP-запроса к сайту, fetch_all не вызывается вообще), а
--check-site (по умолчанию) - вести себя как раньше.

main() тяжёлый (КП из Google-таблицы, прокси, xlsx, Telegram) - мокаем всё
внешнее и проверяем ГЛАВНОЕ: вызывается fetch_all или нет, и результаты[]."""
import sys
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
    """Общие подмены для запуска main() без реальной сети/Google/Telegram."""
    monkeypatch.setattr(vr, 'WORK_ROOT', tmp_path)

    class _FakeKPS:
        @staticmethod
        def kp_sheet_url(pid):
            return ''  # без Google-таблицы - берём "снапшот" (наш мок ниже)

    monkeypatch.setitem(sys.modules, 'kp_sheets', _FakeKPS)

    rows = [_make_row()]
    # main()/_check_site делают ЛОКАЛЬНЫЙ `import kp as kpmod` - это просто
    # алиас на тот же объект модуля kp, патчим его напрямую.
    monkeypatch.setattr(kp_module, 'load_kp_rows', lambda pid: rows)
    monkeypatch.setattr(kp_module, 'load_kp', lambda pid, refresh=True: {r.domain: r for r in rows})
    monkeypatch.setattr(vr, 'project_use_proxy', lambda pid: False)
    monkeypatch.setattr(vr, 'fetch_all', fetch_all_spy)
    monkeypatch.setattr(region_checker, 'build_region_context', lambda kp, subs: SimpleNamespace())
    monkeypatch.setattr(vr, '_per_city', lambda pid: False)

    def _fake_write(*a, **kw):
        pass
    monkeypatch.setattr(vr, '_записать_xlsx', _fake_write)

    class _FakeTN:
        @staticmethod
        def send_report_from_env(**kw):
            return {'skipped': True}
    monkeypatch.setitem(sys.modules, 'telegram_notify', _FakeTN)


def test_no_check_site_skips_fetch_all_entirely(monkeypatch, tmp_path):
    calls = []
    def _spy(*a, **kw):
        calls.append(a)
        return {}
    _patch_common(monkeypatch, tmp_path, _spy)

    monkeypatch.setattr(sys, 'argv',
                        ['variables_run.py', '--project', 'smu', '--no-check-site'])
    rc = vr.main()
    assert rc == 0
    assert calls == [], 'fetch_all не должен вызываться при --no-check-site'
    print('✓ --no-check-site: сайт не скачивается вообще (fetch_all не вызван)')


def test_check_site_default_still_calls_fetch_all(monkeypatch, tmp_path):
    """Регресс на вынос блока в _check_site: дефолтное поведение (--check-site
    по умолчанию True) должно остаться прежним - fetch_all вызывается."""
    calls = []
    def _spy(*a, **kw):
        calls.append(a)
        return {}
    _patch_common(monkeypatch, tmp_path, _spy)

    monkeypatch.setattr(sys, 'argv', ['variables_run.py', '--project', 'smu'])
    rc = vr.main()
    assert rc == 0
    assert len(calls) == 1, 'fetch_all должен вызваться ровно один раз при check_site=True (по умолчанию)'
    print('✓ check-site по умолчанию (True) - fetch_all вызван, как раньше')


def test_explicit_check_site_flag_calls_fetch_all(monkeypatch, tmp_path):
    calls = []
    def _spy(*a, **kw):
        calls.append(a)
        return {}
    _patch_common(monkeypatch, tmp_path, _spy)

    monkeypatch.setattr(sys, 'argv',
                        ['variables_run.py', '--project', 'smu', '--check-site'])
    rc = vr.main()
    assert rc == 0
    assert len(calls) == 1
    print('✓ явный --check-site тоже вызывает fetch_all')
