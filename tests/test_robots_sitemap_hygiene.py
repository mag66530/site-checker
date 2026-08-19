# -*- coding: utf-8 -*-
"""Доп. чек-лист: гигиена файла robots.txt + формат/кодировка/адреса sitemap.

Чистые функции - без сети. Прозвон адресов карты проверяется на фейковой
сессии в test_sitemap_probe.py.
"""
import indexing_checker as ic
import sitemap_audit as sa
from report_priorities import extra_site_tasks, indexing_site_findings


# ── robots.txt: вес / формат / кодировка ─────────────────────────────


def test_вес_робots_считается_по_сырым_байтам():
    маленький = ic.analyze_robots_file(b'User-agent: *\nDisallow: /admin/\n')
    assert маленький['size_bytes'] == 32 and маленький['too_big'] is False
    большой = ic.analyze_robots_file(b'#' * (500 * 1024 + 1))
    assert большой['too_big'] is True
    # ровно на пределе - ещё не находка
    assert ic.analyze_robots_file(b'#' * (500 * 1024))['too_big'] is False


def test_html_вместо_robots_видно_по_содержимому():
    """Типовая поломка: сайт отдаёт на /robots.txt свою страницу с кодом 200."""
    f = ic.analyze_robots_file(b'<!DOCTYPE html><html><body>404</body></html>',
                               'text/html; charset=utf-8')
    assert f['looks_html'] is True and f['content_type'] == 'text/html'
    обычный = ic.analyze_robots_file(b'User-agent: *\n', 'text/plain')
    assert обычный['looks_html'] is False and обычный['content_type'] == 'text/plain'


def test_bom_снимается_и_первая_группа_не_теряется():
    """С BOM ключ первой директивы становится «\\ufeffuser-agent» - группа
    правил пропадает целиком. Поэтому BOM и находка, и снимается до разбора."""
    сырые = '﻿User-agent: *\nDisallow: /basket/\n'.encode('utf-8')
    f = ic.analyze_robots_file(сырые)
    assert f['bom'] is True and f['utf8_ok'] is True
    assert not f['text'].startswith('﻿')
    info = ic.parse_robots(f['text'], 'a.ru')
    assert '*' in info.groups                      # группа на месте
    dis, _rule, _agent = ic.robots_verdict(info, 'https://a.ru/basket/')
    assert dis is True                             # и правило работает


def test_битая_кодировка_видна():
    f = ic.analyze_robots_file(b'User-agent: *\n# \xff\xfe\xfa broken\n')
    assert f['utf8_ok'] is False
    assert ic.analyze_robots_file('# пример\n'.encode('utf-8'))['utf8_ok'] is True


# ── robots.txt: закомментированные директивы ─────────────────────────


def test_закомментированные_директивы_находятся():
    текст = (
        'User-agent: *\n'
        '# Disallow: /bitrix/\n'          # закомментировано - находка
        '#Disallow: /admin/\n'            # без пробела - тоже
        '   ## Sitemap: https://a.ru/sitemap.xml\n'
        'Disallow: /search/\n'            # рабочая - не находка
        '# просто комментарий\n'          # не директива - не находка
        'Disallow: /order/  # хвостовой комментарий\n'   # правило рабочее
    )
    найдено = ic.find_commented_directives(текст)
    строки = [c['line'] for c in найдено]
    assert строки == [2, 3, 4]
    assert 'Disallow: /bitrix/' in найдено[0]['text']


def test_закомментированные_директивы_не_ломают_разбор_правил():
    """Комментарии по стандарту срезаются: закомментированное правило НЕ
    должно начать работать из-за нашей проверки."""
    info = ic.parse_robots('User-agent: *\n# Disallow: /basket/\n', 'a.ru')
    dis, _r, _a = ic.robots_verdict(info, 'https://a.ru/basket/')
    assert dis is False
    assert len(info.commented) == 1


# ── robots.txt: Clean-Param ──────────────────────────────────────────


def test_clean_param_собирается():
    info = ic.parse_robots(
        'User-agent: Yandex\n'
        'Clean-param: utm_source&utm_medium /catalog/\n'
        'Clean-Param: sort\n'
        'Disallow: /basket/\n', 'a.ru')
    assert info.clean_params == ['utm_source&utm_medium /catalog/', 'sort']
    # Clean-Param не должен попасть в правила Allow/Disallow
    assert len(info.groups['yandex']) == 1


def test_clean_param_нет_совсем():
    info = ic.parse_robots('User-agent: *\nDisallow: /basket/\n', 'a.ru')
    assert info.clean_params == []


# ── robots.txt: вывод в отчёт ────────────────────────────────────────


def _тексты(находки):
    return ' | '.join(f.problem for f in находки)


def test_находки_по_файлу_robots_попадают_в_проблемы():
    s = {'host': 'a.ru', 'robots_file': {
        'size_bytes': 600 * 1024, 'too_big': True, 'limit_bytes': 500 * 1024,
        'content_type': 'text/html', 'looks_html': True, 'bom': True,
        'utf8_ok': False, 'clean_params': [], 'commented': [
            {'line': 2, 'text': '# Disallow: /bitrix/'}]}}
    т = _тексты(indexing_site_findings(s))
    assert '500 КБ' in т
    assert 'HTML-страница' in т
    assert 'BOM' in т
    assert 'UTF-8' in т
    assert 'закомментирована' in т
    assert 'Clean-Param' in т
    # все находки указывают на сам файл
    assert all(f.url == 'https://a.ru/robots.txt'
               for f in indexing_site_findings(s))


def test_чистый_robots_даёт_только_замечание_про_clean_param():
    s = {'host': 'a.ru', 'robots_file': {
        'size_bytes': 400, 'too_big': False, 'limit_bytes': 500 * 1024,
        'content_type': 'text/plain', 'looks_html': False, 'bom': False,
        'utf8_ok': True, 'clean_params': [], 'commented': []}}
    находки = indexing_site_findings(s)
    assert len(находки) == 1 and 'Clean-Param' in находки[0].problem
    assert находки[0].level == 'Предупреждение'


def test_полностью_чистый_robots_молчит():
    s = {'host': 'a.ru', 'robots_file': {
        'size_bytes': 400, 'too_big': False, 'limit_bytes': 500 * 1024,
        'content_type': 'text/plain', 'looks_html': False, 'bom': False,
        'utf8_ok': True, 'clean_params': ['sort'], 'commented': []}}
    assert indexing_site_findings(s) == []
    assert extra_site_tasks(indexing_summary=s) == []


def test_нет_данных_по_файлу_молчим():
    """Старый прогон / robots не скачался - находок про файл быть не должно."""
    assert indexing_site_findings({'host': 'a.ru'}) == []
    assert extra_site_tasks(indexing_summary={'host': 'a.ru'}) == []


def test_задачи_по_файлу_robots():
    s = {'host': 'a.ru', 'robots_file': {
        'size_bytes': 600 * 1024, 'too_big': True, 'limit_bytes': 500 * 1024,
        'content_type': 'text/plain', 'looks_html': False, 'bom': True,
        'utf8_ok': True, 'clean_params': [], 'commented': []}}
    группы = {t.task_group: t for t in extra_site_tasks(indexing_summary=s)}
    assert 'robots_file_hygiene' in группы
    assert группы['robots_file_hygiene'].priority == 1
    assert 'BOM' in группы['robots_file_hygiene'].what
    assert 'robots_clean_param' in группы


# ── sitemap: формат и кодировка ──────────────────────────────────────


def test_xml_карта_проходит():
    f = sa.analyze_sitemap_file(
        'https://a.ru/sitemap.xml',
        b'<?xml version="1.0" encoding="UTF-8"?><urlset><url>'
        b'<loc>https://a.ru/</loc></url></urlset>', 'application/xml')
    assert f['kind'] == 'xml'
    assert f['format_why'] is None and f['encoding_why'] is None
    assert f['declared_encoding'] == 'utf-8'


def test_txt_карта_проходит_и_разбирается():
    data = b'https://a.ru/\nhttps://a.ru/catalog/\n'
    f = sa.analyze_sitemap_file('https://a.ru/sitemap.txt', data, 'text/plain')
    assert f['kind'] == 'txt' and f['format_why'] is None
    assert sa._txt_sitemap_urls(f['text']) == ['https://a.ru/',
                                               'https://a.ru/catalog/']


def test_html_вместо_карты_это_находка():
    f = sa.analyze_sitemap_file('https://a.ru/sitemap.xml',
                                b'<!doctype html><html></html>', 'text/html')
    assert f['kind'] == 'html'
    assert 'HTML-страница' in f['format_why']


def test_мусор_вместо_карты_это_находка():
    f = sa.analyze_sitemap_file('https://a.ru/sitemap.xml',
                                b'nothing useful here', 'text/plain')
    assert f['kind'] == 'непонятный' and f['format_why']


def test_заявленная_не_utf8_кодировка():
    f = sa.analyze_sitemap_file(
        'https://a.ru/sitemap.xml',
        b'<?xml version="1.0" encoding="windows-1251"?><urlset></urlset>')
    assert f['kind'] == 'xml'
    assert 'windows-1251' in f['encoding_why']


def test_нечитаемые_байты_карты():
    f = sa.analyze_sitemap_file('https://a.ru/sitemap.xml',
                                b'<?xml version="1.0"?><urlset>\xff\xfe</urlset>')
    assert 'не читается как UTF-8' in f['encoding_why']


def test_txt_карта_не_путается_с_мусором():
    # строка без схемы => это не список адресов
    assert sa._txt_sitemap_urls('https://a.ru/\nпросто текст\n') == []
    assert sa._txt_sitemap_urls('') == []


# ── sitemap: выбор среза для прозвона ────────────────────────────────


def test_срез_берётся_с_равным_шагом_по_всей_карте():
    urls = [f'https://a.ru/{i}/' for i in range(1000)]
    срез = sa.pick_probe_sample(urls, 10)
    assert len(срез) == 10
    # не первые десять: шаг по всему списку
    assert срез[0] == 'https://a.ru/0/' and срез[-1] == 'https://a.ru/900/'
    assert len(set(срез)) == 10


def test_срез_меньше_лимита_берётся_целиком():
    urls = ['https://a.ru/1/', 'https://a.ru/2/']
    assert sa.pick_probe_sample(urls, 200) == urls
    assert sa.pick_probe_sample([], 200) == []
    assert sa.pick_probe_sample(urls, 0) == []


# ── sitemap: вывод в отчёт ───────────────────────────────────────────


def test_адреса_карты_не_200_и_noindex_в_проблемах():
    s = {'host': 'a.ru', 'sitemap_audit': {
        'format_issues': [{'url': 'https://a.ru/sitemap.xml',
                           'why': 'вместо карты сайта отдаётся HTML-страница'}],
        'encoding_issues': [{'url': 'https://a.ru/sitemap.xml',
                             'why': 'в XML-декларации заявлена кодировка '
                                    'windows-1251, а не UTF-8'}],
        'url_probe': {'checked': 200, 'sample_of': 5000,
                      'bad_status': [{'url': 'https://a.ru/old/', 'status': 301}],
                      'noindex': [{'url': 'https://a.ru/x/',
                                   'signal': 'meta robots: noindex'}]}}}
    находки = indexing_site_findings(s)
    т = _тексты(находки)
    assert 'не в формате .xml/.txt' in т
    assert 'кодировка карты сайта' in т
    assert 'отвечает не 200' in т
    assert 'закрыт noindex' in т
    # формат - ошибка, заявленная кодировка - мягче
    уровни = {f.problem[:20]: f.level for f in находки}
    assert уровни['карта сайта не в фор'] == 'Ошибка'
    assert [f.level for f in находки
            if 'кодировка карты' in f.problem] == ['Предупреждение']
    # адрес находки - сам проблемный URL, а не карта
    assert any(f.url == 'https://a.ru/old/' for f in находки)


def test_нечитаемая_кодировка_карты_это_ошибка():
    s = {'host': 'a.ru', 'sitemap_audit': {'encoding_issues': [
        {'url': 'https://a.ru/sitemap.xml', 'why': 'файл не читается как UTF-8'}]}}
    находки = indexing_site_findings(s)
    assert len(находки) == 1 and находки[0].level == 'Ошибка'


def test_задачи_по_карте_сайта():
    s = {'host': 'a.ru', 'sitemap_audit': {
        'format_issues': [{'url': 'https://a.ru/sitemap.xml', 'why': 'HTML'}],
        'url_probe': {'checked': 200, 'sample_of': 900,
                      'bad_status': [{'url': 'https://a.ru/old/', 'status': 404}],
                      'noindex': [{'url': 'https://a.ru/x/', 'signal': 'meta'}]}}}
    группы = {t.task_group: t for t in extra_site_tasks(indexing_summary=s)}
    assert 'sitemap_format' in группы
    assert группы['sitemap_bad_status'].volume == 1
    assert '200' in группы['sitemap_bad_status'].what
    assert 'noindex' in группы['sitemap_noindex'].what


def test_пустой_прозвон_молчит():
    s = {'host': 'a.ru', 'sitemap_audit': {
        'format_issues': [], 'encoding_issues': [],
        'url_probe': {'checked': 200, 'sample_of': 200, 'bad_status': [],
                      'noindex': [], 'unreachable': [], 'blocked': 0}}}
    assert indexing_site_findings(s) == []
    assert extra_site_tasks(indexing_summary=s) == []


def test_недоехавший_адрес_это_предупреждение_без_задачи():
    """До адреса не доехали МЫ - клиенту в работу это ставить нельзя."""
    s = {'host': 'a.ru', 'sitemap_audit': {'url_probe': {
        'checked': 30, 'sample_of': 20000, 'bad_status': [], 'noindex': [],
        'blocked': 0,
        'unreachable': [{'url': 'https://a.ru/slow/', 'why': 'TimeoutError'}]}}}
    находки = indexing_site_findings(s)
    assert len(находки) == 1
    assert находки[0].level == 'Предупреждение'
    assert 'не удалось проверить' in находки[0].problem
    assert 'TimeoutError' in находки[0].detail
    # в «Плане работ» задачи нет: это ограничение проверки, не дефект сайта
    assert extra_site_tasks(indexing_summary=s) == []
