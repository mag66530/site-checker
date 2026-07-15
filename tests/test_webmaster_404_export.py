"""Тесты webmaster_404_export.py - «Проверка страниц в индексе на 404-ошибку»
(регулярный мониторинг через Яндекс.Вебмастер). Чистые функции (разбор CSV,
вердикт по живому статусу) без сети/браузера - см. докстринг модуля про то,
как разбор проверен на реальной выгрузке mepen.ru (30126 строк)."""
from webmaster_404_export import (
    parse_indexing_csv, dedup_by_url, recheck_verdict, recheck_candidates,
)

# Заголовок и форма строк - как в реальном экспорте Яндекс.Вебмастера
# «Страницы в поиске → Последние изменения» (Скачать таблицу → CSV).
_HEADER = '"updateDate","url","httpCode","status","target","lastAccess","title","event"'


def _csv(*rows: str) -> str:
    return '\n'.join([_HEADER, *rows])


def test_searchable_и_low_demand_не_кандидаты():
    # Самые частые статусы в реальной выгрузке (SEARCHABLE - страница в
    # поиске, LOW_DEMAND - просто малоценная, не ошибка) - не находки.
    raw = _csv(
        '"15.07.2026","https://mepen.ru/catalog/a/","200","SEARCHABLE","","15.07.2026","A","ADD"',
        '"14.07.2026","https://mepen.ru/catalog/b/","200","LOW_DEMAND","https://mepen.ru/catalog/b/","26.06.2026","B","DELETE"',
    )
    assert parse_indexing_csv(raw) == []


def test_http_error_и_unknown_url_кандидаты():
    raw = _csv(
        '"14.07.2026","https://mepen.ru/catalog/c/","500","HTTP_ERROR","","05.07.2026","C","DELETE"',
        '"14.07.2026","https://mepen.ru/catalog/d/","0","UNKNOWN_URL","","01.07.2026","D","DELETE"',
    )
    out = parse_indexing_csv(raw)
    assert {c['url'] for c in out} == {
        'https://mepen.ru/catalog/c/', 'https://mepen.ru/catalog/d/'}


def test_robots_txt_error_не_кандидат():
    # Похоже на «ошибку» по названию, но это отдельная, уже покрытая тема
    # (пункт 1.7 - соблюдение директив) - не сюда.
    raw = _csv(
        '"14.07.2026","https://mepen.ru/bitrix/admin/","0","ROBOTS_TXT_ERROR","","01.07.2026","E","DELETE"',
    )
    assert parse_indexing_csv(raw) == []


def test_redirect_notsearchable_не_кандидат():
    raw = _csv(
        '"14.07.2026","https://mepen.ru/old/","301","REDIRECT_NOTSEARCHABLE","","01.07.2026","F","DELETE"',
    )
    assert parse_indexing_csv(raw) == []


def test_пустой_csv_пустой_список():
    assert parse_indexing_csv(_HEADER) == []
    assert parse_indexing_csv('') == []


# ── dedup_by_url ────────────────────────────────────────────────────────

def test_dedup_оставляет_одну_запись_на_url():
    candidates = [
        {'url': 'https://mepen.ru/x/', 'status': 'HTTP_ERROR'},
        {'url': 'https://mepen.ru/x/', 'status': 'UNKNOWN_URL'},
        {'url': 'https://mepen.ru/y/', 'status': 'HTTP_ERROR'},
    ]
    out = dedup_by_url(candidates)
    assert len(out) == 2
    urls = {c['url'] for c in out}
    assert urls == {'https://mepen.ru/x/', 'https://mepen.ru/y/'}


def test_dedup_оставляет_первую_встреченную_запись():
    # Яндекс отдаёт от новых событий к старым - первая встреченная запись
    # для URL и есть самая свежая, её и оставляем.
    candidates = [
        {'url': 'https://mepen.ru/x/', 'status': 'HTTP_ERROR', 'last_access': 'свежая'},
        {'url': 'https://mepen.ru/x/', 'status': 'UNKNOWN_URL', 'last_access': 'старая'},
    ]
    out = dedup_by_url(candidates)
    assert len(out) == 1
    assert out[0]['last_access'] == 'свежая'


# ── recheck_verdict ──────────────────────────────────────────────────────

def test_вердикт_200_уже_не_проблема():
    assert recheck_verdict(200) == 'уже не проблема (200)'


def test_вердикт_404_подтверждено():
    v = recheck_verdict(404)
    assert v.startswith('подтверждено')
    assert '404' not in v or 'не существует' in v  # текст про факт, не про код


def test_вердикт_410_тоже_подтверждено():
    assert recheck_verdict(410).startswith('подтверждено')


def test_вердикт_5xx_подтверждено():
    assert recheck_verdict(500).startswith('подтверждено')
    assert recheck_verdict(503).startswith('подтверждено')


def test_вердикт_none_не_удалось_проверить():
    # Сеть/таймаут при повторной проверке - НЕ считается подтверждённой
    # находкой (не наговариваем на сайт то, чего не смогли проверить).
    v = recheck_verdict(None)
    assert v == 'не удалось проверить'
    assert not v.startswith('подтверждено')


def test_вердикт_прочий_код_тоже_подтверждено():
    assert recheck_verdict(403).startswith('подтверждено')


# ── recheck_candidates (сеть заглушена через monkeypatch requests) ──────

def test_recheck_candidates_использует_live_status(monkeypatch):
    # recheck_candidates делает `import requests` ЛОКАЛЬНО внутри функции -
    # но это тот же объект модуля из sys.modules, что и здесь, так что
    # патчить requests.head на уровне теста достаточно.
    import requests

    class _FakeResp:
        def __init__(self, code):
            self.status_code = code

    def _fake_head(url, timeout, allow_redirects, proxies):
        return _FakeResp(404 if 'broken' in url else 200)

    monkeypatch.setattr(requests, 'head', _fake_head)

    candidates = [
        {'url': 'https://mepen.ru/broken/', 'status': 'UNKNOWN_URL'},
        {'url': 'https://mepen.ru/fixed/', 'status': 'HTTP_ERROR'},
    ]
    out = recheck_candidates(candidates)
    by_url = {c['url']: c for c in out}
    assert by_url['https://mepen.ru/broken/']['verdict'].startswith('подтверждено')
    assert by_url['https://mepen.ru/fixed/']['verdict'] == 'уже не проблема (200)'


if __name__ == '__main__':
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v) and 'monkeypatch' not in
           v.__code__.co_varnames[:v.__code__.co_argcount]]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f'✓ {fn.__name__}'); ok += 1
        except Exception:
            print(f'✗ {fn.__name__}'); traceback.print_exc()
    print(f'\n{ok}/{len(fns)} прошло')
    import sys
    sys.exit(0 if ok == len(fns) else 1)
