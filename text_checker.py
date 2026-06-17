"""
text_checker.py – поиск битых переменных в видимом тексте страницы.

Точная копия логики из Node.js версии:
  - {{...}}            – незаменённые шаблонные подстановки
  - %name%             – Битрикс-стиль (мин 3 символа имени!)
  - undefined          – отдельным словом
  - [object Object]    – артефакт склейки JSON

Перед поиском удаляем из HTML то, что не видит пользователь:
  - <script>, <style>, HTML-комментарии
  - значения атрибутов href, src, srcset, style, data-*
  (там часто URL-кодировка вида %D0%97, и это НЕ битая переменная)
"""
import re
from dataclasses import dataclass


# Паттерны: имя → regex
BUILTIN_PATTERNS = {
    '{{...}}':         re.compile(r'\{\{[^{}\n]{1,80}\}\}'),
    # Имя минимум 3 символа – это исключает URL-кодировку (%XX, ровно 2 hex)
    '%переменная%':    re.compile(r'%[a-zA-Zа-яА-Я_][a-zA-Zа-яА-Я0-9_]{2,40}%'),
    'undefined':       re.compile(r'(^|[^\w])undefined([^\w]|$)'),
    '[object Object]': re.compile(r'\[object Object\]'),
}

MAX_FINDINGS_PER_PATTERN = 5


@dataclass
class TextIssue:
    pattern: str        # имя паттерна ('{{...}}', и т.д.)
    match: str          # что именно нашли ('{{city}}')
    context: str        # окружающий текст для понимания где это


def parse_patterns_config(text: str | None) -> list[str]:
    """Из строки 'pat1, pat2' получить список активных паттернов."""
    if not text:
        return list(BUILTIN_PATTERNS.keys())
    return [
        p.strip() for p in text.split(',')
        if p.strip() in BUILTIN_PATTERNS
    ]


def strip_non_visible(html: str) -> str:
    """Удалить из HTML то, что не показывается пользователю."""
    # <script>...</script>, <style>...</style>, комментарии
    html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', ' ', html, flags=re.IGNORECASE)
    html = re.sub(r'<style\b[^>]*>[\s\S]*?</style>', ' ', html, flags=re.IGNORECASE)
    html = re.sub(r'<!--[\s\S]*?-->', ' ', html)
    # Значения атрибутов с URL/inline-стилями – там кодировка
    html = re.sub(
        r'''\s(?:href|src|srcset|action|style|data-[\w-]+)\s*=\s*(?:"[^"]*"|'[^']*')''',
        ' ', html, flags=re.IGNORECASE,
    )
    return html


def html_to_visible_text(html: str) -> str:
    """Превратить HTML в видимый текст."""
    text = strip_non_visible(html)
    # Вырезаем сами теги
    text = re.sub(r'<[^>]+>', ' ', text)
    # HTML entities
    text = (text.replace('&nbsp;', ' ')
                .replace('&amp;', '&')
                .replace('&lt;', '<')
                .replace('&gt;', '>')
                .replace('&quot;', '"'))
    # Нормализуем пробелы
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def make_context(text: str, index: int, length: int, span: int = 80) -> str:
    """Кусок текста вокруг найденного места для отчёта."""
    start = max(0, index - span)
    end = min(len(text), index + length + span)
    ctx = re.sub(r'\s+', ' ', text[start:end]).strip()
    if start > 0:
        ctx = '…' + ctx
    if end < len(text):
        ctx = ctx + '…'
    return ctx


def find_text_issues(html: str, patterns_config: str | None = None) -> list[TextIssue]:
    """
    Найти битые переменные в HTML.
    Возвращает список TextIssue (макс 5 находок на каждый паттерн).
    """
    if not html or not isinstance(html, str):
        return []
    active = parse_patterns_config(patterns_config)
    if not active:
        return []

    visible = html_to_visible_text(html)
    issues = []

    for name in active:
        pattern = BUILTIN_PATTERNS[name]
        count = 0
        for m in pattern.finditer(visible):
            matched = m.group(0)
            pos = m.start()

            # Для undefined - извлекаем именно слово
            if name == 'undefined':
                idx = matched.find('undefined')
                matched = 'undefined'
                pos += idx

            issues.append(TextIssue(
                pattern=name,
                match=matched,
                context=make_context(visible, pos, len(matched)),
            ))
            count += 1
            if count >= MAX_FINDINGS_PER_PATTERN:
                break

    return issues
