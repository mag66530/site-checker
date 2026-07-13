"""
security_checker.py - заголовки безопасности HTTP-ответа (доп. чек-лист,
пункт «1.8 Нет ошибок заголовков безопасности»).

Проверяем ответ сервера, не HTML. Заголовки уже собраны http_checker'ом
(CheckResult.headers, ключи в lower). Политика МЯГКАЯ - сайт-визитка не
банк:
  • нет HSTS / X-Content-Type-Options / защиты от кликджекинга = ПРЕДУПРЕЖДЕНИЕ;
  • битое ЗНАЧЕНИЕ заголовка (HSTS max-age=0, obsolete ALLOW-FROM,
    X-Content-Type-Options не nosniff, конфликтующие дубли) = БАГ -
    заголовок есть, но настроен во вред/впустую;
  • CSP: отсутствие = предупреждение (чек-лист требует настроенный
    Content-Security-Policy); если есть, но с unsafe-inline И unsafe-eval
    сразу - защита от XSS фактически выключена = предупреждение.

«Ошибки заголовков» из формулировки пункта = именно битые значения (issues),
а не сам факт отсутствия (warnings).
"""
import re
from typing import Optional

# HTTPS-only заголовки: на http их не бывает и требовать бессмысленно.
_HSTS = 'strict-transport-security'
_CSP = 'content-security-policy'
_XFO = 'x-frame-options'
_XCTO = 'x-content-type-options'
_REFERRER = 'referrer-policy'


def _dup_conflict(raw: str) -> bool:
    """Заголовок склеен из нескольких значений (http_checker соединяет
    повторы через ', '). Для одиночных заголовков разные значения =
    конфликт: браузеры видят два указания и ведут себя непредсказуемо."""
    parts = [p.strip().lower() for p in (raw or '').split(',') if p.strip()]
    return len(set(parts)) > 1


def check_security_headers(headers: Optional[dict], url: str = '') -> Optional[dict]:
    """Проверить заголовки безопасности одного ответа.

    headers - dict финального ответа (ключи lower) | None.
    Возвращает dict для CheckResult.security или None (нет заголовков -
    проверять нечего, например сетевой сбой)."""
    if not headers:
        return None
    is_https = (url or '').lower().startswith('https')
    issues, warnings, present = [], [], []

    def _get(name):
        return headers.get(name)

    # ── HSTS (только https) ──
    hsts = _get(_HSTS)
    if is_https:
        if hsts is None:
            warnings.append('нет заголовка Strict-Transport-Security (HSTS) - '
                            'браузер не форсит https')
        else:
            present.append('HSTS')
            m = re.search(r'max-age\s*=\s*(\d+)', hsts, re.I)
            if m and int(m.group(1)) == 0:
                issues.append('HSTS с max-age=0 - заголовок есть, но отключает '
                              'сам себя')
            elif not m:
                issues.append('HSTS без директивы max-age - невалидное значение')

    # ── X-Content-Type-Options ──
    xcto = _get(_XCTO)
    if xcto is None:
        warnings.append('нет заголовка X-Content-Type-Options: nosniff - '
                        'браузер может угадывать тип файла')
    else:
        present.append('X-Content-Type-Options')
        if 'nosniff' not in xcto.lower():
            issues.append(f'X-Content-Type-Options = «{xcto.strip()}» вместо '
                          f'nosniff - значение не работает')

    # ── Защита от кликджекинга: X-Frame-Options ИЛИ CSP frame-ancestors ──
    xfo = _get(_XFO)
    csp = _get(_CSP)
    has_frame_ancestors = bool(csp and 'frame-ancestors' in csp.lower())
    if xfo is None and not has_frame_ancestors:
        warnings.append('нет защиты от кликджекинга (X-Frame-Options или CSP '
                        'frame-ancestors)')
    elif xfo is not None:
        present.append('X-Frame-Options')
        low = xfo.lower()
        if 'allow-from' in low:
            issues.append('X-Frame-Options: ALLOW-FROM - устаревшая директива, '
                          'браузеры её игнорируют (нужен CSP frame-ancestors)')
        elif _dup_conflict(xfo):
            issues.append(f'X-Frame-Options задан дважды с разными значениями '
                          f'(«{xfo.strip()}») - конфликт')
        elif low.strip() not in ('deny', 'sameorigin'):
            warnings.append(f'X-Frame-Options = «{xfo.strip()}» - нестандартное '
                            f'значение (ожидается DENY или SAMEORIGIN)')

    # ── CSP ──
    if csp:
        present.append('CSP')
        low = csp.lower()
        if 'unsafe-inline' in low and 'unsafe-eval' in low:
            warnings.append('CSP с unsafe-inline и unsafe-eval сразу - защита '
                            'от XSS фактически выключена')
    else:
        warnings.append('нет заголовка Content-Security-Policy - '
                        'нет политики загрузки скриптов/стилей (защита от XSS)')

    # ── Referrer-Policy - мелочь, только отметка присутствия ──
    if _get(_REFERRER):
        present.append('Referrer-Policy')

    return {
        'checked': True,
        'present': present,
        'issues': issues,
        'warnings': warnings,
    }
