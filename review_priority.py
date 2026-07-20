# -*- coding: utf-8 -*-
"""
review_priority.py - «Закупаются отзывы на важные филиалы»: приоритет докупки.

По конфигу catalogs/reviews-<pid>.csv (city,country,yandex_url,2gis_url) для
каждого филиала тянем ЖИВЬЁМ текущий рейтинг и число отзывов из Яндекс.Бизнеса
(Карты) и 2ГИС, затем строим приоритет докупки по правилам клиента:

  • докупаем по 2 отзыва на филиал; если рейтинг низкий (есть негатив) - 3;
  • приоритет: сначала филиалы с рейтингом < 4.7, затем города от
    миллионников к менее населённым;
  • цель по бренду за цикл - 22-24 отзыва: набираем приоритетные филиалы,
    пока сумма не дойдёт до цели (это план на текущий цикл).

Рейтинг/отзывы - публичные страницы (сессия не нужна). Async + семафор.
"""
import asyncio
import csv
import re
from pathlib import Path

import twogis_check

BASE = Path(__file__).parent

# Правила клиента (можно менять).
RATING_THRESHOLD = 4.7     # ниже - приоритет докупки
NEGATIVE_RATING = 4.5      # ниже - считаем что есть негатив → заказываем 3
ORDER_DEFAULT = 2
ORDER_NEGATIVE = 3
TARGET_MIN, TARGET_MAX = 22, 24   # отзывов на бренд за цикл
_CONCURRENCY = 6

# Население городов (тыс. чел.) - для приоритета «от миллионников к меньшим».
# РФ-миллионники + крупные + столицы/крупные города СНГ. Нет в списке → 0.
POPULATION = {
    'москва': 13100, 'санкт-петербург': 5600, 'новосибирск': 1635,
    'екатеринбург': 1540, 'казань': 1315, 'нижний новгород': 1210,
    'челябинск': 1180, 'красноярск': 1190, 'самара': 1160, 'уфа': 1160,
    'ростов-на-дону': 1140, 'краснодар': 1100, 'омск': 1110, 'воронеж': 1050,
    'пермь': 1030, 'волгоград': 1000, 'саратов': 830, 'тюмень': 855,
    'тольятти': 685, 'ижевск': 650, 'барнаул': 630, 'ульяновск': 625,
    'иркутск': 610, 'хабаровск': 610, 'махачкала': 605, 'ярославль': 600,
    'владивосток': 600, 'томск': 570, 'оренбург': 560, 'кемерово': 555,
    'новокузнецк': 545, 'рязань': 535, 'набережные челны': 540,
    'астрахань': 525, 'пенза': 515, 'киров': 470, 'липецк': 500,
    'чебоксары': 495, 'балашиха': 510, 'калининград': 490, 'тула': 460,
    'курск': 440, 'севастополь': 510, 'сочи': 465, 'ставрополь': 455,
    'улан-удэ': 435, 'тверь': 425, 'магнитогорск': 410, 'иваново': 400,
    'брянск': 400, 'белгород': 390, 'сургут': 400, 'владимир': 350,
    'нижний тагил': 340, 'архангельск': 335, 'чита': 335, 'калуга': 340,
    'смоленск': 320, 'волжский': 315, 'череповец': 310, 'вологда': 315,
    'саранск': 300, 'курган': 305, 'орёл': 300, 'подольск': 300,
    'якутск': 355, 'грозный': 330, 'тамбов': 290, 'стерлитамак': 275,
    'кострома': 270, 'петрозаводск': 280, 'нижневартовск': 280,
    'новороссийск': 275, 'йошкар-ола': 275, 'таганрог': 245, 'сыктывкар': 245,
    'нальчик': 240, 'шахты': 230, 'дзержинск': 230, 'орск': 220,
    'братск': 220, 'ангарск': 220, 'благовещенск': 240, 'энгельс': 225,
    'великий новгород': 225, 'старый оскол': 220, 'мурманск': 270,
    'псков': 210, 'бийск': 200, 'южно-сахалинск': 205, 'армавир': 190,
    'рыбинск': 180, 'северодвинск': 180, 'абакан': 185, 'петропавловск-камчатский': 180,
    'норильск': 180, 'сызрань': 165, 'уссурийск': 180, 'новочеркасск': 165,
    'златоуст': 160, 'электросталь': 160, 'альметьевск': 160,
    # СНГ - крупные
    'алматы': 2100, 'ташкент': 2900, 'минск': 2020, 'баку': 2300,
    'астана': 1350, 'нур-султан': 1350, 'бишкек': 1080, 'ереван': 1080,
    'самарканд': 560, 'шымкент': 1100, 'караганда': 500, 'гомель': 510,
    'могилёв': 355, 'витебск': 360, 'брест': 350, 'гродно': 360,
    'наманган': 650, 'андижан': 450, 'фергана': 300, 'гянджа': 335,
    'усть-каменогорск': 340, 'павлодар': 360, 'актобе': 500, 'тараз': 360,
    'атырау': 355, 'костанай': 240, 'петропавловск': 220, 'уральск': 335,
    'актау': 240, 'кызылорда': 300, 'кокшетау': 155, 'семей': 350,
    'туркестан': 165, 'экибастуз': 155, 'жезказган': 85, 'каракол': 85,
}


def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').strip().lower().replace('ё', 'е'))


def load_branches(project_id):
    """catalogs/reviews-<pid>.csv → [{'city','country','yandex_url','2gis_url'}].
    None - если конфига нет."""
    f = BASE / 'catalogs' / f'reviews-{project_id}.csv'
    if not f.is_file():
        return None
    out = []
    with open(f, encoding='utf-8-sig', newline='') as fh:
        for row in csv.DictReader(fh):
            city = (row.get('city') or '').strip()
            if not city:
                continue
            out.append({
                'city': city,
                'country': (row.get('country') or '').strip(),
                'yandex_url': (row.get('yandex_url') or '').strip(),
                '2gis_url': (row.get('2gis_url') or '').strip(),
            })
    return out


_RE_YA_RATING = re.compile(
    r'"ratingValue"\s+content="([\d.]+)"|itemprop="ratingValue"\s+content="([\d.]+)"')
_RE_YA_COUNT = re.compile(r'"reviewCount"\s+content="(\d+)"')


async def _fetch_yandex(ctx, sem, url):
    """Рейтинг и число отзывов организации с Яндекс.Карт (метатеги
    ratingValue/reviewCount). → {'rating': float|None, 'count': int|None}."""
    if not url:
        return {'rating': None, 'count': None}
    async with sem:
        page = await ctx.new_page()
        rating = count = None
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(2500)
            html = await page.content()
            mr = _RE_YA_RATING.search(html)
            if mr:
                rating = float(mr.group(1) or mr.group(2))
            mc = _RE_YA_COUNT.search(html)
            if mc:
                count = int(mc.group(1))
        except Exception:
            pass
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return {'rating': rating, 'count': count}


async def _fetch_2gis_light(ctx, sem, url):
    """Рейтинг и число отзывов из 2ГИС без скролла ленты (быстро)."""
    city, fid = twogis_check.parse_firm(url)
    if not fid:
        return {'rating': None, 'count': None}
    city = city or 'moscow'
    async with sem:
        page = await ctx.new_page()
        data = {'rating': None, 'count': None}
        try:
            await page.goto(f'https://2gis.ru/{city}/firm/{fid}/tab/reviews',
                            wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_timeout(3000)
            html = await page.content()
            r = twogis_check._RE_RATING.search(html)
            c = twogis_check._RE_COUNT.search(html)
            data['rating'] = float(r.group(1)) if r else None
            data['count'] = int(c.group(1)) if c else None
        except Exception:
            pass
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return data


async def _fetch_all(branches, proxy_url, log):
    from playwright.async_api import async_playwright
    ctx_kw = {'user_agent': twogis_check.__dict__.get('UA')
              or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148 Safari/537.36',
              'locale': 'ru-RU'}
    if proxy_url:
        ctx_kw['proxy'] = {'server': proxy_url}
    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        try:
            ctx = await br.new_context(**ctx_kw)
            sem = asyncio.Semaphore(_CONCURRENCY)
            ytasks = [_fetch_yandex(ctx, sem, b['yandex_url']) for b in branches]
            gtasks = [_fetch_2gis_light(ctx, sem, b['2gis_url']) for b in branches]
            yres = await asyncio.gather(*ytasks)
            gres = await asyncio.gather(*gtasks)
            for b, y, g in zip(branches, yres, gres):
                b['yandex'] = y
                b['twogis'] = g
            if log:
                log(f'Отзывы-приоритет: собрано {len(branches)} филиалов '
                    f'(Яндекс+2ГИС)')
            return branches
        finally:
            await br.close()


def _branch_rating(b):
    """Рейтинг филиала для приоритета = минимальный из доступных (худший)."""
    rr = [x for x in (b.get('yandex', {}).get('rating'),
                      b.get('twogis', {}).get('rating')) if x is not None]
    return min(rr) if rr else None


def compute_priority(branches):
    """Приоритет докупки. → dict для отчёта."""
    for b in branches:
        r = _branch_rating(b)
        b['rating'] = r
        b['population'] = POPULATION.get(_norm(b['city']), 0)
        b['low_rating'] = (r is not None and r < RATING_THRESHOLD) or r is None
        b['negative'] = (r is not None and r < NEGATIVE_RATING)
        b['order'] = ORDER_NEGATIVE if b['negative'] else ORDER_DEFAULT

    # Сортировка: сначала приоритетные (низкий рейтинг), внутри - по населению
    # (города-миллионники выше); затем остальные по населению. Население - для
    # ПОРЯДКА, в таблицу не выводим.
    def _key(b):
        return (0 if b['low_rating'] else 1, -b['population'], _norm(b['city']))
    branches.sort(key=_key)

    return {
        'available': True,
        'branches': branches,
        'total_branches': len(branches),
        'low_rating_count': sum(1 for b in branches if b['low_rating']),
    }


def run(project_id, proxy_url=None, log=None):
    """Полная проверка приоритета докупки отзывов. → dict для листа отчёта
    или {'available': False, 'note': ...}."""
    branches = load_branches(project_id)
    if branches is None:
        return {'available': False,
                'note': f'Нет конфига catalogs/reviews-{project_id}.csv '
                        '(city,country,yandex_url,2gis_url).'}
    if not branches:
        return {'available': False, 'note': 'Конфиг отзывов пуст.'}
    try:
        asyncio.run(_fetch_all(branches, proxy_url, log))
    except Exception as e:
        return {'available': False, 'note': f'Отзывы-приоритет: {e}'}
    return compute_priority(branches)
