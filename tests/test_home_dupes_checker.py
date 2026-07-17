"""Тесты разбора дублей главной (home_dupes_checker) - без сети."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from home_dupes_checker import (  # noqa: E402
    V_ABSENT, V_CANONICAL, V_DUPLICATE, V_MAIN, V_REDIRECT, V_ERROR,
    _canonical_from_html, _classify, _is_main_url, _norm_path,
    _same_home_norm, home_variants,
)

HOME = 'https://stalmetural.ru/'


def test_norm_path_collapses_root_forms():
    assert _norm_path('/') == '/'
    assert _norm_path('//') == '/'
    assert _norm_path('/////') == '/'
    assert _norm_path('/index.php') == '/'
    assert _norm_path('/index.html') == '/'
    assert _norm_path('/catalog/') == '/catalog'


def test_same_home_norm_matches_root_variants():
    assert _same_home_norm('https://stalmetural.ru/', HOME)
    assert _same_home_norm('https://stalmetural.ru//', HOME)
    assert _same_home_norm('https://stalmetural.ru/index.php', HOME)
    # другой хост (www) - НЕ та же главная
    assert not _same_home_norm('https://www.stalmetural.ru/', HOME)
    # другая схема - НЕ та же (http vs https)
    assert not _same_home_norm('http://stalmetural.ru/', HOME)
    # внутренняя страница - не главная
    assert not _same_home_norm('https://stalmetural.ru/catalog', HOME)


def test_is_main_url_strict():
    assert _is_main_url('https://stalmetural.ru/', HOME)
    assert _is_main_url('https://stalmetural.ru', HOME)          # без слэша = корень
    # строго: // и index.php - это НЕ «главная», а кандидаты в дубли
    assert not _is_main_url('https://stalmetural.ru//', HOME)
    assert not _is_main_url('https://stalmetural.ru/index.php', HOME)
    assert not _is_main_url('https://stalmetural.ru/?dubli=1', HOME)
    assert not _is_main_url('https://www.stalmetural.ru/', HOME)


def test_variants_cover_www_scheme_index_slash_query():
    vs = home_variants(HOME)
    assert 'https://stalmetural.ru/' in vs
    assert 'https://www.stalmetural.ru/' in vs
    assert 'http://stalmetural.ru/' in vs
    assert 'https://stalmetural.ru/index.php' in vs
    assert 'https://stalmetural.ru/index.html' in vs
    assert 'https://stalmetural.ru//' in vs
    assert 'https://stalmetural.ru/?dubli=1' in vs
    assert len(vs) == len(set(vs))                                # без повторов


def test_canonical_extraction_any_attr_order():
    assert _canonical_from_html(
        '<link rel="canonical" href="https://stalmetural.ru/">', HOME
    ) == 'https://stalmetural.ru/'
    # href раньше rel, одинарные кавычки
    assert _canonical_from_html(
        "<link href='/' rel=canonical>", HOME) == 'https://stalmetural.ru/'
    assert _canonical_from_html('<p>нет каноникал</p>', HOME) is None


def test_classify_redirect_to_main_is_ok():
    v, _ = _classify('http://stalmetural.ru/', 301, 'https://stalmetural.ru/',
                     None, HOME, final='https://stalmetural.ru/')
    assert v == V_REDIRECT


def test_classify_200_with_good_canonical_is_ok():
    v, _ = _classify('https://www.stalmetural.ru/', 200, None,
                     'https://stalmetural.ru/', HOME)
    assert v == V_CANONICAL


def test_classify_200_without_canonical_is_duplicate():
    v, _ = _classify('https://www.stalmetural.ru/', 200, None, None, HOME)
    assert v == V_DUPLICATE
    # index.php с 200 и self-canonical - тоже дубль
    v2, _ = _classify('https://stalmetural.ru/index.php', 200, None,
                      'https://stalmetural.ru/index.php', HOME)
    assert v2 == V_DUPLICATE


def test_classify_main_and_absent_and_error():
    v_main, _ = _classify('https://stalmetural.ru/', 200, None, None, HOME)
    assert v_main == V_MAIN
    v_absent, _ = _classify('https://stalmetural.ru/index.php', 404, None,
                            None, HOME)
    assert v_absent == V_ABSENT
    v_err, _ = _classify('https://stalmetural.ru/', 'timeout', None, None, HOME)
    assert v_err == V_ERROR
