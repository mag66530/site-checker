"""
goals_tester.py - проверка ВСЕХ целей Яндекс.Метрики проекта (страница
«Проверка целей» в панели).

Эталон - каталог целей, выгруженный из Метрики («Конверсии»):
catalogs/goals-<проект>.json (номер, название, условие, тип). Движок открывает
страницы сайта в браузере, выполняет безопасные действия (клики по телефонам,
почте, соцсетям/мессенджерам, кнопкам открытия форм - БЕЗ отправки заявок) и
слушает запросы к Метрике: каждая сработавшая JS-цель шлёт hit вида
goal://<хост>/<идентификатор>.

Вердикты по типам целей:
  js         - Сработала / НЕ сработала (если ждали от кликов) / Нет автодействия;
               формные цели (отправка заявки) не дублируем заявкой - статус
               «Прогоном форм» + подтягиваем результат из последнего отчёта форм.
  url/url_re - открываем страницу: 200 + счётчик отправил визит → «Сработает».
  auto       - автоцель Метрики (клики tel/mailto, формы, файлы…) - фиксируется
               Метрикой автоматически, отдельная проверка не нужна (информируем).
  jivo       - события чата Jivo запускает оператор/посетитель - только вручную.
  composite  - составная, Метрика считает её из шагов - смотрим цели-шаги.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin

ROOT = Path(__file__).parent
CATALOGS = ROOT / 'catalogs'

# Метрика: hit сработавшей JS-цели
_RE_GOAL = re.compile(r"goal://[^/]+/([^&\s\"?#]+)")

# Безопасные «общие» клики на каждой странице (без отправки форм):
# телефоны, почта, мессенджеры и соцсети (клик фиксируется, переход гасим).
GENERIC_CLICK_SELECTORS = [
    "a[href^='tel:']",
    "a[href^='mailto:']",
    "a[href*='wa.me'], a[href*='api.whatsapp'], a[href*='whatsapp:']",
    "a[href*='t.me'], a[href*='tg://']",
    "a[href*='vk.com'], a[href*='vk.me']",
    "a[href*='ok.ru']",
    "a[href*='dzen.ru'], a[href*='zen.yandex']",
    "a[href*='rutube.ru']",
    "a[href*='max.ru'], a[href*='web.max']",
    "a[href*='yandex.ru/profile'], a[href*='yandex.ru/maps/org']",
]
MAX_PER_SELECTOR = 5     # кликаем не больше N элементов на селектор (шапка+подвал+
                         # блоки «в корзину»: похожие/ранее просмотренные/акции)

# План действий по проектам: какие страницы открыть и какие кнопки нажать
# (кнопки открытия модалок дают цели «*click»; сами формы НЕ отправляем).
# «ожидаемые» - идентификаторы js-целей, которые ДОЛЖНЫ сработать от этих
# действий: если такая не сработала - это красное «НЕ сработала». Цели без
# автодействия получают серый статус «Нет автодействия» (не шумим ложным красным).
ACTIONS = {
    'smu': {
        'страницы': [
            ('Главная',   'https://stalmetural.ru/',
             [# СРОЧНО (до автозакрытия): модалка «Ваш город - Москва?».
              # На боевом сайте кнопка = «Все верно» (button.city-popup__btn--yes),
              # в части версий - «Да (N)». Жмём «оставить город» = закрыть модалку,
              # иначе её оверлей блокирует ВСЕ клики страницы (→ куча «Не проверено»).
              '!button.city-popup__btn--yes, .city-popup__btn--yes, '
              'button:has-text("Все верно"), button:has-text("Да"), '
              '.city-confirm button:has-text("Да")',
              # «Заказать звонок» в шапке = #call-back-form → фиксирует callorderclick
              # (устаревшей цели call_ordering сайт больше не шлёт).
              '#call-back-form', '#txt-back-form', '#txt-back-form-footer',
              '#call-back-form-main, [class*="manager-connect"], a:has-text("Связаться с менеджером")',
              'a:has-text("Показать больше категорий")',                   # morecatalog
              'a:has-text("Перейти ко всем категориям каталога")',         # gotomorecatalog
              'a:has-text("категориям производства"), a:has-text("ко всем категориям произ")',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Контакты',  'https://stalmetural.ru/contacts/',
             [# смена города: «изменить» в шапке → выбрать другой город (izmenit_gorod)
              {'цепочка': ['a:has-text("изменить"), text=изменить',
                           'a:has-text("Санкт-Петербург"), a:has-text("Казань")']}]),
            # breadcrumbphone - клик по номеру в «хлебных крошках» каталога
            ('Каталог',   'https://stalmetural.ru/catalog/',
             ['.breadcrumbs a[href^="tel:"], [class*="breadcrumb"] a[href^="tel:"]',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            # Листинг труб (реальные карточки с кнопкой «в корзину»): tocart/addocart,
            # «Расчет стоимости» (.cost-calc) → в модалке «В корзину»
            # (raschetst/raschetaddtocart), «Скачать прайс-лист» ловится onclick-генериком.
            ('Листинг',   'https://stalmetural.ru/catalog/truba-profilnaya/',
             [# кнопка «В корзину» на СМУ - это div.btn.btn-catalog (не <button>/<a>)
              '.add-to-cart, div.btn:has-text("В корзину"), .btn-catalog:has-text("В корзину"), button:has-text("В корзину"), a:has-text("В корзину"), text=Добавить в корзину',
              # «Расчет стоимости» (div.cost-calc) открывает попап → в нём «В корзину»
              # (div.btn) даёт raschetst + raschetaddtocart. Кнопка - div, не <button>.
              {'цепочка': ['div.cost-calc >> visible=true',
                           '[class*="popup"] div.btn:has-text("В корзину"), .modal div.btn:has-text("В корзину"), [class*="popup"] button:has-text("В корзину"), div.btn:has-text("В корзину")']},
              '.one-click-to-buy, text=Купить в один клик',
              'text=Срочный заказ, text=Быстрый заказ',
              'text=Не нашли что искали, text=Не нашли']),
            # «Скачать прайс-лист» (a.btn, onclick reachGoal) → price_download_category
            ('Листинг (прайс)', 'https://stalmetural.ru/catalog/list-goryachekatanyy/',
             ['a.btn:has-text("Скачать прайс"), a:has-text("Скачать прайс-лист"), text=Скачать прайс']),
            # Корзина (товар положен «В корзину» на листинге): клик по полю купона
            # + ввод буквы → цель coupon.
            ('Корзина',   'https://stalmetural.ru/basket/',
             [{'цепочка': [{'ввод': '.basket-coupon-block-field input, input[id*="coupon" i], input[name*="coupon" i]',
                            'текст': 'а'}]}]),
            # Первый товар открываем, чтобы он попал в «Ранее просмотренные» второго.
            ('Товар (труба)', 'https://stalmetural.ru/catalog/truba-profilnaya/2972110-truba-profilnaya-100kh10-mm-gost-8639-82-kvadratnaya/',
             ['.one-click-to-buy', 'text=Добавить в корзину, text=В корзину']),
            ('Товар',     'https://stalmetural.ru/catalog/izgotovlenie-pruzhin/1285453-izgotovlenie-pruzhin-rastyazheniya/',
             ['.one-click-to-buy', '#call-back-form-product',
              '.copy-btn:has(.an-ico-link-price)',
              '[class*="favorite"], [class*="favourite"], [class*="to-fav"], button:has(.an-ico-heart)',
              '[class*="share"], .an-ico-share, button:has-text("Поделиться")',
              '.add-to-cart, text=Добавить в корзину, text=В корзину']),
            ('Доставка',  'https://stalmetural.ru/delivery/', ['#call-back-form-delivery']),
            ('Вакансии',  'https://stalmetural.ru/vacancy/',
             ['text=Откликнуться, text=Отклик, button:has-text("Откликнуться")']),
            # Избранное/поделиться настроены ТОЛЬКО на хабаровском поддомене -
            # ловим click_favorites/click_share именно там.
            ('Товар (Хабаровск)', 'https://habarovsk.stalmetural.ru/catalog/truba-profilnaya/2972110-truba-profilnaya-100kh10-mm-gost-8639-82-kvadratnaya/',
             ['[class*="favorite"], [class*="favourite"], [class*="to-fav"], button:has(.an-ico-heart)',
              '[class*="share"], .an-ico-share, button:has-text("Поделиться")']),
            ('Избранное (Хабаровск)', 'https://habarovsk.stalmetural.ru/favorites/', []),
            # 404: несуществующий адрес - должна сработать цель 404error
            ('Страница 404', 'https://stalmetural.ru/nesuschestvuyushaya-404-xyz/', []),
        ],
        'ожидаемые': [
            'tel', 'email', 'clickwapp', 'clicktg', 'clickvk', 'clickmax',
            'click_vk_podval', 'click_ok_podval', 'click_tg_podval',
            'click_dzen_podval', 'click_rutube_podval', 'click_max_podval',
            'click_yandexorg_podval', 'breadcrumbphone',
            'callorderclick', 'zayavkaclick', 'svyazclick', 'oneclickbuy',
            'managerclick', 'morecatalog', 'gotomorecatalog', 'moreuslugi',
            'moreproizvodstvo', 'click_favorites', 'click_share', 'addocart',
            'tocart', '404error', 'click_yes_confirm',
            'izmenit_gorod', 'raschetst', 'raschetaddtocart',
            'price_download_category',
        ],
        # Подтверждено заказчиком: таких кнопок/форм на сайте НЕТ - статус
        # «Не найдена на сайте» вместо «Нет в коде».
        # call_ordering: кнопка «Заказать звонок» в шапке есть, но сайт шлёт с неё
        # актуальную цель callorderclick, а не call_ordering (старый идентификатор).
        'нет_на_сайте': ['phone_header', 'phone_footer', 'zvonok_text_category',
                         'call_ordering'],
    },
    'imp': {
        'страницы': [
            ('Главная',   'https://inmetprom.ru/',
             [# ШАПКА: заявка/просчёт, каталог (klik-na-katalog-v-shapke), акции, прайс
              '[data-my-modal="#modal-callback"], .banner-fast-order__application',
              'header a:has-text("Каталог"), .header a:has-text("Каталог")',
              'header a:has-text("Акции"), a:has-text("Акции")',
              'header a:has-text("Прайс"), a:has-text("Прайс-лист")',
              # СМЕНА ГОРОДА: открыть модалку города → кликнуть другой город (izmenit_gorod)
              {'цепочка': ['[data-my-modal="#city-modal-id"], .choose-city',
                           '.city-modal__city:not(.active), #city-modal-id a:has-text("Санкт-Петербург")']},
              # ПОДВАЛ: звонок менеджеру, написать нам, городской телефон
              '.footer-manager-call, footer a:has-text("Звонок менеджеру")',
              '.footer-email, footer a:has-text("Написать нам")',
              '.footer-phone, footer .telephone-utf']),
            ('Контакты',  'https://inmetprom.ru/contacts/',
             ['.footer-manager-call', '.footer-email']),
            ('Листинг',   'https://inmetprom.ru/catalog/reshetchatyj-nastil/',
             ['a:has-text("Скачать прайс-лист"), button:has-text("Скачать прайс-лист"), a:has-text("Скачать прайс")',
              '.add-to-cart-btn',
              'text=Быстрый заказ, [class*="fast-order"], [class*="bystryy"]',
              '.tags a, [class*="tag"] a, [class*="tags"] a',
              'button:has-text("Показать ещё"), a:has-text("Показать ещё")']),
            # Спецпредложения/акции: «в корзину» и «быстрый заказ» карточек акций.
            ('Акции',     'https://inmetprom.ru/specials/',
             ['.add-to-cart-btn, button:has-text("В корзину")',
              'text=Быстрый заказ, [class*="fast-order"], [class*="bystryy"]']),
            ('Товар',     'https://inmetprom.ru/list-gesti-0-2-mm-klass-1-gost-13345-85/',
             ['.add-to-cart-btn',                          # «в корзину» ВСЕХ блоков (похожие/ранее/с этим товаром/акции)
              'text=Быстрый заказ, [class*="fast-order"], [class*="bystryy"]',
              'text=Оставить отзыв, text=Написать отзыв, a:has-text("Отзыв")',
              'text=Нужна консультация, [class*="konsultac"]']),
            ('Вакансии',  'https://inmetprom.ru/vakansii/',
             ['text=Узнать подробнее, text=Откликнуться, text=Интересует вакансия']),
            ('Страница 404', 'https://inmetprom.ru/nesuschestvuyushaya-404-xyz/', []),
        ],
        'ожидаемые': [
            'tel-shapka-gorod', 'klik-v-shapke-po-elektronnoy-pochte',
            'klik-v-shapke-na-whatsapp', 'klik-na-tg-v-shapke',
            'klik-na-prays-list-iz-shapki', 'klik-na-katalog-v-shapke',
            'click-proschet', 'klik-na-aktsii', 'click-podval-gorod-nomer',
            'klik-v-podvale-zvonok-menedzheru', 'click-podval-email',
            'klik-v-podvale-po-napisat-nam', 'klik-na-tg-v-mobilke',
            'klik-na-whatsapp-v-mobilke', 'click-telephone-utf-gorod',
            'izmenit_gorod', 'click_yes_confirm', 'bystryy-zakaz-katalog',
            'bystryy-zakaz-listing', 'v-cart-listing', 'v-cart-listing-img',
            'bistrii-zakaz-listing', 'bistrii-zakaz-listing-img',
            'listing-skachat-prays-list', 'listing-klik-po-tegam', 'v-cart-kartochka',
            'bistrii-zakaz-cartochka', 'v-cart-kartochka-ranee-prosmotrennye',
            'bistrii-zakaz-cartochka-ranee-prosmotrennye',
            'v-korzinu-kartochka-pokhozhiye-tovary',
            'bystryy-zakaz-kartochka-pokhozhiye-tovary',
            'v-korzinu-kartochka-s-etim-tovarom-pokupayut',
            'bystryy-zakaz-kartochka-s-etim-tovarom-pokupayut',
            'v-korzinu-kartochka-tovara-aktsii', 'bystryy-zakaz-kartochka-tovara-aktsii',
            'tovar_konsultaciya', 'klik-tel', '404error',
        ],
    },
    'mpe': {
        'страницы': [
            # Цели МПЭ прошиты в onclick="ym(...,'reachGoal','X')" - генерик-кликер
            # движка снимает их сам (login/citys/about/catalog/smotretvse/raschet/
            # rekvizity_podval). Ниже - только то, что требует особых шагов.
            ('Главная',   'https://mepen.ru/',
             ['header.header-kostyl .bottom-header-right button.popup_form',
              '[onclick*="rasschitatzakaz"]',              # «Прикрепить файл» в форме заказа (скрытый - dispatch)
              'a.link_more',                               # «Узнать больше» → about
              'a.footer-link[href="/catalog/"]']),         # «Смотреть все» в подвале → smotretvse
            ('Контакты',  'https://mepen.ru/contacts/',
             ['[onclick*="rekvizity_contacts"], a:has-text("Реквизиты компании")']),
            # Реквизиты: «Скачать реквизиты» → skachat_rekvizity
            ('Реквизиты', 'https://mepen.ru/rekvizity/',
             ['[onclick*="skachat_rekvizity"], a:has-text("Скачать реквизиты")']),
            # Листинг с корзиной (болты): «В корзину» → tocart, клик по карточке →
            # klik_kartochka_tovara. Товар кладётся в корзину для купона ниже.
            ('Листинг (болты)', 'https://mepen.ru/catalog/zheleznodorozhnaya-avtomatika/zheleznodorozhnyy-krepezh/bolt/',
             ['[onclick*="tocart"], button:has-text("В корзину"), text=В корзину',
              '[onclick*="klik_kartochka_tovara"]']),
            # Корзина: клик по полю купона → skidochnyy_kupon (товар уже в корзине).
            ('Корзина',   'https://mepen.ru/personal/basket/',
             ['[onclick*="skidochnyy_kupon"], text=Введите код купона, [class*="coupon"]']),
            # Авторизация: кнопка «Авторизоваться» → klik_avtorizovatsya (форма
            # пустая - вход не произойдёт, заявка не уходит, цель фиксируется).
            ('Авторизация', 'https://mepen.ru/personal/',
             ['[onclick*="klik_avtorizovatsya"], button:has-text("Авторизоваться")']),
            ('Товар',     'https://mepen.ru/catalog/tovar/telezhka-tip-b-gcl/',
             ['text=Нужна консультация', 'text=Нашли дешевле',
              '[onclick*="tovar_v_korzinu"], text=в корзину']),
            ('Страница 404', 'https://mepen.ru/nesuschestvuyushaya-404-xyz/', []),
        ],
        'ожидаемые': [
            'tel', 'email', 'catalog', 'raschet', 'rasschitatzakaz', 'smotretvse',
            'telegram', 'clickwapp', 'tocart', 'tovar_v_korzinu',
            'klik_kartochka_tovara', 'tovar_konsultaciya', 'klik_nashli_deshevle',
            'citys', 'about', 'rekvizity_podval', 'rekvizity_contacts',
            'skachat_rekvizity', 'skidochnyy_kupon', 'login', 'klik_avtorizovatsya',
        ],
    },
}


def загрузить_каталог(pid: str) -> dict | None:
    f = CATALOGS / f'goals-{pid}.json'
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding='utf-8'))


def _план_для_домена(домен: str) -> dict:
    """Универсальный план прогона для сайта на той же платформе, что СМУ РФ
    (СНГ-домены stalmetural.*, smg.az и т.п.): те же кнопки и ожидаемые цели,
    только другой домен. Товар/доставку не трогаем - их адреса у стран разные."""
    d = (домен or '').rstrip('/')
    if not d:
        return {'страницы': []}
    return {
        'страницы': [
            ('Главная', d + '/',
             ['#call-back-form', '#txt-back-form', '#txt-back-form-footer',
              '#call-back-form-main, [class*="manager-connect"], a:has-text("Связаться с менеджером")',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Контакты', d + '/contacts/', []),
            ('Каталог', d + '/catalog/',
             ['.breadcrumbs a[href^="tel:"], [class*="breadcrumb"] a[href^="tel:"]',
              'button:has-text("Показать больше"), a:has-text("Показать больше")',
              'a:has-text("ко всем категориям"), a:has-text("всем категориям")']),
            ('Страница 404', d + '/nesuschestvuyushaya-404-xyz/', []),
        ],
        'ожидаемые': ACTIONS['smu']['ожидаемые'],
    }


def _базовый(pid: str) -> str:
    """Базовый проект для суб-проекта страны: smu-uz → smu (формы и их конфиг
    едины для всех стран проекта, лежат под базовым кодом)."""
    return (pid or '').split('-')[0]


def _формные_цели(pid: str) -> set[str]:
    """Идентификаторы целей, привязанных к ОТПРАВКЕ форм в конфиге форм-тестера
    (их не триггерим здесь, чтобы не слать заявки)."""
    p = ROOT / 'forms_tester' / 'projects' / _базовый(pid) / 'config.py'
    if not p.is_file():
        return set()
    txt = p.read_text(encoding='utf-8')
    return set(re.findall(r'"цель"\s*:\s*"([\w\-.]+)"', txt))


def _результаты_форм(pid: str) -> dict[str, str]:
    """Статусы целей из последнего прогона форм: идентификатор → статус.
    Берём из ДВУХ источников:
      1) fired_goals.json - ВСЕ цели, сработавшие при формах (ловятся из вывода
         форм-прогона, включая те, что движок пишет только в лог);
      2) лист «Цели» отчёта форм (цели спец-сценариев)."""
    base = _базовый(pid)
    out: dict[str, str] = {}
    # 1) Все пойманные при формах цели.
    fj = ROOT / 'cache' / 'forms' / base / 'fired_goals.json'
    if fj.is_file():
        try:
            for gid in json.loads(fj.read_text(encoding='utf-8')):
                gid = str(gid).strip()
                if gid:
                    out[gid] = 'Сработала'
        except Exception:
            pass
    # 2) Лист «Цели» отчёта форм (статусы спец-сценариев дополняют/уточняют).
    f = ROOT / 'cache' / 'forms' / base / 'log_forms.xlsx'
    if not f.is_file():
        return out
    try:
        from openpyxl import load_workbook
        wb = load_workbook(f, data_only=True)
        if 'Цели' not in wb.sheetnames:
            return out
        ws = wb['Цели']
        hdr = [str(c.value or '').strip() for c in ws[1]]
        i_id = hdr.index('Цель (идентификатор)') if 'Цель (идентификатор)' in hdr else -1
        i_st = hdr.index('Статус') if 'Статус' in hdr else -1
        if i_id < 0 or i_st < 0:
            return out
        for row in ws.iter_rows(min_row=2, values_only=True):
            gid = str(row[i_id] or '').strip()
            st = str(row[i_st] or '').strip()
            if gid:
                out[gid] = st          # последняя строка по цели побеждает
    except Exception:
        pass
    return out


def _совпало(цель: dict, fired: set[str]) -> str | None:
    """Какой из сработавших идентификаторов закрывает цель (учитывая «содержит»)."""
    ids = цель.get('идентификаторы') or []
    for gid in ids:
        if цель.get('содержит'):
            for f in fired:
                if gid.lower() in f.lower():
                    return f
        elif gid in fired:
            return gid
    return None


def выполнить_прогон(pid: str, headless: bool = True, log=print, stop=None) -> dict:
    """Открывает страницы, кликает, слушает Метрику. Возвращает
    {'fired': set(id), 'страницы': [{'название','url','код','счётчик','визит'}]}."""
    каталог = загрузить_каталог(pid) or {}
    # Явный план проекта; для суб-проектов (страны СМУ) - универсальный по домену.
    план = ACTIONS.get(pid) or _план_для_домена(каталог.get('домен', ''))
    counter = str(каталог.get('счётчик') or '')
    fired: set[str] = set()
    привязки: set[str] = set()       # reachGoal-идентификаторы, найденные явно
    визиты: dict[str, bool] = {}     # url → watch-hit отправлен
    страницы_инфо = []
    _re_reach = re.compile(r"reachGoal\W{1,4}([\w\-]+)")
    _seen_js: set[str] = set()
    _код_части: list[str] = []        # весь код страниц + их JS (для поиска целей)

    def _собрать_привязки(html: str, base_url: str):
        """Копит reachGoal-идентификаторы И ВЕСЬ код (HTML + все JS того же хоста),
        чтобы потом надёжно проверить, упоминается ли цель в коде сайта вообще
        (как строковый литерал), а не только сразу после reachGoal(."""
        привязки.update(_re_reach.findall(html))
        _код_части.append(html.lower())
        try:
            import requests as _rq
            host = re.sub(r'^https?://', '', base_url).split('/')[0].split(':')[0]
            base_host = '.'.join(host.split('.')[-2:])   # stalmetural.ru
            srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
            for src in srcs[:40]:
                u = src if src.startswith('http') else urljoin(base_url, src)
                # берём JS с того же домена/поддоменов (там и живут reachGoal)
                if base_host not in u or u in _seen_js:
                    continue
                _seen_js.add(u)
                try:
                    js = _rq.get(u, timeout=15, headers={'User-Agent': 'Mozilla/5.0'},
                                 verify=os.environ.get('REQUESTS_CA_BUNDLE', True)).text
                    привязки.update(_re_reach.findall(js))
                    _код_части.append(js.lower())
                except Exception:
                    continue
        except Exception:
            pass

    from playwright.sync_api import sync_playwright
    # Облачная среда (агентский прокси режет TLS браузера): гоняем трафик страницы
    # через сетевой стек драйвера (route.fetch). Локально флага нет - напрямую.
    _via_driver = bool(os.environ.get('CCR_AGENT_PROXY_ENABLED'))
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless,
                               args=["--disable-blink-features=AutomationControlled",
                                     "--no-sandbox"])
        # Прикидываемся обычным Chrome: часть сайтов (напр. inmetprom.ru) отдаёт
        # 403 «голому» headless-браузеру. Реальный User-Agent + заголовки часто
        # снимают такую блокировку.
        _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
        ctx = b.new_context(locale='ru-RU', viewport={'width': 1440, 'height': 900},
                            ignore_https_errors=_via_driver, user_agent=_UA,
                            extra_http_headers={
                                'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
                                'Accept': ('text/html,application/xhtml+xml,'
                                           'application/xml;q=0.9,image/avif,'
                                           'image/webp,*/*;q=0.8'),
                            })
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        try:
            ctx.clear_cookies()   # чтобы вылезла модалка выбора города (кука не стоит)
        except Exception:
            pass
        page = ctx.new_page()

        текущий_url = {'u': ''}

        def _на_запрос(req):
            u = req.url
            if 'mc.yandex' in u or 'mc.webvisor' in u:
                m = _RE_GOAL.search(unquote(u))
                if m:
                    gid = m.group(1)
                    if gid not in fired:
                        fired.add(gid)
                        log(f"   🎯 цель: {gid}")
                if f'/watch/{counter}' in u:
                    визиты[текущий_url['u']] = True

        ctx.on('request', _на_запрос)
        # новые вкладки (клики по соцсетям с target=_blank) сразу закрываем
        ctx.on('page', lambda p: p != page and p.close())
        if _via_driver:
            def _route(route, request):
                try:
                    route.fulfill(response=route.fetch(timeout=40000))
                except Exception:
                    try:
                        route.abort()
                    except Exception:
                        pass
            ctx.route('**/*', _route)

        _всего = len(план['страницы'])
        for _idx, (название, url, клики) in enumerate(план['страницы'], 1):
            if stop and stop():
                log('⛔ Остановлено')
                break
            log(f"ПРОГРЕСС {_idx}/{_всего}")
            log(f"- Страница: {название}  {url}")
            текущий_url['u'] = url
            код = 0
            try:
                resp = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                код = resp.status if resp else 0
                page.wait_for_timeout(1500)
            except Exception as e:
                log(f"   ⚠️ не открылась: {e}")
                страницы_инфо.append({'название': название, 'url': url, 'код': код,
                                      'счётчик': False, 'визит': False})
                continue
            # СРОЧНЫЕ клики (префикс '!'): выполняются сразу после загрузки -
            # до прокрутки и генериков. Нужны для модалки города «Да/Нет»,
            # которая автозакрывается через несколько секунд.
            for sel in [s[1:] for s in клики
                        if isinstance(s, str) and s.startswith('!')]:
                try:
                    el = page.locator(sel).first
                    el.click(timeout=3500)
                    page.wait_for_timeout(500)
                except Exception:
                    pass

            html = page.content()
            есть_счётчик = counter in html if counter else False
            _собрать_привязки(html, url)

            # прокрутка вниз (ленивые блоки + подвал с соцсетями)
            try:
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(1200)
            except Exception:
                pass

            # общие безопасные клики (переход гасим сразу возвратом)
            for sel in GENERIC_CLICK_SELECTORS:
                try:
                    els = page.locator(sel)
                    n = min(els.count(), MAX_PER_SELECTOR)
                    for i in range(n):
                        el = els.nth(i)
                        try:
                            el.scroll_into_view_if_needed(timeout=2000)
                            el.click(timeout=2500, no_wait_after=True)
                            page.wait_for_timeout(350)
                        except Exception:
                            continue
                        if page.url != url:      # утащило по ссылке - вернёмся
                            try:
                                page.go_back(wait_until='domcontentloaded',
                                             timeout=15000)
                                page.wait_for_timeout(800)
                            except Exception:
                                page.goto(url, wait_until='domcontentloaded',
                                          timeout=30000)
                except Exception:
                    continue

            # ЭЛЕМЕНТЫ С ЦЕЛЬЮ В onclick: на СМУ/МПЭ цели прошиты прямо в
            # onclick="ym(...,'reachGoal','X')" - кликаем их все напрямую.
            # Submit-кнопки и input НЕ трогаем (иначе ушла бы пустая заявка).
            try:
                _rg = page.locator(
                    'a[onclick*="reachGoal"], div[onclick*="reachGoal"], '
                    'span[onclick*="reachGoal"], '
                    'button[onclick*="reachGoal"]:not([type="submit"])')
                _видели_цель: set[str] = set()   # по одной цели каждого id хватит
                _обработано = 0
                for i in range(_rg.count()):
                    if _обработано >= 30:
                        break
                    el = _rg.nth(i)
                    # id цели из onclick - чтобы не жать 40 одинаковых «городов»
                    try:
                        _oc = el.get_attribute('onclick') or ''
                        _m = re.search(r"reachGoal[^)]*['\"]([\w\-.]+)['\"]", _oc)
                        _gid = _m.group(1) if _m else f'#{i}'
                    except Exception:
                        _gid = f'#{i}'
                    if _gid in _видели_цель:
                        continue
                    _видели_цель.add(_gid)
                    _обработано += 1
                    # scroll не должен «съедать» скрытый элемент: если не вышло -
                    # всё равно шлём dispatch_event (onclick с reachGoal сработает
                    # без перехода по ссылке - важно для города в скрытом попапе).
                    try:
                        el.scroll_into_view_if_needed(timeout=1000)
                    except Exception:
                        pass
                    try:
                        try:
                            el.click(timeout=1500, no_wait_after=True)
                        except Exception:
                            el.dispatch_event('click')   # скрытый/перекрытый
                        page.wait_for_timeout(280)
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(120)
                    except Exception:
                        pass
                    if page.url != url:      # ссылка увела - вернёмся
                        try:
                            page.go_back(wait_until='domcontentloaded', timeout=15000)
                        except Exception:
                            page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        page.wait_for_timeout(600)
            except Exception:
                pass

            # подвал: кликаем ТОЛЬКО соцсети/мессенджеры (открываются новой
            # вкладкой - её закрываем; на текущей странице ничего не ломается).
            # Внутренние ссылки не трогаем, чтобы не уходить со страницы.
            _soc = ("vk.com", "vk.me", "ok.ru", "t.me", "dzen.ru", "rutube.ru",
                    "max.ru", "wa.me", "whatsapp", "yandex.ru/maps",
                    "yandex.ru/profile")
            try:
                foot = page.locator("footer a[href], .footer a[href]")
                for i in range(min(foot.count(), 25)):
                    try:
                        el = foot.nth(i)
                        href = (el.get_attribute("href") or "").lower()
                        if not any(s in href for s in _soc):
                            continue
                        el.scroll_into_view_if_needed(timeout=1200)
                        el.click(timeout=1800, no_wait_after=True)
                        page.wait_for_timeout(200)
                    except Exception:
                        continue
            except Exception:
                pass

            # клики проекта (кнопки модалок форм, «в корзину» разных блоков и т.п.).
            # Кликаем НЕСКОЛЬКО элементов на селектор: на карточке товара кнопки
            # «в корзину»/«быстрый заказ» повторяются в блоках «похожие», «ранее
            # просмотренные», «с этим товаром покупают», «акции» - у каждой СВОЯ
            # цель. Escape ДО клика снимает модалку прошлого (перекрытие - причина
            # «через раз»), Escape ПОСЛЕ закрывает открытую.
            for sel in клики:
                if isinstance(sel, str) and sel.startswith('!'):
                    continue        # срочный - уже выполнен сразу после загрузки
                # ЦЕПОЧКА: {'цепочка': [шаг1, шаг2, ...]} - действия ПОДРЯД без
                # Escape между ними (модалка остаётся открытой). Шаг - селектор
                # (клик) или {'ввод': селектор, 'текст': 'а'} (клик + ввод текста,
                # напр. поле купона в корзине). Нужно, когда одна цель ведёт к
                # другой: «Расчёт стоимости» → в модалке «В корзину».
                if isinstance(sel, dict) and sel.get('цепочка'):
                    try:
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(150)
                        for step in sel['цепочка']:
                            if isinstance(step, dict) and step.get('ввод'):
                                el = page.locator(step['ввод']).first
                                if el.count() == 0:
                                    break
                                el.scroll_into_view_if_needed(timeout=1500)
                                el.click(timeout=2500)
                                el.type(step.get('текст', 'а'), delay=120)
                                page.wait_for_timeout(900)
                                continue
                            el = page.locator(step).first
                            if el.count() == 0:
                                break
                            el.scroll_into_view_if_needed(timeout=1500)
                            try:
                                el.click(timeout=2500)
                            except Exception:
                                el.click(timeout=2500, force=True)
                            page.wait_for_timeout(900)
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(200)
                        if page.url != url:
                            try:
                                page.go_back(wait_until='domcontentloaded', timeout=15000)
                            except Exception:
                                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                            page.wait_for_timeout(600)
                    except Exception:
                        pass
                    continue
                try:
                    total = page.locator(sel).count()
                except Exception:
                    continue
                for i in range(min(total, MAX_PER_SELECTOR)):
                    try:
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(150)
                        el = page.locator(sel).nth(i)
                        if el.count() == 0:
                            break
                        el.scroll_into_view_if_needed(timeout=1500)
                        try:
                            el.click(timeout=2000)
                        except Exception:
                            el.click(timeout=2000, force=True)
                        page.wait_for_timeout(500)
                        # клик мог быть по ссылке-переходу (Каталог/Акции/Прайс в
                        # шапке): цель сработала, но нужно вернуться и кликать дальше.
                        if page.url != url:
                            try:
                                page.go_back(wait_until='domcontentloaded', timeout=15000)
                            except Exception:
                                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                            page.wait_for_timeout(600)
                            break   # после ухода нумерация сбилась - к следующему селектору
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(150)
                    except Exception:
                        continue
            page.wait_for_timeout(300)

            страницы_инфо.append({'название': название, 'url': url, 'код': код,
                                  'счётчик': есть_счётчик,
                                  'визит': визиты.get(url, False)})
        b.close()

    return {'fired': fired, 'страницы': страницы_инфо, 'привязки': привязки,
            'код': ''.join(_код_части)}


def _url_цели_проверка(каталог: dict, страницы_инфо: list) -> dict[str, dict]:
    """Для url-целей: найти открытую страницу, чей адрес содержит «url_часть»."""
    out = {}
    for g in каталог.get('цели', []):
        if g['тип'] not in ('url', 'url_re'):
            continue
        часть = g.get('url_часть') or ''
        hit = None
        for s in страницы_инфо:
            try:
                ok = (re.search(часть, s['url']) if g['тип'] == 'url_re'
                      else часть.lower() in s['url'].lower())
            except re.error:
                ok = False
            if ok:
                hit = s
                break
        out[g['номер']] = {'страница': hit}
    return out


# Цели, которым нужно ОСОБОЕ действие (его автотест намеренно не делает: это не
# «нет в коде», а «нужен ручной шаг / отдельная настройка автотеста»). Ключи -
# подстроки в названии/условии/идентификаторе цели → человеческое описание.
_СПЕЦ_ДЕЙСТВИЯ = [
    (('вход', 'авториз', 'логин', 'login', 'акка', 'регистрац', 'registr'),
     'вход или регистрация в личном кабинете'),
    (('избранн', 'favorit', 'wishlist', 'сравн', 'sravn'),
     'добавление товара в избранное/сравнение'),
    (('купон', 'kupon', 'скидочн', 'промокод', 'promo'),
     'применение купона или промокода'),
    (('скачив', 'скачать', 'download', 'реквизит', 'rekvizit'),
     'скачивание файла или реквизитов'),
    (('смотреть', 'smotretvse', 'показать', 'показат', 'ещё', 'eshe', 'load', 'pagina'),
     'просмотр/подгрузка каталога (пагинация, «смотреть всё»)'),
    (('оплат', 'оформ', 'oformit', 'checkout'),
     'оформление или оплата заказа'),
]


def _нужно_спец_действие(g: dict) -> str | None:
    """Если цель завязана на особое действие (вход, оплата, купон, избранное,
    скачивание, пагинация) - вернуть человеческое описание, иначе None."""
    text = ((g.get('название', '') + ' ' + g.get('условие', '') + ' '
             + ' '.join(g.get('идентификаторы') or [])).lower())
    for keys, label in _СПЕЦ_ДЕЙСТВИЯ:
        if any(k in text for k in keys):
            return label
    return None


# Цели-клики по соцсетям/мессенджерам/телефону/почте. Такие ссылки Метрика часто
# считает сама («внешняя ссылка»/«мессенджер»/«клик по телефону») - reachGoal в
# коде может не быть, но САМ КЛИК мы выполняем (GENERIC_CLICK_SELECTORS на каждой
# странице), значит действие посетителя совершено и Метрика цель зачтёт.
_СОЦ_КОНТАКТ_КЛЮЧИ = (
    'vk', 'вконтакте', 'ok_podval', 'одноклассник', 'dzen', 'дзен', 'rutube',
    'рутуб', '_tg', 'tg_', 'clicktg', 'телеграм', 'telegram', 'whatsapp', 'wapp',
    'ватсап', 'max_podval', 'clickmax', 'yandexorg', 'яндекс-организ',
    'соц.сет', 'соцсет',
)


def _клик_соцсети(g: dict) -> bool:
    text = (' '.join(g.get('идентификаторы') or []).lower() + ' '
            + (g.get('название', '') or '').lower())
    return any(k in text for k in _СОЦ_КОНТАКТ_КЛЮЧИ)


def _вход_в_аккаунт(g: dict) -> bool:
    """Цель «вход/авторизация в личном кабинете». На сайтах входа как такового
    нет (или он не найден) - помечаем «Не найдено», а не «нужно спец-действие»."""
    ids = ' '.join(g.get('идентификаторы') or []).lower()
    name = (g.get('название', '') or '').lower()
    return ('login' in ids or 'avtoriz' in ids
            or 'вход в аккаунт' in name or 'авторизов' in name)


def _лид_цель(g: dict) -> bool:
    """Составная «лид»-цель («Основная цель на лиды», «Весь сайт», d_Goal/Lid_Goal):
    Метрика сама агрегирует её из под-целей - отдельного goal-сигнала нет, считается
    в кабинете. Помечаем как автоцель Метрики."""
    ids = ' '.join(g.get('идентификаторы') or []).lower()
    name = (g.get('название', '') or '').lower()
    return ('d_goal' in ids or 'lid_goal' in ids or 'lidgoal' in ids
            or 'основная цель на лиды' in name or 'весь сайт' in name
            or 'лиды не основные' in name)


# Цвета статусов и порядок вывода - на уровне модуля (используются в
# классификации и в рисовании листов).
_GREEN, _RED, _GREY, _BLUE = '1E8E3E', 'C62828', '757575', '1565C0'
_ФОН = {_GREEN: 'E6F4EA', _RED: 'FCE8E6', _BLUE: 'E8F0FE', _GREY: 'F1F3F4'}
_ПОРЯДОК = {'Сработала': 0, 'Сработала (формы)': 1, 'Действие выполнено': 2,
            'Сработает': 3, 'Нет в коде сайта': 4, 'НЕ сработала': 5, 'Проблема': 6,
            'Нужно спец-действие': 7, 'Не найдено на сайте': 8,
            'Не найдена на сайте': 8, 'Не проверено': 9,
            'Нет автопроверки': 10, 'Проверяется формами': 11,
            'Автоцель (Метрика сама)': 12, 'Только в Метрике': 12, 'Составная': 13,
            'Вручную': 14}


def _классифицировать(pid: str, каталог: dict, прогон: dict) -> dict:
    """Сводит каталог целей с результатами прогона: список строк со статусом/
    цветом/пояснением + счётчики категорий. Без рисования (его делают отдельно)."""
    import re as _re2
    fired = прогон['fired']
    страницы = прогон['страницы']
    привязки = {i.lower() for i in прогон.get('привязки', set())}
    код = прогон.get('код', '')       # весь код страниц + JS (нижний регистр)
    формные = _формные_цели(pid)
    форм_статусы = _результаты_форм(pid)
    url_map = _url_цели_проверка(каталог, страницы)
    _план = ACTIONS.get(pid) or _план_для_домена(каталог.get('домен', ''))
    ожидаемые = {i.lower() for i in _план.get('ожидаемые', [])}
    нет_на_сайте = {i.lower() for i in _план.get('нет_на_сайте', [])}
    GREEN, RED, GREY, BLUE = _GREEN, _RED, _GREY, _BLUE
    _код_кэш: dict[str, bool] = {}

    def _id_в_коде(gid: str) -> bool:
        """Цель упоминается в коде сайта: есть в reachGoal-списке ИЛИ встречается
        как строковый литерал ('id' / \"id\") где-либо в HTML/JS проверенных
        страниц. Литерал в кавычках защищает от ложных совпадений (tel в hotel)."""
        gid = (gid or '').lower()
        if not gid:
            return False
        if gid in привязки or any(gid in b for b in привязки):
            return True
        if gid in _код_кэш:
            return _код_кэш[gid]
        found = bool(_re2.search(r'["\']' + _re2.escape(gid) + r'["\']', код)) if код else False
        _код_кэш[gid] = found
        return found

    def _привязана(g) -> str:
        ids = g.get('идентификаторы') or []
        return 'есть' if any(_id_в_коде(i) for i in ids) else 'не найдена'

    def _форма_поймала(g) -> str | None:
        """Идентификатор цели, которую ПОЙМАЛА «Проверка форм» (лист «Цели» её
        отчёта). Формы отправляются по-настоящему, поэтому цели на onsubmit там
        реально фиксируются - даже если статический скан кода их не нашёл. Это
        снимает ложное «нет в коде сайта» с целей отправки форм."""
        for gid in (g.get('идентификаторы') or []):
            st = (форм_статусы.get(gid) or '').lower()
            if st.startswith('сработал') or 'зафиксир' in st:
                return gid
        return None

    def _авто_действие_сделано(условие: str) -> bool:
        """Автоцель Метрики: выполнил ли автотест соответствующее действие.
        Клики по телефону/почте/соцсетям/мессенджерам мы делаем; формы/файлы/
        поиск - нет (формы закрывает «Проверка форм»)."""
        c = (условие or '').lower()
        делаем = ('телефон', 'номер', 'email', 'почт', 'соц', 'мессенджер',
                  'whatsapp', 'telegram', 'вконтакте')
        не_делаем = ('форм', 'файл', 'скачив', 'поиск', 'чат', 'контактные данные',
                     'оформлени')
        if any(k in c for k in не_делаем):
            return False
        return any(k in c for k in делаем)

    счёт = {'ok': 0, 'ok_forms': 0, 'bad': 0, 'no_code': 0, 'forms': 0,
            'special': 0, 'manual': 0, 'info': 0}
    # Прозрачен ли код сайта для статического анализа. Если reachGoal почти не
    # нашли (сайт грузит цели через GTM/минифицированный бандл - как ИМП), то
    # вывод «нет в коде» НЕЛЬЗЯ делать - это была бы наша слепота, а не баг сайта.
    _код_надёжен = len(привязки) >= 3
    _строки: list[dict] = []
    for g in каталог.get('цели', []):
        t = g['тип']
        способ = статус = детали = ''
        цвет = GREY
        if t == 'js':
            hit = _совпало(g, fired)
            форма_id = _форма_поймала(g)
            в_формах = any(gid in формные for gid in (g.get('идентификаторы') or []))
            _особое = _нужно_спец_действие(g)
            if _лид_цель(g):
                # Составная лид-цель: Метрика агрегирует сама на сервере - в отчёте
                # всегда «автоцель», а не зелёное срабатывание под-цели.
                способ, статус, цвет = 'автоцель Метрики', 'Автоцель (Метрика сама)', BLUE
                детали = ('составная «лид»-цель (Основная цель на лиды/Весь сайт): '
                          'Метрика собирает её из под-целей сама на сервере - '
                          'отдельного goal-сигнала в трафике нет, смотрите в кабинете')
                счёт['info'] += 1
            elif hit:
                способ, статус, цвет = 'клики автотеста', 'Сработала', GREEN
                детали = f'зафиксирован идентификатор «{hit}»'
                счёт['ok'] += 1
            elif форма_id:
                # Цель реально сработала при ОТПРАВКЕ формы («Проверка форм») -
                # даже если статический скан reachGoal не нашёл. Это НЕ «нет в коде».
                способ, статус, цвет = 'через формы', 'Сработала (формы)', GREEN
                детали = ('зафиксирована при отправке формы на странице «Проверка '
                          f'форм» (идентификатор «{форма_id}»)')
                счёт['ok_forms'] += 1
            elif any(gid.lower() in нет_на_сайте
                     for gid in (g.get('идентификаторы') or [])):
                # Подтверждено вручную: такой кнопки/формы на сайте нет.
                способ, статус, цвет = 'вручную', 'Не найдена на сайте', GREY
                детали = ('кнопки/формы под эту цель на сайте нет (проверено '
                          'вручную) - цель осталась в Метрике от старой версии сайта')
                счёт['manual'] += 1
            elif _вход_в_аккаунт(g) and not any(
                    gid.lower() in ожидаемые for gid in (g.get('идентификаторы') or [])):
                # Вход в личный кабинет: на этом сайте кнопки/страницы входа в
                # плане прогона нет - найти не удалось, проверяется вручную.
                # (Если вход в плане есть - напр. МПЭ /personal/ - цель идёт по
                # обычной логике: сработала/НЕ сработала.)
                способ, статус, цвет = 'вручную', 'Не найдено на сайте', GREY
                детали = ('кнопку/страницу входа в личный кабинет на сайте найти '
                          'не удалось - проверьте вручную')
                счёт['manual'] += 1
            elif в_формах:
                # Цель привязана к отправке формы в конфиге, но результата форм ещё
                # нет: подскажем запустить «Проверку форм» (результат подтянется сам).
                способ, статус, цвет = 'через формы', 'Проверяется формами', BLUE
                детали = ('цель срабатывает при отправке формы - запустите «Проверку '
                          'форм» (её результат автоматически подтянется в этот отчёт)')
                счёт['forms'] += 1
            elif (any(gid.lower() in ожидаемые for gid in (g.get('идентификаторы') or []))
                  and _код_надёжен and _привязана(g) == 'есть'):
                # Действие мы выполняли (клик), reachGoal ЕСТЬ в коде, но цель не
                # поймалась - это реальная проблема настройки/привязки.
                способ, статус, цвет = 'клики автотеста', 'НЕ сработала', RED
                детали = ('действие выполнялось (клик по телефону/почте/кнопке), '
                          'reachGoal есть в коде, но цель не зафиксирована - '
                          'проверьте её настройку/привязку')
                счёт['bad'] += 1
            elif _клик_соцсети(g):
                # Клик по соцсети/мессенджеру мы ВЫПОЛНЯЕМ на каждой странице
                # (GENERIC_CLICK_SELECTORS). Такие цели Метрика чаще считает сама
                # как «внешнюю ссылку»/«мессенджер» (reachGoal в коде может не быть,
                # goal-хита в трафике нет), но действие посетителя совершено - в
                # Метрике цель зачтётся.
                способ, статус, цвет = 'клик по соцсети', 'Действие выполнено', GREEN
                детали = ('клик по ссылке соцсети/мессенджера выполнен. Метрика '
                          'считает такие цели сама («внешняя ссылка»/«мессенджер») - '
                          'отдельного goal-сигнала в трафике нет, но действие '
                          'совершено, и в Метрике цель зачтётся')
                счёт['ok'] += 1
            elif _особое and (not _код_надёжен or _привязана(g) == 'есть'):
                # reachGoal в коде есть (или код непрозрачен), но цель срабатывает
                # только на особое действие, которого автотест не делает.
                способ, статус, цвет = 'спец-действие', 'Нужно спец-действие', GREY
                _прив = f"; reachGoal в коде: {_привязана(g)}" if _код_надёжен else ''
                детали = (f'нужно {_особое} - автотест этот шаг не выполняет '
                          f'(можно добавить отдельным сценарием){_прив}')
                счёт['special'] += 1
            elif _код_надёжен and _привязана(g) == 'не найдена':
                # Код сайта прозрачен, а reachGoal этой цели в нём не встречается -
                # это не наша слепота, а отсутствие отправки на стороне сайта.
                способ, статус, цвет = 'проверка кода', 'Нет в коде сайта', RED
                детали = ('reachGoal этой цели НЕ найден в коде сайта - цель создана '
                          'в Метрике, но сайт её не отправляет')
                счёт['no_code'] += 1
            elif _особое:
                способ, статус, цвет = 'спец-действие', 'Нужно спец-действие', GREY
                детали = (f'нужно {_особое} - автотест этот шаг не выполняет '
                          '(можно добавить отдельным сценарием)')
                счёт['special'] += 1
            else:
                # Код непрозрачен (GTM/бандл) или действие не входило в прогон.
                способ, статус, цвет = 'вручную', 'Не проверено', GREY
                _подск = f"; привязка reachGoal в коде: {_привязана(g)}" if _код_надёжен else ''
                детали = ('цель грузится через GTM/бандл или требует действия, '
                          'которого не было в прогоне - проверяется вручную/в '
                          'Метрике' + _подск)
                счёт['manual'] += 1
        elif t in ('url', 'url_re'):
            способ = 'визит страницы'
            s = (url_map.get(g['номер']) or {}).get('страница')
            if s and s['код'] == 200 and s['счётчик']:
                статус, цвет = 'Сработала', GREEN
                детали = (f"цель = визит на страницу; мы открыли {s['url']} "
                          "(ответ 200, счётчик Метрики на странице стоит) - "
                          "значит визит засчитан и цель фиксируется"
                          + (', визит подтверждён в трафике' if s['визит'] else ''))
                счёт['ok'] += 1
            elif s:
                статус, цвет = 'Проблема', RED
                детали = f"страница {s['url']}: код {s['код']}, счётчик {'есть' if s['счётчик'] else 'НЕ найден'}"
                счёт['bad'] += 1
            else:
                статус, цвет = 'Нет автопроверки', GREY
                детали = (f"цель = визит на страницу «{g.get('url_часть','')}», а эта "
                          "страница не входит в список открываемых (напр. «спасибо»/"
                          "заказ/оплата - на них не попасть без реального заказа). "
                          "Добавьте её адрес в прогон - и автопроверка появится")
                счёт['manual'] += 1
        elif t == 'auto':
            сделано = _авто_действие_сделано(g.get('условие', ''))
            способ = 'автоцель Метрики'
            if сделано:
                статус, цвет = 'Действие выполнено', GREEN
                детали = ('автоцель Метрики (клик по телефону/почте/соцсети). Такие '
                          'цели Метрика считает САМА на своём сервере - отдельного '
                          'goal-сигнала в трафике нет, увидеть факт срабатывания извне '
                          'нельзя. Но нужное действие автотест выполнил - значит в '
                          'Метрике цель зачтётся')
                счёт['ok'] += 1
            else:
                статус, цвет = 'Автоцель (Метрика сама)', BLUE
                детали = ('это автоцель Метрики: Яндекс считает её сам на сервере '
                          '(отправка формы/файл/поиск/контакты) - отдельного '
                          'goal-сигнала в трафике нет, извне факт срабатывания не '
                          'виден. Действие закрывает «Проверка форм» (для форм) либо '
                          'оно совершается реальным посетителем - смотрите цифры в '
                          'самой Метрике')
                счёт['info'] += 1
        elif t == 'jivo':
            способ, статус, цвет = 'вручную', 'Вручную', GREY
            детали = 'события чата Jivo зависят от посетителя/оператора'
            счёт['manual'] += 1
        else:  # composite
            способ, статус, цвет = 'по шагам', 'Составная', BLUE
            детали = 'Метрика вычисляет из шагов - смотри цели-шаги выше/ниже'
            счёт['info'] += 1

        _строки.append({'номер': g['номер'], 'название': g['название'],
                        'условие': g['условие'], 'статус': статус,
                        'детали': детали, 'цвет': цвет})

    # Сортировка по статусу для читаемости листа.
    _строки.sort(key=lambda x: (_ПОРЯДОК.get(x['статус'].split(':')[0].strip(), 20),
                                x['название']))
    return {'строки': _строки, 'счёт': счёт, 'страницы': страницы,
            'код_надёжен': _код_надёжен, 'проект': каталог.get('проект', ''),
            'счётчик': каталог.get('счётчик', ''),
            'всего': len(каталог.get('цели', []))}


def _рисовать_цели(ws, строки):
    """Рисует лист целей: шапка + цветные плашки статусов + сетка + автофильтр."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    ws.sheet_view.showGridLines = False
    headers = ['№ цели', 'Название', 'Статус', 'Что это значит', 'Условие (из Метрики)']
    for c, (h, w) in enumerate(zip(headers, (12, 42, 20, 62, 44)), 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='EEF3FB')
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = 'A2'
    _tside = Side(style='thin', color='D9DCE1')
    _tbord = Border(left=_tside, right=_tside, top=_tside, bottom=_tside)
    r = 2
    for s in строки:
        vals = [s['номер'], s['название'], s['статус'], s['детали'], s['условие']]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r, c, v)
            cell.alignment = Alignment(wrap_text=(c in (2, 3, 4)), vertical='top')
            cell.border = _tbord
        st = ws.cell(r, 3)
        st.font = Font(color=s['цвет'], bold=True)
        st.fill = PatternFill('solid', fgColor=_ФОН.get(s['цвет'], 'FFFFFF'))
        r += 1
    for c in range(1, 6):
        ws.cell(1, c).border = _tbord
    ws.auto_filter.ref = f"A1:E{max(2, r - 1)}"


def _рисовать_сводку(sm, данные):
    """Лист «Сводка» одной страны: итоги по категориям + страницы прогона."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    GREEN, RED, GREY, BLUE = _GREEN, _RED, _GREY, _BLUE
    счёт = данные['счёт']
    страницы = данные['страницы']
    _код_надёжен = данные['код_надёжен']
    каталог = {'проект': данные['проект'], 'счётчик': данные['счётчик'],
               'цели': [None] * данные['всего']}
    sm.sheet_view.showGridLines = False
    sm.column_dimensions['A'].width = 4
    sm.column_dimensions['B'].width = 32
    sm.column_dimensions['C'].width = 9
    sm.column_dimensions['D'].width = 74
    _thin = Side(style='thin', color='D9DCE1')
    _bord = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)

    sm.merge_cells('A1:D1')
    sm['A1'] = f"Проверка целей Метрики - {каталог.get('проект','')}"
    sm['A1'].font = Font(bold=True, size=15)
    sm.merge_cells('A2:D2')
    sm['A2'] = (f"Счётчик {каталог.get('счётчик','')} · целей в каталоге: "
                f"{len(каталог.get('цели', []))} · прогон "
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')}")
    sm['A2'].font = Font(italic=True, color='5F6368')

    _подтв = счёт['ok'] + счёт['ok_forms']
    _пробл = счёт['no_code'] + счёт['bad']
    sm.merge_cells('A3:D3')
    sm['A3'] = (f"Подтверждено: {_подтв}   ·   Проблемы: {_пробл}   ·   "
                f"Требует действия/вручную: {счёт['special'] + счёт['manual']}   ·   "
                f"Формы и авто: {счёт['forms'] + счёт['info']}")
    sm['A3'].font = Font(bold=True, color='3C4043')

    # Категории сгруппированы по смыслу; между группами - пустая строка-разделитель.
    _группы = [
        ('ПОДТВЕРЖДЕНО', [
            ('✅ Сработали при кликах', счёт['ok'], GREEN,
             'цель реально зафиксирована во время кликов автотеста'),
            ('✅ Сработали через формы', счёт['ok_forms'], GREEN,
             'цель поймана при отправке формы («Проверка форм») - reachGoal рабочий'),
        ]),
        ('ПРОБЛЕМЫ (к разработчикам)', [
            ('❌ Нет в коде сайта', счёт['no_code'], RED,
             'reachGoal этой цели в коде сайта не найден - цель в Метрике есть, но '
             'сайт её не отправляет'),
            ('❌ НЕ сработала (reachGoal есть)', счёт['bad'], RED,
             'reachGoal в коде есть, действие выполняли, но цель не поймалась - '
             'проверить её настройку/кнопку'),
        ]),
        ('ТРЕБУЕТ ДЕЙСТВИЯ / ВРУЧНУЮ', [
            ('🟡 Нужно спец-действие', счёт['special'], GREY,
             'цель на вход/оплату/купон/избранное/скачивание - автотест этот шаг '
             'пока не делает (можно добавить сценарием)'),
            ('🖐 Не проверено', счёт['manual'], GREY,
             'цель грузится через GTM/бандл или её страницы не было в прогоне - '
             'смотрится вручную/в Метрике'),
        ]),
        ('ФОРМЫ И АВТО-ЦЕЛИ', [
            ('📝 Проверяется формами', счёт['forms'], BLUE,
             'цель отправки формы - запустите «Проверку форм», результат подтянется сюда'),
            ('ℹ️ Авто / составные', счёт['info'], BLUE,
             'Метрика считает сама на сервере (goal-сигнала в трафике нет - увидеть '
             'извне нельзя)'),
        ]),
    ]

    hr = 5
    for c, h in enumerate(['', 'Категория', 'Кол-во', 'Что это значит'], 1):
        cell = sm.cell(hr, c, h)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='5B6470')
        cell.border = _bord
        cell.alignment = Alignment(horizontal='center' if c == 3 else 'left')
    rr = hr + 1
    for заг, строки in _группы:
        sm.cell(rr, 2, заг).font = Font(bold=True, size=9, color='80868B')
        rr += 1
        for назв, кол, цв, пояс in строки:
            sm.cell(rr, 2, назв).font = Font(bold=True, color=цв)
            cc = sm.cell(rr, 3, кол)
            cc.font = Font(bold=True, color=цв)
            cc.alignment = Alignment(horizontal='center')
            cc.fill = PatternFill('solid', fgColor=_ФОН.get(цв, 'FFFFFF'))
            пc = sm.cell(rr, 4, пояс)
            пc.alignment = Alignment(wrap_text=True, vertical='top')
            for c in (2, 3, 4):
                sm.cell(rr, c).border = _bord
            sm.row_dimensions[rr].height = 30
            rr += 1
        rr += 1   # пустая строка-разделитель между группами

    if not _код_надёжен:
        sm.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=4)
        sm.cell(rr, 2, '⚠️ Код сайта грузится через GTM/бандл - reachGoal статически '
                       'не виден, поэтому «нет в коде» здесь не выносим (чтобы не '
                       'обвинять сайт зря).').font = Font(italic=True, color='B06000')
        rr += 2

    sm.cell(rr, 2, 'Страницы прогона').font = Font(bold=True)
    rr += 1
    for s in страницы:
        sm.cell(rr, 2, s['название']).font = Font(bold=True)
        sm.cell(rr, 4, f"{s['url']} - код {s['код']}, "
                       f"счётчик {'✓' if s['счётчик'] else '✗ НЕ найден'}")
        rr += 1


def построить_отчёт(pid: str, каталог: dict, прогон: dict,
                    out_path: str | Path) -> Path:
    """Отчёт по одному сайту: лист «Сводка» + лист «Цели Метрики»."""
    from openpyxl import Workbook
    данные = _классифицировать(pid, каталог, прогон)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Цели Метрики'
    _рисовать_цели(ws, данные['строки'])
    _рисовать_сводку(wb.create_sheet('Сводка', 0), данные)
    wb.active = 0
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def построить_сводный_отчёт(результаты: list, out_path: str | Path) -> Path:
    """Один файл на несколько сайтов: лист «Сводка» (строка на сайт) + по листу
    целей на каждый сайт. результаты = [(pid, каталог, прогон, метка), ...]."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    сайты = []
    for pid, каталог, прогон, метка in результаты:
        d = _классифицировать(pid, каталог, прогон)
        d['метка'] = метка or (каталог.get('проект') or pid)
        сайты.append(d)

    wb = Workbook()
    sm = wb.active
    sm.title = 'Сводка'
    sm.sheet_view.showGridLines = False
    _thin = Side(style='thin', color='D9DCE1')
    _bord = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
    for c, w in enumerate([26, 8, 12, 12, 20, 14], 1):
        sm.column_dimensions[get_column_letter(c)].width = w
    sm.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    sm.cell(1, 1, 'Проверка целей Метрики - сводка по сайтам · '
                  f"{datetime.now().strftime('%d.%m.%Y %H:%M')}").font = Font(bold=True, size=14)
    hdr = ['Сайт', 'Целей', '✅ Подтв.', '❌ Проблемы', '🟡 Действие/вручную', '📝 Формы/авто']
    for c, h in enumerate(hdr, 1):
        cell = sm.cell(3, c, h)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='5B6470')
        cell.border = _bord
        cell.alignment = Alignment(horizontal='left' if c == 1 else 'center',
                                   wrap_text=True, vertical='center')
    r = 4
    for d in сайты:
        s = d['счёт']
        подтв = s['ok'] + s['ok_forms']
        пробл = s['no_code'] + s['bad']
        vals = [d['метка'], d['всего'], подтв, пробл,
                s['special'] + s['manual'], s['forms'] + s['info']]
        for c, v in enumerate(vals, 1):
            cell = sm.cell(r, c, v)
            cell.border = _bord
            cell.alignment = Alignment(horizontal='left' if c == 1 else 'center')
            if c == 1:
                cell.font = Font(bold=True)
            elif c == 3 and подтв:
                cell.font = Font(bold=True, color=_GREEN)
                cell.fill = PatternFill('solid', fgColor=_ФОН[_GREEN])
            elif c == 4 and пробл:
                cell.font = Font(bold=True, color=_RED)
                cell.fill = PatternFill('solid', fgColor=_ФОН[_RED])
        r += 1

    # По листу целей на каждый сайт (имя листа = метка, уникальное, <=31 символ).
    занятые = set()
    for d in сайты:
        имя = (d['метка'] or 'Сайт').replace('/', '-').replace('\\', '-')[:28] or 'Сайт'
        база, i = имя, 2
        while имя in занятые:
            имя = f"{база[:26]} {i}"
            i += 1
        занятые.add(имя)
        _рисовать_цели(wb.create_sheet(имя), d['строки'])

    wb.active = 0
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path
