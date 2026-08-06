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
    # Справочные цифры находки (замеры, объёмы, «проверено N путей») - идут
    # хвостом в колонку «Как исправить». Раньше такие числа жили только на
    # детальных листах; листов не стало, а цифры нужны, чтобы исполнитель
    # понимал масштаб, не открывая сайт.
    fix_note: str = ''


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


def _geo_findings(region: Optional[dict], *, city, page_type, url) -> list:
    """Технический регион поддомена (гео-сигналы meta geo.*/Schema
    addressLocality) - считается только на главной. Сам чекер кладёт эти
    тексты в warnings (не issues) - Предупреждение, не Ошибка."""
    geo = (region or {}).get('geo') or {}
    return [Finding('Предупреждение', 'Регион и город', str(w), city, page_type, url)
           for w in geo.get('warnings') or []]


_META_UNIQUE_LABEL = {
    'title': 'Title', 'description': 'Meta description',
    'h1': 'H1', 'h2': 'H2', 'h3': 'H3', 'h4': 'H4', 'h5': 'H5', 'h6': 'H6',
}


def _meta_unique_findings(meta_unique: Optional[dict], *, city, page_type,
                          url) -> list:
    """Заголовки и мета: issues - список словарей {тип, найдено, пояснение}
    (та же форма, что у region), а не строк - через generic _from_issue_dict
    получался бы str(dict) вида "{'тип': 'title', ...}" вместо текста."""
    out = []
    for i in (meta_unique or {}).get('issues') or []:
        label = _META_UNIQUE_LABEL.get(i.get('тип'), i.get('тип', ''))
        out.append(Finding(
            'Ошибка', 'Заголовки и мета', i.get('пояснение', ''),
            city, page_type, url, detail=f'{label}: {i.get("найдено", "")}'))
    return out


def _cis_findings(cis: Optional[dict], *, city, page_type, url) -> list:
    """СНГ-домены: issues - список словарей {тип, зона, найдено, контекст,
    пояснение} (та же форма, что у region/meta_unique), а не строк -
    через generic _from_issue_dict получался бы str(dict) вместо текста."""
    out = []
    for i in (cis or {}).get('issues') or []:
        out.append(Finding(
            'Ошибка', 'СНГ-домены', i.get('пояснение', ''),
            city, page_type, url,
            detail=f'{i.get("зона", "")}: «{i.get("найдено", "")}» - '
                   f'{i.get("контекст", "")}'))
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


def _search_check_findings(search_check: Optional[dict]) -> list:
    """Поиск по сайту (находит категории/теги в статике выдачи) - раньше
    была секция на листе «Вёрстка»."""
    if not search_check or not search_check.get('available'):
        return []
    out = []
    if search_check.get('found_category') is False:
        out.append(Finding(
            'Предупреждение', 'Вёрстка',
            f'поиск по сайту не находит категории (запрос '
            f'«{search_check.get("query", "")}»)',
            url=search_check.get('search_url', '')))
    if search_check.get('tag_query') and not search_check.get('found_tag'):
        out.append(Finding(
            'Предупреждение', 'Вёрстка',
            f'поиск по сайту не находит тег/фильтр (запрос '
            f'«{search_check["tag_query"]}»)',
            url=search_check.get('search_url', '')))
    return out


_FILTER_BAD_VERDICTS = {'empty', 'not_narrowed', 'http_error'}
_FILTER_VERDICT_TEXT = {
    'empty': 'фильтр применился, но выдача пустая',
    'not_narrowed': 'фильтр применился, но список товаров не изменился',
    'http_error': 'ошибка загрузки страницы при проверке фильтра',
    'apply_failed': 'не удалось применить фильтр автоматически - проверить вручную',
    'no_cards': 'карточки товаров на странице не распознаны - проверить вручную',
    'filter_absent': 'фильтр не найден на странице - проверить вручную',
    'config_error': 'ошибка конфига проверки фильтра',
}


def _filters_test_findings(filters_test: Optional[dict]) -> list:
    """Тест фильтрации товаров (браузер, живой драйв фильтра по категориям
    прогона) - раньше была секция на листе «Вёрстка»."""
    if not filters_test or not filters_test.get('available'):
        return []
    out = []
    for cs in filters_test.get('cases') or []:
        verdict = cs.get('verdict')
        if verdict == 'ok':
            continue
        level = 'Ошибка' if verdict in _FILTER_BAD_VERDICTS else 'Предупреждение'
        text = _FILTER_VERDICT_TEXT.get(verdict, verdict or 'неизвестный статус')
        out.append(Finding(level, 'Вёрстка', f'фильтр «{cs.get("name", "")}»: {text}',
                           url=cs.get('category', ''), detail=cs.get('detail', '')))
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


def _norm_host(s: str) -> str:
    from urllib.parse import urlsplit
    h = (s or '').strip().lower()
    if '//' in h:
        h = urlsplit(h).netloc or h
    return h.split(':')[0].lstrip('.').removeprefix('www.')


_CT_BROW_LABEL = {
    'not_replaced': 'номер не подменяется в браузере',
    'no_element': 'элемент с номером не найден на странице',
    'error': 'ошибка загрузки страницы при проверке подмены',
}


def _calltracking_findings(results, calltracking_check: Optional[dict]) -> list:
    """Замена рекл. номера (коллтрекинг): статика (kp_result.ad_check -
    каждый прогон) + браузерная подмена (calltracking_check - по галочке,
    only главные). Раньше был только на своём листе (полная таблица по
    каждому городу, даже без проблем) - тут только реальные расхождения."""
    out = []
    brow_by_host = {}
    for b in (calltracking_check or {}).get('results') or []:
        brow_by_host[_norm_host(b.get('url'))] = b
    for r in results or []:
        if getattr(r, 'type_code', '') != 'main':
            continue
        kp = getattr(r, 'kp_result', None) or {}
        ad = kp.get('ad_check') or {}
        host = _norm_host(getattr(r, 'subdomain', '') or kp.get('domain', ''))
        b = brow_by_host.get(host) or {}
        if ad.get('status') == 'bug':
            out.append(Finding(
                'Ошибка', 'Замена рекл. номера',
                'рекламный номер в конфиге коллтрекинга не совпадает с КП',
                r.city, r.type_label, r.url, detail=ad.get('comment', '')))
        b_status = b.get('status')
        if b_status in _CT_BROW_LABEL:
            level = 'Ошибка' if b_status == 'not_replaced' else 'Предупреждение'
            out.append(Finding(level, 'Замена рекл. номера',
                               f'{_CT_BROW_LABEL[b_status]} (реклама)',
                               r.city, r.type_label, r.url))
        seo = b.get('seo') or {}
        seo_status = seo.get('status')
        if seo_status in _CT_BROW_LABEL:
            level = 'Ошибка' if seo_status == 'not_replaced' else 'Предупреждение'
            out.append(Finding(level, 'Замена рекл. номера',
                               f'{_CT_BROW_LABEL[seo_status]} (поиск)',
                               r.city, r.type_label, r.url))
    return out


def _availability_findings(r) -> list:
    """Страница не открывается вовсе - самая базовая находка."""
    if r.is_ok:
        return []
    return [Finding('Ошибка', 'Доступность страниц',
                    f'страница отвечает {r.http_code or "?"} ({r.status}) - '
                    f'не открывается', r.city, r.type_label, r.url,
                    detail=r.error_message or '')]


def _text_issue_findings(r) -> list:
    """Битые переменные в тексте страницы (шаблон не подставил значение:
    {{city}}, %price%, undefined и т.п.) - раньше только на отдельном
    листе «Битые тексты», в «Проблемы» не попадали вообще."""
    if not getattr(r, 'has_text_issues', False):
        return []
    return [
        Finding('Ошибка', 'Битые тексты',
               f'битая переменная в тексте: {issue.match}',
               r.city, r.type_label, r.url,
               detail=f'тип шаблона: {issue.pattern} · где: {issue.context}')
        for issue in r.text_issues]


def _index_404_code(status) -> int:
    try:
        return int(str(status).strip() or 0)
    except (ValueError, TypeError):
        return 0


def _index_404_findings(index_404_check: Optional[dict]) -> list:
    """404 в индексе: 'dead' (явные 404/410) и 'errors' (5xx/прочие ошибки
    у страниц из поиска) - раньше 'errors' не читался вообще, только на
    старом листе показывался."""
    out = []
    for h in (index_404_check or {}).get('hosts') or []:
        for e in h.get('dead') or []:
            out.append(Finding(
                'Ошибка', '404 в индексе',
                'страница есть в поиске, но открывается с ошибкой',
                url=e.get('url', ''),
                detail=f'код {e.get("status", "")} · источник: '
                       f'{e.get("source", "")}'))
        for e in h.get('errors') or []:
            code = _index_404_code(e.get('status'))
            problem = ('сервер не ответил на страницу из поиска'
                      if code >= 500 or code == 0 else
                      'страница из поиска недоступна (не 404, но и не открывается)')
            out.append(Finding(
                'Ошибка', '404 в индексе', problem,
                url=e.get('url', ''),
                detail=f'код {e.get("status", "")} · источник: '
                       f'{e.get("source", "")}'))
    return out


def _metrika_404_findings(metrika_reports, results) -> list:
    """404 по данным Яндекс.Метрики (реальные визиты на несуществующую
    страницу) - раньше отдельный лист «404 из Метрики». Источник ДРУГОЙ,
    чем у «404 в индексе» (Вебмастер/GSC: страница есть в поиске) - тут
    источник посещений, поэтому пишем это прямо в detail, а не молчим:
    если URL совпадает с 404/410, пойманным самим чек-листом - «подтверждено
    обходом сайта»; если нет - «только по Метрике» (вне выборки прогона или
    домен не проверялся)."""
    if not metrika_reports:
        return []
    from urllib.parse import urlparse as _urlparse
    sc_failed_urls, sc_failed_paths = set(), set()
    for r in results or []:
        if r.is_error and r.http_code in (404, 410):
            sc_failed_urls.add(r.url)
            try:
                p = _urlparse(r.url).path
                if p:
                    sc_failed_paths.add(p)
            except ValueError:
                pass

    out = []
    for report in metrika_reports:
        for page in report.pages:
            url = page.page_url or ''
            confirmed = url in sc_failed_urls
            if not confirmed and url:
                try:
                    confirmed = _urlparse(url).path in sc_failed_paths
                except ValueError:
                    pass
            problem = ('страница отвечает ошибкой - подтверждено и обходом '
                      'сайта, и реальными визитами (Метрика)' if confirmed else
                      'по данным Метрики были визиты на страницу с ошибкой - '
                      'при обходе сайта её не поймали (вне выборки прогона '
                      'или чужой домен), но переходы на неё реальны')
            out.append(Finding(
                'Ошибка', '404 в индексе', problem, url=url or '(URL не пришёл в письме)',
                detail=f'{report.country_name}, {report.report_date}: '
                       f'просмотров {page.views}, посетителей {page.visitors}'
                       + (f' · реферер: {page.referer}' if page.referer else '')
                       + f' · заголовок страницы: «{page.page_title}»'))
    return out


def _markup_findings(markup: Optional[dict], *, city, page_type, url) -> list:
    """Разметка (OG/Schema.org) - как _from_issue_dict, но с field_details
    ("Offer/цена: 21 из 60") в detail - раньше это была отдельная колонка
    на листе «Разметка», при объединении в общий адаптер терялась."""
    if not markup:
        return []
    out = []
    details = markup.get('field_details') or []
    detail_text = '; '.join(details[:3]) + (f' … +{len(details) - 3}' if len(details) > 3 else '')
    for text in markup.get('issues') or []:
        out.append(Finding('Ошибка', 'Разметка', str(text), city, page_type, url,
                           detail=detail_text))
    for text in markup.get('warnings') or []:
        out.append(Finding('Предупреждение', 'Разметка', str(text), city, page_type, url,
                           detail=detail_text))
    return out


def collect_findings(results, *, console_check: dict = None,
                     index_404_check: dict = None,
                     metrika_reports: list = None,
                     calltracking_check: dict = None,
                     search_check: dict = None,
                     filters_test: dict = None) -> list:
    """Собрать находки со всех страниц прогона (results) + отдельных
    проверок браузером (console_check, index_404_check, calltracking_check,
    search_check, filters_test - те не привязаны к result-у напрямую: свой
    список страниц/хостов/категорий). Возвращает list[Finding]."""
    out: list = []
    for r in results or []:
        city, page_type, url = r.city, r.type_label, r.url
        out.extend(_availability_findings(r))
        out.extend(_text_issue_findings(r))
        out.extend(_from_issue_dict(r.indexing, section='Индексация',
                                    city=city, page_type=page_type, url=url))
        out.extend(_from_issue_dict(r.meta, section='Метаданные',
                                    city=city, page_type=page_type, url=url))
        out.extend(_from_issue_dict(getattr(r, 'seo_text', None), section='Метаданные',
                                    city=city, page_type=page_type, url=url))
        out.extend(_layout_findings(r.layout, city=city, page_type=page_type,
                                    url=url))
        out.extend(_markup_findings(r.markup, city=city, page_type=page_type,
                                    url=url))
        out.extend(_from_issue_dict(r.security, section='Безопасность',
                                    city=city, page_type=page_type, url=url))
        out.extend(_images_findings(r.images, city=city, page_type=page_type,
                                    url=url))
        out.extend(_meta_unique_findings(getattr(r, 'meta_unique', None),
                                         city=city, page_type=page_type,
                                         url=url))
        out.extend(_cis_findings(getattr(r, 'cis', None), city=city,
                                 page_type=page_type, url=url))
        out.extend(_region_findings(r.region, city=city, page_type=page_type,
                                    url=url))
        out.extend(_geo_findings(r.region, city=city, page_type=page_type,
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
    out.extend(_metrika_404_findings(metrika_reports, results))
    out.extend(_calltracking_findings(results, calltracking_check))
    out.extend(_search_check_findings(search_check))
    out.extend(_filters_test_findings(filters_test))
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

    ('Битые тексты', '', 2, 'Разработка', 'broken_text_vars',
     'Убрать битые переменные из текста',
     'Шаблонизатор не подставил значение - фрагмент кода виден покупателю прямо на странице.'),

    ('404 в индексе', '', 1, 'SEO + разработка', 'index_404',
     'Убрать из поиска удалённые страницы',
     'Пользователь из поиска попадает в пустоту, вес страниц теряется - нужен 301 на живой раздел.'),

    ('Индексация', 'noindex', 1, 'SEO', 'idx_noindex',
     'Проверить расхождение robots/noindex',
     'Сигналы индексации страницы противоречат друг другу - решить, что верно.'),
    ('Индексация', 'дубль главной', 1, 'Разработка', 'home_dup',
     'Склеить дубли главной страницы',
     'Поисковик делит вес и доверие между несколькими адресами одной и той же главной.'),
    ('Индексация', 'не в индексе', 2, 'SEO', 'arsenkin_not_indexed',
     'Разобраться, почему страницы не в индексе',
     'Раз страницы нет в индексе - она не приносит трафик, даже если технически всё верно.'),
    ('Индексация', 'canonical', 2, 'Разработка', 'idx_canonical',
     'Добавить/поправить rel="canonical"',
     'Без canonical поиск сам решает, какой адрес считать основным.'),
    ('Индексация', 'не чпу', 2, 'SEO + разработка', 'url_format_sef',
     'Перевести технические адреса на ЧПУ',
     'Адрес с ?ID=/.php хуже читается людьми и плодит дубли с параметрами.'),
    ('Индексация', 'кириллица в адресе', 3, 'SEO + разработка', 'url_format_cyr',
     'Перевести адреса на латиницу',
     'Кириллица в адресе превращается в %D0%… при копировании ссылки.'),
    ('Индексация', 'заглавные буквы в адресе', 3, 'Разработка', 'url_format_case',
     'Привести адреса к нижнему регистру',
     '/Catalog/ и /catalog/ - для поиска два разных адреса, то есть дубли.'),
    ('Индексация', 'подчёркивания', 3, 'SEO + разработка', 'url_format_underscore',
     'Заменить подчёркивания на дефисы в адресах',
     'Поиск считает словоразделителем дефис, а не «_».'),
    ('Индексация', 'спецсимволы в адресе', 3, 'Разработка', 'url_format_junk',
     'Убрать пробелы и спецсимволы из адресов',
     'Такие адреса ломаются при копировании и в выгрузках.'),
    ('Индексация', 'user-agent', 3, 'SEO', 'robots_ua_groups',
     'Навести порядок в группах robots.txt',
     'Отдельные группы для Яндекса и Google позволяют задавать им разные правила.'),
    ('Индексация', 'отгрузки', 3, 'SEO', 'otgruzki_links',
     'Добавить перелинковку из «Отгрузок» на каталог',
     'Живой раздел без ссылок на каталог не передаёт ему вес.'),
    # Подстроки - латиницей: в тексте находки склонения («нет даты публикации»),
    # а имена полей разметки стоят там всегда и в одной форме.
    ('Индексация', 'datepublished', 3, 'Контент', 'article_dates',
     'Проставить даты у статей и новостей',
     'Без дат поиск не понимает свежесть материала и реже показывает его в сниппете.'),
    ('Индексация', 'datemodified', 3, 'Контент', 'article_dates',
     'Проставить даты у статей и новостей',
     'Без дат поиск не понимает свежесть материала и реже показывает его в сниппете.'),

    ('Страница 404', '', 2, 'Разработка', 'p404_test',
     'Починить страницу 404',
     'Правильная страница 404 (уникальный title, ссылки, форма) удерживает посетителя, а не отпугивает его.'),

    ('Нагрузка и парсинг', 'бота за парсера', 1, 'Разработка', 'stress_banned',
     'Разобраться с защитой от ботов',
     'Защита банит и обычных роботов поисковых систем, не только парсеры - риск потери индексации.'),
    ('Нагрузка и парсинг', '', 2, 'Разработка', 'stress_5xx',
     'Стабилизировать сервер под нагрузкой',
     'Ошибки сервера под обходом/нагрузкой роботы воспринимают как нестабильный сайт - хуже краулинг.'),

    ('Фильтры ПС', '', 1, 'SEO', 'ps_sanctions_item',
     'Разобрать санкции поисковых систем',
     'Санкция резко режет видимость сайта в поиске.'),

    ('Ошибки сервисов', '', 2, 'SEO + разработка', 'service_issue_item',
     'Разобрать проблему в сервисах',
     'Сервис (Вебмастер/GSC/Метрика) диагностировал проблему по официальным данным.'),

    ('Валидация и скорость', 'сжатие', 2, 'Разработка', 'static_compression',
     'Включить сжатие статики (Gzip/Brotli)',
     'Несжатые CSS/JS едут к посетителю в разы дольше - страдает скорость на всех страницах.'),
    ('Валидация и скорость', 'кеш статики', 2, 'Разработка', 'static_cache',
     'Настроить кеш статики (Cache-Control/ETag)',
     'Без кеша браузер качает одни и те же файлы при каждом заходе.'),
    ('Валидация и скорость', 'дольше 8 секунд', 2, 'Разработка', 'page_resources_slow',
     'Ускорить загрузку ресурсов страницы',
     'Долгая загрузка бьёт по поведенческим факторам и по конверсии на мобильных.'),
    ('Валидация и скорость', '', 3, 'Разработка', 'w3c_errors',
     'Поправить ошибки валидатора W3C',
     'Невалидный HTML/CSS может рендериться браузерами по-разному и мешать роботу.'),

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
    ('Метаданные', 'дубль', 1, 'Контент', 'meta_dup_same_city',
     'Развести дублирующиеся title/description/H1 внутри города',
     'Поиск считает одинаковые страницы дублями и хуже ранжирует обе.'),
    ('Метаданные', 'совпадает с другим городом', 3, 'SEO', 'meta_dup_cross_city',
     'Проверить межгородские совпадения title/description/H1',
     'Само по себе не санкция, но стоит проверить наличие ключа и города в тексте.'),
    ('Метаданные', 'зеркало адреса', 2, 'Разработка', 'meta_url_mirror',
     'Склеить зеркала адреса редиректом/canonical',
     'Одна страница доступна по нескольким адресам - поиск делит вес между ними.'),
    ('Метаданные', 'тестовый поддомен', 1, 'Разработка', 'meta_test_domain',
     'Закрыть тестовый поддомен от индексации',
     'Открытый тестовый поддомен - полный дубль сайта в индексе.'),
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
    ('Вёрстка', 'слайдер', 2, 'Разработка', 'ux_slider',
     'Починить слайдер',
     'Посетитель видит только первый слайд - остальные предложения пропадают.'),
    ('Вёрстка', 'выпадающее меню', 2, 'Разработка', 'ux_dropdown',
     'Починить выпадающее меню в шапке',
     'Разделы каталога недоступны из шапки - страдают и навигация, и перелинковка.'),
    ('Вёрстка', 'cookie-баннер', 3, 'Разработка', 'ux_cookie',
     'Научить cookie-баннер запоминать выбор',
     'Баннер на каждой странице раздражает и перекрывает контент.'),
    ('Вёрстка', 'модальная форма', 2, 'Разработка', 'ux_modal',
     'Закрывать модальную форму по клику вне неё',
     'Посетитель застревает в окне и уходит с сайта вместо заявки.'),
    ('Вёрстка', '', 3, 'Разработка', 'layout_generic',
     'Проверить вёрстку и адаптивность',
     'Вёрстка и адаптивность влияют на удобство покупки на любом устройстве.'),

    ('Регион и город', 'технический регион не совпадает', 2, 'Разработка',
     'region_geo_mismatch',
     'Поправить технический регион (geo-теги/addressLocality)',
     'Гео-сигналы в коде не совпадают с реальным городом поддомена - '
     'путает роботов и локальные сервисы.'),
    ('Регион и город', 'технический регион не задан', 3, 'Разработка',
     'region_geo_missing',
     'Добавить технические гео-сигналы (meta geo.*/addressLocality)',
     'Без гео-сигналов в коде труднее подтвердить регион поддомена для '
     'локального ранжирования.'),
    ('Регион и город', '', 1, 'SEO + разработка', 'region_wrong_city',
     'Убрать чужой город со страницы',
     'Смешение городов путает и покупателя, и региональное ранжирование.'),

    ('СНГ-домены', '', 1, 'SEO + разработка', 'cis_purity',
     'Убрать упоминания РФ/СНГ/чужих стран с СНГ-домена',
     'СНГ-домен должен выглядеть как локальный сайт этой страны, а не филиал РФ-сайта.'),

    ('Контакты по городам', '', 1, 'SEO + разработка', 'kp_contacts',
     'Свести контакты страницы с картой присутствия (КП)',
     'Неверный телефон/адрес города путает покупателя и портит доверие к сайту.'),

    ('Замена рекл. номера', '', 1, 'Разработка', 'calltracking',
     'Починить подмену рекламного номера (коллтрекинг)',
     'Без рабочей подмены звонки с рекламы не отслеживаются - реклама '
     'выглядит менее эффективной, чем есть на самом деле.'),
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

# Дубль webmaster_api._SANCTION_CODE_RE (та же логика: реальная санкция/
# угроза - по коду-маркеру, а не по одной FATAL-серьёзности). report_priorities
# намеренно не импортирует webmaster_api (без сети/API - чистые функции).
_SANCTION_CODE_RE = re.compile(
    r'THREAT|MALWARE|SECUR|VIRUS|SPAM|QUALITY|SANC|FRAUD|PHISH|CHEAT|'
    r'OVEROPT|ADS?_|ADVERT|MOBILE_REDIRECT|DECEPT|CLOAK|DOORWAY', re.I)


_META_FIELD_LABEL = {'title': 'title', 'description': 'description', 'h1': 'H1'}


def metadata_site_findings(meta_summary: Optional[dict]) -> list:
    """Дубли метаданных (same_city/cross_city), дубли УРЛОВ и тестовые
    домены - раньше только на листе «Метаданные», страничная проверка
    title/description/H1 уже итемизирована через r.meta, а вот эти
    межстраничные группы - нет. Один Finding на каждую затронутую
    страницу группы (не одна строка на группу), чтобы список был виден
    без открытия детального листа."""
    s = meta_summary or {}
    out = []

    for g in s.get('duplicates', {}).get('same_city') or []:
        field = _META_FIELD_LABEL.get(g.get('field', ''), g.get('field', ''))
        for p in g.get('pages') or []:
            out.append(Finding(
                'Ошибка', 'Метаданные',
                f'дубль {field} внутри города: одинаковое значение у нескольких страниц',
                p.get('city', ''), p.get('type_label', ''), p.get('url', ''),
                detail=g.get('value', '')))

    for g in s.get('duplicates', {}).get('cross_city') or []:
        field = _META_FIELD_LABEL.get(g.get('field', ''), g.get('field', ''))
        for p in g.get('pages') or []:
            out.append(Finding(
                'Предупреждение', 'Метаданные',
                f'{field} совпадает с другим городом - нет ключа/города в тексте?',
                p.get('city', ''), p.get('type_label', ''), p.get('url', ''),
                detail=g.get('value', '')))

    for d in s.get('url_duplicates') or []:
        if d.get('problem') == 'duplicate':
            level, problem = 'Ошибка', 'зеркало адреса отвечает 200 без редиректа - дубль страницы'
        elif d.get('problem') == 'not_301':
            level, problem = 'Предупреждение', 'зеркало адреса редиректит временно (302/303/307), а не 301'
        else:
            continue
        out.append(Finding(level, 'Метаданные', problem, url=d.get('variant', ''),
                           detail=f'канонический адрес: {d.get("canonical", "")}'))

    for t in s.get('test_domains') or []:
        if t.get('state') != 'indexable':
            continue
        out.append(Finding(
            'Ошибка', 'Метаданные',
            'тестовый поддомен открыт для индексации - дубль всего сайта',
            url=f'https://{t.get("host", "")}/'))

    return out


def home_dupes_findings(home_dupes: Optional[dict]) -> list:
    """Реальные дубли главной (адрес отвечает 200, поисковик его НЕ
    склеивает с канонической главной) - раньше только на листе «Дубли
    главной». ✔-варианты (склеено/это и есть главная) - не находки."""
    out = []
    for v in (home_dupes or {}).get('variants') or []:
        if v.get('verdict') != 'duplicate':
            continue
        out.append(Finding(
            'Ошибка', 'Индексация',
            'дубль главной страницы - адрес отвечает 200, но не склеен '
            'с канонической главной (нет редиректа/canonical)',
            url=v.get('url', ''), detail=v.get('note', '')))
    return out


def arsenkin_findings(arsenkin: Optional[dict]) -> list:
    """URL не в индексе Яндекса/Google (по данным Арсенкина) - раньше
    только на листе «Индексация (Арсенкин)»."""
    if not arsenkin or not arsenkin.get('available'):
        return []
    eng = arsenkin.get('engines') or {'yandex': True, 'google': True}
    out = []
    for r in arsenkin.get('rows') or []:
        missing = [name for name, checked in (('Яндекс', eng.get('yandex')),
                                              ('Google', eng.get('google')))
                  if checked and r.get('yandex' if name == 'Яндекс' else 'google') is False]
        if missing:
            out.append(Finding(
                'Ошибка', 'Индексация',
                f'страница не в индексе: {", ".join(missing)}',
                url=r.get('url', '')))
    return out


def page404_findings(p404_check: Optional[dict]) -> list:
    """Тест страницы 404 (код/дизайн/title/ссылки/форма) - раньше только
    на листе «Страница 404». Хост-уровневая находка (не привязана к
    конкретной проверенной странице прогона)."""
    out = []
    for h in (p404_check or {}).get('hosts') or []:
        city, host = h.get('city', ''), h.get('host', '')
        url = f'https://{host}/' if host else ''
        for t in h.get('issues') or []:
            out.append(Finding('Ошибка', 'Страница 404', t, city, url=url))
        for t in h.get('warnings') or []:
            out.append(Finding('Предупреждение', 'Страница 404', t, city, url=url))
    return out


def stress_check_findings(stress_check: Optional[dict]) -> list:
    """Ошибки сервера (5xx/обрывы) при быстром обходе, высокой нагрузке и
    кривых дублях URL - раньше только на листе «Нагрузка и парсинг»."""
    if not stress_check or not stress_check.get('available'):
        return []
    out = []
    parsing = stress_check.get('parsing') or {}
    load = stress_check.get('load') or {}
    dups = stress_check.get('duplicates') or {}

    banned = parsing.get('banned')
    if banned:
        out.append(Finding(
            'Ошибка', 'Нагрузка и парсинг',
            f'сайт принял бота за парсера и закрыл доступ (код {banned.get("code")}) '
            f'после {banned.get("after", 0)} успешных страниц',
            url=banned.get('url', '')))

    for e in parsing.get('server_errors') or []:
        out.append(Finding('Ошибка', 'Нагрузка и парсинг',
                           f'ошибка сервера ({e.get("code")}) при быстром обходе',
                           url=e.get('url', '')))

    _net = len(parsing.get('network_errors') or [])
    if _net:
        out.append(Finding('Ошибка', 'Нагрузка и парсинг',
                           f'обрывы связи при быстром обходе: {_net}'))

    for p in load.get('pages') or []:
        if p.get('server_5xx') or p.get('network_errors'):
            out.append(Finding(
                'Ошибка', 'Нагрузка и парсинг',
                'сервер отдаёт ошибки под параллельной нагрузкой',
                url=p.get('url', ''),
                detail=f'5xx {p.get("server_5xx", 0)}, обрывов '
                       f'{p.get("network_errors", 0)} из {p.get("sent", 0)} запросов'))
        elif p.get('degraded'):
            out.append(Finding(
                'Предупреждение', 'Нагрузка и парсинг',
                'ответ замедляется более чем в 3 раза под нагрузкой',
                url=p.get('url', '')))

    for e in dups.get('server_errors') or []:
        out.append(Finding(
            'Ошибка', 'Нагрузка и парсинг',
            f'ошибка сервера ({e.get("code")}) на кривом дубле URL ({e.get("kind", "")})',
            url=e.get('url', '')))

    return out


def ps_filters_findings(ps_filters: Optional[dict]) -> list:
    """Санкции/угрозы поисковых систем - реальные (не любая FATAL-проблема
    Вебмастера, см. _SANCTION_CODE_RE) плюс маркеры ручных мер в почте
    GSC. Раньше только на листе «Фильтры ПС»; extra_site_tasks() агрегирует
    те же данные в «План работ» отдельно (эта функция сюда не дублируется -
    не проходит через group_into_tasks)."""
    out = []
    sanc = (ps_filters or {}).get('yandex') or []
    for s in sanc:
        if not _SANCTION_CODE_RE.search(str(s.get('code') or '')):
            continue
        out.append(Finding(
            'Ошибка', 'Фильтры ПС',
            f'санкция/угроза в диагностике Вебмастера: {s.get("title", s.get("code", ""))}',
            url=f'https://{s.get("host", "")}/' if s.get('host') else '',
            detail=f'дата: {s.get("date", "")}'))
    for h in (ps_filters or {}).get('gsc_hits') or []:
        out.append(Finding(
            'Ошибка', 'Фильтры ПС',
            f'маркер ручных мер/безопасности в письме GSC: {h.get("subject", "")}',
            detail=f'дата: {h.get("date", "")}'))
    return out


_SVC_SEV_TO_LEVEL = {'fatal': 'Ошибка', 'critical': 'Ошибка',
                     'possible': 'Предупреждение', 'recommendation': 'Предупреждение'}


def service_issues_findings(service_issues: Optional[list]) -> list:
    """Ошибки сервисов (Вебмастер/GSC/Метрика по API, не из почты) - раньше
    только на листе «Ошибки сервисов». extra_site_tasks() агрегирует те же
    данные в «План работ» отдельно (по хосту, не по issue) - эта функция
    сюда не дублируется, не проходит через group_into_tasks. 'info' -
    справочная информация, не находка, пропускаем."""
    out = []
    for i in service_issues or []:
        level = _SVC_SEV_TO_LEVEL.get(getattr(i, 'severity', None))
        if not level:
            continue
        host = getattr(i, 'host', '')
        out.append(Finding(
            level, 'Ошибки сервисов',
            getattr(i, 'title', '') or getattr(i, 'code', ''),
            url=f'https://{host}/' if host else '',
            detail=f'дата: {getattr(i, "date", "")}'))
    return out


def w3c_findings(w3c_check: Optional[dict]) -> list:
    """Ошибки валидатора W3C (HTML/CSS) по выборке страниц - числа уже
    видны колонками на «Страницы», но без отдельной находки не попадали
    бы в «Проблемы» вовсе. Одна находка на страницу (не на ошибку -
    ошибок валидатора у боевых сайтов обычно десятки, разбивка по каждой
    не нужна), детали - на «Валидация и скорость»."""
    out = []
    for p in (w3c_check or {}).get('pages') or []:
        if p.get('error'):
            continue
        h, cs = p.get('html') or {}, p.get('css') or {}
        if h.get('error') or cs.get('error'):
            continue
        n = (h.get('errors', 0) or 0) + (cs.get('errors', 0) or 0)
        if not n:
            continue
        out.append(Finding(
            'Предупреждение', 'Валидация и скорость',
            f'ошибок валидатора W3C (HTML+CSS): {n}',
            url=p.get('url', ''),
            detail=f'HTML: {h.get("errors", 0)} · CSS: {cs.get("errors", 0)}'))
    return out


_URL_FORMAT_KINDS = (
    ('non_sef', 'адрес не ЧПУ (технический: ?ID=, .php, .asp)',
     'Перевести адреса на ЧПУ и закрыть технические от индексации'),
    ('cyrillic', 'кириллица в адресе',
     'Перевести адрес на латиницу (транслит) с 301-редиректом'),
    ('uppercase', 'ЗАГЛАВНЫЕ буквы в адресе',
     'Привести адрес к нижнему регистру с 301-редиректом'),
    ('underscore', 'подчёркивания вместо дефисов в адресе',
     'Заменить «_» на «-» с 301-редиректом'),
    ('junk_chars', 'пробелы/спецсимволы в адресе',
     'Убрать пробелы и спецсимволы из адреса'),
)


def url_format_findings(indexing_summary: Optional[dict]) -> list:
    """ЧПУ и формат адресов - по одной находке на КАЖДЫЙ кривой адрес (раньше
    только сводкой с 10 примерами на листе «Индексация»). Одна строка на адрес,
    чтобы задачу можно было отфильтровать и раздать как есть."""
    uf = (indexing_summary or {}).get('url_format') or {}
    if not uf.get('total_bad'):
        return []
    проверено = uf.get('checked', 0)
    out = []
    for kind, text, как in _URL_FORMAT_KINDS:
        всего = uf.get(kind + '_n', 0)
        if not всего:
            continue
        пути = uf.get(kind) or []
        for p in пути:
            out.append(Finding(
                'Предупреждение', 'Индексация', text,
                url=_idx_url(indexing_summary, p),
                detail=f'таких адресов: {всего}',
                fix_note=f'{как}. Проверено путей каталога: {проверено}.'))
        if всего > len(пути):
            out.append(Finding(
                'Предупреждение', 'Индексация', text,
                url=_idx_url(indexing_summary, '/'),
                detail=f'…и ещё {всего - len(пути)} таких адресов сверх '
                       f'показанных {len(пути)}',
                fix_note=f'{как}. Проверено путей каталога: {проверено}.'))
    return out


def robots_hygiene_findings(indexing_summary: Optional[dict]) -> list:
    """Гигиена robots.txt: отдельные группы User-agent для Яндекса/Google и
    группа «*» для прочих роботов. Раньше - только секцией на листе
    «Индексация» (Disallow: / и закрытые CSS/JS уже идут отдельными
    находками в indexing_site_findings)."""
    s = indexing_summary or {}
    ua = s.get('ua_groups')
    if s.get('error') or not ua:
        return []
    out = []
    нет = [n for n, k in (('Yandex', 'yandex'), ('Googlebot', 'google'))
           if not ua.get(k)]
    if нет:
        out.append(Finding(
            'Предупреждение', 'Индексация',
            f'в robots.txt нет отдельных групп User-agent: {", ".join(нет)}',
            url=_idx_url(s, '/robots.txt'),
            detail='роботы работают по общей группе «*»',
            fix_note='Завести отдельные группы User-agent для Яндекса и '
                     'Google - директивы для них часто отличаются.'))
    if not ua.get('star'):
        out.append(Finding(
            'Предупреждение', 'Индексация',
            'в robots.txt нет группы User-agent: * - для прочих роботов правил нет',
            url=_idx_url(s, '/robots.txt'),
            fix_note='Добавить группу «User-agent: *» с базовыми правилами.'))
    return out


def content_sections_findings(indexing_summary: Optional[dict]) -> list:
    """Раздел «Отгрузки» без перелинковки на каталог и статьи/новости без дат
    публикации/обновления - раньше только на листе «Индексация». ✅/«не
    найдено» не переносим: раздел опционален, отсутствие - не находка."""
    s = indexing_summary or {}
    if s.get('error'):
        return []
    out = []

    otg = s.get('otgruzki') or {}
    if otg.get('found') and not otg.get('catalog_links'):
        out.append(Finding(
            'Предупреждение', 'Индексация',
            'в разделе «Отгрузки» нет ссылок на каталог',
            url=_idx_url(s, otg.get('found', '')),
            fix_note='Добавить перелинковку из отгрузок на категории - раздел '
                     'живой и может передавать вес каталогу.'))

    nd = s.get('news_dates') or {}
    статья = nd.get('article')
    if статья:
        if not nd.get('published'):
            out.append(Finding(
                'Предупреждение', 'Индексация',
                'у статьи нет даты публикации (datePublished / <time datetime>)',
                url=_idx_url(s, статья),
                fix_note='Проставить дату публикации - поиск показывает её в '
                         'сниппете и учитывает свежесть материала.'))
        elif not nd.get('modified'):
            out.append(Finding(
                'Предупреждение', 'Индексация',
                'у статьи есть дата публикации, но нет даты обновления '
                '(dateModified)',
                url=_idx_url(s, статья),
                fix_note='Проставлять dateModified при правках - поиск видит, '
                         'что материал поддерживают.'))
    return out


def interlinking_note(results) -> Optional[dict]:
    """Перелинковка (внутренний вес) - вывод по САЙТУ, а не дефект страницы:
    отдаём отдельным блоком под таблицей «Проблемы» и ТОЛЬКО когда есть
    проблема (ссылок на тех/инфо не меньше, чем на каталог). Считаем по тем же
    данным, что и удалённая секция листа «Индексация»: r.indexing['int_links'].
    Возвращает {'text': …, 'detail': …} или None."""
    страницы = [r for r in (results or [])
                if (getattr(r, 'indexing', None) or {}).get('int_links')]
    if not страницы:
        return None
    tot = {'home': 0, 'catalog': 0, 'tech': 0, 'other': 0}
    цели: dict = {}
    for r in страницы:
        for k, v in r.indexing['int_links'].items():
            tot[k] = tot.get(k, 0) + v
        for p, n in (r.indexing.get('tech_targets') or {}).items():
            цели[p] = цели.get(p, 0) + n
    всего = sum(tot.values())
    if not всего or tot.get('tech', 0) < tot.get('catalog', 0):
        return None
    доля = {k: tot.get(k, 0) * 100 // всего
            for k in ('catalog', 'home', 'tech', 'other')}
    топ = sorted(цели.items(), key=lambda kv: -kv[1])[:5]
    return {
        'text': 'Внутренний вес льётся на тех/инфо-страницы: ссылок на них не '
                'меньше, чем на каталог. Обычно виноват распухший футер - '
                'каталог и категории должны получать больше всего ссылок.',
        'detail': (f'Ссылок в выборке прогона ({len(страницы)} страниц): '
                   f'{всего}. На каталог/категории {доля["catalog"]}% · на '
                   f'главную {доля["home"]}% · на тех/инфо {доля["tech"]}% · '
                   f'прочее {доля["other"]}%.'
                   + (' Топ тех/инфо-получателей: '
                      + ' · '.join(f'{p} ({n})' for p, n in топ) if топ else '')),
    }


def static_delivery_findings(w3c_check: Optional[dict]) -> list:
    """Сжатие и кеш статики (п.1.17) и общее время загрузки ресурсов (п.1.16)
    - раньше только на листе «Валидация и скорость». По строке на страницу
    выборки: файлы у страниц разные, и видно, где именно проблема."""
    if not w3c_check or not w3c_check.get('available'):
        return []
    show = w3c_check.get('show') or {'valid': True, 'static': True}
    out = []
    for p in w3c_check.get('pages') or []:
        if p.get('error'):
            continue
        url = p.get('url', '')
        t = p.get('timings') or {}
        if not t:
            continue

        if show.get('static', True):
            cp = t.get('compression') or {}
            if cp.get('checked'):
                ok, n = cp.get('ok', 0), cp['checked']
                файлы = '; '.join(u.rsplit('/', 1)[-1]
                                  for u in (cp.get('missing') or [])[:6])
                if ok < n:
                    out.append(Finding(
                        'Предупреждение', 'Валидация и скорость',
                        ('сжатие CSS/JS не включено (Gzip/Brotli)' if not ok
                         else 'сжатие CSS/JS включено не для всех файлов'),
                        url=url, detail=f'сжато {ok} из {n}'
                                        + (f'; без сжатия: {файлы}' if файлы else ''),
                        fix_note='Включить Gzip/Brotli для статики на сервере - '
                                 'это самый дешёвый способ ускорить загрузку.'))
            ca = t.get('caching') or {}
            if ca.get('checked'):
                ok, n = ca.get('ok', 0), ca['checked']
                файлы = '; '.join(u.rsplit('/', 1)[-1]
                                  for u in (ca.get('missing') or [])[:6])
                if ok < n:
                    out.append(Finding(
                        'Предупреждение', 'Валидация и скорость',
                        ('кеш статики не настроен (Cache-Control/ETag/Expires)'
                         if not ok else 'кеш статики настроен не для всех файлов'),
                        url=url, detail=f'с кешем {ok} из {n}'
                                        + (f'; без кеша: {файлы}' if файлы else ''),
                        fix_note='Задать заголовки кеша для CSS/JS - повторные '
                                 'заходы не будут качать статику заново.'))

        if show.get('valid', True) and (t.get('total_ms') or 0) > 8000:
            bt = t.get('by_type') or {}
            части = [f'HTML {t.get("html_ms", 0)}мс']
            for k, ru in (('css', 'CSS'), ('js', 'JS'), ('font', 'шрифты'),
                          ('img', 'картинки')):
                d = bt.get(k) or {}
                if d.get('count'):
                    части.append(f'{ru} {d["ms"]}мс/{d["count"]}шт/{d["kb"]}КБ')
            sl = t.get('slowest') or {}
            самый = (f' Самый долгий: {sl["ms"]}мс - '
                     f'{sl["url"].rsplit("/", 1)[-1]} ({sl.get("kind", "")}).'
                     if sl.get('url') else '')
            out.append(Finding(
                'Предупреждение', 'Валидация и скорость',
                f'ресурсы страницы грузятся дольше 8 секунд '
                f'({t.get("total_ms", 0) // 1000} с)',
                url=url, detail=' · '.join(части),
                fix_note='Сжать и облегчить самые тяжёлые ресурсы, убрать '
                         'лишние запросы.' + самый))
    return out


def ux_interactive_findings(console_check: Optional[dict]) -> list:
    """Интерактив, проверенный браузером: слайдер, выпадающее меню,
    cookie-баннер, закрытие модальной формы. Раньше - секция «Интерактив» на
    листе «Ошибки JavaScript»; по сути это вёрстка/UX, поэтому раздел
    «Вёрстка». ✅-строки не переносим - в «Проблемах» только находки."""
    pages = (console_check or {}).get('pages') or []
    out = []

    for p in pages:
        ux = p.get('ux') or {}
        url = p.get('url', '')
        if ux.get('slider') == 'fail':
            out.append(Finding(
                'Предупреждение', 'Вёрстка',
                'слайдер не отреагировал на стрелку «вперёд»', url=url,
                fix_note='Проверить вручную: если стрелка не листает, '
                         'посетитель не увидит остальные слайды.'))
        if ux.get('dropdown') == 'fail':
            out.append(Finding(
                'Предупреждение', 'Вёрстка',
                'выпадающее меню не открылось по наведению', url=url,
                fix_note='Проверить вручную: разделы каталога недоступны из '
                         'шапки, страдает и навигация, и перелинковка.'))
        ck = ux.get('cookie') or {}
        if ck.get('status') == 'short':
            out.append(Finding(
                'Предупреждение', 'Вёрстка',
                f'cookie-баннер запоминает выбор лишь на {ck.get("days")} дн. '
                f'(нужно от 7)', url=url,
                fix_note='Продлить срок хранения согласия минимум до недели.'))
        elif ck.get('status') == 'not_remembered':
            out.append(Finding(
                'Предупреждение', 'Вёрстка',
                'cookie-баннер не запоминает выбор - появляется снова после '
                'перезагрузки', url=url,
                fix_note='Сохранять согласие в cookie/localStorage - иначе '
                         'баннер раздражает на каждой странице.'))

        mob = p.get('mobile') or {}
        for key, dev in (('form_close', 'ПК'), ('form_close_m', 'моб.')):
            v = mob.get(key)
            статус, имя = ((v.get('status'), v.get('name') or 'модальная форма')
                           if isinstance(v, dict) else (v, 'модальная форма'))
            if статус == 'not_closed':
                out.append(Finding(
                    'Предупреждение', 'Вёрстка',
                    f'модальная форма «{имя}» не закрывается по клику вне неё '
                    f'({dev})', url=url,
                    fix_note='Закрывать окно по клику на затемнение - иначе '
                             'посетитель застревает в форме.'))
    return out


def _idx_url(indexing_summary, path):
    """Путь -> полный URL хоста прогона (для гиперссылки в «Проблемах»)."""
    host = (indexing_summary or {}).get('host', '')
    if not path:
        return f'https://{host}/' if host else ''
    if path.startswith('http'):
        return path
    return f'https://{host}{path}' if host else path


def indexing_site_findings(indexing_summary: Optional[dict]) -> list:
    """Сайт-уровневые находки индексации (пути/файлы sitemap и robots.txt,
    не привязаны к одной странице прогона) - раньше только на листе
    «Индексация» + агрегатом в «Плане работ» (extra_site_tasks), в
    «Проблемы» не попадали вообще. Здесь - по одной находке на каждый
    путь/файл, чтобы список был виден и без открытия детального листа."""
    s = indexing_summary or {}
    out = []

    for j in s.get('junk_open') or []:
        out.append(Finding('Предупреждение', 'Индексация',
                           f'служебный адрес не закрыт в robots.txt: {j.get("label", "")}',
                           url=_idx_url(s, j.get('path', ''))))

    for d in s.get('disallowed') or []:
        out.append(Finding('Ошибка', 'Индексация',
                           'путь есть в sitemap/каталоге, но закрыт в robots.txt Disallow',
                           url=_idx_url(s, d.get('path', '')),
                           detail=f'правило: {d.get("rule", "")}'))

    bd = s.get('blanket_disallow') or []
    if bd:
        out.append(Finding('Ошибка', 'Индексация',
                           'Disallow: / закрывает сайт целиком от индексации',
                           url=_idx_url(s, '/'),
                           detail='User-agent: ' + ', '.join(bd)))

    for a in s.get('assets_closed') or []:
        out.append(Finding('Ошибка', 'Индексация',
                           'свой CSS/JS закрыт в robots.txt - Google не отрендерит страницу',
                           url=a.get('url', ''), detail=f'правило: {a.get("rule", "")}'))

    for f in (s.get('directive_check') or {}).get('findings') or []:
        out.append(Finding('Предупреждение', 'Индексация',
                           'страница держится только на robots.txt (отвечает 200 без noindex)',
                           url=_idx_url(s, f.get('path', '')),
                           detail=f'правило: {f.get("rule", "")} · код {f.get("status", "")}'))

    for a in s.get('advisory_open') or []:
        out.append(Finding('Предупреждение', 'Индексация',
                           f'спорный для индекса раздел открыт: {a.get("label", "")}',
                           url=_idx_url(s, a.get('path', ''))))

    pg = s.get('pagination') or {}
    if pg.get('status') == 200 and pg.get('canon_ok') is False:
        out.append(Finding('Предупреждение', 'Индексация',
                           'на пагинации категории нет canonical на страницу без номера',
                           url=_idx_url(s, pg.get('base', '')),
                           detail=f'canonical: {pg.get("canonical") or "нет"}'))
    if pg.get('loadmore') and pg.get('pag_links') is False:
        out.append(Finding('Предупреждение', 'Индексация',
                           'бесконечная прокрутка без ссылок пагинации в HTML',
                           url=_idx_url(s, pg.get('base', ''))))

    aud = ((s.get('sitemap_audit') or {}))
    mc = aud.get('missing_catalog') or {}
    for kind_label, key in (('категория', 'categories'), ('фильтр', 'filters'),
                            ('услуга', 'services')):
        for path in mc.get(key) or []:
            out.append(Finding('Ошибка', 'Индексация',
                               f'{kind_label} из выгрузки отсутствует в sitemap',
                               url=_idx_url(s, path)))

    for b in aud.get('bad_urls') or []:
        out.append(Finding('Предупреждение', 'Индексация',
                           f'битый/некорректный URL в sitemap: {b.get("why", "")}',
                           url=b.get('url', '')))

    for fs in aud.get('file_stats') or []:
        urls_n, bytes_n = fs.get('urls', 0), fs.get('bytes', 0)
        if urls_n > 50000 or bytes_n > 50 * 1024 * 1024:
            out.append(Finding('Ошибка', 'Индексация',
                               'файл sitemap нарушает лимит протокола (50 000 ссылок / 50 МБ)',
                               url=fs.get('url', ''),
                               detail=f'{urls_n} URL, {bytes_n // 1048576} МБ'))
        elif urls_n > 10000 or bytes_n > 10 * 1024 * 1024:
            out.append(Finding('Предупреждение', 'Индексация',
                               'файл sitemap больше рекомендуемого лимита (10 000 ссылок / 10 МБ)',
                               url=fs.get('url', ''),
                               detail=f'{urls_n} URL, {bytes_n // 1048576} МБ'))

    _tot = aud.get('total') or 0
    if _tot:
        _missing_meta = [label for label, key in (
            ('lastmod', 'with_lastmod'), ('changefreq', 'with_changefreq'),
            ('priority', 'with_priority')) if aud.get(key, 0) == 0]
        if _missing_meta:
            out.append(Finding('Предупреждение', 'Индексация',
                               f'в sitemap ни у одной записи нет {", ".join(_missing_meta)}',
                               url=_idx_url(s, '/sitemap.xml')))

    for j in (s.get('html_sitemap') or {}).get('junk_links') or []:
        out.append(Finding('Предупреждение', 'Индексация',
                           f'служебная ссылка в HTML-карте сайта: {j.get("label", "")}',
                           url=j.get('url', '')))

    for r in s.get('required_pages') or []:
        if r.get('found'):
            continue
        out.append(Finding('Ошибка', 'Индексация',
                           f'обязательная страница не найдена: {r.get("label", "")}',
                           url=_idx_url(s, '/')))

    smc = s.get('sitemap_checks') or None
    if smc is not None:
        if not smc.get('has_directive'):
            out.append(Finding('Ошибка', 'Индексация',
                               'в robots.txt нет директивы Sitemap - роботы не видят карту сайта',
                               url=_idx_url(s, '/robots.txt')))
        else:
            for d in smc.get('directives') or []:
                if d.get('status') != 200:
                    out.append(Finding(
                        'Ошибка', 'Индексация',
                        'sitemap из robots.txt не открывается',
                        url=d.get('url', ''),
                        detail=f'код: {d.get("status") if d.get("status") is not None else "нет ответа"}'))
            if smc.get('matches_project') is False:
                out.append(Finding(
                    'Предупреждение', 'Индексация',
                    'ни одна директива Sitemap в robots.txt не совпадает с sitemap проекта из настроек',
                    url=_idx_url(s, '/robots.txt')))

    wm = s.get('wm_sitemaps')
    if wm is not None:
        wm_list = wm.get('sitemaps') or []
        if wm.get('error'):
            pass  # сбой получения данных Вебмастера - не находка сайта
        elif not wm_list:
            out.append(Finding('Ошибка', 'Индексация',
                               'в Яндекс.Вебмастере не добавлено ни одного sitemap-файла',
                               url=_idx_url(s, '/')))
        else:
            for sm in wm_list:
                if sm.get('errors'):
                    out.append(Finding(
                        'Ошибка', 'Индексация',
                        f'sitemap в Яндекс.Вебмастере с ошибками: {sm.get("errors")}',
                        url=sm.get('url', '')))

    return out


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

    # Остальные уровни серьёзности (fatal - уже отдельной задачей выше;
    # info - справочная информация, не проблема, не заводим задачу).
    _SVC_SEV_META = {
        'critical': (1, 'критические', 'Критическая проблема (безопасность/'
                    'битые ссылки и т.п.) - серьёзно мешает сайту и роботу.'),
        'possible': (2, 'возможные', 'Возможная проблема - стоит проверить, '
                    'даже если сервис не уверен на 100%.'),
        'recommendation': (3, 'рекомендации', 'Рекомендация сервиса - не '
                           'срочно, но полезно сделать.'),
    }
    _svc_by_sev: dict = {}
    for i in (service_issues or []):
        sev = getattr(i, 'severity', None)
        if sev not in _SVC_SEV_META:
            continue
        _svc_by_sev.setdefault(sev, set()).add(getattr(i, 'host', ''))
    for sev, hosts in _svc_by_sev.items():
        priority, label, why = _SVC_SEV_META[sev]
        tasks.append(Task(
            priority=priority, task_group=f'wm_service_{sev}',
            title=f'Разобрать проблемы в сервисах ({label})',
            what=', '.join(sorted(hosts)),
            volume=len(hosts), owner='SEO + разработка', why=why,
            where='Лист «Ошибки сервисов»'))

    # filter_sanctions() (webmaster_api.py) кладёт в ps_filters['yandex'] ЛЮБУЮ
    # фатальную проблему Вебмастера, не только настоящую санкцию (код-маркер
    # угрозы/качества/спама) - "сайт не открывается" тоже FATAL, но это не
    # санкция ПС, а доступность (та же проблема уже отдельной задачей
    # 'wm_fatal' выше). Без фильтра тут - вводящее в заблуждение дублирование.
    sanc = (ps_filters or {}).get('yandex') or []
    real_sanc = [s for s in sanc
                if _SANCTION_CODE_RE.search(str(s.get('code') or ''))]
    gsc_hits = (ps_filters or {}).get('gsc_hits') or []
    if real_sanc or gsc_hits:
        tasks.append(Task(
            priority=1, task_group='ps_sanctions',
            title='Разобрать санкции поисковых систем',
            what=(f'Яндекс: {len(real_sanc)} хост(ов) с санкцией/угрозой'
                 if real_sanc else '') + (', ' if real_sanc and gsc_hits else '')
                + (f'Google: {len(gsc_hits)} писем с маркерами ручных мер'
                  if gsc_hits else ''),
            volume=len(real_sanc) + len(gsc_hits), owner='SEO',
            why='Санкция резко режет видимость сайта в поиске.',
            where='Лист «Фильтры ПС»'))

    return tasks
