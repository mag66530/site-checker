"""Тесты telegram_notify.py — без реальной сети."""
import sys
sys.path.insert(0, '/home/claude/site-checker-py-current')

from telegram_notify import format_summary_message, escape_html


def test_escape_html():
    assert escape_html('') == ''
    assert escape_html('hello') == 'hello'
    assert escape_html('a < b') == 'a &lt; b'
    assert escape_html('Tom & Jerry') == 'Tom &amp; Jerry'
    assert escape_html('<script>alert(1)</script>') == '&lt;script&gt;alert(1)&lt;/script&gt;'
    print('✓ escape_html: спецсимволы экранируются')


def test_format_success():
    """Прогон без проблем — зелёная плашка."""
    msg = format_summary_message(
        project_name='СМУ — Сталметурал',
        started_at='26.05.2026 19:43',
        duration_sec=14,
        total_checks=30,
        ok_count=30,
        warn_count=0,
        err_count=0,
    )
    assert 'Прогон' in msg
    assert 'СМУ' in msg
    assert '🔴' not in msg and '✅' not in msg  # эмодзи убраны
    assert 'Проблем не найдено' in msg
    print('✓ format_summary_message: «всё ок» отображается')


def test_format_critical():
    """Прогон с ошибками — красная плашка + топ-проблем."""
    msg = format_summary_message(
        project_name='СМУ — Сталметурал',
        started_at='26.05.2026 19:43',
        duration_sec=14,
        total_checks=30,
        ok_count=24,
        warn_count=0,
        err_count=6,
        top_problems=[
            {'city': 'Москва', 'url': 'https://stalmetural.ru/catalog/broken/', 'status': '404'},
            {'city': 'Казань', 'url': 'https://kazan.stalmetural.ru/catalog/test/', 'status': '404'},
        ],
    )
    assert '🔴' not in msg  # иконка убрана
    assert 'Не работает' in msg
    assert '404' in msg
    assert 'broken' in msg  # URL остаётся в href ссылки
    assert '<a href=' in msg  # ссылки кликабельны
    assert '<code>' not in msg  # больше не сырые URL в <code>
    assert '<b>Москва</b>' in msg  # группировка по городу
    assert 'Полный отчёт' in msg
    print('✓ format_summary_message: «есть ошибки» с топ-проблемами')


def test_format_with_metrika():
    """Прогон с данными Метрики."""
    msg = format_summary_message(
        project_name='СМУ',
        started_at='26.05.2026 19:43',
        duration_sec=14,
        total_checks=30,
        ok_count=30,
        warn_count=0,
        err_count=0,
        metrika_pages_count=3,
        metrika_data_date='2026-05-25',
    )
    assert '404 из Метрики' in msg
    assert '25.05.2026' in msg
    assert '3' in msg
    print('✓ format_summary_message: данные Метрики включены')


def test_format_link_label():
    """Длинный URL уходит в href, а видимая подпись — короткий слаг."""
    long_url = 'https://example.com/' + 'x' * 200 + '/page'
    msg = format_summary_message(
        project_name='СМУ',
        started_at='26.05.2026 19:43',
        duration_sec=14,
        total_checks=1,
        ok_count=0,
        warn_count=0,
        err_count=1,
        top_problems=[{'city': 'Москва', 'url': long_url, 'status': '404'}],
    )
    assert '<a href=' in msg  # ссылка кликабельна
    assert long_url in msg  # полный URL в href
    assert 'Page' in msg  # видимая подпись — очеловеченный слаг
    assert f'>{long_url}<' not in msg  # сырой длинный URL не показывается как текст
    print('✓ format_summary_message: подпись ссылки — короткий слаг, URL в href')


def test_format_escapes_in_project_name():
    """Если в имени проекта будут спецсимволы — должны экранироваться."""
    msg = format_summary_message(
        project_name='Test <a>',
        started_at='26.05.2026 19:43',
        duration_sec=14,
        total_checks=1,
        ok_count=1,
        warn_count=0,
        err_count=0,
    )
    assert '&lt;a&gt;' in msg
    assert '<a>' not in msg  # сырой HTML не должен пройти
    print('✓ format_summary_message: спецсимволы в имени экранированы')


if __name__ == '__main__':
    test_escape_html()
    test_format_success()
    test_format_critical()
    test_format_with_metrika()
    test_format_link_label()
    test_format_escapes_in_project_name()
    print('\n✅ Все тесты telegram_notify.py прошли')
