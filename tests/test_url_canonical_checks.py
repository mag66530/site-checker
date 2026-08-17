# -*- coding: utf-8 -*-
"""Раздел 2 доп. чек-листа: формат адресов (длина, домен дважды) и canonical
(относительный адрес, границы <head>). Всё без сети."""
import indexing_checker as ic
from report_priorities import url_format_findings


# ── Длина адреса ─────────────────────────────────────────────────────


def test_длинный_адрес_находка():
    короткий = '/catalog/truba/'
    длинный = '/catalog/' + 'a' * 2100 + '/'
    uf = ic.check_url_format([короткий, длинный], root_domain='site.ru')
    assert uf['too_long_n'] == 1
    assert uf['too_long'] == [длинный]
    assert uf['checked'] == 2


def test_длина_считается_вместе_с_доменом():
    """2048 - предел ПОЛНОГО адреса, а на входе только путь: домен и https://
    надо прибавить, иначе адрес на грани проходил бы как короткий."""
    путь = '/' + 'a' * 2035          # 2036 символов пути
    # + домен (7) + «https://» (8) = 2051 > 2048
    assert ic.check_url_format([путь], root_domain='site.ru')['too_long_n'] == 1
    # то же без домена: 2036 + 8 = 2044 - влезает
    assert ic.check_url_format([путь])['too_long_n'] == 0


# ── Домен внутри пути ────────────────────────────────────────────────


def test_домен_дважды_в_пути():
    пути = ['/catalog/truba/',                       # норма
            '/site.ru/catalog/truba/',               # домен вторым разом
            '/https://site.ru/catalog/',             # со схемой
            '/http:/site.ru/catalog/']               # схема с потерянным слэшем
    uf = ic.check_url_format(пути, root_domain='site.ru')
    assert uf['domain_twice_n'] == 3
    assert '/catalog/truba/' not in uf['domain_twice']


def test_домен_дважды_не_путает_www_и_регистр():
    uf = ic.check_url_format(['/WWW.SITE.RU/catalog/'], root_domain='www.site.ru')
    # адрес попадёт в domain_twice (проверяется первым), а не в uppercase
    assert uf['domain_twice_n'] == 1 and uf['uppercase_n'] == 0


def test_без_домена_проекта_ловим_только_схему():
    uf = ic.check_url_format(['/site.ru/catalog/', '/https://x/'])
    assert uf['domain_twice_n'] == 1          # только адрес со схемой


def test_чужой_домен_в_пути_не_ловим():
    """Домен ПАРТНЁРА в пути - не наша ошибка склейки (и бывает в слагах)."""
    uf = ic.check_url_format(['/catalog/gost-nlmk-ru/'], root_domain='site.ru')
    assert uf['domain_twice_n'] == 0


# ── Старые виды не сломались ─────────────────────────────────────────


def test_прежние_виды_на_месте():
    uf = ic.check_url_format([
        '/catalog/index.php?ID=5',       # non_sef
        '/catalog/труба/',               # cyrillic
        '/Catalog/Truba/',               # uppercase
        '/catalog/truba_stalnaya/',      # underscore
        '/catalog/truba stalnaya/',      # junk_chars (пробел)
    ], root_domain='site.ru')
    assert (uf['non_sef_n'], uf['cyrillic_n'], uf['uppercase_n'],
            uf['underscore_n'], uf['junk_chars_n']) == (1, 1, 1, 1, 1)
    assert uf['total_bad'] == 5


def test_все_виды_учтены_в_итоге_и_в_отчёте():
    """Страж: новый вид находки обязан попасть и в total_bad, и в вывод отчёта.
    На этом уже спотыкались - total_bad считался по своему списку из пяти
    видов, и добавленные виды молча не доходили до «Проблем»."""
    from report_priorities import _URL_FORMAT_KINDS
    assert set(ic.URL_FORMAT_KINDS) == {k for k, _t, _f in _URL_FORMAT_KINDS}
    # total_bad считается по тому же списку
    uf = ic.check_url_format(['/site.ru/x/'], root_domain='site.ru')
    assert uf['total_bad'] == 1


def test_новые_виды_попадают_в_проблемы():
    s = {'host': 'site.ru', 'url_format': ic.check_url_format(
        ['/site.ru/catalog/', '/' + 'a' * 2100], root_domain='site.ru')}
    т = ' | '.join(f.problem for f in url_format_findings(s))
    assert 'домен указан в адресе дважды' in т
    assert 'длиннее 2048' in т


# ── canonical: границы <head> ────────────────────────────────────────


_В_HEAD = ('<html><head><link rel="canonical" href="https://site.ru/x/">'
           '</head><body>текст</body></html>')
_В_BODY = ('<html><head><title>т</title></head><body>'
           '<link rel="canonical" href="https://site.ru/x/"></body></html>')


def test_canonical_в_head_и_вне_head():
    в_head = ic.find_canonicals_ex(_В_HEAD)
    assert len(в_head) == 1 and в_head[0]['in_head'] is True
    в_body = ic.find_canonicals_ex(_В_BODY)
    assert len(в_body) == 1 and в_body[0]['in_head'] is False


def test_без_закрывающего_head_не_обвиняем():
    """Битая вёрстка без </head>: судить не берёмся, иначе находка на пустом
    месте у каждой такой страницы."""
    html = '<html><link rel="canonical" href="https://site.ru/x/"><body>т'
    assert ic.find_canonicals_ex(html)[0]['in_head'] is True


def test_старый_find_canonicals_не_сломан():
    assert ic._find_canonicals(_В_HEAD) == ['https://site.ru/x/']


def test_вне_head_это_ошибка_страницы():
    ix = ic.analyze_page_indexing(_В_BODY, {}, 'https://site.ru/x/', None)
    assert ix['canonical_outside_head'] is True
    assert any('вне <head>' in i for i in ix['issues']), ix['issues']

    ix_ok = ic.analyze_page_indexing(_В_HEAD, {}, 'https://site.ru/x/', None)
    assert ix_ok['canonical_outside_head'] is False
    assert not any('вне <head>' in i for i in ix_ok['issues'])


# ── canonical: относительный адрес ───────────────────────────────────


def test_относительный_адрес_распознаётся():
    assert ic.is_relative_url('/catalog/x/') is True
    assert ic.is_relative_url('catalog/x/') is True
    assert ic.is_relative_url('https://site.ru/x/') is False
    assert ic.is_relative_url('//site.ru/x/') is False       # protocol-relative
    assert ic.is_relative_url('') is False


def test_относительный_canonical_это_находка():
    html = ('<html><head><link rel="canonical" href="/x/"></head>'
            '<body></body></html>')
    ix = ic.analyze_page_indexing(html, {}, 'https://site.ru/x/', None)
    assert ix['canonical_relative'] is True
    assert any('относительный адрес' in i for i in ix['issues'])


def test_относительный_canonical_на_себя_не_даёт_ложного_другого_url():
    """Раньше сравнение шло по сырому href: относительный canonical никогда не
    совпадал с адресом страницы, и выписывалось «canonical ведёт на другой
    URL». Теперь адрес сначала достраивается до абсолютного."""
    html = ('<html><head><link rel="canonical" href="/x/"></head>'
            '<body></body></html>')
    ix = ic.analyze_page_indexing(html, {}, 'https://site.ru/x/', None)
    assert ix['canonical_self'] is True
    assert not any('другой URL' in w for w in ix['warnings'])
    assert not any('другой домен' in i for i in ix['issues'])


def test_абсолютный_canonical_на_чужой_url_по_прежнему_замечание():
    html = ('<html><head><link rel="canonical" href="https://site.ru/other/">'
            '</head><body></body></html>')
    ix = ic.analyze_page_indexing(html, {}, 'https://site.ru/x/', None)
    assert ix['canonical_relative'] is False
    assert ix['canonical_self'] is False
    assert any('другой URL' in w for w in ix['warnings'])
