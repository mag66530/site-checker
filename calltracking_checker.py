"""
calltracking_checker.py - проверка работы замены рекламного номера
(коллтрекинг, статическая подмена). Пункт чек-листа «Проверка работы замены
рекламного номера (мониторинг)».

Как устроена подмена на сайте (Sipuni static call-tracking, файл
sipuni-calltracking.js):
  • в статическом HTML в элементах .ct_phone стоит обычный (SEO/общий) номер;
  • при РЕКЛАМНОМ визите (?utm_source=yandex → Яндекс.Директ,
    ?utm_source=google → Google Ads; либо gclid / рекламный referrer)
    скрипт заменяет номер на РЕКЛАМНЫЙ подменный;
  • пул номеров и источники заданы прямо в HTML в init-вызове
    sipuniCalltracking({sources, phones}).

Этот модуль - СТАТИЧЕСКАЯ проверка (по одному HTML, без браузера): убеждаемся,
что (1) скрипт коллтрекинга подключён, (2) в его конфиге задан рекламный
номер и (3) он совпадает с phone_ad города из КП. JS здесь не выполняется -
реально ли срабатывает подмена в браузере, проверяет отдельный браузерный
чек (calltracking_browser.py, ?utm_source=yandex → читаем .ct_phone).

Статусы check_ad_number:
  ok     - коллтрекинг есть, рекламный номер совпадает с КП;
  bug    - коллтрекинг есть, но номер в конфиге НЕ совпадает с КП;
  na     - подмена (скрипт коллтрекинга) на странице не обнаружена, либо
           пул номеров не удалось разобрать - нейтрально (не баг: часть
           проектов может не использовать JS-подмену).
"""
import re

# Скрипт Sipuni подключён (…/sipuni-calltracking.js, допускаем .min).
_RE_SIPUNI_SCRIPT = re.compile(r'sipuni[-_]?calltracking(?:\.min)?\.js', re.I)
# init-вызов на странице: sipuniCalltracking({...}, window)
_RE_SIPUNI_INIT = re.compile(r'sipuniCalltracking\s*\(', re.I)
# Номер в пуле: 'phone': ['74991300786']  /  "phone":["7..."] (первый в списке).
_RE_POOL_PHONE = re.compile(
    r'''['"]phone['"]\s*:\s*\[\s*['"](\d{6,15})['"]''', re.I)
# Блок phones:[...] целиком - запасной разбор, если формат чуть иной.
_RE_PHONES_BLOCK = re.compile(r'phones\s*:\s*\[(.*?)\]', re.I | re.S)
_RE_QUOTED_NUM = re.compile(r'''['"](\d{10,15})['"]''')


def _nat(num) -> str:
    """Национальный номер (10 цифр для РФ/КЗ) для сверки вне формата."""
    d = re.sub(r'\D', '', str(num or ''))
    if not d:
        return ''
    if d.startswith('998') and len(d) >= 12:
        return d[-9:]
    if d.startswith('375') and len(d) >= 12:
        return d[-9:]
    if len(d) >= 11 and d[0] in '78':
        return d[-10:]
    if len(d) == 10:
        return d
    return d[-10:] if len(d) > 10 else d


def parse_config(html: str) -> dict:
    """Разобрать коллтрекинг Sipuni из HTML главной.
    Возвращает {has_script, has_init, ad_numbers: set(нац. номеров пула)}."""
    html = html or ''
    has_script = bool(_RE_SIPUNI_SCRIPT.search(html))
    has_init = bool(_RE_SIPUNI_INIT.search(html))
    ad_numbers = {_nat(m.group(1)) for m in _RE_POOL_PHONE.finditer(html)}
    ad_numbers.discard('')
    # Запасной разбор: если 'phone':[...] не нашли, но блок phones:[...] есть -
    # берём длинные номера из него.
    if not ad_numbers:
        mb = _RE_PHONES_BLOCK.search(html)
        if mb:
            ad_numbers = {_nat(m.group(1))
                          for m in _RE_QUOTED_NUM.finditer(mb.group(1))}
            ad_numbers.discard('')
    return {'has_script': has_script, 'has_init': has_init,
            'ad_numbers': ad_numbers}


def check_ad_number(html: str, kp_ad: str) -> dict:
    """Сверить рекламный номер подмены (коллтрекинг) с phone_ad из КП.

    Возвращает {status, comment, configured, kp} или None, если в КП нет
    рекламного номера (сверять не с чем)."""
    kp_nat = _nat(kp_ad)
    if not kp_nat:
        return None
    cfg = parse_config(html)
    has_ct = cfg['has_script'] or cfg['has_init']
    configured = sorted(cfg['ad_numbers'])

    if not has_ct and not configured:
        return {'status': 'na', 'configured': [], 'kp': kp_nat,
                'comment': 'подмена рекламного номера (скрипт коллтрекинга) '
                           'на странице не обнаружена'}
    if not configured:
        return {'status': 'na', 'configured': [], 'kp': kp_nat,
                'comment': 'скрипт коллтрекинга подключён, но пул рекламных '
                           'номеров в конфиге не найден - проверьте вручную'}
    if kp_nat in cfg['ad_numbers']:
        return {'status': 'ok', 'configured': configured, 'kp': kp_nat,
                'comment': 'рекламный номер подмены настроен и совпадает с КП'}
    return {'status': 'bug', 'configured': configured, 'kp': kp_nat,
            'comment': 'рекламный номер в коллтрекинге не совпадает с КП '
                       '(в конфиге сайта: '
                       + ', '.join(configured) + f'; в КП: {kp_nat})'}
