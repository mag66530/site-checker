"""Выбор города в шапке и кнопка «Написать» в подвале.

Ложная находка на прогоне SHOPMET: «нет выбора города в шапке», хотя
переключатель есть - он просто подписан самим городом («Самара»), без слова
«город», и классы у сайта Tailwind-утилитарные. Аналогично кнопка связи в
подвале называется «Написать», а детектор ждал «Написать нам».
"""
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ))

from content_checker import _город_кликабелен, check_content


def _by_key(res):
    return {b.key: b for b in res.blocks}


# ── Город в шапке ───────────────────────────────────────────────────
ШАПКА_SHOPMET = (
    '<button class="group relative inline-flex items-center rounded-full">'
    '<span class="w-1.5 h-1.5"></span><span class="relative">'
    '<span class="font-medium truncate" title="Самара">Самара</span>'
    '</span></button>')


def test_кнопка_подписана_городом():
    assert _город_кликабелен(ШАПКА_SHOPMET, 'Самара') is True
    print('✓ кнопка «Самара» засчитана переключателем города')


def test_город_продублирован_внутри_кнопки():
    """У SHOPMET город стоит дважды - вторая копия для подсказки."""
    html = '<button><span>Самара</span><span>Самара</span></button>'

    assert _город_кликабелен(html, 'Самара') is True
    print('✓ «Самара Самара» - всё ещё один город, а не другая подпись')


def test_чужой_город_не_считается():
    assert _город_кликабелен(ШАПКА_SHOPMET, 'Москва') is False
    print('✓ переключатель сверяется с городом ИМЕННО этой страницы')


def test_адрес_в_кнопке_не_проходит():
    """«Москва, ул. Ленина, д. 24» - это адрес, а не выбор города."""
    html = '<button>Москва, ул. Ленина, д. 24</button>'

    assert _город_кликабелен(html, 'Москва') is False
    print('✓ кнопка с адресом не притворяется переключателем')


def test_город_вне_кликабельного_не_считается():
    html = '<div>Самара</div><span>Самара</span>'

    assert _город_кликабелен(html, 'Самара') is False
    print('✓ простое упоминание города в шапке не засчитывается')


def test_город_из_двух_слов():
    html = '<a href="#">Нижний Новгород</a>'

    assert _город_кликабелен(html, 'Нижний Новгород') is True
    print('✓ составное название города работает')


def test_город_с_косой_чертой():
    """В КП город записан как «Астана/Нур-Султан», на сайте - одной половиной."""
    assert _город_кликабелен('<button>Астана</button>',
                             'Астана/Нур-Султан') is True
    assert _город_кликабелен('<button>Нур-Султан</button>',
                             'Астана/Нур-Султан') is True
    print('✓ принимаем любую половину двойного названия')


def test_без_города_прежнее_поведение():
    """Город не передан (свой список URL) - работают слова и классы."""
    со_словом = ('<header><span>Ваш город: Москва</span></header>'
                 '<footer>x</footer>')
    без_ничего = '<header><button>Самара</button></header><footer>x</footer>'

    assert _by_key(check_content(со_словом, 'main'))['hdr_city'].present
    assert not _by_key(check_content(без_ничего, 'main'))['hdr_city'].present
    print('✓ без города проверка работает как раньше')


def test_город_прокидывается_в_проверку():
    html = f'<header>{ШАПКА_SHOPMET}</header><footer>x</footer>'

    assert not _by_key(check_content(html, 'main'))['hdr_city'].present
    assert _by_key(check_content(html, 'main', city='Самара'))['hdr_city'].present
    print('✓ параметр city включает новое правило')


# ── «Написать» в подвале ────────────────────────────────────────────
def test_короткое_написать_с_почтой():
    html = ('<header>x</header><footer>Позвонить Написать '
            'msk@shopmet.ru</footer>')

    assert _by_key(check_content(html, 'main'))['ftr_writeus'].present
    print('✓ кнопка «Написать» рядом с почтой засчитана')


def test_короткое_написать_без_почты_не_считается():
    """Без почты в подвале «написать» может быть «написать отзыв»."""
    html = '<header>x</header><footer>Написать отзыв о компании</footer>'

    assert not _by_key(check_content(html, 'main'))['ftr_writeus'].present
    print('✓ «написать отзыв» без почты не сходит за кнопку связи')


def test_полная_фраза_как_раньше():
    html = '<header>x</header><footer>Написать нам</footer>'

    assert _by_key(check_content(html, 'main'))['ftr_writeus'].present
    print('✓ прежняя формулировка «Написать нам» работает без почты')
