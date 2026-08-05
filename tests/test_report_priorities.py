"""Тесты report_priorities.py - сборка находок (лист «Проблемы») и
приоритезация в план работ (лист «План работ»). Чистые функции, без сети/
openpyxl - синтетические CheckResult и summary-словари."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from http_checker import CheckResult
from content_checker import BlockResult, ContentResult
from text_checker import TextIssue
from report_priorities import (
    Finding, collect_findings, classify, group_into_tasks, extra_site_tasks,
)


def _result(**kw):
    base = dict(url='https://example.ru/', city='Москва', subdomain='example.ru',
               type_code='category', type_label='Категория', is_ok=True,
               http_code=200, status='OK')
    base.update(kw)
    return CheckResult(**base)


# ── collect_findings ────────────────────────────────────────────────────

def test_недоступная_страница_даёт_находку_доступности():
    r = _result(is_ok=False, http_code=404, status='NOT_FOUND',
               error_message='страница не найдена')
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].level == 'Ошибка'
    assert out[0].section == 'Доступность страниц'
    assert '404' in out[0].problem


def test_рабочая_страница_без_доп_проверок_ничего_не_даёт():
    r = _result()
    assert collect_findings([r]) == []


def test_битые_тексты_дают_находку_на_каждую_переменную():
    """Раньше text_issues были только на отдельном листе «Битые тексты»,
    в collect_findings не итемизировались вообще."""
    issues = [
        TextIssue(pattern='{{...}}', match='{{city}}',
                 context='Купить трубу в {{city}} с доставкой'),
        TextIssue(pattern='%переменная%', match='%price%',
                 context='Цена от %price% рублей'),
    ]
    r = _result(text_issues=issues, has_text_issues=True)
    out = collect_findings([r])
    assert len(out) == 2
    assert all(f.section == 'Битые тексты' and f.level == 'Ошибка' for f in out)
    assert '{{city}}' in out[0].problem
    assert '{{...}}' in out[0].detail
    assert 'Купить трубу' in out[0].detail
    assert '%price%' in out[1].problem


def test_indexing_issues_and_warnings_разносятся_по_уровню():
    r = _result(indexing={'issues': ['canonical ведёт на закрытый URL'],
                          'warnings': ['нет rel="canonical" на странице']})
    out = collect_findings([r])
    levels = {f.problem: f.level for f in out}
    assert levels['canonical ведёт на закрытый URL'] == 'Ошибка'
    assert levels['нет rel="canonical" на странице'] == 'Предупреждение'
    assert all(f.section == 'Индексация' for f in out)


def test_region_issue_читается_из_структуры_тип_зона_пояснение():
    r = _result(region={'город': 'Москва', 'issues': [
        {'тип': 'город', 'зона': 'title', 'найдено': 'Норильск',
         'пояснение': 'город «Норильск» на странице «Москва»'},
    ]})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'Регион и город'
    assert 'другой город' in out[0].problem
    assert 'Норильск' in out[0].detail


def test_geo_findings_технический_регион_даёт_предупреждение():
    """region['geo']['warnings'] (гео-теги в коде главной поддомена) -
    раньше показывались только на листе, в «Проблемы» не попадали."""
    r = _result(region={'город': 'Пенза', 'issues': [], 'geo': {
        'signals': [], 'localities': [], 'locality_match': None,
        'warnings': ['технический регион не задан в коде (нет meta geo.* '
                    'и addressLocality в Schema.org) - регион в '
                    'Яндекс.Вебмастере проверить вручную']}})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].level == 'Предупреждение'
    assert out[0].section == 'Регион и город'
    assert 'технический регион не задан' in out[0].problem


def test_geo_findings_совпадение_ничего_не_даёт():
    r = _result(region={'город': 'Пенза', 'issues': [], 'geo': {
        'signals': ['geo.region=RU-PNZ'], 'localities': ['Пенза'],
        'locality_match': True, 'warnings': []}})
    assert collect_findings([r]) == []


def test_content_bugs_дают_находку_на_блок():
    content = ContentResult(type_code='product', blocks=[
        BlockResult(key='rec_links', label='Блок «похожие товары»',
                   required=True, present=False),
        BlockResult(key='price', label='Цена', required=True, present=True),
    ])
    r = _result(content=content)
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'Блоки на странице'
    assert 'похожие товары' in out[0].problem


def test_soft_404_это_одна_находка_а_не_список_блоков():
    content = ContentResult(type_code='category', is_soft_404=True, blocks=[
        BlockResult(key='h1', label='H1', required=True, present=False),
        BlockResult(key='price', label='Цена', required=True, present=False),
    ])
    r = _result(content=content)
    out = collect_findings([r])
    assert len(out) == 1
    assert 'soft-404' in out[0].problem


def test_meta_unique_findings_заголовки_и_мета():
    """issues - список словарей {тип, найдено, пояснение} (та же форма, что
    у region), а не строк - раньше шли через generic-обработчик и в
    «Проблемах» получался бы str(dict) вместо текста."""
    r = _result(meta_unique={'issues': [
        {'тип': 'h1', 'найдено': 'теги внутри',
         'пояснение': 'внутри H1 вложенные теги/стили - H1 должен быть '
                      'чистым текстом: Купить трубу <b>стальную</b>'},
    ]})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'Заголовки и мета'
    assert out[0].level == 'Ошибка'
    assert out[0].problem == ('внутри H1 вложенные теги/стили - H1 должен '
                              'быть чистым текстом: Купить трубу <b>стальную</b>')
    assert out[0].detail == 'H1: теги внутри'


def test_markup_findings_сохраняет_field_details():
    """Разметка (OG/Schema.org): field_details («Offer/цена: 21 из 60») -
    раньше отдельная колонка на удалённом листе «Разметка», при переносе в
    generic-обработчик терялась бы; _markup_findings кладёт её в detail."""
    r = _result(markup={
        'issues': ['в разметке Product: нет поля «предложение/цена»'],
        'warnings': ['в разметке Organization: нет поля «логотип»'],
        'field_details': ['Offer/цена: 21 из 60', 'Organization/логотип: 11 из 11'],
    })
    out = collect_findings([r])
    assert len(out) == 2
    for f in out:
        assert f.section == 'Разметка'
        assert 'Offer/цена: 21 из 60' in f.detail
        assert 'Organization/логотип: 11 из 11' in f.detail
    assert {f.level for f in out} == {'Ошибка', 'Предупреждение'}


def test_cis_findings_снг_домены():
    """issues - список словарей {тип, зона, найдено, контекст, пояснение}
    (та же форма, что у region/meta_unique), а не строк - раньше шли через
    generic-обработчик и в «Проблемах» получался бы str(dict) вместо текста."""
    r = _result(cis={'страна': 'Казахстан', 'issues': [
        {'тип': 'страна', 'зона': 'title', 'найдено': 'России',
         'контекст': 'доставка по России и странам СНГ',
         'пояснение': 'упоминание «Россия» на сайте страны «Казахстан»'},
    ]})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'СНГ-домены'
    assert out[0].problem == 'упоминание «Россия» на сайте страны «Казахстан»'
    assert 'title' in out[0].detail and 'России' in out[0].detail


def test_kp_result_bug_даёт_находку_ok_не_даёт():
    r = _result(kp_result={'issues': [
        {'field': 'Телефон', 'status': 'bug', 'comment': 'номер не из КП'},
        {'field': 'Email', 'status': 'ok', 'comment': 'совпало'},
    ]})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'Контакты по городам'
    assert 'Телефон' in out[0].problem
    assert out[0].level == 'Ошибка'


def test_page_phone_critical_даёт_ошибку():
    r = _result(page_phone={'status': 'critical', 'comment': 'в КП нет номера'})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].level == 'Ошибка'


def test_page_phone_ok_ничего_не_даёт():
    r = _result(page_phone={'status': 'ok', 'comment': ''})
    assert collect_findings([r]) == []


def test_contacts_addr_mismatched_даёт_находку_на_город():
    r = _result(contacts_addr={'on_page': 2, 'matched': 1,
                               'mismatched': [{'city': 'Норильск',
                                              'site': 'ул. Ленина, 1',
                                              'kp': 'ул. Ленина, 2'}],
                               'not_in_kp': []})
    out = collect_findings([r])
    assert len(out) == 1
    assert 'Норильск' in out[0].problem
    assert 'Ленина, 1' in out[0].detail and 'Ленина, 2' in out[0].detail


def test_images_no_alt_даёт_находку_на_каждую_картинку():
    r = _result(images={'no_alt': ['/img/a.jpg', '/img/b.jpg (alt пустой)']})
    out = collect_findings([r])
    assert len(out) == 2
    assert {f.detail for f in out} == {'/img/a.jpg', '/img/b.jpg (alt пустой)'}
    assert all(f.level == 'Ошибка' and f.section == 'Изображения' for f in out)
    assert all('без alt' in f.problem for f in out)


def test_images_broken_даёт_находку_с_url_картинки_в_detail():
    r = _result(images={'broken_imgs': [{'url': 'https://a.ru/img/x.jpg'}]})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].detail == 'https://a.ru/img/x.jpg'
    assert 'битые картинки' in out[0].problem


def test_images_broken_шаблонный_src_объясняется_а_не_дублируется_голым():
    """src вида ${ product.thumb } - не 404 конкретного файла, а
    неотрендеренный JS-шаблон (переменная не подставилась). detail должен
    объяснять это, а не просто повторять бессмысленный текст."""
    r = _result(images={'broken_imgs': [{'url': '${ product.thumb }'}]})
    out = collect_findings([r])
    assert len(out) == 1
    assert '${ product.thumb }' in out[0].detail
    assert 'неотрендеренный' in out[0].detail
    assert 'шаблон' in out[0].detail


def test_images_warnings_попадают_без_детализации():
    r = _result(images={'warnings': ['современные форматы (webp/avif) не используются']})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].level == 'Предупреждение'
    assert out[0].detail == ''


def test_images_cat_и_prod_warnings_не_теряются():
    r = _result(images={
        'cat_warnings': ['картинка категории не уникальна - та же картинка '
                        'на других категориях (каждому разделу нужна своя)'],
        'cat_dup': {'name': 'category.jpg', 'n': 2},
        'prod_warnings': ['фото товара дублируется в разных категориях - '
                          'та же картинка у товаров из других разделов '
                          '(в каждой категории свои фото товаров)'],
        'prod_dup': {'name': 'product.jpg', 'n': 2, 'cats': 2},
    })
    out = collect_findings([r])
    assert len(out) == 2
    by_detail = {f.detail: f for f in out}
    assert by_detail['category.jpg'].problem.startswith('картинка категории не уникальна')
    assert by_detail['product.jpg'].problem.startswith('фото товара дублируется')
    assert all(f.level == 'Предупреждение' for f in out)


def test_layout_css_broken_даёт_находку_с_url_и_кодом():
    r = _result(layout={'css_broken': [{'url': 'https://a.ru/s.css', 'status': 404}]})
    out = collect_findings([r])
    assert len(out) == 1
    assert 'не грузится часть CSS-стилей' in out[0].problem
    assert out[0].detail == 'https://a.ru/s.css (код 404)'


def test_layout_mixed_content_даёт_находку_на_каждый_ресурс():
    r = _result(layout={'mixed_content': ['http://a.ru/x.js', 'http://a.ru/y.css']})
    out = collect_findings([r])
    assert len(out) == 2
    assert {f.detail for f in out} == {'http://a.ru/x.js', 'http://a.ru/y.css'}
    assert all('mixed content' in f.problem for f in out)


def test_layout_menu_broken_даёт_находку_с_url_и_кодом():
    r = _result(layout={'menu': {'checked': 5,
                                 'broken': [{'url': 'https://a.ru/oplata/', 'code': 404}]}})
    out = collect_findings([r])
    assert len(out) == 1
    assert 'битые ссылки в меню шапки' in out[0].problem
    assert out[0].detail == 'https://a.ru/oplata/ (код 404)'


def test_layout_favicon_404_даёт_находку_с_url():
    r = _result(layout={'favicon': {'url': 'https://a.ru/favicon.ico', 'status': 404}})
    out = collect_findings([r])
    assert len(out) == 1
    assert 'favicon не грузится' in out[0].problem
    assert out[0].detail == 'https://a.ru/favicon.ico'


def test_layout_viewport_остаётся_общей_находкой_без_детали():
    r = _result(layout={'issues': ['нет тега viewport - мобильная версия не масштабируется']})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].detail == ''


def test_console_check_ошибки_js_и_адаптивность():
    console = {'pages': [
        {'url': 'https://example.ru/', 'errors': ['TypeError: x is undefined'],
         'mobile': {'viewports': {'390': {'overflow': 40, 'overlaps': []}}}},
        {'url': 'https://example.ru/2/', 'errors': [], 'mobile': None},
    ]}
    out = collect_findings([], console_check=console)
    sections = [f.section for f in out]
    assert 'Ошибки JavaScript' in sections
    assert 'Вёрстка' in sections


def test_index_404_check_даёт_находку_на_каждый_dead_url():
    idx = {'hosts': [{'host': 'example.ru', 'dead': [
        {'url': 'https://example.ru/old/', 'status': 404, 'source': 'Яндекс'},
    ]}]}
    out = collect_findings([], index_404_check=idx)
    assert len(out) == 1
    assert out[0].section == '404 в индексе'
    assert out[0].url == 'https://example.ru/old/'


def test_index_404_check_errors_тоже_дают_находки_не_только_dead():
    """'errors' (5xx/прочие ошибки у страниц из поиска) раньше не читался
    вообще - только 'dead' (явные 404/410)."""
    idx = {'hosts': [{'host': 'example.ru', 'errors': [
        {'url': 'https://example.ru/heavy/', 'status': 503, 'source': 'GSC'},
        {'url': 'https://example.ru/weird/', 'status': 0, 'source': 'Яндекс'},
    ]}]}
    out = collect_findings([], index_404_check=idx)
    assert len(out) == 2
    assert all(f.section == '404 в индексе' for f in out)
    assert all('сервер не ответил' in f.problem for f in out)


def test_calltracking_config_bug_даёт_находку():
    r = _result(type_code='main', kp_result={'ad_check': {
        'status': 'bug', 'comment': 'в конфиге сайта: 79991112233; в КП: 79994445566'}})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'Замена рекл. номера'
    assert out[0].level == 'Ошибка'
    assert 'не совпадает с КП' in out[0].problem


def test_calltracking_config_ok_ничего_не_даёт():
    r = _result(type_code='main', kp_result={'ad_check': {'status': 'ok'}})
    assert collect_findings([r]) == []


def test_calltracking_browser_not_replaced_ошибка_no_element_предупреждение():
    r = _result(type_code='main', subdomain='example.ru')
    ct = {'results': [{'url': 'https://example.ru/', 'status': 'not_replaced',
                       'seo': {'status': 'no_element'}}]}
    out = collect_findings([r], calltracking_check=ct)
    assert len(out) == 2
    by_level = {f.level for f in out}
    assert by_level == {'Ошибка', 'Предупреждение'}
    assert any('реклама' in f.problem for f in out)
    assert any('поиск' in f.problem for f in out)


def test_calltracking_только_на_главных_страницах():
    """Браузерная проверка совпадает по хосту - не должна размножаться на
    КАЖДУЮ страницу того же поддомена, только на главную."""
    r = _result(type_code='category', subdomain='example.ru')
    ct = {'results': [{'url': 'https://example.ru/', 'status': 'not_replaced'}]}
    assert collect_findings([r], calltracking_check=ct) == []


def test_search_check_не_находит_категорию_даёт_предупреждение():
    sc = {'available': True, 'query': 'арматура', 'found_category': False,
         'search_url': 'https://a.ru/search/?q=арматура'}
    out = collect_findings([], search_check=sc)
    assert len(out) == 1
    assert out[0].section == 'Вёрстка'
    assert out[0].level == 'Предупреждение'
    assert 'не находит категории' in out[0].problem


def test_search_check_находит_ничего_не_даёт():
    sc = {'available': True, 'query': 'арматура', 'found_category': True,
         'search_url': 'https://a.ru/search/?q=арматура'}
    assert collect_findings([], search_check=sc) == []


def test_filters_test_плохой_вердикт_даёт_ошибку_а_нейтральный_предупреждение():
    ft = {'available': True, 'cases': [
        {'name': 'По диаметру', 'verdict': 'empty', 'category': 'https://a.ru/cat/',
         'detail': 'после фильтра 0 товаров'},
        {'name': 'По цвету', 'verdict': 'filter_absent', 'category': 'https://a.ru/cat2/'},
        {'name': 'По марке', 'verdict': 'ok', 'category': 'https://a.ru/cat3/'},
    ]}
    out = collect_findings([], filters_test=ft)
    assert len(out) == 2
    by_level = {f.problem: f.level for f in out}
    assert any('фильтр «По диаметру»' in p and lvl == 'Ошибка'
              for p, lvl in by_level.items())
    assert any('фильтр «По цвету»' in p and lvl == 'Предупреждение'
              for p, lvl in by_level.items())


# ── classify / group_into_tasks ─────────────────────────────────────────

def test_classify_известного_типа_находки():
    f = Finding('Ошибка', 'Изображения', 'битые картинки (404) - изображение не отображается')
    meta = classify(f)
    assert meta['priority'] == 1
    assert meta['task_group'] == 'img_broken'


def test_classify_неизвестной_находки_безопасный_дефолт():
    f = Finding('Предупреждение', 'Совсем новый раздел', 'что-то невиданное')
    meta = classify(f)
    assert meta['priority'] == 3
    assert meta['task_group'] == 'other::Совсем новый раздел'
    assert meta['title']  # не пустой - находка не потерялась


def test_group_into_tasks_считает_объём_по_уникальным_url():
    findings = [
        Finding('Ошибка', 'Изображения', 'есть картинки без alt или с пустым alt=""',
               url='https://a.ru/1/'),
        Finding('Ошибка', 'Изображения', 'есть картинки без alt или с пустым alt=""',
               url='https://a.ru/2/'),
        Finding('Ошибка', 'Изображения', 'есть картинки без alt или с пустым alt=""',
               url='https://a.ru/2/'),  # дубль url - не считается дважды
    ]
    tasks = group_into_tasks(findings)
    assert len(tasks) == 1
    assert tasks[0].volume == 2
    assert tasks[0].owner == 'Контент + разработка'


def test_group_into_tasks_сортирует_по_приоритету_потом_объёму():
    findings = [
        Finding('Предупреждение', 'Безопасность', 'нет HSTS', url='https://a.ru/1/'),
        Finding('Ошибка', 'Доступность страниц', 'страница отвечает 404 (NOT_FOUND) - не открывается',
               url='https://a.ru/2/'),
    ]
    tasks = group_into_tasks(findings)
    assert tasks[0].priority == 1  # доступность критична, идёт первой
    assert tasks[1].priority == 3


# ── extra_site_tasks ─────────────────────────────────────────────────────

def test_extra_site_tasks_junk_open():
    tasks = extra_site_tasks(indexing_summary={'junk_open': [
        {'label': 'сортировка', 'path': '/catalog/?sort=price'},
        {'label': 'пагинация', 'path': '/catalog/?page=2'},
    ]})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'robots_junk'
    assert tasks[0].volume == 2


def test_extra_site_tasks_wm_anomaly_группируется_по_метрике():
    wm = {'hosts': [
        {'host': 'a.ru', 'anomalies': [
            {'metric': 'Обход: ошибки сервера (5xx)', 'severity': 'fatal'}]},
        {'host': 'b.ru', 'anomalies': [
            {'metric': 'Обход: ошибки сервера (5xx)', 'severity': 'fatal'}]},
    ]}
    tasks = extra_site_tasks(wm_metrics=wm)
    assert len(tasks) == 1
    assert tasks[0].volume == 2
    assert tasks[0].priority == 1


class _Issue:
    def __init__(self, host, severity):
        self.host = host
        self.severity = severity


def test_extra_site_tasks_fatal_service_issues():
    """fatal и critical - РАЗНЫЕ задачи (не всё в одну кучу)."""
    tasks = extra_site_tasks(service_issues=[
        _Issue('aktau.example.kz', 'fatal'),
        _Issue('example.ru', 'critical'),
    ])
    assert len(tasks) == 2
    fatal = next(t for t in tasks if t.task_group == 'wm_fatal')
    critical = next(t for t in tasks if t.task_group == 'wm_service_critical')
    assert fatal.volume == 1
    assert critical.volume == 1
    assert critical.priority == 1


def test_extra_site_tasks_service_issues_possible_recommendation():
    tasks = extra_site_tasks(service_issues=[
        _Issue('a.ru', 'possible'), _Issue('a.ru', 'recommendation'),
        _Issue('b.ru', 'info'),   # info - не проблема, задачи не будет
    ])
    assert len(tasks) == 2
    groups = {t.task_group: t for t in tasks}
    assert groups['wm_service_possible'].priority == 2
    assert groups['wm_service_recommendation'].priority == 3
    assert 'wm_service_info' not in groups


def test_extra_site_tasks_пусто_если_ничего_не_передано():
    assert extra_site_tasks() == []


def test_extra_site_tasks_blanket_disallow():
    tasks = extra_site_tasks(indexing_summary={'blanket_disallow': ['*', 'Yandex']})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'robots_blanket_disallow'
    assert tasks[0].priority == 1


def test_extra_site_tasks_assets_closed():
    tasks = extra_site_tasks(indexing_summary={
        'assets_closed': [{'url': 'https://a.ru/style.css', 'rule': '/style.css'}],
        'assets_checked': 5})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'robots_assets_closed'
    assert tasks[0].volume == 1


def test_extra_site_tasks_directive_check_findings():
    tasks = extra_site_tasks(indexing_summary={'directive_check': {'checked': 3, 'findings': [
        {'rule': '/basket/', 'path': '/basket/', 'status': 200},
    ]}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'robots_directive_weak'
    assert tasks[0].priority == 2


def test_extra_site_tasks_advisory_open():
    tasks = extra_site_tasks(indexing_summary={'advisory_open': [
        {'label': 'старая акция', 'path': '/promo/2020/'},
    ]})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'robots_advisory_open'
    assert tasks[0].priority == 3


def test_extra_site_tasks_pagination_canonical_плохой():
    tasks = extra_site_tasks(indexing_summary={'pagination': {
        'base': '/catalog/', 'status': 200, 'canonical': None, 'canon_ok': False,
        'loadmore': None, 'pag_links': None}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'pagination_canonical'


def test_extra_site_tasks_pagination_loadmore_без_ссылок():
    tasks = extra_site_tasks(indexing_summary={'pagination': {
        'base': '/catalog/', 'status': 200, 'canonical': 'https://a.ru/catalog/',
        'canon_ok': True, 'loadmore': True, 'pag_links': False}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'pagination_loadmore_links'


def test_extra_site_tasks_pagination_ok_ничего_не_даёт():
    tasks = extra_site_tasks(indexing_summary={'pagination': {
        'base': '/catalog/', 'status': 200, 'canonical': 'https://a.ru/catalog/',
        'canon_ok': True, 'loadmore': True, 'pag_links': True}})
    assert tasks == []


def test_extra_site_tasks_sitemap_missing_catalog():
    tasks = extra_site_tasks(indexing_summary={'sitemap_audit': {'missing_catalog': {
        'categories': ['/catalog/a/'], 'filters': [], 'services': ['/uslugi/x/']}}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'sitemap_missing_catalog'
    assert tasks[0].volume == 2


def test_extra_site_tasks_sitemap_bad_urls():
    tasks = extra_site_tasks(indexing_summary={'sitemap_audit': {
        'bad_urls': [{'url': 'https://a.ru/x', 'why': 'дубль'}]}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'sitemap_bad_urls'


def test_extra_site_tasks_ps_filters_доступность_не_считается_санкцией():
    """FATAL в диагностике Вебмастера - не всегда санкция ПС: 'сайт не
    открывается' (SITE_ERROR) - доступность, не санкция, и уже отдельная
    задача 'wm_fatal' - без фильтра по коду тут было бы вводящее в
    заблуждение дублирование."""
    tasks = extra_site_tasks(ps_filters={'yandex': [
        {'host': 'a.ru', 'code': 'SITE_ERROR', 'severity': 'fatal'},
        {'host': 'b.ru', 'code': 'DNS_ERROR', 'severity': 'fatal'},
    ]})
    assert tasks == []


def test_extra_site_tasks_ps_filters_реальная_санкция_считается():
    tasks = extra_site_tasks(ps_filters={'yandex': [
        {'host': 'a.ru', 'code': 'SITE_ERROR', 'severity': 'fatal'},
        {'host': 'b.ru', 'code': 'MANUAL_QUALITY_SANCTIONS', 'severity': 'fatal'},
    ]})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'ps_sanctions'
    assert tasks[0].volume == 1   # только реальная санкция, SITE_ERROR не в счёте
    assert 'Яндекс: 1 хост' in tasks[0].what


def test_extra_site_tasks_html_sitemap_junk():
    tasks = extra_site_tasks(indexing_summary={'html_sitemap': {
        'junk_links': [{'url': 'https://a.ru/basket/', 'label': 'Корзина'}]}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'html_sitemap_junk'
    assert tasks[0].priority == 3
