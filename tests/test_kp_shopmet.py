# -*- coding: utf-8 -*-
"""Сверка КП на страницах SHOPMET: ложные дефекты Telegram и адреса.

Живые случаи с shopmet.ru:
  • контакт менеджера - ссылка t.me/+79090140017 (номер вместо ника), и в КП в
    колонке Telegram тоже номер. Проверка искала только ник и писала «Telegram
    на сайте отсутствует»;
  • на «Контактах» первым идёт заголовок «Адреса склада и часы работы», а сам
    адрес - ниже. Парсер брал первое вхождение слова «адрес» и выдавал в отчёт
    обрывок фразы «склада и» как адрес сайта."""
import kp

_HDR_TG = ('<header><a href="https://t.me/+79090140017">Telegram</a>'
           '<a href="https://wa.me/79090140017">WhatsApp</a></header>')


def _строка(**kw):
    поля = dict(domain='ekb.shopmet.ru', city='Екатеринбург', phone_common='79090140017',
                all_phones='9090140017', email='ekb@shopmet.ru', address='',
                country='Россия', telegram='79090140017', whatsapp='79090140017')
    поля.update(kw)
    return kp.KPRow(**поля)


def _поле(res, имя):
    return next(f for f in res['fields'] if f['field'] == имя)


def test_телеграм_по_номеру_совпадает():
    html = f'<html><body>{_HDR_TG}<p>ekb@shopmet.ru 8 909 014-00-17</p></body></html>'
    res = kp.check_variables(html, 'ekb.shopmet.ru', row=_строка())
    поле = _поле(res, 'Telegram')
    assert поле['status'] == 'ok', поле
    assert '014-00-17' in поле['found']


def test_телеграм_по_номеру_отсутствует_на_сайте():
    html = '<html><body><header>без мессенджеров</header></body></html>'
    поле = _поле(kp.check_variables(html, 'ekb.shopmet.ru', row=_строка()), 'Telegram')
    assert поле['status'] == 'bug'
    assert 'отсутствует' in поле['note']


def test_телеграм_по_номеру_другой_номер():
    html = ('<html><body><header><a href="https://t.me/+79001234567">tg</a>'
            '</header></body></html>')
    поле = _поле(kp.check_variables(html, 'ekb.shopmet.ru', row=_строка()), 'Telegram')
    assert поле['status'] == 'bug'
    assert 'не совпадает' in поле['note']


def test_ник_в_кп_и_на_сайте_не_ломается_номерной_ссылкой():
    """У проекта с ником в КП рядом может висеть номерная ссылка - ник главнее."""
    html = ('<html><body><header><a href="https://t.me/smu_manager2">tg</a>'
            '<a href="https://t.me/+79001234567">чат</a></header></body></html>')
    поле = _поле(kp.check_variables(html, 'ekb.shopmet.ru',
                                    row=_строка(telegram='smu_manager2')), 'Telegram')
    assert поле['status'] == 'ok', поле


def test_адрес_не_берётся_из_заголовка_про_склад():
    """«Адреса склада и часы работы» - не адрес; берём карточку ниже."""
    контакты = ('<html><body><h1>Адреса склада и часы работы</h1>'
                '<div>Адрес Екатеринбург, Машиностроителей, д. 19</div>'
                '</body></html>')
    поле = _поле(kp.check_variables('<html><body></body></html>', 'ekb.shopmet.ru',
                                    контакты, row=_строка()), 'Адрес')
    assert 'склада' not in поле['found']
    assert 'Машиностроителей' in поле['found']
    assert поле['status'] == 'bug' and 'нет в КП' in поле['note']


def test_адрес_только_город_остаётся_неполным():
    """В поле адреса голый город - дефект сайта, его нельзя терять."""
    контакты = ('<html><body><h1>Адреса склада и часы работы</h1>'
                '<div>Адрес Иркутск Часы работы Понедельник 09:00</div></body></html>')
    поле = _поле(kp.check_variables('<html><body></body></html>', 'irkutsk.shopmet.ru',
                                    контакты,
                                    row=_строка(domain='irkutsk.shopmet.ru',
                                                city='Иркутск')), 'Адрес')
    assert поле['found'].startswith('Иркутск')
    assert поле['status'] == 'bug' and 'неполный' in поле['note']


def test_адрес_совпадает_с_кп():
    контакты = '<html><body><div>Адрес: ул. Ленина, д.24</div></body></html>'
    поле = _поле(kp.check_variables('<html><body></body></html>', 'shopmet.ru',
                                    контакты,
                                    row=_строка(domain='shopmet.ru', city='Москва',
                                                address='ул. Ленина, д.24')), 'Адрес')
    assert поле['status'] == 'ok', поле
