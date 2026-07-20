"""Тесты telegram_notify.py - без реальной сети."""
import os
import sys
sys.path.insert(0, '/home/claude/site-checker-py-current')

import telegram_notify
from telegram_notify import format_summary_message, escape_html, send_report_from_env


def test_escape_html():
    assert escape_html('') == ''
    assert escape_html('hello') == 'hello'
    assert escape_html('a < b') == 'a &lt; b'
    assert escape_html('Tom & Jerry') == 'Tom &amp; Jerry'
    assert escape_html('<script>alert(1)</script>') == '&lt;script&gt;alert(1)&lt;/script&gt;'
    print('✓ escape_html: спецсимволы экранируются')


def test_format_success():
    """Прогон без проблем - зелёная плашка."""
    msg = format_summary_message(
        project_name='СМУ - Стальметурал',
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
    """Прогон с ошибками - без эмодзи, блок срочных убран."""
    msg = format_summary_message(
        project_name='СМУ - Стальметурал',
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
    assert 'Не работает: <b>6</b>' in msg
    assert 'Самые срочные' not in msg  # блок срочных убран
    assert '<a href=' not in msg  # ссылок больше нет
    assert 'Полный отчёт' in msg
    print('✓ format_summary_message: «есть ошибки», блок срочных отсутствует')


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


def test_format_escapes_in_project_name():
    """Если в имени проекта будут спецсимволы - должны экранироваться."""
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


def test_send_report_from_env_skips_without_creds():
    """Без TG_BOT_TOKEN/TG_RECIPIENTS - тихо пропускаем (skipped), не падаем."""
    for k in ('TG_BOT_TOKEN', 'TG_RECIPIENTS', 'TG_PROXY'):
        os.environ.pop(k, None)
    res = send_report_from_env('СМУ', 'текст', None)
    assert res.get('skipped') is True
    assert res.get('sent') == 0
    print('✓ send_report_from_env: без кредов пропуск (skipped)')


def test_send_report_from_env_parses_recipients():
    """Получатели из TG_RECIPIENTS режутся по запятой/пробелу/;, прокси проброшен."""
    captured = {}

    def _fake(bot_token, recipients, project_name, summary_text,
              report_file=None, *, proxy_url=None, log=None, report_filename=None):
        captured.update(bot_token=bot_token, recipients=recipients,
                        proxy_url=proxy_url, project_name=project_name)
        return {'sent': len(recipients), 'failed': 0}

    orig = telegram_notify.send_run_notification
    telegram_notify.send_run_notification = _fake
    try:
        os.environ['TG_BOT_TOKEN'] = 'BOT'
        os.environ['TG_RECIPIENTS'] = '111, 222 333;444'
        os.environ['TG_PROXY'] = 'http://proxy:8080'
        res = send_report_from_env('СМУ', 'текст', None)
    finally:
        telegram_notify.send_run_notification = orig
        for k in ('TG_BOT_TOKEN', 'TG_RECIPIENTS', 'TG_PROXY'):
            os.environ.pop(k, None)

    assert captured['recipients'] == ['111', '222', '333', '444']
    assert captured['bot_token'] == 'BOT'
    assert captured['proxy_url'] == 'http://proxy:8080'
    assert res['sent'] == 4
    print('✓ send_report_from_env: получатели/прокси разобраны из окружения')


def test_send_report_from_env_custom_filename():
    """report_filename прокидывается до send_document (имя вложения в чате)."""
    import tempfile
    from pathlib import Path

    captured = {}

    def _fake_doc(bot_token, chat_id, file_path, *, caption=None, proxy_url=None,
                  parse_mode='HTML', timeout=120, filename=None):
        captured['filename'] = filename
        return {'ok': True}

    tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
    tmp.write(b'PK\x03\x04 fake xlsx')
    tmp.close()

    orig = telegram_notify.send_document
    telegram_notify.send_document = _fake_doc
    try:
        os.environ['TG_BOT_TOKEN'] = 'BOT'
        os.environ['TG_RECIPIENTS'] = '111'
        os.environ.pop('TG_PROXY', None)
        send_report_from_env('СМУ', 'текст', Path(tmp.name),
                             report_filename='Form-smu-20.07.2026.xlsx')
    finally:
        telegram_notify.send_document = orig
        os.unlink(tmp.name)
        for k in ('TG_BOT_TOKEN', 'TG_RECIPIENTS'):
            os.environ.pop(k, None)

    assert captured['filename'] == 'Form-smu-20.07.2026.xlsx'
    print('✓ send_report_from_env: имя вложения (report_filename) прокинуто')


if __name__ == '__main__':
    test_escape_html()
    test_format_success()
    test_format_critical()
    test_format_with_metrika()
    test_format_escapes_in_project_name()
    test_send_report_from_env_skips_without_creds()
    test_send_report_from_env_parses_recipients()
    test_send_report_from_env_custom_filename()
    print('\n✅ Все тесты telegram_notify.py прошли')
