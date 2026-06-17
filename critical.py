"""
critical.py – выделение критических ошибок прогона для срочных уведомлений (п.4.3).

Чистый анализ результатов проверки (без Telegram/отчёта) – какие находки считать
критическими и достойными немедленного внимания SEO/руководителя.

Два уровня:
  • availability  – ПАДЕНИЕ ДОСТУПНОСТИ: сервер не отвечает (5xx/таймаут/нет
    соединения) на любой странице ИЛИ недоступна главная страница города.
    Под это шлём ОТДЕЛЬНОЕ срочное сообщение.
  • others        – прочие критические (в блок подписи к отчёту):
        kp          – контакты на сайте ≠ КП (телефон/почта/адрес);
        cannot_buy  – нельзя купить: нет цены/кнопки заказа или пустой раздел;
        not_found   – 404 на странице выборки или soft-404 («заглушка» 200);
        text        – битые шаблонные переменные в текстах.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

# Сервер не отвечает – это падение доступности на любой странице.
_SERVER_DOWN = ('server_error', 'timeout', 'network_error')

_STATUS_LABEL = {
    'not_found': '404 не найдена',
    'client_error': 'ошибка доступа',
    'server_error': 'сервер не отвечает (5xx)',
    'timeout': 'нет ответа (таймаут)',
    'network_error': 'нет соединения',
    'cancelled': 'отменено',
}

_OTHER_CATEGORIES = ('kp', 'cannot_buy', 'not_found', 'text')


@dataclass
class CriticalItem:
    category: str          # availability | kp | cannot_buy | not_found | text
    city: str
    path: str
    detail: str
    url: str = ''


@dataclass
class CriticalSummary:
    availability: list = field(default_factory=list)            # для срочного сообщения
    others: dict = field(default_factory=lambda: {c: [] for c in _OTHER_CATEGORIES})

    @property
    def total(self) -> int:
        return len(self.availability) + sum(len(v) for v in self.others.values())

    @property
    def has_availability(self) -> bool:
        return bool(self.availability)

    @property
    def has_any(self) -> bool:
        return self.total > 0


def _path(url: str) -> str:
    try:
        return urlparse(url).path or url
    except Exception:
        return url


def is_availability_down(r) -> bool:
    """Падение доступности: сервер не отвечает на любой странице ИЛИ упала главная."""
    if getattr(r, 'status', '') in _SERVER_DOWN:
        return True
    if getattr(r, 'is_error', False) and getattr(r, 'type_code', '') == 'main':
        return True
    return False


def analyze(results) -> CriticalSummary:
    """Разобрать результаты прогона на критические находки."""
    s = CriticalSummary()
    for r in results or []:
        if getattr(r, 'status', '') == 'cancelled':
            continue
        city = getattr(r, 'city', '') or '–'
        url = getattr(r, 'url', '') or ''
        path = _path(url)

        # 1) Падение доступности (сервер / главная) – дальше контента нет.
        if is_availability_down(r):
            label = _STATUS_LABEL.get(getattr(r, 'status', ''), 'не открылась')
            where = 'главная' if getattr(r, 'type_code', '') == 'main' else path
            s.availability.append(
                CriticalItem('availability', city, path, f'{where}: {label}', url))
            continue

        # Прочие недоступности (напр. 404 не на главной) – в not_found.
        if not getattr(r, 'is_ok', False):
            if getattr(r, 'status', '') == 'not_found':
                s.others['not_found'].append(
                    CriticalItem('not_found', city, path, '404', url))
            continue

        content = getattr(r, 'content', None)

        # 2) soft-404 («заглушка»: 200, но контент «страница не найдена»).
        if content is not None and getattr(content, 'is_soft_404', False):
            s.others['not_found'].append(
                CriticalItem('not_found', city, path, '404-заглушка', url))
            continue

        # 3) Контакты ≠ КП.
        kp = getattr(r, 'kp_result', None)
        if kp and kp.get('has_issues'):
            bad = [i.get('field', '') for i in (kp.get('issues') or [])
                   if i.get('status') in ('bug', 'critical')]
            if bad:
                s.others['kp'].append(
                    CriticalItem('kp', city, path, ', '.join(bad), url))

        # 4) Нельзя купить: пустой раздел ИЛИ нет цены/кнопки заказа.
        if content is not None:
            pk = getattr(content, 'page_kind', '')
            bug_keys = {getattr(b, 'key', '') for b in getattr(content, 'bugs', [])}
            if pk == 'empty':
                s.others['cannot_buy'].append(
                    CriticalItem('cannot_buy', city, path, 'пустой раздел', url))
            elif bug_keys & {'price', 'btn_order'}:
                what = []
                if 'price' in bug_keys:
                    what.append('нет цены')
                if 'btn_order' in bug_keys:
                    what.append('нет кнопки заказа')
                s.others['cannot_buy'].append(
                    CriticalItem('cannot_buy', city, path, ', '.join(what), url))

        # 5) Битые переменные в текстах.
        if getattr(r, 'has_text_issues', False):
            n = len(getattr(r, 'text_issues', []) or [])
            s.others['text'].append(
                CriticalItem('text', city, path, f'{n} битых', url))

    return s
