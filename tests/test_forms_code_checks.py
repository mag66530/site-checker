"""Проверки формы ПО КОДУ (requests-путь «по умолчанию по коду»).

Раньше форма, проверенная по коду, заполняла в отчёте только «Статус», а
~20 колонок оставались пустыми и в матрице показывались прочерками. Пользователь:
«Почему так много прочерков, надо уменьшить, я уверена что можно больше
проверять». Теперь по статическому HTML заполняются реальные вердикты там, где
ответ виден прямо в разметке: CSRF, согласие 2.13, выпадающие списки, типы
файлов, подсказки полей.

Тесты фиксируют:
1) полная форма → все структурные колонки становятся ✓ (а не прочерк);
2) проблемная форма → честные ✗/⚠;
3) отсутствие элемента (списка/файла) → прочерк (N/A), а не ложный вердикт;
4) CSRF учитывает SameSite-cookie (нет ложной «Нет» на защищённом сайте);
5) Set-Cookie из ответа requests разбирается для оценки SameSite.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'forms_tester'))

import test_all as t          # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402


_COL = {
    'csrf_защита': 'CSRF-защита',
    'согласие_чекбоксы': 'Наличие чек боксов согласия',
    'согласие_предустановка': 'Чек боксы согласия не предустановлены',
    'согласие_ссылка': 'Ссылка на политику',
    'согласие_обязательно': 'Без согласия не отправить',
    'выпадающие_списки': 'Выпадающие списки',
    'типы_файлов': 'Типы файлов формы',
    'подсказки': 'Подсказки полей',
}


def _form(html):
    return BeautifulSoup(html, 'html.parser').find('form')


def _символы(res):
    """{колонка_отчёта: символ_матрицы} для наглядной проверки, что прочерков нет."""
    return {_COL[k]: t._матрица_классифицировать(_COL[k], v)[0] for k, v in res.items()}


def test_полная_форма_все_колонки_галочки():
    html = '''
    <form>
      <input type=hidden name=sessid value=abc123>
      <input type=text name=name placeholder="Ваше имя">
      <input type=tel name=phone placeholder="Телефон">
      <select name=city><option value=1>Москва</option></select>
      <input type=file name=doc accept=".pdf,.jpg">
      <input type=checkbox name=agree required>
      <a href="/policy">Политика конфиденциальности</a>
      <input type=checkbox name=agree2 required>
      <button type=submit>Отправить</button>
    </form>'''
    res = t._html_структурные_проверки(_form(html), html, t.csrf_куки_инфо([]))
    симв = _символы(res)
    # Ни одного прочерка - всё стало реальными галочками.
    assert '–' not in симв.values(), симв
    assert all(s == '✓' for s in симв.values()), симв
    print('✓ полная форма: 8 колонок из прочерков → галочки', симв)


def test_проблемная_форма_честные_крестики():
    html = '''
    <form>
      <input type=text name=name>
      <select name=x></select>
      <input type=file name=doc>
      <input type=checkbox name=agree checked>
    </form>'''
    # session-cookie SameSite=None + нет токена → CSRF реально уязвим.
    куки = t.csrf_куки_инфо([{'name': 'PHPSESSID', 'sameSite': 'None'}])
    симв = _символы(t._html_структурные_проверки(_form(html), html, куки))
    assert симв['CSRF-защита'] == '✗'
    assert симв['Выпадающие списки'] == '✗'            # пустой <select>
    assert симв['Типы файлов формы'] == '✗'            # accept не задан → любые
    assert симв['Чек боксы согласия не предустановлены'] == '✗'   # checked по умолчанию
    assert симв['Ссылка на политику'] == '✗'
    assert симв['Подсказки полей'] == '⚠'              # нет placeholder
    print('✓ проблемная форма: честные ✗/⚠', симв)


def test_нет_элемента_прочерк_а_не_ложный_вердикт():
    # Ни <select>, ни <input type=file> → эти колонки прочерк (N/A), не ✗.
    html = '<form><input type=text name=q placeholder=Поиск></form>'
    res = t._html_структурные_проверки(_form(html), html, t.csrf_куки_инфо([]))
    assert res['выпадающие_списки'] == 'не найдено'
    assert res['типы_файлов'] == ''
    симв = _символы(res)
    assert симв['Выпадающие списки'] == '–'
    assert симв['Типы файлов формы'] == '–'
    print('✓ нет списка/файла → прочерк (N/A), не ложный ✗')


def test_csrf_без_сессионных_cookie_неприменим():
    # Публичная форма без токена и без сессионных cookie → CSRF неприменим («Есть»),
    # а НЕ ложная «Нет». Это паритет с браузерным csrf_вердикт.
    html = '<form><input type=text name=q></form>'
    for cookies, ожид in (
        ([], 'Есть'),                                          # нет сессий
        ([{'name': 'PHPSESSID', 'sameSite': 'Lax'}], 'Есть'),  # SameSite защищает
        ([{'name': 'PHPSESSID', 'sameSite': 'None'}], 'Нет'),  # уязвим
    ):
        res = t._html_структурные_проверки(_form(html), html, t.csrf_куки_инфо(cookies))
        assert res['csrf_защита'] == ожид, (cookies, res['csrf_защита'])
    print('✓ CSRF учитывает SameSite: нет ложной «Нет» на защищённом сайте')


def test_csrf_значение_из_скрипта_подхватывается():
    # Поле sessid пустое в разметке, но значение задаёт JS (в скрипте) → «Есть».
    html = ('<form><input type=hidden name=sessid value=""></form>'
            '<script>var x = {"sessid":"deadbeef00"};</script>')
    res = t._html_структурные_проверки(_form(html), html, t.csrf_куки_инфо([]))
    assert res['csrf_защита'] == 'Есть', res['csrf_защита']
    print('✓ значение токена из JS-скрипта подхватывается → «Есть», не «Проверить»')


def test_файл_с_accept_показывает_типы():
    html = '<form><input type=file name=d accept=".pdf,.png,.pdf"></form>'
    res = t._html_структурные_проверки(_form(html), html, t.csrf_куки_инфо([]))
    # Дубли убраны, порядок сохранён, значение не «любые».
    assert res['типы_файлов'] == '.pdf, .png'
    assert t._матрица_классифицировать('Типы файлов формы', res['типы_файлов'])[0] == '✓'
    print('✓ загрузчик с accept → перечислены типы (✓), не «любые»')


class _FakeHeaders:
    """Имитация urllib3 HTTPHeaderDict (у неё есть .getlist)."""
    def __init__(self, cookies):
        self._c = cookies

    def getlist(self, name):
        return self._c if name.lower() == 'set-cookie' else []


class _FakeRaw:
    def __init__(self, cookies):
        self.headers = _FakeHeaders(cookies)


class _FakeResp:
    def __init__(self, cookies):
        self.raw = _FakeRaw(cookies)          # response.raw.headers.getlist(...)
        self.headers = {}


def test_разбор_set_cookie_из_ответа():
    resp = _FakeResp([
        'PHPSESSID=abc; path=/; HttpOnly; SameSite=Lax',
        'ym_uid=1; SameSite=None',
    ])
    got = t._куки_из_ответа_requests(resp)
    assert {'name': 'PHPSESSID', 'sameSite': 'Lax'} in got
    assert {'name': 'ym_uid', 'sameSite': 'None'} in got
    print('✓ Set-Cookie из ответа requests разбирается (имя + SameSite)')


def test_разбор_set_cookie_пустой_ответ():
    # Нет заголовков Set-Cookie → пустой список, без исключений.
    class _R:
        raw = None
        headers = {}
    assert t._куки_из_ответа_requests(_R()) == []
    print('✓ ответ без Set-Cookie → пустой список (без падения)')


# ── Телефон принимает мусор: буквы/знаки/слишком длинный (кейс СМУ-Алматы) ──
class _FakePhone:
    """Поле телефона. keeper(val) - что поле ОСТАВИТ после вставки val (модель
    маски): без маски держит всё; с маской - только цифры до 11."""
    def __init__(self, keeper):
        self.v, self.keep = '', keeper
    def count(self):
        return 1
    def evaluate(self, js, *a):
        return {'type': 'tel', 'pattern': None, 'maxlength': -1,
                'inputmode': 'tel', 'mask': '', 'cls': ''}
    def input_value(self, timeout=0):
        return self.v
    def fill(self, val, timeout=0, force=False):
        self.v = self.keep(val)
    def type(self, val, timeout=0):
        self.v = self.keep(val)


class _FakeEmpty:
    def count(self):
        return 0
    def evaluate(self, *a, **k):
        return {}
    def input_value(self, *a, **k):
        return ''
    def fill(self, *a, **k):
        pass
    def type(self, *a, **k):
        pass


_FakeEmpty.first = _FakeEmpty()


class _FakeScope:
    def __init__(self, phone):
        self._p = phone
    def locator(self, sel):
        if 'tel' in sel and 'phone' in sel:            # phone_sel
            w = type('W', (), {})()
            w.first = self._p
            return w
        return _FakeEmpty()


def test_телефон_принимает_буквы_и_знаки_с_примером():
    # Поле без маски держит буквы/знаки (кейс СМУ-Алматы) → флаг + КОНКРЕТНЫЙ
    # пример «вписали … → осталось …» прямо в тексте.
    r = t.проверка_полей_форм(_FakeScope(_FakePhone(lambda v: v)), None)
    assert r['телефон_мусор_принят'] is True
    d = r['телефон_детали']
    assert 'можно вписать ЛИШНИЕ символы' in d
    assert 'вписали «абв 12 !@#»' in d and 'осталось' in d   # пример на месте
    print('✓ поле принимает буквы/знаки → флаг + пример «вписали … → осталось …»')


def test_телефон_с_маской_не_флагуется():
    # Маска держит только цифры до 11 → ни букв/знаков, ни переполнения → чисто.
    def маска(v):
        return ''.join(c for c in v if c.isdigit())[:11]
    r = t.проверка_полей_форм(_FakeScope(_FakePhone(маска)), None)
    assert r['телефон_мусор_принят'] is False
    assert 'можно вписать ЛИШНИЕ символы' not in r['телефон_детали']
    print('✓ поле с маской (только цифры ≤11) → мусор не принят (нет ложного флага)')


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v', '-s']))
