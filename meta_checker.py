"""
meta_checker.py – п.1.3.1 чек-листа: единственность ключевых SEO-тегов.

На странице ключевые теги должны быть в ЕДИНСТВЕННОМ экземпляре:
  • <title>              – ровно 1 непустой (0 → нет тега; ≥2 → дубли);
  • <meta name=descr…>   – ровно 1 непустой (0 → нет; ≥2 → дубли; 1 пустой → пустой);
  • <h1>                 – не больше 1 (≥2 → несколько H1). Отсутствие H1 не
                           дублируем — его по типу страницы уже ловит
                           структурная проверка (лист «Структура страниц»).
Плюс дубли H2: два и более <h2> с ОДИНАКОВЫМ текстом — шаблонная ошибка.
(Несколько РАЗНЫХ H2 — норма, не баг: их «текстовость» проверяет п.1.3.2.)

Считаем по «структурному» HTML: вырезаем <svg> (там бывают свои <title>) и
<template> (неактивный контент), чтобы не завышать счётчики.
"""
from __future__ import annotations

import re
from collections import Counter

MAX_SHOWN = 3        # сколько примеров текста показать в отчёте

_RE_SVG = re.compile(r'<svg\b[^>]*>.*?</svg>', re.I | re.S)
_RE_TEMPLATE = re.compile(r'<template\b[^>]*>.*?</template>', re.I | re.S)
_RE_TAGS = re.compile(r'<[^>]+>')
_RE_META = re.compile(r'<meta\b[^>]*>', re.I)


def _clean_struct(html: str) -> str:
    html = _RE_SVG.sub(' ', html or '')
    html = _RE_TEMPLATE.sub(' ', html)
    return html


def _txt(inner: str) -> str:
    s = _RE_TAGS.sub(' ', inner or '')
    s = s.replace('&nbsp;', ' ').replace('&amp;', '&')
    return re.sub(r'\s+', ' ', s).strip()


def _tag_texts(html: str, tag: str) -> list[str]:
    return [_txt(m) for m in re.findall(rf'<{tag}\b[^>]*>(.*?)</{tag}>', html, re.I | re.S)]


def _meta_descriptions(html: str) -> list[str]:
    """Содержимое всех <meta name="description"> (в т.ч. пустых). og:description
    и прочие НЕ считаем — только name="description"."""
    out = []
    for tag in _RE_META.findall(html):
        if not re.search(r'''name\s*=\s*["']?\s*description\b''', tag, re.I):
            continue
        m = (re.search(r'content\s*=\s*"([^"]*)"', tag, re.I)
             or re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
             or re.search(r'content\s*=\s*([^\s"\'>]+)', tag, re.I))
        out.append(m.group(1).strip() if m else '')
    return out


def _short(s: str, n: int = 60) -> str:
    s = (s or '').strip()
    return s if len(s) <= n else s[:n - 1] + '…'


def check_meta_uniqueness(html: str, url: str = '', type_code: str = '') -> dict:
    """Проверка единственности title/description/H1 (+ дубли H2).
    Возвращает {'issues': [...], 'counts': {...}}."""
    h = _clean_struct(html or '')
    titles = [t for t in _tag_texts(h, 'title') if t]
    h1s = [t for t in _tag_texts(h, 'h1') if t]
    h2s = [t for t in _tag_texts(h, 'h2') if t]
    descs = _meta_descriptions(h)
    descs_ne = [d for d in descs if d]

    issues: list[dict] = []

    # ── title ──
    if len(titles) == 0:
        issues.append({'тип': 'title', 'найдено': '—',
                       'пояснение': 'нет тега <title> на странице'})
    elif len(titles) >= 2:
        issues.append({'тип': 'title', 'найдено': f'{len(titles)} тегов',
                       'пояснение': 'на странице несколько <title>: '
                                    + ' | '.join(_short(t, 45) for t in titles[:MAX_SHOWN])})

    # ── meta description ──
    if len(descs) == 0:
        issues.append({'тип': 'description', 'найдено': '—',
                       'пояснение': 'нет meta description'})
    elif len(descs) >= 2:
        issues.append({'тип': 'description', 'найдено': f'{len(descs)} тегов',
                       'пояснение': 'несколько meta description: '
                                    + ' | '.join(_short(d, 45) for d in descs[:MAX_SHOWN])})
    elif not descs_ne:
        issues.append({'тип': 'description', 'найдено': 'пустой',
                       'пояснение': 'meta description есть, но пустой'})

    # ── H1 (несколько) ──
    if len(h1s) >= 2:
        issues.append({'тип': 'h1', 'найдено': f'{len(h1s)} шт.',
                       'пояснение': 'на странице несколько H1: '
                                    + ' | '.join(_short(t, 40) for t in h1s[:MAX_SHOWN])})

    # ── H2 дубли (одинаковый текст) ──
    norm = Counter(re.sub(r'\s+', ' ', t.strip().lower()) for t in h2s)
    dups = [(t, n) for t, n in norm.items() if n >= 2]
    for t, n in dups[:MAX_SHOWN]:
        issues.append({'тип': 'h2', 'найдено': f'×{n}',
                       'пояснение': f'H2 «{_short(t, 45)}» повторяется {n} раза(раз)'})

    return {
        'issues': issues,
        'counts': {'title': len(titles), 'description': len(descs),
                   'h1': len(h1s), 'h2': len(h2s), 'h2_dups': len(dups)},
    }
