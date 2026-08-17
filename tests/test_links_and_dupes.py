# -*- coding: utf-8 -*-
"""Раздел 2 доп. чек-листа: ссылки (редиректы/404/на себя) и дубли адресов
(короткий адрес без раздела, товар в чужой категории). Всё на фейковой сети."""
import asyncio

import http_checker as hc
import meta_checker as mc
from report_priorities import (SEC_DUPES, SEC_LINKS, _link_findings,
                               classify, metadata_site_findings)


# ── Фейковая сессия ──────────────────────────────────────────────────


class _Ответ:
    def __init__(self, status=200, headers=None, body=b''):
        self.status = status
        self.headers = headers or {}
        self.url = 'about:blank'
        self._body = body

    class _Тело:
        def __init__(self, data):
            self._data = data

        async def read(self, n=None):
            return self._data[:n] if n else self._data

    @property
    def content(self):
        return self._Тело(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Сессия:
    """Карта url → _Ответ (или Exception). HEAD и GET отдают одно и то же."""

    def __init__(self, карта):
        self.карта = карта
        self.вызовы = []

    def _выдать(self, url, метод):
        self.вызовы.append((метод, url))
        о = self.карта.get(url)
        if isinstance(о, Exception):
            raise о
        return о if о is not None else _Ответ(200)

    def head(self, url, **kw):
        return self._выдать(url, 'HEAD')

    def get(self, url, **kw):
        return self._выдать(url, 'GET')


# ── _link_probe: цепочка редиректов ──────────────────────────────────


def test_редирект_виден_а_не_проглочен():
    """Главная причина, по которой пункт не работал: раньше запрос шёл с
    allow_redirects=True и 301 не был виден вовсе - оставался финальный код."""
    s = _Сессия({
        'https://a.ru/old/': _Ответ(301, {'Location': 'https://a.ru/new/'}),
        'https://a.ru/new/': _Ответ(200),
    })
    r = asyncio.run(hc._link_probe(s, 'https://a.ru/old/', 20000, None))
    assert r['code'] == 301
    assert r['location'] == 'https://a.ru/new/'
    assert r['final_code'] == 200 and r['hops'] == 1
    assert r['loop'] is False


def test_редирект_в_404():
    s = _Сессия({
        'https://a.ru/old/': _Ответ(302, {'Location': '/gone/'}),
        'https://a.ru/gone/': _Ответ(404),
    })
    r = asyncio.run(hc._link_probe(s, 'https://a.ru/old/', 20000, None))
    assert (r['code'], r['final_code'], r['hops']) == (302, 404, 1)
    assert r['final_url'] == 'https://a.ru/gone/'


def test_цепочка_из_нескольких_переходов():
    s = _Сессия({
        'https://a.ru/1/': _Ответ(301, {'Location': '/2/'}),
        'https://a.ru/2/': _Ответ(301, {'Location': '/3/'}),
        'https://a.ru/3/': _Ответ(200),
    })
    r = asyncio.run(hc._link_probe(s, 'https://a.ru/1/', 20000, None))
    assert r['hops'] == 2 and r['final_code'] == 200


def test_цикл_редиректов_не_вешает():
    s = _Сессия({
        'https://a.ru/1/': _Ответ(301, {'Location': '/2/'}),
        'https://a.ru/2/': _Ответ(301, {'Location': '/1/'}),
    })
    r = asyncio.run(hc._link_probe(s, 'https://a.ru/1/', 20000, None))
    assert r['loop'] is True and r['hops'] <= hc.MAX_LINK_HOPS


def test_прямая_ссылка_без_редиректа():
    s = _Сессия({'https://a.ru/x/': _Ответ(200)})
    r = asyncio.run(hc._link_probe(s, 'https://a.ru/x/', 20000, None))
    assert (r['code'], r['final_code'], r['hops']) == (200, 200, 0)
    assert r['location'] is None


def test_нет_ответа_не_считается_битым():
    s = _Сессия({'https://a.ru/x/': RuntimeError('таймаут')})
    r = asyncio.run(hc._link_probe(s, 'https://a.ru/x/', 20000, None))
    assert r['code'] is None and r['final_code'] is None


def test_старый_link_status_отдаёт_финальный_код():
    s = _Сессия({
        'https://a.ru/old/': _Ответ(301, {'Location': '/new/'}),
        'https://a.ru/new/': _Ответ(200),
    })
    assert asyncio.run(hc._link_status(s, 'https://a.ru/old/', 20000, None)) == 200


# ── check_content_links: разбор страницы ─────────────────────────────


_HTML = '''<html><body>
  <a href="/ok/">живая</a>
  <a href="/gone/">битая</a>
  <a href="/old/">через редирект</a>
  <a href="/dead-redirect/">редирект в 404</a>
  <a href="/page/">сама на себя</a>
  <a href="#anchor">закладка</a>
  <a href="https://vendor.com/moved/">внешняя с редиректом</a>
  <a href="https://vendor.com/live/">внешняя живая</a>
</body></html>'''

_КАРТА = {
    'https://a.ru/ok/': _Ответ(200),
    'https://a.ru/gone/': _Ответ(404),
    'https://a.ru/old/': _Ответ(301, {'Location': '/ok/'}),
    'https://a.ru/dead-redirect/': _Ответ(301, {'Location': '/gone/'}),
    'https://a.ru/page/': _Ответ(200),
    'https://vendor.com/moved/': _Ответ(302, {'Location': 'https://vendor.com/live/'}),
    'https://vendor.com/live/': _Ответ(200),
}


def _разобрать(html=_HTML, карта=_КАРТА, **kw):
    s = _Сессия(карта)
    return asyncio.run(hc.check_content_links(
        s, html, 'https://a.ru/page/', link_cache={}, **kw)), s


def test_ссылки_разложены_по_видам():
    res, _ = _разобрать()
    # «битая» - только прямой 404; «301, а за ним 404» - свой вид, иначе одна
    # ссылка давала бы две находки и было бы неясно, что править
    assert [b['url'] for b in res['broken']] == ['https://a.ru/gone/']
    assert [b['url'] for b in res['redirect_to_error']] == \
        ['https://a.ru/dead-redirect/']
    assert [b['url'] for b in res['redirects']] == [
        'https://a.ru/old/', 'https://a.ru/dead-redirect/']
    assert res['self_links'] == ['/page/']       # «#anchor» не считается


def test_редирект_в_ошибку_даёт_одну_находку():
    res, _ = _разобрать()
    находки = [f for f in _link_findings(res, url='https://a.ru/page/')
               if f.url == 'https://a.ru/dead-redirect/']
    assert len(находки) == 1
    assert 'а он - на ошибку' in находки[0].problem


def test_внешние_ссылки_отдельно():
    res, _ = _разобрать()
    assert res['ext_checked'] == 2
    assert [b['url'] for b in res['ext_redirects']] == ['https://vendor.com/moved/']
    assert res['ext_broken'] == []
    # внешние не попали во внутренние списки
    assert all('vendor.com' not in b['url'] for b in res['redirects'])


def test_403_отдельным_мягким_видом():
    """403 - не «битая»: страница существует, закрыт доступ. Но и не норма:
    свой сайт не должен закрывать страницу, на которую сам ссылается."""
    html = ('<html><body><a href="/secret/">внутр</a>'
            '<a href="https://vendor.com/secret/">внешн</a></body></html>')
    карта = {'https://a.ru/secret/': _Ответ(403),
             'https://vendor.com/secret/': _Ответ(403)}
    res, _ = _разобрать(html=html, карта=карта)
    assert [b['url'] for b in res['forbidden']] == ['https://a.ru/secret/']
    assert [b['url'] for b in res['ext_forbidden']] == \
        ['https://vendor.com/secret/']
    # в «битые» 403 по-прежнему не попадает
    assert res['broken'] == [] and res['ext_broken'] == []
    находки = _link_findings(res, url='https://a.ru/page/')
    assert len(находки) == 2
    assert all(f.level == 'Предупреждение' for f in находки)
    assert all('закрытую страницу (403)' in f.problem for f in находки)
    for f in находки:
        assert classify(f)['task_group'] == 'links_forbidden'


def test_403_после_редиректа_не_двоится():
    """301 → 403 уже показан как «редирект в ошибку» - второй находки быть
    не должно."""
    html = '<html><body><a href="/old/">через редирект</a></body></html>'
    карта = {'https://a.ru/old/': _Ответ(301, {'Location': '/secret/'}),
             'https://a.ru/secret/': _Ответ(403)}
    res, _ = _разобрать(html=html, карта=карта)
    assert res['forbidden'] == []
    assert [b['url'] for b in res['redirect_to_error']] == ['https://a.ru/old/']


def test_внешних_не_больше_бюджета_на_прогон():
    """Внешние 403/429 - обычно антибот, поэтому их лимит на ВЕСЬ прогон
    маленький: чем больше проверим, тем больше шума."""
    html1 = '<html><body>' + ''.join(
        f'<a href="https://v{i}.com/">в{i}</a>' for i in range(8)
    ) + '</body></html>'
    # на второй странице есть и внутренняя ссылка - иначе звонить стало бы
    # нечего вовсе и функция вернула бы None
    html2 = '<html><body><a href="/ok/">своя</a>' + ''.join(
        f'<a href="https://w{i}.com/">w{i}</a>' for i in range(8)
    ) + '</body></html>'
    кеш, бюджет = {}, [5]
    s = _Сессия({})
    r1 = asyncio.run(hc.check_content_links(
        s, html1, 'https://a.ru/1/', link_cache=кеш, ext_budget=бюджет))
    r2 = asyncio.run(hc.check_content_links(
        s, html2, 'https://a.ru/2/', link_cache=кеш, ext_budget=бюджет))
    assert r1['ext_checked'] == 5        # первая страница выбрала весь лимит
    assert бюджет[0] == 0
    assert r2['ext_checked'] == 0        # второй уже ничего не досталось
    assert r2['checked'] == 1            # внутренние при этом проверяются
    чужие = {u for _m, u in s.вызовы if '.com' in u}
    assert len(чужие) == 5


def test_страница_только_с_внешними_при_исчерпанном_лимите():
    """Звонить нечего - штатный None, а не пустой отчёт."""
    html = '<html><body><a href="https://v1.com/">в</a></body></html>'
    res = asyncio.run(hc.check_content_links(
        _Сессия({}), html, 'https://a.ru/1/', link_cache={}, ext_budget=[0]))
    assert res is None


def test_уже_проверенные_внешние_не_тратят_бюджет():
    """Подвальные ссылки одни и те же на всех страницах - они лежат в кеше
    прогона и лимит съедать не должны, иначе он кончится на второй странице."""
    html = '<html><body><a href="https://v1.com/">в</a></body></html>'
    кеш, бюджет = {}, [2]
    s = _Сессия({})
    for _ in range(3):
        asyncio.run(hc.check_content_links(
            s, html, 'https://a.ru/x/', link_cache=кеш, ext_budget=бюджет))
    assert бюджет[0] == 1               # потрачена ровно одна ссылка
    assert len([u for _m, u in s.вызовы if 'v1.com' in u]) == 1


def test_лимит_внешних_ссылок():
    html = '<html><body>' + ''.join(
        f'<a href="https://vendor{i}.com/">в{i}</a>' for i in range(20)
    ) + '</body></html>'
    res, s = _разобрать(html=html, карта={}, ext_limit=3)
    assert res['ext_checked'] == 3
    # чужих хостов дёрнули ровно три
    внешние = {u for _m, u in s.вызовы if 'vendor' in u}
    assert len(внешние) == 3


def test_на_главной_ссылка_на_себя_не_находка():
    """На главной ссылка на «/» - это логотип в шапке, так сделан любой сайт.
    Находка была бы на каждой главной и только отвлекала (проверено вживую на
    СМУ: единственной находкой по ссылкам был именно логотип)."""
    html = '<html><body><a href="/">логотип</a><a href="/ok/">к</a></body></html>'
    s = _Сессия({'https://a.ru/ok/': _Ответ(200), 'https://a.ru/': _Ответ(200)})
    res = asyncio.run(hc.check_content_links(
        s, html, 'https://a.ru/', link_cache={}))
    assert res['self_links'] == []


def test_на_внутренней_странице_ссылка_на_себя_находка():
    html = ('<html><body><a href="/">логотип</a>'
            '<a href="/catalog/truba/">эта же страница</a></body></html>')
    s = _Сессия({'https://a.ru/': _Ответ(200),
                 'https://a.ru/catalog/truba/': _Ответ(200)})
    res = asyncio.run(hc.check_content_links(
        s, html, 'https://a.ru/catalog/truba/', link_cache={}))
    assert res['self_links'] == ['/catalog/truba/']    # логотип не считается


def test_якорь_не_считается_ссылкой_на_себя():
    html = '<html><body><a href="#top">наверх</a></body></html>'
    res, _ = _разобрать(html=html, карта={})
    assert res is None or res['self_links'] == []


def test_кеш_прогона_звонит_ссылку_один_раз():
    кеш = {}
    s = _Сессия(_КАРТА)
    for _ in range(3):
        asyncio.run(hc.check_content_links(
            s, _HTML, 'https://a.ru/page/', link_cache=кеш))
    дёрнули = [u for _m, u in s.вызовы if u == 'https://a.ru/ok/']
    # /ok/ - и сама ссылка, и цель редиректа с /old/: по разу за первый проход
    assert len(дёрнули) <= 2


def test_бюджет_ограничивает_прозвон():
    бюджет = [2]
    res, _ = _разобрать(budget=бюджет)
    assert бюджет[0] == 0
    assert res['checked'] + res['ext_checked'] == 2


# ── Вывод в «Проблемы» ───────────────────────────────────────────────


def test_находки_по_ссылкам_в_проблемах():
    res, _ = _разобрать()
    находки = _link_findings(res, city='Москва', page_type='Категория',
                             url='https://a.ru/page/')
    т = {f.problem: f for f in находки}
    assert any('несуществующую страницу (404)' in p for p in т)
    assert any('а он - на ошибку' in p for p in т)
    assert any('ведёт на редирект 301' in p for p in т)
    assert any('сама на себя' in p for p in т)
    assert all(f.section == SEC_LINKS for f in находки)
    # у находки виден и адрес ссылки, и страница, где она стоит
    битая = [f for f in находки if '404' in f.problem][0]
    assert битая.url == 'https://a.ru/gone/'
    assert 'https://a.ru/page/' in битая.detail


def test_задачи_для_ссылок_есть_у_каждой_находки():
    res, _ = _разобрать()
    for f in _link_findings(res, url='https://a.ru/page/'):
        правило = classify(f)
        assert правило['task_group'], f.problem
        assert правило['task_group'] != 'links_generic', f.problem


def test_нет_данных_по_ссылкам_молчим():
    assert _link_findings(None) == []
    assert _link_findings({}) == []


# ── Дубли: короткий адрес без раздела ────────────────────────────────


def test_короткий_вариант_адреса():
    assert mc.short_path_variant('/catalog/truba/profil/') == '/profil/'
    assert mc.short_path_variant('/catalog/truba/') == '/truba/'
    assert mc.short_path_variant('/truba/') == ''        # уже один сегмент
    assert mc.short_path_variant('/') == ''
    assert mc.short_path_variant('') == ''


def test_подстановка_чужой_категории():
    assert mc.swap_category('/catalog/truba/tovar-1/', '/catalog/list/') == \
        '/catalog/list/tovar-1/'
    # та же категория - подставлять нечего
    assert mc.swap_category('/catalog/truba/tovar-1/', '/catalog/truba/') == ''
    # корневой слаг (ИМП): родителя нет
    assert mc.swap_category('/tovar-1/', '/catalog/list/') == ''
    assert mc.swap_category('/catalog/truba/tovar-1/', '') == ''


def _страница(title):
    return _Ответ(200, {}, f'<html><head><title>{title}</title></head></html>'
                  .encode('utf-8'))


def test_дубль_без_раздела_ловится_по_совпадению_заголовка():
    карта = {
        'https://a.ru/catalog/truba/profil/': _страница('Профильная труба'),
        'https://a.ru/profil/': _страница('Профильная труба'),
    }
    s = _Сессия(карта)
    res = asyncio.run(mc.check_short_path_duplicates(
        ['https://a.ru/catalog/truba/profil/'], _сессия=s))
    assert len(res) == 1
    assert res[0]['variant'] == 'https://a.ru/profil/'
    assert res[0]['title'] == 'Профильная труба'


def test_другая_страница_по_короткому_адресу_не_дубль():
    """Короткий адрес может быть живой ДРУГОЙ страницей (тег, лендинг) - код
    200 сам по себе дублём не делает, иначе находка была бы ложной."""
    карта = {
        'https://a.ru/catalog/truba/profil/': _страница('Профильная труба'),
        'https://a.ru/profil/': _страница('Профили для гипсокартона'),
    }
    res = asyncio.run(mc.check_short_path_duplicates(
        ['https://a.ru/catalog/truba/profil/'], _сессия=_Сессия(карта)))
    assert res == []


def test_короткий_адрес_редиректит_на_исходный_это_норма():
    карта = {
        'https://a.ru/catalog/truba/profil/': _страница('Профильная труба'),
        # редирект на исходный - штатная склейка
        'https://a.ru/profil/': _страница('Профильная труба'),
    }
    s = _Сессия(карта)

    class _СессияРедирект(_Сессия):
        def _выдать(self, url, метод):
            if url == 'https://a.ru/profil/':
                о = _страница('Профильная труба')
                о.url = 'https://a.ru/catalog/truba/profil/'
                self.вызовы.append((метод, url))
                return о
            return super()._выдать(url, метод)

    res = asyncio.run(mc.check_short_path_duplicates(
        ['https://a.ru/catalog/truba/profil/'], _сессия=_СессияРедирект(карта)))
    assert res == []


def test_товар_в_чужой_категории():
    карта = {
        'https://a.ru/catalog/truba/tovar-1/': _страница('Труба 20 мм'),
        'https://a.ru/catalog/list/tovar-1/': _страница('Труба 20 мм'),
    }
    res = asyncio.run(mc.check_product_cross_category(
        ['https://a.ru/catalog/truba/tovar-1/'], ['/catalog/list/'],
        _сессия=_Сессия(карта)))
    assert len(res) == 1
    assert res[0]['variant'] == 'https://a.ru/catalog/list/tovar-1/'


def _страница_с_canonical(title, canonical):
    return _Ответ(200, {}, (
        f'<html><head><title>{title}</title>'
        f'<link rel="canonical" href="{canonical}"></head></html>'
    ).encode('utf-8'))


def test_дубль_прикрытый_canonical_это_замечание():
    """Двойник с canonical на основной адрес в индекс не попадёт - ошибкой это
    называть нельзя, но адрес живой и покупателю доступен."""
    карта = {
        'https://a.ru/catalog/truba/t-1/': _страница('Труба 20 мм'),
        'https://a.ru/catalog/list/t-1/': _страница_с_canonical(
            'Труба 20 мм', 'https://a.ru/catalog/truba/t-1/'),
    }
    res = asyncio.run(mc.check_product_cross_category(
        ['https://a.ru/catalog/truba/t-1/'], ['/catalog/list/'],
        _сессия=_Сессия(карта)))
    assert len(res) == 1 and res[0]['canonical_ok'] is True
    находки = metadata_site_findings(
        {'host': 'a.ru', 'product_cross_category': res})
    assert находки[0].level == 'Предупреждение'
    assert 'прикрыт canonical' in находки[0].detail


def test_дубль_с_canonical_на_себя_остаётся_ошибкой():
    карта = {
        'https://a.ru/catalog/truba/t-1/': _страница('Труба 20 мм'),
        # canonical указывает на сам двойник - значит дубль открыт
        'https://a.ru/catalog/list/t-1/': _страница_с_canonical(
            'Труба 20 мм', 'https://a.ru/catalog/list/t-1/'),
    }
    res = asyncio.run(mc.check_product_cross_category(
        ['https://a.ru/catalog/truba/t-1/'], ['/catalog/list/'],
        _сессия=_Сессия(карта)))
    assert len(res) == 1 and res[0]['canonical_ok'] is False
    находки = metadata_site_findings(
        {'host': 'a.ru', 'product_cross_category': res})
    assert находки[0].level == 'Ошибка'


def test_товар_без_категории_в_адресе_пропускаем():
    """ИМП: адрес карточки - корневой слаг; SHOPMET: /product/<slug>.
    Подставлять категорию некуда - находки быть не должно."""
    res = asyncio.run(mc.check_product_cross_category(
        ['https://a.ru/list-otsinkovannyj-nlmk/'], ['/catalog/list/'],
        _сессия=_Сессия({})))
    assert res == []


def test_дубли_адресов_ушли_в_свой_раздел():
    s = {'host': 'a.ru',
         'url_duplicates': [{'variant': 'http://a.ru/', 'canonical': 'https://a.ru/',
                             'problem': 'duplicate'}],
         'test_domains': [{'host': 'dev.a.ru', 'state': 'indexable'}],
         'short_path_duplicates': [{'canonical': 'https://a.ru/catalog/truba/profil/',
                                    'variant': 'https://a.ru/profil/',
                                    'title': 'Профильная труба'}],
         'product_cross_category': [{'canonical': 'https://a.ru/catalog/truba/t-1/',
                                     'variant': 'https://a.ru/catalog/list/t-1/',
                                     'title': 'Труба 20 мм'}]}
    находки = metadata_site_findings(s)
    свои = [f for f in находки if f.section == SEC_DUPES]
    assert len(свои) == 4, [f.problem for f in находки]
    т = ' | '.join(f.problem for f in свои)
    assert 'зеркало адреса' in т
    assert 'тестовый поддомен' in т
    assert 'короткому адресу без раздела' in т
    assert 'чужой категории' in т
    # ни одна из них больше не числится «Метаданными»
    assert not any(f.section == 'Метаданные' for f in находки)
    for f in свои:
        assert classify(f)['task_group'] != 'dupes_generic', f.problem
