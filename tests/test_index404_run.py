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


def test_resolve_sites_roots_only():
    """По одному сайту на домен - корень; город-поддомены-клоны пропускаем;
    чужой домен из общего аккаунта отсекаем."""
    account = [
        ('https:stalmetural.ru:443', 'stalmetural.ru'),
        ('https:smg.az:443', 'smg.az'),
        ('https:steelgroup.az:443', 'steelgroup.az'),
        ('https:abakan.stalmetural.ru:443', 'abakan.stalmetural.ru'),   # клон
        ('https:arhangelsk.stalmetural.ru:443', 'arhangelsk.stalmetural.ru'),
        ('https:mepen.ru:443', 'mepen.ru'),          # чужой проект
    ]
    hosts = sorted(h for _, h in m._resolve_sites(account, 'smu'))
    assert hosts == ['smg.az', 'stalmetural.ru', 'steelgroup.az']
    # ни один город-поддомен и ни один чужой домен не попал
    assert not any('abakan' in h or 'arhangelsk' in h or 'mepen' in h
                   for h in hosts)


def test_resolve_sites_keeps_subdomain_if_no_root():
    """Если у домена в аккаунте НЕТ корневого хоста - оставляем поддомен,
    чтобы домен не выпал молча."""
    account = [('https:osh.stalmetural.kg:443', 'osh.stalmetural.kg')]
    hosts = [h for _, h in m._resolve_sites(account, 'smu')]
    assert hosts == ['osh.stalmetural.kg']


def test_resolve_sites_fallback_when_no_match():
    """Если ни один сайт аккаунта не совпал с проектом - берём аккаунт как
    есть (лучше проверить, чем молча ничего не сделать)."""
    account = [('https:other.com:443', 'other.com')]
    assert m._resolve_sites(account, 'smu') == account


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
