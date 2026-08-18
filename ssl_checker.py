# -*- coding: utf-8 -*-
"""
ssl_checker.py - проверка SSL-сертификата хоста (чек-лист «корректно настроен
SSL-сертификат»).

Внешние сервисы (leaderssl и подобные) не нужны: сертификат отдаёт сам сервер
при рукопожатии TLS, читаем его стандартной библиотекой.

Что проверяем:
  • срок действия - истёк = БАГ; осталось меньше 14 дней = БАГ (не успеют
    продлить), меньше 30 = предупреждение;
  • имя хоста - сертификат выписан на этот домен (или на *.домен). Несовпадение
    = баг: браузер покажет предупреждение и посетитель уйдёт;
  • цепочка - сервер отдаёт промежуточные сертификаты. Неполная цепочка
    коварна: в десктопных браузерах часто работает (они дотягивают недостающее
    сами), а на части мобильных и у поисковых роботов - нет;
  • дата начала - сертификат ещё не вступил в силу (редко, но бывает при
    неверных часах на сервере).

Проверка СКВОЗНАЯ: сертификат один на хост, поэтому дёргаем один раз на
поддомен, не на каждую страницу.
"""
from __future__ import annotations

import datetime as _dt
import socket
import ssl
from typing import Optional

# Сколько дней до истечения считаем «уже пора» и «скоро».
SSL_CRITICAL_DAYS = 14
SSL_WARN_DAYS = 30

_TIMEOUT = 15


def _parse_dt(value: str) -> Optional[_dt.datetime]:
    """Дата из сертификата («Jun  1 12:00:00 2026 GMT») → datetime UTC."""
    if not value:
        return None
    try:
        d = _dt.datetime.strptime(value, '%b %d %H:%M:%S %Y %Z')
        return d.replace(tzinfo=_dt.timezone.utc)
    except Exception:      # noqa: BLE001
        return None


def cert_hosts(cert: dict) -> list:
    """Все имена, на которые выписан сертификат: CN + subjectAltName.
    ЧИСТАЯ функция - есть юнит-тест."""
    out = []
    for поле in (cert or {}).get('subject') or ():
        for k, v in поле:
            if k == 'commonName' and v:
                out.append(str(v).lower())
    for k, v in (cert or {}).get('subjectAltName') or ():
        if k.lower() == 'dns' and v:
            out.append(str(v).lower())
    return list(dict.fromkeys(out))


def host_matches(host: str, names: list) -> bool:
    """Подходит ли хост под имена сертификата (с учётом *.example.ru).
    Маска покрывает РОВНО один уровень: *.a.ru подходит к b.a.ru, но не к
    c.b.a.ru - так же считают браузеры.
    ЧИСТАЯ функция - есть юнит-тест."""
    h = (host or '').lower().strip('.')
    if not h:
        return False
    for name in names or []:
        n = (name or '').lower().strip('.')
        if not n:
            continue
        if n == h:
            return True
        if n.startswith('*.'):
            хвост = n[2:]
            if h.endswith('.' + хвост) and h.count('.') == хвост.count('.') + 1:
                return True
    return False


def analyze_cert(cert: dict, host: str, chain_len: int = 0,
                 now: Optional[_dt.datetime] = None) -> dict:
    """Разбор сертификата в находки. Отделён от сети, чтобы проверялся тестами.

    → {'issues': [...], 'warnings': [...], 'expires': iso|None,
       'days_left': int|None, 'issuer': str, 'hosts': [...], 'chain_len': int}
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    out = {'issues': [], 'warnings': [], 'expires': None, 'days_left': None,
           'issuer': '', 'hosts': [], 'chain_len': chain_len}
    if not cert:
        out['issues'].append('не удалось прочитать SSL-сертификат сайта')
        return out

    имена = cert_hosts(cert)
    out['hosts'] = имена
    издатель = ''
    for поле in cert.get('issuer') or ():
        for k, v in поле:
            if k in ('organizationName', 'commonName') and not издатель:
                издатель = str(v)
    out['issuer'] = издатель

    не_ранее = _parse_dt(cert.get('notBefore', ''))
    не_позже = _parse_dt(cert.get('notAfter', ''))
    if не_позже:
        out['expires'] = не_позже.isoformat()
        дней = (не_позже - now).days
        out['days_left'] = дней
        if дней < 0:
            out['issues'].append('SSL-сертификат истёк - браузер показывает '
                                 'предупреждение вместо сайта')
        elif дней <= SSL_CRITICAL_DAYS:
            out['issues'].append(f'SSL-сертификат истекает через {дней} дн. - '
                                 f'продлить сейчас')
        elif дней <= SSL_WARN_DAYS:
            out['warnings'].append(f'SSL-сертификат истекает через {дней} дн.')
    else:
        out['warnings'].append('в SSL-сертификате не разобрать срок действия')

    if не_ранее and не_ранее > now:
        out['issues'].append('SSL-сертификат ещё не вступил в силу - '
                             'проверить дату начала и часы на сервере')

    if имена and not host_matches(host, имена):
        out['issues'].append('SSL-сертификат выписан на другой домен - '
                             'браузер не считает соединение доверенным')

    # 1 сертификат в цепочке = сервер отдал только свой, без промежуточных.
    if chain_len == 1:
        out['warnings'].append('сервер отдаёт неполную цепочку SSL - часть '
                               'мобильных браузеров и роботов не подтвердит '
                               'сертификат')
    return out


def check_ssl(host: str, port: int = 443, *,
              now: Optional[_dt.datetime] = None) -> dict:
    """Скачать сертификат хоста и разобрать. Сеть - только здесь.

    Ошибку проверки НЕ выдаём за дефект сайта: если не дозвонились (таймаут,
    DNS, прокси), пишем 'error' и молчим - так же, как в остальных проверках.
    """
    out = {'host': host, 'available': False, 'error': None,
           'issues': [], 'warnings': []}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                try:
                    цепочка = len(ss.get_verified_chain() or ())
                except Exception:      # noqa: BLE001
                    цепочка = 0        # метод есть не во всех сборках Python
    except ssl.SSLCertVerificationError as e:
        # Сертификату не доверяют - это и есть находка, а не сбой проверки.
        out['available'] = True
        out['issues'].append(f'SSL-сертификат не проходит проверку: '
                             f'{getattr(e, "verify_message", None) or e}')
        return out
    except Exception as e:             # noqa: BLE001
        out['error'] = f'{type(e).__name__}: {e}'
        return out
    out['available'] = True
    out.update(analyze_cert(cert, host, цепочка, now=now))
    return out
