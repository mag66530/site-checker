"""Тесты report_priorities.py - сборка находок (лист «Проблемы») и
приоритезация в план работ (лист «План работ»). Чистые функции, без сети/
openpyxl - синтетические CheckResult и summary-словари."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from http_checker import CheckResult
from content_checker import BlockResult, ContentResult
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
    r = _result(meta_unique={'issues': ['H1 содержит вложенные теги'], 'warnings': []})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'Заголовки и мета'
    assert out[0].level == 'Ошибка'


def test_cis_findings_снг_домены():
    r = _result(cis={'issues': ['упоминание «Россия» на СНГ-домене'], 'warnings': []})
    out = collect_findings([r])
    assert len(out) == 1
    assert out[0].section == 'СНГ-домены'


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


def test_extra_site_tasks_fatal_service_issues():
    class _Issue:
        def __init__(self, host, severity):
            self.host = host
            self.severity = severity
    tasks = extra_site_tasks(service_issues=[
        _Issue('aktau.example.kz', 'fatal'),
        _Issue('example.ru', 'critical'),
    ])
    assert len(tasks) == 1
    assert tasks[0].task_group == 'wm_fatal'
    assert tasks[0].volume == 1


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


def test_extra_site_tasks_html_sitemap_junk():
    tasks = extra_site_tasks(indexing_summary={'html_sitemap': {
        'junk_links': [{'url': 'https://a.ru/basket/', 'label': 'Корзина'}]}})
    assert len(tasks) == 1
    assert tasks[0].task_group == 'html_sitemap_junk'
    assert tasks[0].priority == 3
