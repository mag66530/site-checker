# -*- coding: utf-8 -*-
"""
yandex_map_check.py - контакты организации с публичной карточки Яндекс.Карт
(yandex.ru/maps/org/...), сверка с КП. Используется в «Проверке КП».

Страница та же самая, что review_priority.py уже дёргает ради рейтинга -
сессия/авторизация не нужна. Берём microdata (itemprop) вместо текста/классов:
классы у Яндекса меняются часто, itemprop="name/telephone/url", meta
itemprop="address" - разметка schema.org, живёт дольше вёрстки.

Если карточку не удалось прочитать (сайт недоступен, разметка изменилась,
у города нет ссылки в КП) - это ШТАТНЫЙ исход (available=False), не ошибка:
остальные города продолжают проверяться.
"""
import re

# Первое совпадение itemprop="name" - название организации (идёт в документе
# ДО отзывов; у отзывов тоже itemprop="name" - это имя автора отзыва).
_RE_NAME = re.compile(r'itemprop="name"[^>]*>([^<]{1,120})')
_RE_PHONE = re.compile(r'itemprop="telephone"[^>]*>([^<]{5,40})')
_RE_ADDRESS = re.compile(r'itemprop="address"[^>]*content="([^"]{1,160})"')
_RE_URL = re.compile(r'itemprop="url"[^>]*href="([^"]{1,200})"')


def extract(html: str) -> dict:
    """HTML карточки → {name, phone, address, site, available}.
    available=False, если ни одного поля не нашлось (не та страница/заблокирована)."""
    m_name = _RE_NAME.search(html)
    m_phone = _RE_PHONE.search(html)
    m_addr = _RE_ADDRESS.search(html)
    m_url = _RE_URL.search(html)
    name = (m_name.group(1).strip() if m_name else '')
    phone = (m_phone.group(1).strip() if m_phone else '')
    address = (m_addr.group(1).strip() if m_addr else '')
    site = (m_url.group(1).strip() if m_url else '')
    return {
        'name': name, 'phone': phone, 'address': address, 'site': site,
        'available': bool(name or phone or address or site),
    }


async def afetch(ctx, sem, url: str) -> dict:
    """Async: карточка организации по ссылке из КП. Своя вкладка в общем
    контексте, ограничена семафором (тот же паттерн, что twogis_check/
    review_priority - переиспользуем один браузерный контекст на весь прогон,
    а не поднимаем браузер на каждый город)."""
    if not url:
        return {'url': url, 'available': False, 'name': '', 'phone': '',
                'address': '', 'site': '', 'error': 'ссылки нет'}
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(2500)
            html = await page.content()
            data = extract(html)
            data['url'] = url
            data['error'] = None if data['available'] else 'карточка не распозналась'
            return data
        except Exception as e:  # noqa: BLE001
            return {'url': url, 'available': False, 'name': '', 'phone': '',
                    'address': '', 'site': '', 'error': str(e)}
        finally:
            try:
                await page.close()
            except Exception:
                pass
