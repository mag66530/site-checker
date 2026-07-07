"""Тесты run_scheduled - сборка creds/params для автозапуска из окружения."""
import run_scheduled as rs


def test_recipients_parsing(monkeypatch):
    monkeypatch.setenv('telegram_recipients_imp', '111, 222 333;444')
    assert rs._recipients('imp') == ['111', '222', '333', '444']
    monkeypatch.delenv('telegram_recipients_imp', raising=False)
    assert rs._recipients('imp') == []


def test_build_creds_from_env(monkeypatch):
    monkeypatch.setenv('proxy_url', 'http://u:p@h:8080')
    monkeypatch.setenv('telegram_bot_token', 'BOT')
    monkeypatch.setenv('telegram_recipients_smu', '999')
    monkeypatch.setenv('metrika_smu_email', 'm@ya.ru')
    monkeypatch.setenv('metrika_smu_password', 'pw')
    monkeypatch.setenv('yandex_oauth_smu', 'TOK')
    c = rs.build_creds('smu')
    assert c['proxy_url'] == 'http://u:p@h:8080'
    assert c['tg_token'] == 'BOT'
    assert c['tg_recipients'] == ['999']
    assert c['metrika'] == ('m@ya.ru', 'pw')
    # yab/twogis используют ту же почту Метрики + свою папку
    assert c['yab'][:2] == ('m@ya.ru', 'pw') and c['yab'][2] == 'Я.Бизнес'
    assert c['twogis'][2] == '2ГИС'
    assert c['webmaster_oauth'] == 'TOK'


def test_build_creds_empty_when_no_env(monkeypatch):
    for k in ('proxy_url', 'telegram_bot_token', 'telegram_recipients_mpe',
              'metrika_mpe_email', 'metrika_mpe_password', 'yandex_oauth_mpe',
              'webmaster_oauth', 'yandex_oauth'):
        monkeypatch.delenv(k, raising=False)
    c = rs.build_creds('mpe')
    assert c['proxy_url'] is None
    assert c['tg_recipients'] == []
    assert c['metrika'] == (None, None)
    assert c['webmaster_oauth'] is None


def test_build_params_profiles():
    p_std = rs.build_params('smu', 'standard', 1, True)
    assert p_std['budget'] == {'cats': 5, 'filters': 5, 'products': 3}
    assert p_std['random_cities'] == 5
    assert p_std['notify_days'] == 1 and p_std['fetch_notifications'] is True
    assert p_std['check_main'] and p_std['check_products'] and p_std['check_text']

    p_quick = rs.build_params('smu', 'quick', 7, False)
    assert p_quick['budget'] == {'cats': 3, 'filters': 3, 'products': 2}
    assert p_quick['random_cities'] == 2
    assert p_quick['notify_days'] == 7 and p_quick['fetch_notifications'] is False
