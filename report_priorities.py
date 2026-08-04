"""
report_priorities.py - сборка находок со ВСЕХ проверок в единый плоский список
(лист «Проблемы») и группировка в приоритезированный план работ (лист «План
работ»). Чистые функции без сети/openpyxl - только чтение уже посчитанных
результатов чек-листа (CheckResult.* и summary-словари, которые и так идут в
reporter.build_report).

Устройство:
  collect_findings()   - один Finding на одну находку одной страницы.
  TAXONOMY + classify() - находка -> приоритет/ответственный/почему важно
                          (таблица-словарь ПОВЕРХ находок - ни один из ~15
                          существующих чекеров не трогаем; текст находки не
                          совпал ни с одним правилом - безопасный дефолт,
                          находка не теряется).
  group_into_tasks()   - находки одной "сущности" (например, «SEO-текст без
                         таблицы» на 12 страницах) - одна строка «Плана
                         работ» с суммарным объёмом.
  extra_site_tasks()   - несколько задач уровня САЙТА/ХОСТА (фатальные
                         проблемы Вебмастера, всплески 5xx/404, служебные
                         адреса в robots.txt, санкции ПС) - эти находки не
                         привязаны к одной странице, поэтому в «Проблемы» не
                         попадают, а в «План работ» идут напрямую.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Finding:
    """Одна находка на одной странице - строка листа «Проблемы»."""
    level: str          # 'Ошибка' | 'Предупреждение'
    section: str        # раздел чек-листа (было отдельным листом)
    problem: str        # текст находки (как есть у чекера)
    city: str = ''
    page_type: str = ''
    url: str = ''
    detail: str = ''


@dataclass
class Task:
    """Одна строка листа «План работ» - находки одного task_group, слитые
    в одну задачу с суммарным объёмом."""
    priority: int        # 1 = критично, 2 = важно, 3 = плановое
    task_group: str       # ключ группировки (не показываем - только для merge)
    title: str            # «Задача»
    what: str              # «Что именно не так»
    volume: int            # «Объём» - сколько страниц/хостов затронуто
    why: str               # «Почему это важно»
    owner: str              # «Кому»
    where: str              # «Где смотреть детали»


PRIORITY_LABEL = {1: '1. Критично', 2: '2. Важно', 3: '3. Плановое'}


# ── 1. Сборка находок со всех проверок ───────────────────────────────

def _from_issue_dict(d: Optional[dict], *, section: str, city: str,
                     page_type: str, url: str) -> list:
    """Общий случай: d = {'issues': [str,...], 'warnings': [str,...]}
    (indexing/meta/layout/markup/security/images - все одинаковы)."""
    out = []
    if not d:
        return out
    for text in d.get('issues') or []:
        out.append(Finding('Ошибка', section, str(text), city, page_type, url))
    for text in d.get('warnings') or []:
        out.append(Finding('Предупреждение', section, str(text), city, page_type, url))
    return out


_REGION_KIND_LABEL = {
    'город': 'на странице упоминается другой город',
    'телефон': 'на странице указан телефон другого города',
    'почта': 'на странице указана почта другого города',
}


def _region_findings(region: Optional[dict], *, city, page_type, url) -> list:
    out = []
    for i in (region or {}).get('issues') or []:
        kind = i.get('тип', '')
        out.append(Finding(
            'Ошибка', 'Регион и город',
            _REGION_KIND_LABEL.get(kind, f'найдено чужое упоминание ({kind})'),
            city, page_type, url,
            detail=f'{i.get("зона", "")}: «{i.get("найдено", "")}» - '
                   f'{i.get("пояснение", "")}'))
    return out


def _content_findings(content, *, city, page_type, url) -> list:
    out = []
    if content is None:
        return out
    if getattr(content, 'is_soft_404', False):
        out.append(Finding('Ошибка', 'Блоки на странице',
                           'контент похож на страницу-404 (soft-404)',
                           city, page_type, url))
        return out
    for bug in content.bugs:
        out.append(Finding('Ошибка', 'Блоки на странице',
                           f'нет обязательного блока: {bug.label}',
                           city, page_type, url, detail=bug.note or ''))
    return out


_RE_TEMPLATE_PLACEHOLDER = re.compile(r'\$\{|\{\{|<%')


def _describe_img_src(src: str) -> str:
    """detail для конкретной картинки: сам адрес + пояснение, если это не
    реальный файл, а неотрендеренный шаблон (${...}/{{...}}/<%...%>) - JS
    не подставил значение в src. Это баг скрипта на ВСЕЙ странице (картинка
    не появится ни у одного товара с таким блоком), а не 404 одного файла -
    без пояснения такой src выглядит как бессмысленный мусор."""
    src = (src or '').strip()
    if _RE_TEMPLATE_PLACEHOLDER.search(src):
        return (f'src="{src}" - это не адрес файла, а неотрендеренный '
                f'JS-шаблон (переменная не подставилась) - картинка не '
                f'появится ни у одного товара с таким блоком, чинить надо '
                f'скрипт вывода, а не искать конкретный файл')
    return src


def _images_findings(images: Optional[dict], *, city, page_type, url) -> list:
    """Изображения: конкретная картинка (без alt / битая 404) - отдельной
    находкой с адресом картинки в detail, а не общий текст на всю страницу
    (как было раньше на листе «Изображения»). Остальные warnings (форматы/
    вес/lazy/имена) - как есть, общим текстом: там просто счётчики, для них
    нет одного конкретного URL. cat_warnings/prod_warnings (дубли картинок
    категорий/товаров) не входят в issues/warnings вообще - без этого блока
    терялись целиком."""
    out = []
    im = images or {}
    for src in im.get('no_alt') or []:
        out.append(Finding('Ошибка', 'Изображения',
                           'есть картинки без alt или с пустым alt=""',
                           city, page_type, url, detail=_describe_img_src(src)))
    for b in im.get('broken_imgs') or []:
        out.append(Finding('Ошибка', 'Изображения',
                           'битые картинки (404) - изображение не '
                           'отображается на странице',
                           city, page_type, url,
                           detail=_describe_img_src(b.get('url', ''))))
    for text in im.get('warnings') or []:
        out.append(Finding('Предупреждение', 'Изображения', str(text),
                           city, page_type, url))
    for w in im.get('cat_warnings') or []:
        d = im.get('cat_dup') or im.get('cat_img') or {}
        out.append(Finding('Предупреждение', 'Изображения', str(w),
                           city, page_type, url, detail=d.get('name', '')))
    for w in im.get('prod_warnings') or []:
        d = im.get('prod_dup') or im.get('prod_img') or {}
        out.append(Finding('Предупреждение', 'Изображения', str(w),
                           city, page_type, url, detail=d.get('name', '')))
    return out


_LAYOUT_ITEM_TEXT = {
    'css_broken': ('не грузится часть CSS-стилей (битые ссылки на файлы '
                   'стилей) - страница может выводиться без вёрстки'),
    'mixed_content': ('ресурсы грузятся по http на https-странице (mixed '
                      'content) - браузер их блокирует, картинки/стили/'
                      'скрипты ломаются'),
    'menu_broken': ('битые ссылки в меню шапки (404) - переходы по тех. '
                    'страницам/каталогу не работают'),
    'favicon': 'favicon не грузится (битая ссылка в link rel="icon")',
}


def _layout_findings(layout: Optional[dict], *, city, page_type, url) -> list:
    """Вёрстка: конкретный битый CSS-файл / конкретный http-ресурс (mixed
    content) / конкретная битая ссылка меню / favicon - отдельной находкой
    с адресом в detail, а не общий текст на всю страницу (как было раньше
    на листе «Вёрстка»). Остальные issues/warnings (viewport, семантика,
    минификация и т.п.) - как есть, там нечего итемизировать по URL."""
    out = []
    lt = layout or {}
    itemized_texts = set()

    for c in lt.get('css_broken') or []:
        text = _LAYOUT_ITEM_TEXT['css_broken']
        itemized_texts.add(text)
        out.append(Finding('Ошибка', 'Вёрстка', text, city, page_type, url,
                           detail=f'{c.get("url", "")} (код {c.get("status", "")})'))

    for m in lt.get('mixed_content') or []:
        text = _LAYOUT_ITEM_TEXT['mixed_content']
        itemized_texts.add(text)
        out.append(Finding('Ошибка', 'Вёрстка', text, city, page_type, url,
                           detail=m))

    for b in (lt.get('menu') or {}).get('broken') or []:
        text = _LAYOUT_ITEM_TEXT['menu_broken']
        itemized_texts.add(text)
        out.append(Finding('Ошибка', 'Вёрстка', text, city, page_type, url,
                           detail=f'{b.get("url", "")} (код {b.get("code", "")})'))

    fav = lt.get('favicon') or {}
    if fav.get('status') in (404, 410):
        text = _LAYOUT_ITEM_TEXT['favicon']
        itemized_texts.add(text)
        out.append(Finding('Ошибка', 'Вёрстка', text, city, page_type, url,
                           detail=fav.get('url', '')))

    for text in lt.get('issues') or []:
        if text not in itemized_texts:
            out.append(Finding('Ошибка', 'Вёрстка', str(text),
                               city, page_type, url))
    for text in lt.get('warnings') or []:
        out.append(Finding('Предупреждение', 'Вёрстка', str(text),
                           city, page_type, url))
    return out


def _console_findings(console_check: Optional[dict]) -> list:
    out = []
    for p in (console_check or {}).get('pages') or []:
        errs = p.get('errors') or []
        if errs:
            out.append(Finding('Ошибка', 'Ошибки JavaScript',
                               'ошибки JavaScript в консоли браузера',
                               url=p.get('url', ''),
                               detail='; '.join(errs[:3])))
        mob = p.get('mobile') or {}
        vps = mob.get('viewports') or ({'390': mob} if mob else {})
        for w, m in vps.items():
            if (m.get('overflow') or 0) > 8:
                out.append(Finding(
                    'Ошибка', 'Вёрстка',
                    f'на {w}px контент шире экрана на {m["overflow"]}px '
                    f'(горизонтальный скролл/обрезка)', url=p.get('url', '')))
            if m.get('overlaps'):
                out.append(Finding(
                    'Предупреждение', 'Вёрстка',
                    f'на {w}px блоки накладываются друг на друга',
                    url=p.get('url', '')))
    return out


def _kp_findings(kp_result: Optional[dict], *, city, page_type, url) -> list:
    """Сверка контактов главной с КП (kp.check_against_kp): issues -
    [{field, status, comment}], status='ok' - не находка."""
    out = []
    for i in (kp_result or {}).get('issues') or []:
        status = i.get('status')
        if status == 'ok':
            continue
        level = 'Ошибка' if status in ('bug', 'critical') else 'Предупреждение'
        out.append(Finding(level, 'Контакты по городам',
                           f'{i.get("field", "")}: {i.get("comment", "")}',
                           city, page_type, url))
    return out


def _page_phone_findings(page_phone: Optional[dict], *, city, page_type, url) -> list:
    """Сверка телефона в контенте тех. страницы с КП: {status, comment}."""
    if not page_phone or page_phone.get('status') == 'ok':
        return []
    level = 'Ошибка' if page_phone.get('status') in ('bug', 'critical') else 'Предупреждение'
    return [Finding(level, 'Контакты по городам', page_phone.get('comment', ''),
                    city, page_type, url)]


def _contacts_addr_findings(contacts_addr: Optional[dict], *, city, page_type,
                            url) -> list:
    """Сверка адресов всех городов на странице «Контакты»:
    {on_page, matched, mismatched:[{city,site,kp}], not_in_kp}."""
    out = []
    for m in (contacts_addr or {}).get('mismatched') or []:
        out.append(Finding(
            'Ошибка', 'Контакты по городам',
            f'адрес города «{m.get("city", "")}» на странице не совпадает с КП',
            city, page_type, url,
            detail=f'на сайте: «{m.get("site", "")}», в КП: «{m.get("kp", "")}»'))
    return out


def _availability_findings(r) -> list:
    """Страница не открывается вовсе - самая базовая находка."""
    if r.is_ok:
        return []
    return [Finding('Ошибка', 'Доступность страниц',
                    f'страница отвечает {r.http_code or "?"} ({r.status}) - '
                    f'не открывается', r.city, r.type_label, r.url,
                    detail=r.error_message or '')]


def _index_404_findings(index_404_check: Optional[dict]) -> list:
    out = []
    for h in (index_404_check or {}).get('hosts') or []:
        for e in h.get('dead') or []:
            out.append(Finding(
                'Ошибка', '404 в индексе',
                'страница есть в поиске, но открывается с ошибкой',
                url=e.get('url', ''),
                detail=f'код {e.get("status", "")} · источник: '
                       f'{e.get("source", "")}'))
    return out


def collect_findings(results, *, console_check: dict = None,
                     index_404_check: dict = None) -> list:
    """Собрать находки со всех страниц прогона (results) + отдельных
    проверок браузером (console_check, index_404_check - те не привязаны к
    result-у: свой список страниц/хостов). Возвращает list[Finding]."""
    out: list = []
    for r in results or []:
        city, page_type, url = r.city, r.type_label, r.url
        out.extend(_availability_findings(r))
        out.extend(_from_issue_dict(r.indexing, section='Индексация',
                                    city=city, page_type=page_type, url=url))
        out.extend(_from_issue_dict(r.meta, section='Метаданные',
                                    city=city, page_type=page_type, url=url))
        out.extend(_layout_findings(r.layout, city=city, page_type=page_type,
                                    url=url))
        out.extend(_from_issue_dict(r.markup, section='Разметка',
                                    city=city, page_type=page_type, url=url))
        out.extend(_from_issue_dict(r.security, section='Безопасность',
                                    city=city, page_type=page_type, url=url))
        out.extend(_images_findings(r.images, city=city, page_type=page_type,
                                    url=url))
        out.extend(_from_issue_dict(getattr(r, 'meta_unique', None),
                                    section='Заголовки и мета',
                                    city=city, page_type=page_type, url=url))
        out.extend(_from_issue_dict(getattr(r, 'cis', None), section='СНГ-домены',
                                    city=city, page_type=page_type, url=url))
        out.extend(_region_findings(r.region, city=city, page_type=page_type,
                                    url=url))
        out.extend(_content_findings(r.content, city=city, page_type=page_type,
                                     url=url))
        out.extend(_kp_findings(getattr(r, 'kp_result', None), city=city,
                                page_type=page_type, url=url))
        out.extend(_page_phone_findings(getattr(r, 'page_phone', None), city=city,
                                        page_type=page_type, url=url))
        out.extend(_contacts_addr_findings(getattr(r, 'contacts_addr', None),
                                           city=city, page_type=page_type, url=url))
    out.extend(_console_findings(console_check))
    out.extend(_index_404_findings(index_404_check))
    return out


# ── 2. Таксономия: находка -> приоритет/ответственный/почему важно ──────
# Правила по (раздел, подстрока в тексте находки), проверяются ПО ПОРЯДКУ,
# первое совпадение побеждает. Ничего не совпало -> _DEFAULT (находка не
# теряется, просто уходит в конец «Планового» без экспертной формулировки).
# task_group - ключ группировки в одну строку «Плана работ» (несколько
# похожих находок = один task_group = одна задача с суммарным объёмом).

_DEFAULT = {'priority': 3, 'owner': 'Разработка',
           'why': 'Требует ручной проверки - не хватает готового правила '
                  'приоритезации для этого типа находки.',
           'task_group': None, 'title': None}

# (раздел, подстрока, приоритет, ответственный, task_group, задача, почему)
_RULES = [
    ('Доступность страниц', '', 1, 'Разработка', 'availability',
     'Починить недоступные страницы',
     'Страница не открывается - покупатель и робот получают ошибку вместо контента.'),

    ('404 в индексе', '', 1, 'SEO + разработка', 'index_404',
     'Убрать из поиска удалённые страницы',
     'Пользователь из поиска попадает в пустоту, вес страниц теряется - нужен 301 на живой раздел.'),

    ('Индексация', 'noindex', 1, 'SEO', 'idx_noindex',
     'Проверить расхождение robots/noindex',
     'Сигналы индексации страницы противоречат друг другу - решить, что верно.'),
    ('Индексация', 'canonical', 2, 'Разработка', 'idx_canonical',
     'Добавить/поправить rel="canonical"',
     'Без canonical поиск сам решает, какой адрес считать основным.'),

    ('Блоки на странице', 'похожие товар', 2, 'Разработка', 'blocks_related',
     'Вернуть блок «Похожие товары»',
     'Меньше просмотров товаров и меньше добавлений в корзину.'),
    ('Блоки на странице', 'soft-404', 1, 'Разработка', 'blocks_soft404',
     'Разобрать страницы, похожие на 404',
     'Контент страницы фактически пустой - для пользователя и поиска это провал.'),
    ('Блоки на странице', '', 2, 'Разработка', 'blocks_generic',
     'Дополнить обязательные блоки страницы',
     'Отсутствие ожидаемого блока (цена/кнопка/фото и т.п.) мешает покупке.'),

    ('Заголовки и мета', 'вложенные теги', 2, 'Разработка', 'h1_nested',
     'Очистить H1 от вложенных тегов',
     'H1 - главный заголовок страницы, он должен быть простым текстом.'),
    ('Заголовки и мета', 'иерархия', 2, 'Контент', 'h_hierarchy',
     'Восстановить иерархию заголовков',
     'Ломается смысловая структура текста для поиска и нейроответов.'),
    ('Заголовки и мета', '', 2, 'SEO', 'headings_generic',
     'Проверить заголовки и мета-теги',
     'Заголовки/мета помогают и пользователю, и поиску понять страницу.'),

    ('Метаданные', 'canonical', 2, 'Разработка', 'idx_canonical',
     'Добавить/поправить rel="canonical"',
     'Без canonical поиск сам решает, какой адрес считать основным.'),
    ('Метаданные', 'нет таблицы', 2, 'Контент', 'seo_text_table',
     'Доработать SEO-тексты категорий (таблицы)',
     'Такие тексты хуже цитируются нейроответами и AI-выдачей.'),
    ('Метаданные', 'не структурирован', 2, 'Контент', 'seo_text_struct',
     'Доработать SEO-тексты категорий (структура)',
     'Такие тексты хуже цитируются нейроответами и AI-выдачей.'),
    ('Метаданные', 'главного ключа', 2, 'Контент', 'seo_text_key',
     'Доработать SEO-тексты категорий (ключ)',
     'Такие тексты хуже цитируются нейроответами и AI-выдачей.'),
    ('Метаданные', 'текста нет', 2, 'Контент', 'seo_text_missing',
     'Доработать SEO-тексты категорий (текст)',
     'Частотной категории нужен текст с главным ключом.'),
    ('Метаданные', 'caption', 2, 'Контент', 'seo_text_caption',
     'Доработать SEO-тексты категорий (таблицы без подписи)',
     'Чек-лист требует подпись и шапку у таблиц.'),
    ('Метаданные', 'одинаковый h1', 2, 'SEO', 'h1_dup_city',
     'Развести H1 по городам',
     'Заголовок без города не отличает региональную страницу от московской.'),
    ('Метаданные', 'нет города', 2, 'SEO', 'city_missing',
     'Добавить город в title и description',
     'Без города поддомен не отличается от Москвы и хуже ранжируется в регионе.'),
    ('Метаданные', 'нет meta description', 2, 'SEO', 'meta_desc_empty',
     'Заполнить пустые meta description',
     'Поиск подставит случайный фрагмент вместо продающего описания.'),
    ('Метаданные', 'длинный', 3, 'SEO', 'meta_len',
     'Привести в норму длины title и description',
     'Поиск обрезает сниппет, теряется призыв к действию.'),
    ('Метаданные', '', 2, 'SEO', 'meta_generic',
     'Проверить метаданные страницы',
     'Title/description/H1 напрямую влияют на клики из поиска.'),

    ('Изображения', 'без alt', 2, 'Контент + разработка', 'img_alt',
     'Прописать alt у изображений',
     'Теряется трафик из поиска по картинкам, страдает доступность.'),
    ('Изображения', 'битые картинки', 1, 'Разработка', 'img_broken',
     'Починить битые картинки',
     'Пустые места вместо фото снижают доверие и конверсию.'),
    ('Изображения', 'не уникальна', 2, 'Контент', 'img_unique',
     'Сделать картинки уникальными',
     'Одинаковые изображения снижают уникальность разделов.'),
    ('Изображения', 'дублируется', 2, 'Контент', 'img_unique',
     'Сделать картинки уникальными',
     'Одинаковые изображения снижают уникальность разделов.'),
    ('Изображения', '', 3, 'Контент', 'img_generic',
     'Проверить изображения',
     'Формат/вес/lazy-загрузка картинок влияют на скорость и SEO.'),

    ('Ошибки JavaScript', '', 2, 'Разработка', 'js_errors',
     'Убрать ошибки JavaScript',
     'Ошибки JS ломают интерактив: фильтры, формы, корзину.'),

    ('Разметка', 'изображение', 1, 'Разработка', 'schema_product',
     'Дополнить разметку товара',
     'Без этих полей поиск не покажет цену и фото в сниппете - меньше кликов.'),
    ('Разметка', 'предложение/цена', 1, 'Разработка', 'schema_product',
     'Дополнить разметку товара',
     'Без этих полей поиск не покажет цену и фото в сниппете - меньше кликов.'),
    ('Разметка', 'логотип', 3, 'Разработка', 'schema_org',
     'Дополнить разметку организации и FAQ',
     'Логотип и FAQ дают дополнительные элементы в выдаче.'),
    ('Разметка', 'faqpage', 3, 'Разработка', 'schema_org',
     'Дополнить разметку организации и FAQ',
     'Логотип и FAQ дают дополнительные элементы в выдаче.'),
    ('Разметка', 'листинг', 2, 'Разработка', 'schema_listing',
     'Разметить листинги',
     'Поиск хуже понимает, что на странице список товаров.'),
    ('Разметка', '', 2, 'Разработка', 'schema_generic',
     'Проверить микроразметку Schema.org/OG',
     'Разметка даёт дополнительные элементы в сниппете поиска.'),

    ('Безопасность', '', 3, 'Разработка', 'security_headers',
     'Поправить заголовки безопасности',
     'Заголовки безопасности защищают от кликджекинга/MIME-атак и влияют на оценку сайта.'),

    ('Вёрстка', 'aria-label', 3, 'Разработка', 'layout_aria',
     'Добавить aria-label кнопкам-иконкам',
     'Доступность сайта для людей с нарушениями зрения.'),
    ('Вёрстка', 'не объединены', 3, 'Разработка', 'layout_perf',
     'Оптимизировать загрузку фронтенда',
     'Каждый пункт добавляет к времени отрисовки первой страницы.'),
    ('Вёрстка', 'минифицированы', 3, 'Разработка', 'layout_perf',
     'Оптимизировать загрузку фронтенда',
     'Каждый пункт добавляет к времени отрисовки первой страницы.'),
    ('Вёрстка', 'async/defer', 3, 'Разработка', 'layout_perf',
     'Оптимизировать загрузку фронтенда',
     'Каждый пункт добавляет к времени отрисовки первой страницы.'),
    ('Вёрстка', 'font-display', 3, 'Разработка', 'layout_perf',
     'Оптимизировать загрузку фронтенда',
     'Каждый пункт добавляет к времени отрисовки первой страницы.'),
    ('Вёрстка', 'инлайн-стил', 3, 'Разработка', 'layout_perf',
     'Оптимизировать загрузку фронтенда',
     'Каждый пункт добавляет к времени отрисовки первой страницы.'),
    ('Вёрстка', 'inline-<script', 3, 'Разработка', 'layout_perf',
     'Оптимизировать загрузку фронтенда',
     'Каждый пункт добавляет к времени отрисовки первой страницы.'),
    ('Вёрстка', 'button/div', 2, 'Разработка', 'layout_pseudolinks',
     'Переделать псевдоссылки в <a href>',
     'Робот не пройдёт по такой ссылке, «открыть в новой вкладке» не работает.'),
    ('Вёрстка', 'хлебн', 3, 'Разработка', 'layout_breadcrumb',
     'Последняя хлебная крошка - без ссылки',
     'Мелкая, но заметная ошибка разметки навигации.'),
    ('Вёрстка', 'накладываются', 2, 'Разработка', 'layout_overlap',
     'Разобрать наложение блоков вёрстки',
     'Съехавшая вёрстка на части экранов ломает восприятие и мешает купить.'),
    ('Вёрстка', 'шире экрана', 2, 'Разработка', 'layout_overflow',
     'Убрать горизонтальный скролл/обрезку контента',
     'Часть контента не видна или требует лишнего скролла на мобильном.'),
    ('Вёрстка', 'фильтр', 1, 'Разработка', 'catalog_filters',
     'Починить фильтры в каталоге',
     'Покупатель не может подобрать товар по параметрам - прямая потеря заказов.'),
    ('Вёрстка', '', 3, 'Разработка', 'layout_generic',
     'Проверить вёрстку и адаптивность',
     'Вёрстка и адаптивность влияют на удобство покупки на любом устройстве.'),

    ('Регион и город', '', 1, 'SEO + разработка', 'region_wrong_city',
     'Убрать чужой город со страницы',
     'Смешение городов путает и покупателя, и региональное ранжирование.'),

    ('СНГ-домены', '', 1, 'SEO + разработка', 'cis_purity',
     'Убрать упоминания РФ/СНГ/чужих стран с СНГ-домена',
     'СНГ-домен должен выглядеть как локальный сайт этой страны, а не филиал РФ-сайта.'),

    ('Контакты по городам', '', 1, 'SEO + разработка', 'kp_contacts',
     'Свести контакты страницы с картой присутствия (КП)',
     'Неверный телефон/адрес города путает покупателя и портит доверие к сайту.'),
]


def _normalize(text: str) -> str:
    return (text or '').lower()


def classify(finding: 'Finding') -> dict:
    """Находка -> {priority, owner, why, task_group, title}. Первое
    совпадение (раздел, подстрока) побеждает; нет совпадений - _DEFAULT
    (task_group/title достраиваются от раздела, чтобы не потерять находку)."""
    text = _normalize(finding.problem)
    for section, pattern, priority, owner, task_group, title, why in _RULES:
        if section != finding.section:
            continue
        if pattern and pattern not in text:
            continue
        return {'priority': priority, 'owner': owner, 'why': why,
               'task_group': task_group, 'title': title}
    return {**_DEFAULT, 'task_group': f'other::{finding.section}',
           'title': f'Проверить: {finding.section}'}


# ── 3. Группировка находок в задачи ──────────────────────────────────

def group_into_tasks(findings: list) -> list:
    """Похожие находки (один task_group) -> одна строка «Плана работ» с
    суммарным объёмом (уникальные страницы) и представительным «что не
    так» (текст первой находки группы)."""
    groups: dict = {}
    for f in findings:
        meta = classify(f)
        key = meta['task_group']
        g = groups.setdefault(key, {
            'priority': meta['priority'], 'owner': meta['owner'],
            'why': meta['why'], 'title': meta['title'],
            'urls': set(), 'sample_problem': f.problem,
            'sample_section': f.section,
        })
        if f.url:
            g['urls'].add(f.url)
    tasks = []
    for key, g in groups.items():
        tasks.append(Task(
            priority=g['priority'], task_group=key, title=g['title'],
            what=g['sample_problem'], volume=len(g['urls']) or 1,
            why=g['why'], owner=g['owner'],
            where=f'Лист «Проблемы», раздел «{g["sample_section"]}»'))
    tasks.sort(key=lambda t: (t.priority, -t.volume))
    return tasks


# ── 4. Задачи уровня сайта/хоста (не привязаны к одной странице) ────────
# Эти находки НЕ идут в «Проблемы» (там колонки заточены под конкретную
# страницу), а сразу становятся строками «Плана работ».

def extra_site_tasks(*, indexing_summary: dict = None,
                     wm_metrics: dict = None,
                     service_issues: list = None,
                     ps_filters: dict = None) -> list:
    tasks = []

    junk = (indexing_summary or {}).get('junk_open') or []
    if junk:
        tasks.append(Task(
            priority=2, task_group='robots_junk',
            title='Закрыть служебные адреса в robots.txt',
            what='Открыты: ' + ', '.join(sorted({j.get('label', '') for j in junk})),
            volume=len(junk), owner='SEO',
            why='Робот тратит лимит обхода на мусорные копии страниц.',
            where='Лист «Индексация»'))

    sm_conflicts = (indexing_summary or {}).get('disallowed') or []
    if sm_conflicts:
        tasks.append(Task(
            priority=1, task_group='robots_sitemap_conflict',
            title='Открыть в robots.txt страницы из sitemap',
            what=f'{len(sm_conflicts)} путей каталога из sitemap закрыты Disallow',
            volume=len(sm_conflicts), owner='SEO',
            why='Sitemap говорит «в индекс», robots.txt - «нельзя»: страницы не попадут в поиск.',
            where='Лист «Индексация»'))

    if (indexing_summary or {}).get('blanket_disallow'):
        tasks.append(Task(
            priority=1, task_group='robots_blanket_disallow',
            title='Убрать «Disallow: /» из robots.txt',
            what='User-agent(ы): ' + ', '.join(
                sorted(indexing_summary['blanket_disallow'])),
            volume=1, owner='SEO + разработка',
            why='Директива закрывает сайт целиком - робот не проиндексирует ни одной страницы.',
            where='Лист «Индексация»'))

    _ac = (indexing_summary or {}).get('assets_closed') or []
    if _ac:
        tasks.append(Task(
            priority=1, task_group='robots_assets_closed',
            title='Открыть в robots.txt свои CSS/JS',
            what=f'Закрыто файлов: {len(_ac)} из '
                f'{(indexing_summary or {}).get("assets_checked", 0)}',
            volume=len(_ac), owner='Разработка',
            why='Google не сможет отрендерить страницу без стилей/скриптов - хуже ранжирование.',
            where='Лист «Индексация»'))

    _dc_f = ((indexing_summary or {}).get('directive_check') or {}).get('findings') or []
    if _dc_f:
        tasks.append(Task(
            priority=2, task_group='robots_directive_weak',
            title='Подстраховать закрытые в robots.txt страницы noindex',
            what=f'Страниц отвечают 200 без noindex: {len(_dc_f)}',
            volume=len(_dc_f), owner='Разработка',
            why='Держатся только на robots.txt - если на страницу есть внешняя ссылка, '
               'поисковик может показать её без сниппета.',
            where='Лист «Индексация»'))

    _adv = (indexing_summary or {}).get('advisory_open') or []
    if _adv:
        tasks.append(Task(
            priority=3, task_group='robots_advisory_open',
            title='Решить, нужны ли в индексе спорные разделы',
            what=', '.join(sorted({a.get('label', '') for a in _adv})),
            volume=len(_adv), owner='SEO',
            why='Старые акции/новости/политики часто лучше закрыть noindex - '
               'полезность субъективна, решает SEO.',
            where='Лист «Индексация»'))

    _pg = (indexing_summary or {}).get('pagination') or {}
    if _pg.get('status') == 200 and _pg.get('canon_ok') is False:
        tasks.append(Task(
            priority=2, task_group='pagination_canonical',
            title='Поправить canonical на пагинации категорий',
            what=f'{_pg.get("base", "")}: canonical = {_pg.get("canonical") or "нет"}',
            volume=1, owner='Разработка',
            why='Для Яндекса на странице пагинации нужен canonical на категорию '
               'без номера страницы.',
            where='Лист «Индексация»'))
    if _pg.get('loadmore') and _pg.get('pag_links') is False:
        tasks.append(Task(
            priority=2, task_group='pagination_loadmore_links',
            title='Добавить ссылки пагинации в HTML при JS-подгрузке',
            what=f'{_pg.get("base", "")}: бесконечная прокрутка без <a href> пагинации',
            volume=1, owner='Разработка',
            why='JS-подгрузку роботы не крутят - без ссылок в HTML товары дальше '
               'первой страницы не увидят.',
            where='Лист «Индексация»'))

    _mc = (((indexing_summary or {}).get('sitemap_audit') or {})
           .get('missing_catalog')) or {}
    _n_miss = (len(_mc.get('categories') or []) + len(_mc.get('filters') or [])
              + len(_mc.get('services') or []))
    if _n_miss:
        tasks.append(Task(
            priority=1, task_group='sitemap_missing_catalog',
            title='Добавить в sitemap отсутствующие страницы каталога',
            what=(f'категорий: {len(_mc.get("categories") or [])}, '
                 f'фильтров: {len(_mc.get("filters") or [])}, '
                 f'услуг: {len(_mc.get("services") or [])}'),
            volume=_n_miss, owner='Разработка',
            why='Страниц из выгрузки нет в sitemap - робот может их не найти и не '
               'проиндексировать.',
            where='Лист «Индексация»'))

    _bad_sm = ((indexing_summary or {}).get('sitemap_audit') or {}).get('bad_urls') or []
    if _bad_sm:
        tasks.append(Task(
            priority=2, task_group='sitemap_bad_urls',
            title='Убрать из sitemap битые/некорректные URL',
            what=f'битых URL в sitemap: {len(_bad_sm)}',
            volume=len(_bad_sm), owner='Разработка',
            why='Sitemap с битыми ссылками тратит лимит обхода робота и снижает '
               'доверие к файлу.',
            where='Лист «Индексация»'))

    _hm_junk = ((indexing_summary or {}).get('html_sitemap') or {}).get('junk_links') or []
    if _hm_junk:
        tasks.append(Task(
            priority=3, task_group='html_sitemap_junk',
            title='Убрать служебные ссылки из HTML-карты сайта',
            what=f'служебных ссылок: {len(_hm_junk)}',
            volume=len(_hm_junk), owner='Разработка',
            why='HTML-карта для пользователей и роботов должна вести на контент, '
               'а не в ЛК/корзину/поиск.',
            where='Лист «Индексация»'))

    _sev_by_metric: dict = {}
    for h in (wm_metrics or {}).get('hosts') or []:
        for a in h.get('anomalies') or []:
            key = a.get('metric', 'аномалия')
            d = _sev_by_metric.setdefault(key, {'hosts': set(), 'severity': a.get('severity')})
            d['hosts'].add(h.get('host', ''))
    for metric, d in _sev_by_metric.items():
        priority = 1 if d['severity'] in ('fatal', 'critical') else 2
        tasks.append(Task(
            priority=priority, task_group=f'wm_anomaly::{metric}',
            title=f'Разобрать: {metric}',
            what=f'Затронуто хостов: {len(d["hosts"])}',
            volume=len(d['hosts']), owner='Разработка',
            why='Робот не может нормально обойти сайт - страницы выпадают из индекса.',
            where='Лист «Хосты и аномалии»'))

    fatal_hosts = {i.host for i in (service_issues or [])
                  if getattr(i, 'severity', None) == 'fatal'}
    if fatal_hosts:
        tasks.append(Task(
            priority=1, task_group='wm_fatal',
            title='Закрыть фатальные проблемы в Вебмастере',
            what=', '.join(sorted(fatal_hosts)),
            volume=len(fatal_hosts), owner='SEO + разработка',
            why='Фатальная проблема блокирует индексацию сайта целиком.',
            where='Лист «Ошибки сервисов»'))

    sanc = (ps_filters or {}).get('yandex') or []
    gsc_hits = (ps_filters or {}).get('gsc_hits') or []
    if sanc or gsc_hits:
        tasks.append(Task(
            priority=1, task_group='ps_sanctions',
            title='Разобрать санкции поисковых систем',
            what=(f'Яндекс: {len(sanc)} хост(ов) с санкцией/угрозой'
                 if sanc else '') + (', ' if sanc and gsc_hits else '')
                + (f'Google: {len(gsc_hits)} писем с маркерами ручных мер'
                  if gsc_hits else ''),
            volume=len(sanc) + len(gsc_hits), owner='SEO',
            why='Санкция резко режет видимость сайта в поиске.',
            where='Лист «Фильтры ПС»'))

    return tasks
