"""Тесты platform_profile: определение движка и применимость правил вёрстки.

Смысл: на chunk-сборках (Next.js/Nuxt) четыре правила давали 100% ложных
срабатываний - на каждой странице каждого прогона. Тесты закрепляют, что
(а) движок определяется, (б) на нём эти правила молчат, (в) на классических
CMS они по-прежнему срабатывают, (г) настоящие находки не проглочены.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import platform_profile as pp
from layout_checker import check_layout


# Куски настоящих страниц (сокращённые до опознавательных признаков).
NEXTJS_HTML = (
    '<html><head><link rel="stylesheet" href="/_next/static/css/72d0.css"/>'
    '</head><body><main>'
    # хеш-имена чанков без «.min» - на этом ломалась проверка минификации
    + ''.join(f'<script src="/_next/static/chunks/webpack-c9bf{i}.js"></script>'
              for i in range(8))
    + '<img src="/a.png" style="color:transparent"/>' * 40
    + '<script>self.__next_f.push([1,"' + 'x' * 40000 + '"])</script>'
    + '</main></body></html>'
)
BITRIX_HTML = (
    '<html><head><link rel="stylesheet" href="/bitrix/cache/s1.css"/>'
    '</head><body><main>'
    + '<div style="color:red">x</div>' * 40
    + '<script>var a = "' + 'x' * 40000 + '";</script>'
    + '</main></body></html>'
)

# 12 своих CSS - выше порога «объединения», имена без «.min».
CSS_INFOS = [{'url': f'https://example.ru/_next/static/css/{i}.css',
              'status': 200, 'has_media': True, 'minified': True}
             for i in range(12)]

# Правила, которые обязаны молчать на chunk-сборке.
_BUNDLER_NOISE = ('не объединены', '.min в имени', 'инлайн-стилей',
                  'inline-<script>')


def test_detect_nextjs():
    assert pp.detect_platform(NEXTJS_HTML) == 'nextjs'
    assert pp.detect_platform('<html>__NEXT_DATA__</html>') == 'nextjs'


def test_detect_others():
    assert pp.detect_platform(BITRIX_HTML) == 'bitrix'
    assert pp.detect_platform('<html>/_nuxt/entry.js</html>') == 'nuxt'
    assert pp.detect_platform('<html>Mage.Cookies</html>') == 'magento'
    assert pp.detect_platform('<html>обычная страница</html>') == 'unknown'
    assert pp.detect_platform('') == 'unknown'
    assert pp.detect_platform(None) == 'unknown'


def test_rule_applies_defaults_to_true():
    """Неизвестное правило/движок - показываем находку, а не прячем."""
    assert pp.rule_applies('такого-правила-нет', 'nextjs') is True
    assert pp.rule_applies('assets_bundling', 'unknown') is True
    assert pp.rule_applies('assets_bundling', 'bitrix') is True
    assert pp.rule_applies('assets_bundling', 'nextjs') is False


def test_nextjs_suppresses_bundler_warnings():
    r = check_layout(NEXTJS_HTML, CSS_INFOS, base_url='https://example.ru/')
    joined = ' | '.join(r['warnings'])
    for noise in _BUNDLER_NOISE:
        assert noise not in joined, f'на Next.js не должно быть: {noise}'


def test_same_page_on_bitrix_keeps_warnings():
    """Тот же HTML, но движок классический - правила обязаны сработать.
    Проверяем именно переключатель, а не «правила вообще выключились»."""
    r = check_layout(NEXTJS_HTML, CSS_INFOS, base_url='https://example.ru/',
                     platform='bitrix')
    joined = ' | '.join(r['warnings'])
    for noise in _BUNDLER_NOISE:
        assert noise in joined, f'на Bitrix должно остаться: {noise}'


def test_real_findings_survive_on_nextjs():
    """Главное: чистка шума не должна глушить настоящие находки. Берём
    страницу без viewport и с битым CSS - это баги на любом движке."""
    html = NEXTJS_HTML.replace('<head>', '<head><!-- без viewport -->')
    css = CSS_INFOS + [{'url': 'https://example.ru/_next/static/css/bad.css',
                        'status': 404, 'has_media': False, 'minified': True}]
    r = check_layout(html, css, base_url='https://example.ru/')
    joined = ' | '.join(r['issues'])
    assert 'viewport' in joined
    assert 'не грузится часть CSS' in joined
