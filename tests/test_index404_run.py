"""Тесты index404_run.py - выбор сайтов из аккаунта Вебмастера (без сети).

Главное: качать только сайты, реально существующие в аккаунте Вебмастера
проекта, а не все поддомены из каталога (город-поддомены - это страницы
внутри основного домена, а не отдельные сайты).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index404_run as m


def test_registrable():
    assert m._registrable('spb.stalmetural.ru') == 'stalmetural.ru'
    assert m._registrable('smg.az') == 'smg.az'
    assert m._registrable('www.mepen.uz') == 'mepen.uz'
    assert m._registrable('novosibirsk.stalmetural.ru') == 'stalmetural.ru'


def test_host_from_site_id():
    assert m._host_from_site_id('https:smg.az:443') == 'smg.az'
    assert m._host_from_site_id('https:spb.stalmetural.ru:443') == 'spb.stalmetural.ru'


def test_resolve_sites_filters_foreign():
    """Оставляем сайты проекта (включая настоящий поддомен-сайт),
    выкидываем чужой домен из того же аккаунта."""
    account = [
        ('https:stalmetural.ru:443', 'stalmetural.ru'),
        ('https:smg.az:443', 'smg.az'),
        ('https:steelgroup.az:443', 'steelgroup.az'),
        ('https:spb.stalmetural.ru:443', 'spb.stalmetural.ru'),
        ('https:mepen.ru:443', 'mepen.ru'),          # чужой проект
    ]
    hosts = [h for _, h in m._resolve_sites(account, 'smu')]
    assert 'stalmetural.ru' in hosts and 'smg.az' in hosts
    assert 'spb.stalmetural.ru' in hosts          # настоящий поддомен-сайт проекта
    assert 'mepen.ru' not in hosts                # чужой домен отфильтрован


def test_resolve_sites_fallback_when_no_match():
    """Если ни один сайт аккаунта не совпал с проектом - берём аккаунт как
    есть (лучше проверить, чем молча ничего не сделать)."""
    account = [('https:other.com:443', 'other.com')]
    assert m._resolve_sites(account, 'smu') == account


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
