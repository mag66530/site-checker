# -*- coding: utf-8 -*-
"""
maps_compare.py - сверка данных с карточек карт (Яндекс/2ГИС/Google) с КП.

Один результат на карту × город: MapCheckResult. Источники пишут в общий
формат (name, phone, address, site, available, error) - сверка не знает, с
какой картой работает, поэтому упавший источник (напр. Google) не мешает
остальным (см. yandex_map_check.py, docstring).

Сверка адреса - ТЕ ЖЕ правила, что уже проверены на «Проверке КП» для сайта
(kp.py): address_match(). Телефон - ОТДЕЛЬНО от «Проверки КП»: там сравнение
идёт со всем набором номеров города (phone_set - SEO/Реклама/Общий/старые из
all_phones), а карта показывает ОДИН публичный номер организации - это всегда
«Общий Город» (просьба заказчика: карты сверяем только с ним, не с полным
набором - иначе телефон коллтрекинга/SEO ошибочно засчитывался бы как
совпадение там, где сайт и карта должны показывать один и тот же общий номер).
"""
import re
from dataclasses import dataclass, field

from kp import KPRow, normalize_phone, address_match, phones_in_cell


@dataclass
class MapCheckResult:
    source: str          # 'yandex' | '2gis' | 'google'
    city: str
    url: str
    available: bool                  # карточка прочиталась (не обязательно совпала)
    country: str = ''    # из той же строки КП - для листа «Карты» (Страна|Город)
    # Ссылки на карту в КП просто НЕТ (пустая колонка «Карта» под Яндекс
    # Бизнес/2ГИС/Google) - это НЕ ошибка и НЕ предупреждение, а норма: у
    # города нет карточки на этом сервисе. Отдельно от available=False
    # («ссылка ЕСТЬ, но карточка не прочиталась» - это уже реальная проблема).
    no_link: bool = False
    phone_match: bool | None = None  # None - сверять было нечего (нет тел. в КП/карте)
    address_match: bool | None = None
    site_match: bool | None = None
    name: str = ''
    error: str = ''
    issues: list[str] = field(default_factory=list)
    # Структурированные расхождения для видимой колонки в отчёте (не только
    # всплывающая подсказка): [{'field': 'телефон', 'kp': '...', 'card': '...'}].
    details: list[dict] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return self.available and not self.issues

    @property
    def is_warning(self) -> bool:
        """Ссылка ЕСТЬ, но карточка не прочиталась - реальная проблема (сайт
        сервиса изменился/заблокировал/страница сломана). Отсутствие самой
        ссылки (no_link) сюда не входит - это не предупреждение, а норма."""
        return (not self.available) and not self.no_link

    @property
    def is_error(self) -> bool:
        return self.available and bool(self.issues)


def _norm_site(url: str) -> str:
    """Домен без схемы/www/слэша - 'stalmetural.ru', для грубого сравнения."""
    s = (url or '').lower().strip()
    s = re.sub(r'^https?://', '', s)
    s = re.sub(r'^www\.', '', s)
    return s.split('/')[0]


def compare(source: str, city: str, url: str, card: dict, kp_row: KPRow | None) -> MapCheckResult:
    """card - результат extract()/afetch() конкретной карты (name/phone/address/
    site/available/error). kp_row - строка КП этого города (None - в КП города
    нет вообще, сверять не с чем, только показываем данные карточки)."""
    _country = kp_row.country if kp_row else ''
    # Ссылки на карту в КП нет вообще (пустая колонка) - смотрим на URL, а НЕ
    # на card['error'], чтобы не зависеть от конкретного текста ошибки из
    # yandex_map_check/twogis_map_check. Норма, не ошибка и не предупреждение.
    if not url:
        return MapCheckResult(source=source, city=city, url=url, available=False,
                              country=_country, no_link=True)
    if not card.get('available'):
        return MapCheckResult(source=source, city=city, url=url, available=False,
                              country=_country,
                              error=card.get('error') or 'карточка не прочиталась')

    res = MapCheckResult(source=source, city=city, url=url, available=True,
                         country=_country, name=card.get('name', ''))
    if kp_row is None:
        res.issues.append('города нет в КП - сверять не с чем')
        res.details.append({'field': 'город', 'kp': '–', 'card': city})
        return res

    # Только «Общий Город» - публичный номер организации, тот же, что должен
    # быть виден и на карте. Три случая (то же правило, что в check_variables
    # для «Проверки КП» - мусор в КП НЕ равно пустому полю):
    #   • ячейка пустая              → сверять нечего, phone_match=None;
    #   • ячейка НЕ пустая, но это не
    #     номер (напр. тестовое «2») → ВСЕГДА ✗ - в КП есть инфа, и она не
    #     номер, значит заведомо не совпадает с картой;
    #   • ячейка - валидный номер   → сверяем с телефоном карты как обычно.
    # Разбор валидного номера МЯГКИЙ (как для слотов на «Проверке КП») -
    # понимает и «голый» номер без кода страны.
    raw_common = (kp_row.phone_common or '').strip()
    common_phones = phones_in_cell(raw_common)
    if common_phones:
        map_phone = normalize_phone(card.get('phone', ''))
        res.phone_match = bool(map_phone) and map_phone in common_phones
        if not res.phone_match:
            _kp_disp = ', '.join(common_phones)
            res.issues.append(
                f'телефон на карте ({card.get("phone") or "—"}) не совпал с КП')
            res.details.append({'field': 'телефон', 'kp': _kp_disp,
                                'card': card.get('phone') or '–'})
    elif raw_common and raw_common not in ('–', '-'):
        res.phone_match = False
        res.issues.append(
            f'в КП «Общий Город» указано «{raw_common}» - не похоже на '
            f'телефон, но поле не пустое')
        res.details.append({'field': 'телефон', 'kp': raw_common,
                            'card': card.get('phone') or '–'})

    if kp_row.address:
        res.address_match = address_match(card.get('address', ''), kp_row.address)
        if not res.address_match:
            res.issues.append(
                f'адрес на карте ({card.get("address") or "—"}) не совпал с КП')
            res.details.append({'field': 'адрес', 'kp': kp_row.address,
                                'card': card.get('address') or '–'})

    if kp_row.domain:
        map_site = _norm_site(card.get('site', ''))
        # ТОЛЬКО карта = сам домен КП или ЕГО поддомен - совпадение (тот же
        # бренд, просто городской поддомен). ОБРАТНОЕ (карта = корневой домен,
        # а КП для этого города ждёт конкретный поддомен, напр.
        # izhevsk.stalmetural.ru) - это НЕ совпадение: КП явно указывает,
        # какой сайт должен стоять у ЭТОЙ карточки, и корневой домен вместо
        # городского поддомена - реальная проблема (карточку не обновили),
        # не "тот же бренд" (подтверждено заказчиком на Севастополе/Ижевске).
        res.site_match = bool(map_site) and (
            map_site == kp_row.domain or map_site.endswith('.' + kp_row.domain))
        if not res.site_match:
            res.issues.append(
                f'сайт на карте ({card.get("site") or "—"}) не совпал с КП '
                f'({kp_row.domain})')
            res.details.append({'field': 'сайт', 'kp': kp_row.domain,
                                'card': card.get('site') or '–'})

    return res
