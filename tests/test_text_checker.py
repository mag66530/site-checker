"""Тесты text_checker.py - особенно важно: URL-кодировка не должна ловиться."""
import sys
sys.path.insert(0, '/home/claude/site-checker-py')

from text_checker import find_text_issues


def test_url_encoded_not_caught():
    """Главный кейс из жалоб v0.7: %D0%97 - это кириллица в URL, не битая переменная."""
    html = (
        '<div class="online-block-wapp">'
        '<a href="https://wa.me/79031303669?text=%D0%97%D0%B4%D1%80%D0%B0%D0%B2%D1%81%D1%82%D0%B2%D1%83%D0%B9%D1%82%D0%B5">WhatsApp</a>'
        '</div>'
    )
    issues = find_text_issues(html)
    assert len(issues) == 0, f"URL-кодировка не должна ловиться, нашли: {[i.match for i in issues]}"
    print('✓ URL-кодировка %D0%XX игнорируется')


def test_real_template_issues():
    """Реальные битые переменные - должны быть найдены."""
    html = (
        '<h1>Купить трубу в городе {{city}}</h1>'
        '<p>Цена от %price% рублей</p>'
        '<p>Доставка: undefined дней</p>'
        '<p>Характеристики: [object Object]</p>'
    )
    issues = find_text_issues(html)
    patterns_found = {i.match for i in issues}
    assert '{{city}}' in patterns_found
    assert '%price%' in patterns_found
    assert 'undefined' in patterns_found
    assert '[object Object]' in patterns_found
    print(f'✓ Реальные битые переменные: найдено {len(issues)}')


def test_script_and_style_ignored():
    """Содержимое script/style - НЕ должно ловиться."""
    html = (
        '<html><body>'
        '<h1>Hello</h1>'
        '<script>var x = undefined; var s = "{{not_real}}";</script>'
        '<style>.cls { content: "%fake%"; }</style>'
        '</body></html>'
    )
    issues = find_text_issues(html)
    # В видимом тексте - только "Hello", битых нет
    assert len(issues) == 0, f"Содержимое script/style не должно ловиться: {[i.match for i in issues]}"
    print('✓ Содержимое script/style игнорируется')


def test_mix_real_and_fake():
    """Микс: и URL-кодировка, и реальные битые."""
    html = (
        '<a href="https://wa.me/?text=%D0%97%D0%B4">Click</a>'
        '<h1>В городе {{city}}</h1>'
    )
    issues = find_text_issues(html)
    matches = [i.match for i in issues]
    assert matches == ['{{city}}'], f"Должна быть только {{{{city}}}}, получили: {matches}"
    print('✓ Микс: ловит только настоящее, игнорирует URL-кодировку')


def test_min_price_variable_in_title():
    """#MIN_PRICE# (Битрикс-шаблон мета) - незаменённая переменная
    минимальной цены, должна ловиться и в <title>, и в тексте."""
    html = (
        '<html><head><title>Лист горячекатанный купить, цена от '
        '#MIN_PRICE#. Прокат | Стальметурал</title></head>'
        '<body><h1>Лист</h1><p>от #MIN_PRICE# за тонну, #ЦЕНА_ОПТ#</p>'
        '</body></html>'
    )
    issues = find_text_issues(html)
    matches = [i.match for i in issues]
    assert matches.count('#MIN_PRICE#') == 2, matches
    assert '#ЦЕНА_ОПТ#' in matches
    assert all(i.pattern == '#ПЕРЕМЕННАЯ#' for i in issues)
    print('✓ #MIN_PRICE# ловится в title и тексте')


def test_hash_anchors_and_colors_not_caught():
    """Якоря (#top), хештеги без закрытия, hex-цвета - не переменные."""
    html = (
        '<p>Перейти к #top разделу. Цвет #fff и #ff0000. '
        'Хештег #акция без закрытия. Номер #123# в накладной.</p>'
    )
    issues = find_text_issues(html)
    hash_hits = [i.match for i in issues if i.pattern == '#ПЕРЕМЕННАЯ#']
    assert hash_hits == [], f'Ложные срабатывания #...#: {hash_hits}'
    print('✓ Якоря/хештеги/цвета/числа в решётках не ловятся')


def test_context_is_readable():
    """Контекст должен быть из видимого текста, не из HTML."""
    html = '<div class="x"><h1>В нашем магазине {{city}} есть все товары</h1></div>'
    issues = find_text_issues(html)
    assert len(issues) == 1
    ctx = issues[0].context
    assert '<' not in ctx and '>' not in ctx
    assert '{{city}}' in ctx
    print(f'✓ Контекст читаемый: "{ctx}"')


def test_max_findings_per_pattern():
    """Не больше 5 находок на паттерн."""
    html = '<p>' + 'undefined ' * 20 + '</p>'
    issues = find_text_issues(html)
    undefined_count = sum(1 for i in issues if i.match == 'undefined')
    assert undefined_count == 5, f"Должно быть 5 находок, получили {undefined_count}"
    print('✓ Максимум 5 находок на паттерн')


def test_patterns_config():
    """Можно ограничить набор паттернов через config."""
    html = 'Test {{var}} and undefined'
    issues = find_text_issues(html, patterns_config='{{...}}')
    assert all(i.pattern == '{{...}}' for i in issues)
    assert len(issues) == 1
    print('✓ Конфиг паттернов работает')


def test_empty_input():
    """Пустой ввод не падает."""
    assert find_text_issues('') == []
    assert find_text_issues(None) == []
    print('✓ Пустой ввод не падает')


if __name__ == '__main__':
    test_url_encoded_not_caught()
    test_real_template_issues()
    test_script_and_style_ignored()
    test_mix_real_and_fake()
    test_min_price_variable_in_title()
    test_hash_anchors_and_colors_not_caught()
    test_context_is_readable()
    test_max_findings_per_pattern()
    test_patterns_config()
    test_empty_input()
    print('\n✅ Все тесты text_checker.py прошли')
