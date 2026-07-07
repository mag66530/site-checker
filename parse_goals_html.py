"""
Парсер выгрузки «Конверсии» Яндекс.Метрики (сохранённая HTML-страница) в
компактный каталог целей catalogs/goals-<pid>.json - тот же формат, что уже
использует «Проверка целей».

Запуск:
    python parse_goals_html.py <файл.html> --pid smu-uz [--домен https://...] \
        [--проект "СМУ УЗ"]

Проект и счётчик берутся из самой страницы (<title> и counterId), их можно
переопределить флагами. Домен нужен для url-целей и прогона - если не задан,
подставляется пустой, и его можно вписать в JSON позже.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

# ── Извлечение целей из HTML ─────────────────────────────────────────
_ID_RE = re.compile(r'class="goals-list-item-header__id">ID (\d+)</span>')
_NAME_RE = re.compile(r'class="link__text">(.*?)</span>', re.S)
_TYPE_RE = re.compile(
    r'goals-list-item-header__type"><div class="ellipsis-expandable-text__text"'
    r' title="([^"]*)"')
_TAG_RE = re.compile(r'<[^>]+>')


def _detag(s: str) -> str:
    return html.unescape(_TAG_RE.sub(' ', s or '')).strip()


def _извлечь_цели(text: str) -> list[dict]:
    """Список сырых целей {номер, название, условие} по DOM-структуре Метрики.
    Пустые по названию строки (внутренние подцели составных) отбрасываются."""
    сырьё: dict[str, dict] = {}
    for m in _ID_RE.finditer(text):
        gid = m.group(1)
        before = text[:m.start()]
        names = list(_NAME_RE.finditer(before))
        name = _detag(names[-1].group(1)) if names else ''
        tm = _TYPE_RE.search(text, m.end(), m.end() + 600)
        cond = html.unescape(tm.group(1)) if tm else ''
        cur = сырьё.get(gid)
        if cur is None or (not cur['название'] and name):
            сырьё[gid] = {'номер': gid, 'название': name, 'условие': cond}
    return [g for g in сырьё.values() if g['название']]


# ── Классификация условия в тип цели ─────────────────────────────────
_ID_TOKEN = re.compile(r'идентификатор(?:\s+цели)?(?:\s+содержит)?\s*:\s*([\w\-.]+)')
_AUTO_PHRASES = (
    'все номера телефонов', 'отправка формы', 'отправк', 'страницы подтверждения',
    'скачивание', 'использование поиск', 'контактные данные', 'оплаты заказа',
    'клики по всем', 'мессенджер', 'email-адрес', 'по всем ссылкам',
)


def _классифицировать(g: dict) -> dict:
    """Дополняет цель полями тип / идентификаторы / содержит / url_часть -
    по тем же правилам, что и уже собранные каталоги."""
    c = (g['условие'] or '').strip()
    low = c.lower()
    g.setdefault('идентификаторы', [])
    g.setdefault('содержит', False)
    g.setdefault('url_часть', '')

    if low.startswith('url регулярное') or 'регулярное выражение' in low:
        g['тип'] = 'url_re'
        g['url_часть'] = c.split(':', 1)[1].strip() if ':' in c else ''
        return g
    if 'идентификатор' in low:
        ids = _ID_TOKEN.findall(c)
        g['идентификаторы'] = ids
        g['содержит'] = 'содержит' in low
        назв = (g['название'] or '').lower()
        if any('jivo' in i.lower() for i in ids) or назв.startswith('jivo'):
            g['тип'] = 'jivo'
        else:
            g['тип'] = 'js'
        return g
    if 'url содержит' in low:
        g['тип'] = 'url'
        g['url_часть'] = c.split(':', 1)[1].strip() if ':' in c else ''
        return g
    if (g['название'] or '').lower().startswith('jivo'):
        g['тип'] = 'jivo'
        return g
    if any(p in low for p in _AUTO_PHRASES) or low == 'составная цель' or not c:
        g['тип'] = 'auto'
        return g
    # неожиданное условие - помечаем составной, чтобы не потерять
    g['тип'] = 'composite'
    return g


def построить_каталог(html_text: str, pid: str, проект: str | None,
                      домен: str, счётчик: str | None) -> dict:
    m_cnt = re.search(r'counterId"\s*:\s*"?(\d{6,9})', html_text)
    m_title = re.search(r'<title>([^<]*?)(?:\s*[-—]\s*Конверсии)?', html_text)
    цели = [_классифицировать(g) for g in _извлечь_цели(html_text)]
    return {
        'проект': проект or (_detag(m_title.group(1)) if m_title else pid),
        'счётчик': int(счётчик or (m_cnt.group(1) if m_cnt else 0)),
        'домен': домен,
        'источник': 'Метрика (Конверсии), выгрузка HTML',
        'цели': цели,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('html_file')
    ap.add_argument('--pid', required=True, help='код каталога, напр. smu-uz')
    ap.add_argument('--домен', default='', dest='domain')
    ap.add_argument('--проект', default=None, dest='project')
    ap.add_argument('--счётчик', default=None, dest='counter')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    text = Path(a.html_file).read_text(encoding='utf-8', errors='ignore')
    cat = построить_каталог(text, a.pid, a.project, a.domain, a.counter)
    out = Path(a.out) if a.out else Path('catalogs') / f'goals-{a.pid}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding='utf-8')

    from collections import Counter
    типы = Counter(g['тип'] for g in cat['цели'])
    print(f"{cat['проект']} · счётчик {cat['счётчик']} · целей {len(cat['цели'])} · {dict(типы)}")
    print(f"→ {out}")


if __name__ == '__main__':
    main()
