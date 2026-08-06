"""Тесты indexing_checker - «Соблюдение директив вживую» (пункт 1.7):
чистые функции без сети. Живой запрос (_fetch_for_directive_check) юнит-
тестом не покрывается - только смоук-тестом на реальном HTTP (см.
scratchpad прошлых сессий)."""
from indexing_checker import (
    _sample_disallowed_by_rule, _directive_compliance_verdict,
    _junk_verdict, analyze_page_indexing, parse_robots,
)


# ── _sample_disallowed_by_rule ──────────────────────────────────────────

def test_несколько_путей_с_одним_правилом_один_в_выборке():
    disallowed = [
        {'path': '/catalog/a/?PAGEN_1=2', 'rule': '*PAGEN_1*', 'agent': '*'},
        {'path': '/catalog/b/?PAGEN_1=3', 'rule': '*PAGEN_1*', 'agent': '*'},
        {'path': '/catalog/c/?PAGEN_1=4', 'rule': '*PAGEN_1*', 'agent': '*'},
    ]
    sample = _sample_disallowed_by_rule(disallowed)
    assert len(sample) == 1
    assert sample[0]['path'] == '/catalog/a/?PAGEN_1=2'


def test_разные_правила_все_представлены():
    disallowed = [
        {'path': '/a/', 'rule': '/a/', 'agent': '*'},
        {'path': '/b/', 'rule': '/b/', 'agent': '*'},
        {'path': '/c/', 'rule': '/c/', 'agent': 'yandex'},
    ]
    sample = _sample_disallowed_by_rule(disallowed)
    assert {s['rule'] for s in sample} == {'/a/', '/b/', '/c/'}


def test_обрезка_по_лимиту():
    disallowed = [{'path': f'/{i}/', 'rule': f'/{i}/', 'agent': '*'} for i in range(50)]
    sample = _sample_disallowed_by_rule(disallowed, limit=5)
    assert len(sample) == 5


def test_пустой_список_пустая_выборка():
    assert _sample_disallowed_by_rule([]) == []
    assert _sample_disallowed_by_rule(None) == []


# ── _directive_compliance_verdict ───────────────────────────────────────

def test_недоступна_напрямую_ok():
    assert _directive_compliance_verdict(404, False) == 'ok'
    assert _directive_compliance_verdict(301, False) == 'ok'
    assert _directive_compliance_verdict(500, False) == 'ok'
    assert _directive_compliance_verdict(None, False) == 'ok'


def test_отвечает_200_с_noindex_protected():
    assert _directive_compliance_verdict(200, True) == 'protected'


def test_отвечает_200_без_noindex_robots_only():
    # Главный кейс находки: страница реально доступна и держится ТОЛЬКО на
    # честном слове robots.txt, без собственной подстраховки noindex'ом.
    assert _directive_compliance_verdict(200, False) == 'robots_only'


# ── _junk_verdict ────────────────────────────────────────────────────────

def test_параметрический_дубль_с_canonical_не_находка():
    # Открыта в robots, отвечает 200, НО есть rel=canonical (даже self) -
    # правки по чек-листу: это тоже валидная защита, не находка.
    html = '<html><head><link rel="canonical" href="/catalog/x/?sort=price"></head></html>'
    assert _junk_verdict(200, html, is_param=True) is False


def test_параметрический_дубль_без_canonical_находка():
    assert _junk_verdict(200, '<html><head></head></html>', is_param=True) is True
    assert _junk_verdict(200, None, is_param=True) is True


def test_не_параметрическая_страница_canonical_не_спасает():
    # /admin/, /basket/, /search/ и т.п. - canonical тут ни при чём.
    html = '<html><head><link rel="canonical" href="/basket/"></head></html>'
    assert _junk_verdict(200, html, is_param=False) is True


def test_статус_не_200_не_находка():
    assert _junk_verdict(404, None, is_param=True) is False
    assert _junk_verdict(None, None, is_param=False) is False


# ── analyze_page_indexing: noindex на открытой в robots странице ────────

def _open_robots():
    return parse_robots('User-agent: *\nDisallow: /admin/\n', host='example.ru')


def test_meta_noindex_на_открытой_в_robots_странице_не_ошибка():
    # Правки по чек-листу: страница не обязана быть закрыта в robots.txt,
    # если её защищает noindex - это не расхождение, а валидная альтернатива.
    html = '<html><head><meta name="robots" content="noindex"></head></html>'
    out = analyze_page_indexing(html, {}, 'https://example.ru/catalog/x/',
                                 _open_robots())
    assert out['meta_noindex'] is True
    assert out['robots_disallowed'] is False
    assert out['issues'] == []


def test_x_robots_tag_noindex_на_открытой_в_robots_странице_не_ошибка():
    out = analyze_page_indexing('<html></html>', {'x-robots-tag': 'noindex'},
                                'https://example.ru/catalog/x/', _open_robots())
    assert out['x_robots_noindex'] is True
    assert out['robots_disallowed'] is False
    assert out['issues'] == []


# ── Находки индексации доходят до «Проблем» (листа «Индексация» больше нет) ──

def test_заблокированные_страницы_попадают_в_проблемы():
    from report_priorities import indexing_site_findings

    summary_с_находкой = {
        'host': 'example.ru', 'disallowed': [],
        'directive_check': {'checked': 3, 'findings': [
            {'rule': '/starye-tovary/', 'path': '/starye-tovary/', 'status': 200},
        ]},
    }
    находки = indexing_site_findings(summary_с_находкой)
    assert any('robots.txt' in f.problem for f in находки), находки
    assert any('/starye-tovary/' in f.url for f in находки)

    summary_без_находок = {
        'host': 'example.ru', 'disallowed': [],
        'directive_check': {'checked': 3, 'findings': []},
    }
    assert indexing_site_findings(summary_без_находок) == []


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); print(f"✓ {fn.__name__}"); ok += 1
        except Exception:
            print(f"✗ {fn.__name__}"); traceback.print_exc()
    print(f"\n{ok}/{len(fns)} прошло")
    import sys
    sys.exit(0 if ok == len(fns) else 1)
