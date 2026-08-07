"""Тесты: 403/429/503 - это «нас не пустили», а не «страницы нет».

Кейс МПИ: сайт под антиботом (любой запрос уводит на /challenge), и служебные
пробы получали 403. В отчёте это выглядело как «Политика конфиденциальности:
страница не найдена - создать» и «HTML-карта не найдена», хотя мы просто не
смогли посмотреть. Разница принципиальная: «нет страницы» - задача клиенту,
«нас не пустили» - ограничение проверки.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indexing_checker import BLOCKED_STATUSES, is_blocked_status


def test_blocked_codes():
    for st in (403, 429, 503):
        assert is_blocked_status(st), f'{st} - это защита сайта'


def test_not_blocked_codes():
    """404 и 410 - честное «страницы нет», их подменять нельзя."""
    for st in (200, 301, 404, 410, 500, None):
        assert not is_blocked_status(st), f'{st} не должен считаться защитой'


def test_blocked_statuses_is_tuple_of_ints():
    assert isinstance(BLOCKED_STATUSES, tuple)
    assert all(isinstance(s, int) for s in BLOCKED_STATUSES)


# ── Вердикт в «Проблемах» ────────────────────────────────────────────

from report_priorities import indexing_site_findings


def _required(page: dict):
    """Находки про обязательные страницы для одной записи."""
    return [f for f in indexing_site_findings({'required_pages': [page]})
            if 'обязательн' in (f.problem or '')]


def test_missing_page_is_an_error():
    """404 - честная находка «страницы нет», уходит клиенту в работу."""
    f = _required({'label': 'Cookies', 'found': None, 'blocked': None})
    assert len(f) == 1 and f[0].level == 'Ошибка'
    assert 'не найдена' in f[0].problem


def test_blocked_page_is_a_warning_not_an_error():
    """403 - «не удалось проверить». Ошибкой это называть нельзя: страница
    может существовать, мы её просто не увидели из-за защиты сайта."""
    f = _required({'label': 'Cookies', 'found': None, 'blocked': 403})
    assert len(f) == 1 and f[0].level == 'Предупреждение'
    assert 'не удалось проверить' in f[0].problem
    assert '403' in (f[0].detail or '')


def test_found_page_gives_no_finding():
    """Если страница нашлась, код блокировки на других вариантах адреса
    роли не играет - находки нет вовсе."""
    assert _required({'label': 'Cookies', 'found': '/cookies/',
                      'blocked': 403}) == []
