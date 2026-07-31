"""Тесты twogis_map_check.extract(): разбор панели контактов карточки 2ГИС
из РЕАЛЬНОГО HTML, который прислал пользователь (Москва/Севастополь у СМУ) -
старый парсер (регулярка по внедрённому JSON) не находил сайт вообще ("–"
вместо реального значения), т.к. 2ГИС теперь не кладёт открытым текстом
"url":"...","text":"..." для сайта - только base64 в аналитической ссылке.
Новый парсер читает саму панель контактов (div._8sgdp4) - то же, что видит
пользователь в браузере."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import twogis_map_check as tm

_HEAD = ('<html><head><meta property="og:title" '
        'content="Отзывы о Стальметурал, металлобаза, ">')

# Урезанный, но структурно точный слепок панели контактов - Москва (СМУ).
# Ссылка на сайт - через аналитический редирект link.2gis.ru, домен виден
# только как ТЕКСТ ссылки (сама href - непрозрачный base64-пейлоад).
_MOSCOW_PANEL = """
<div class="_8sgdp4"><div class="_599hh" data-rack="true">
<div class="_172gbf8"><div class="_49kxlr"><div><div>
<span class="_14quei">
  <span class="_wrdavn">МФДЦ Марьино</span>
  <span class="_wrdavn"><a href="/moscow/geo/4504235282599393" class="_2lcm958">Люблинская улица,&nbsp;151</a></span>
</span>
<div class="_1p8iqzw">Марьино, Москва, 109341</div>
</div></div></div></div>
<div class="_172gbf8"><div class="_49kxlr">
<div class="_b0ke8"><a href="tel:+74991303669" target="_blank" class="_2lcm958">
  <bdo dir="ltr">+7 (499) 130&#8210;36&#8210;69</bdo></a></div>
</div></div>
<div class="_172gbf8"><div class="_49kxlr"><span><div>
<a href="https://link.2gis.ru/4.2/28F280AF/aHR0cDovL3N0YWxtZXR1cmFsLnJ1Lw==" target="_blank" class="_1rehek">stalmetural.ru</a>
</div></span>
<div class="_2fgdxvm"><div class="_14uxmys"><span><a href="https://link.2gis.ru/x/youtube" target="_blank" class="_1rehek" aria-label="YouTube"><div class="_rdlyh2"><span class="_1dvs8n">YouTube</span></div></a></span></div>
<div class="_14uxmys"><span><a href="https://link.2gis.ru/x/vk" target="_blank" class="_1rehek" aria-label="ВКонтакте"><div class="_rdlyh2"><span class="_1dvs8n">ВКонтакте</span></div></a></span></div>
<div class="_14uxmys"><span><a href="https://link.2gis.ru/x/wa" target="_blank" class="_1rehek" aria-label="WhatsApp"><div class="_rdlyh2"><span class="_1dvs8n">WhatsApp</span></div></a></span></div>
</div>
</div></div>
</div></div>
"""

# Севастополь - тот же формат, короче (без МФДЦ-строки перед адресом).
_SEVASTOPOL_PANEL = """
<div class="_8sgdp4"><div class="_599hh" data-rack="true">
<div class="_172gbf8"><div class="_49kxlr"><div><div>
<span class="_3yxk2u"><a href="/sevastopol/geo/20407571502792745" class="_2lcm958">Улица Хрусталёва,&nbsp;74а</a></span>
<div class="_1p8iqzw">Гагаринский район, Севастополь</div>
</div></div></div></div>
<div class="_172gbf8"><div class="_49kxlr">
<div class="_b0ke8"><a href="tel:+74991303669" target="_blank" class="_2lcm958">
  <bdo dir="ltr">+7 (499) 130&#8210;36&#8210;69</bdo></a></div>
</div></div>
<div class="_172gbf8"><div class="_49kxlr"><span><div>
<a href="https://link.2gis.ru/4.2/216E1792/aHR0cDovL3N0YWxtZXR1cmFsLnJ1Lw==" target="_blank" class="_1rehek">stalmetural.ru</a>
</div></span></div></div>
</div></div>
"""


# Нижний Новгород - РЕАЛЬНЫЙ фрагмент карточки (прислал пользователь). Слаг
# города с ПОДЧЁРКИВАНИЕМ (n_novgorod), не с дефисом - раньше regex на
# geo-ссылку не пропускал "_" и адрес считался отсутствующим на карточке.
_NNOVGOROD_PANEL = """
<div class="_8sgdp4"><div class="_599hh" data-rack="true">
<div class="_172gbf8" data-divider="true" data-divider-shifted="true">
<div class="_1iftozu"><svg></svg></div>
<div class="_49kxlr"><div class="_1ovqm446"><div class="_z3fqkm"><svg></svg></div>
<div><div>
<span class="_14quei">
  <span class="_wrdavn">​БЦ Муравей</span>
  <span class="_wrdavn">​<a href="/n_novgorod/geo/2674647933923077" class="_2lcm958">Рождественская,&nbsp;13</a></span>
</span>
<div class="_1p8iqzw">Нижегородский район, Нижний Новгород, 603001</div>
</div>
<div class="_rtsy3"><div class="_1fpr72l"><button class="_1n1gqlj7" type="button">Показать вход</button></div></div>
</div></div></div>
</div>
<div class="_172gbf8" data-divider="true" data-divider-shifted="true">
<div class="_1iftozu"><svg></svg></div>
<div class="_49kxlr"><span><div>
<a href="https://link.2gis.ru/x/AAAA" target="_blank" class="_1rehek">n-novgorod.stalmetural.ru/</a>
</div></span></div></div>
</div></div>
"""


def test_address_with_underscore_city_slug_is_found():
    """Слаг «n_novgorod» (с подчёркиванием) - раньше regex ловил только
    дефисные слаги (rostov-na-donu) и адрес Нижнего Новгорода не находился,
    хотя он явно есть на карточке («БЦ Муравей, Рождественская, 13»)."""
    data = tm.extract(_html(_NNOVGOROD_PANEL))
    assert 'Рождественская' in data['address'] and '13' in data['address']
    assert 'Новгород' in data['address']
    print('✓ слаг города с подчёркиванием - адрес всё равно находится')


def test_nnovgorod_site_still_found_alongside_address():
    """Сайт на этой же карточке (с завершающим слэшем) находится тоже - оба
    поля должны работать одновременно, не только одно из двух."""
    data = tm.extract(_html(_NNOVGOROD_PANEL))
    assert data['site'] == 'n-novgorod.stalmetural.ru'
    print('✓ сайт на карточке Нижнего Новгорода тоже находится')


# Сургут - текст ссылки на сайт СО СЛЭШЕМ на конце (2ГИС иногда так
# рендерит) - раньше regex не совпадал из-за него, и сайт считался
# "отсутствует на карточке", хотя он там есть.
_SURGUT_PANEL = """
<div class="_8sgdp4"><div class="_599hh" data-rack="true">
<div class="_172gbf8"><div class="_49kxlr"><span><div>
<a href="https://link.2gis.ru/4.2/AAAA0000/aHR0cDovL3N1cmd1dC5zdGFsbWV0dXJhbC5ydS8=" target="_blank" class="_1rehek">surgut.stalmetural.ru/</a>
</div></span></div></div>
</div></div>
"""


def _html(panel):
    return _HEAD + '</head><body>' + panel + '</body></html>'


def test_site_with_trailing_slash_in_link_text_is_still_found():
    """Ссылка показывает текст «surgut.stalmetural.ru/» (со слэшем) - раньше
    это не проходило по regex (^...$ без допуска на «/») и сайт считался
    отсутствующим на карточке, хотя он там явно есть."""
    data = tm.extract(_html(_SURGUT_PANEL))
    assert data['site'] == 'surgut.stalmetural.ru'
    print('✓ слэш на конце текста ссылки не мешает найти сайт')


def test_moscow_site_found_as_bare_domain_not_social_link():
    data = tm.extract(_html(_MOSCOW_PANEL))
    assert data['available'] is True
    assert data['site'] == 'stalmetural.ru'
    assert data['site'] != 'YouTube' and data['site'] != 'ВКонтакте'
    print('✓ Москва: сайт найден как домен, соцсети (YouTube/VK/WA) не спутаны с ним')


def test_moscow_phone_from_tel_link():
    data = tm.extract(_html(_MOSCOW_PANEL))
    assert data['phone'] == '+74991303669'
    print('✓ Москва: телефон - из tel: ссылки')


def test_moscow_address_includes_street_and_city_line():
    data = tm.extract(_html(_MOSCOW_PANEL))
    assert 'Люблинская' in data['address'] and '151' in data['address']
    assert 'Москва' in data['address']
    print('✓ Москва: адрес - улица+дом из geo-ссылки + строка «район/город»')


def test_sevastopol_site_was_previously_missing_now_found():
    """Раньше именно этот случай давал «–» вместо значения - регулярка по
    JSON не находила сайт вообще. Теперь читаем DOM панели контактов."""
    data = tm.extract(_html(_SEVASTOPOL_PANEL))
    assert data['site'] == 'stalmetural.ru'
    print('✓ Севастополь: сайт теперь находится (раньше был «–»)')


def test_sevastopol_address_uses_short_district_line():
    data = tm.extract(_html(_SEVASTOPOL_PANEL))
    assert 'Хрусталёва' in data['address'] and '74' in data['address']
    assert 'Севастополь' in data['address']
    print('✓ Севастополь: адрес разобран даже без МФДЦ-строки перед geo-ссылкой')


def test_no_contacts_panel_is_unavailable():
    data = tm.extract('<html><head></head><body><div>ничего нет</div></body></html>')
    assert data['available'] is False
    print('✓ без панели контактов и без og:title - available=False, штатный исход')
