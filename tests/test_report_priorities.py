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
