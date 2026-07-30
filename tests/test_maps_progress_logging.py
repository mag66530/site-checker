"""Тесты variables_run._проверить_карты(): построчный прогресс в лог.

Раньше лог на фазе карт молчал от «начали» до «готово» (иногда 15-30 минут
на большом проекте) - страница «Проверка КП» парсит из лога ПОСЛЕДНЮЮ строку
вида «[i/N]» для прогресс-бара (см. checklists/variables_check.py), без таких
строк бар просто замирал на состоянии предыдущей фазы (сайт vs КП), а лог
выглядел зависшим, хотя работа шла. Проверяем, что теперь после КАЖДОЙ
карточки (не раз на всю фазу) уходит [i/N] строка.

Playwright не запускаем по-настоящему - подменяем async_playwright лёгкими
фейками (async-контекст-менеджеры без сети), а yandex_map_check.afetch/
twogis_map_check.afetch - быстрыми async-заглушками."""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import variables_run as vr
from kp import KPRow


class _FakeCtx:
    pass


class _FakeBrowser:
    async def new_context(self, **kw):
        return _FakeCtx()

    async def close(self):
        pass


class _FakeChromium:
    async def launch(self, **kw):
        return _FakeBrowser()


class _FakePW:
    async def __aenter__(self):
        return SimpleNamespace(chromium=_FakeChromium())

    async def __aexit__(self, *a):
        return False


def _install_fake_playwright(monkeypatch):
    import playwright.async_api as pwa
    monkeypatch.setattr(pwa, "async_playwright", lambda: _FakePW())


def _domains(n):
    rows = [(f"host{i}.ru",
             KPRow(domain=f"host{i}.ru", city=f"Город{i}",
                  yandex_map_url=f"https://yandex.ru/{i}",
                  twogis_map_url=f"https://2gis.ru/{i}"))
            for i in range(n)]
    return rows


def test_progress_logged_after_every_card_yandex_only(monkeypatch):
    _install_fake_playwright(monkeypatch)
    import yandex_map_check
    async def _fake_afetch(ctx, sem, url):
        return {"available": True, "name": "X", "phone": "", "address": "", "site": ""}
    monkeypatch.setattr(yandex_map_check, "afetch", _fake_afetch)

    logs = []
    domains = _domains(5)
    results = asyncio.run(vr._проверить_карты(
        domains, None, do_yandex=True, do_2gis=False, log=logs.append))

    assert len(results) == 5
    progress_lines = [ln for ln in logs if re.search(r'\[\d+/\d+\]', ln)]
    assert len(progress_lines) == 5, f'по одной строке [i/N] на каждую карточку: {logs}'
    # Числитель растёт 1..5, знаменатель везде 5 (всего 5 карточек - только Яндекс).
    nums = [tuple(map(int, re.search(r'\[(\d+)/(\d+)\]', ln).groups()))
            for ln in progress_lines]
    assert nums == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)] or sorted(nums) == \
        [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)], \
        f'нумерация должна дойти до N=5 без пропусков: {nums}'
    print('✓ 5 карточек Яндекса - 5 строк прогресса, знаменатель верный')


def test_progress_denominator_covers_both_sources(monkeypatch):
    """И Яндекс, и 2ГИС включены - общий знаменатель = 2×городов, не по-фазово."""
    _install_fake_playwright(monkeypatch)
    import yandex_map_check
    import twogis_map_check

    async def _fake_ya(ctx, sem, url):
        return {"available": True, "name": "X", "phone": "", "address": "", "site": ""}

    async def _fake_gis(ctx, sem, url):
        return {"available": False, "error": "нет ссылки"}

    monkeypatch.setattr(yandex_map_check, "afetch", _fake_ya)
    monkeypatch.setattr(twogis_map_check, "afetch", _fake_gis)

    logs = []
    domains = _domains(3)
    results = asyncio.run(vr._проверить_карты(
        domains, None, do_yandex=True, do_2gis=True, log=logs.append))

    assert len(results) == 6  # 3 города × 2 источника
    progress_lines = [ln for ln in logs if re.search(r'\[\d+/\d+\]', ln)]
    assert len(progress_lines) == 6
    denominators = {int(re.search(r'\[(\d+)/(\d+)\]', ln).group(2)) for ln in progress_lines}
    assert denominators == {6}, f'знаменатель должен быть 6 (3×2) везде: {denominators}'
    print('✓ оба источника включены - знаменатель 6 (3 города × 2), не 3')


def test_no_progress_lines_when_nothing_selected(monkeypatch):
    """Ни Яндекс, ни 2ГИС не включены - функция не должна падать и не должна
    писать бессмысленные [0/0]."""
    _install_fake_playwright(monkeypatch)
    logs = []
    results = asyncio.run(vr._проверить_карты(
        _domains(3), None, do_yandex=False, do_2gis=False, log=logs.append))
    assert results == []
    assert not any(re.search(r'\[\d+/\d+\]', ln) for ln in logs)
    print('✓ ничего не выбрано - пусто, без мусорных строк прогресса')


def test_log_defaults_to_noop_when_not_provided(monkeypatch):
    """log - необязательный параметр (для обратной совместимости с прямым
    вызовом без логирования) - без него функция просто не пишет никуда."""
    _install_fake_playwright(monkeypatch)
    import yandex_map_check
    async def _fake_afetch(ctx, sem, url):
        return {"available": True, "name": "X", "phone": "", "address": "", "site": ""}
    monkeypatch.setattr(yandex_map_check, "afetch", _fake_afetch)
    results = asyncio.run(vr._проверить_карты(
        _domains(2), None, do_yandex=True, do_2gis=False))
    assert len(results) == 2
    print('✓ без log - не падает, просто молчит')
