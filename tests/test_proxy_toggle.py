"""Тесты site_access.render_proxy_toggle() - блок в сайдбаре: IP напрямую/
через прокси + чек-бокс «Прокси включён» (сессия браузера, см. докстринг
site_access.py).

Проект не использует streamlit.testing.AppTest нигде - стиль тестов здесь
такой же, как у остальных тонких UI-обёрток в сессии: подменяем st.checkbox/
st.sidebar/st.button/outbound_ip (реальный рендеринг и сеть вне рантайма
Streamlit либо no-op, либо реальный сетевой запрос - ни то, ни другое не
нужно), проверяем именно РЕШЕНИЕ функции - что она возвращает, что показывает
при отсутствии проекта/прокси, и что не падает."""
import sys
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import proxy_config
import site_access as sa


def _stub_streamlit(monkeypatch, checkbox_return: bool, capture: dict,
                    ip_direct=("1.2.3.4", 50, None), ip_proxy=("5.6.7.8", 80, None)):
    """Подменяет всё, что render_proxy_toggle трогает из Streamlit/сети:
    sidebar (no-op контекст), checkbox (подконтрольный ответ + фиксация
    disabled), markdown/caption/error (no-op, просто не должны падать),
    outbound_ip (без реальной сети)."""
    monkeypatch.setattr(st, "sidebar", nullcontext())
    monkeypatch.setattr(st, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(st, "caption", lambda *a, **k: capture.setdefault("captions", []).append(a[0]))
    monkeypatch.setattr(st, "error", lambda *a, **k: capture.setdefault("errors", []).append(a[0]))

    def _fake_checkbox(label, value=False, disabled=False, key=None, help=None):
        capture["disabled"] = disabled
        capture["label"] = label
        return checkbox_return
    monkeypatch.setattr(st, "checkbox", _fake_checkbox)
    monkeypatch.setattr(sa, "outbound_ip",
                        lambda addr: ip_proxy if addr else ip_direct)


def test_no_project_shows_hint_not_nothing(monkeypatch):
    """Без выбранного проекта раньше блок не рисовался вовсе (тихо пропадал
    из сайдбара) - теперь всегда что-то видно, с понятной причиной."""
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap)
    st.session_state.clear()
    result = sa.render_proxy_toggle(None)
    assert result is None
    assert any("проект" in c.lower() for c in cap.get("captions", []))
    print('✓ без проекта - видна подсказка, не пустота')


def test_no_saved_proxy_disables_checkbox_and_returns_none(monkeypatch):
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: None)
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap)
    st.session_state.clear()
    result = sa.render_proxy_toggle("noproxy")
    assert result is None
    assert cap["disabled"] is True
    print('✓ прокси не настроен - чек-бокс disabled, возврат None')


def test_saved_proxy_checkbox_on_returns_address(monkeypatch):
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: "http://p:1")
    monkeypatch.setattr(proxy_config, "project_use_proxy", lambda pid: False)
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=True, capture=cap)
    st.session_state.clear()
    result = sa.render_proxy_toggle("smu")
    assert result == "http://p:1"
    assert cap["disabled"] is False
    print('✓ прокси настроен, чек-бокс включён - возвращает адрес')


def test_saved_proxy_checkbox_off_returns_none(monkeypatch):
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: "http://p:1")
    monkeypatch.setattr(proxy_config, "project_use_proxy", lambda pid: True)
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap)
    st.session_state.clear()
    result = sa.render_proxy_toggle("smu")
    assert result is None
    print('✓ прокси настроен, чек-бокс выключен пользователем - возвращает None')


def test_default_state_follows_project_use_proxy_on_first_render(monkeypatch):
    """Первый заход за сессию (ключа ещё нет в session_state) - стартовое
    состояние чек-бокса берётся из use_proxy проекта, а не всегда True/False."""
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: "http://p:1")
    monkeypatch.setattr(proxy_config, "project_use_proxy", lambda pid: True)
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=True, capture=cap)
    st.session_state.clear()
    sa.render_proxy_toggle("imp")
    assert st.session_state["proxy_toggle_imp"] is True
    print('✓ стартовое состояние сессии = use_proxy проекта (True)')


def test_existing_session_value_not_overwritten(monkeypatch):
    """Пользователь уже переключил чек-бокс в этой сессии - повторная
    отрисовка страницы НЕ должна сбрасывать его обратно к дефолту."""
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: "http://p:1")
    monkeypatch.setattr(proxy_config, "project_use_proxy", lambda pid: True)  # дефолт True
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap)
    st.session_state.clear()
    st.session_state["proxy_toggle_mpe"] = False  # пользователь уже выключил
    sa.render_proxy_toggle("mpe")
    assert st.session_state["proxy_toggle_mpe"] is False, \
        'setdefault не должен перезаписывать уже выставленное значение'
    print('✓ ранее выбранное пользователем состояние не затирается дефолтом')


def test_ip_lookup_cached_not_repeated_across_reruns(monkeypatch):
    """IP - сетевой запрос; повторный вызов С ТЕМ ЖЕ состоянием чек-бокса
    (имитация rerun'а Streamlit при любом клике где угодно на странице)
    не должен снова идти в сеть - только первый раз."""
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: "http://p:1")
    monkeypatch.setattr(proxy_config, "project_use_proxy", lambda pid: False)
    cap = {}
    calls = []
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap)
    monkeypatch.setattr(sa, "outbound_ip", lambda addr: (calls.append(addr) or ("1.1.1.1", 1, None)))
    st.session_state.clear()
    sa.render_proxy_toggle("smu")
    sa.render_proxy_toggle("smu")  # имитация rerun'а, чек-бокс не менялся
    assert len(calls) == 1, f'второй rerun с тем же состоянием - из кеша: {calls}'
    print('✓ повторный rerun без смены состояния не бьёт по сети - IP из кеша')


def test_ip_refetched_automatically_when_checkbox_toggles(monkeypatch):
    """Смена состояния чек-бокса - НОВЫЙ ключ кеша, значит IP обновляется
    сам собой, без отдельной кнопки «Обновить»."""
    monkeypatch.setattr(proxy_config, "resolve_proxy", lambda pid: "http://p:1")
    monkeypatch.setattr(proxy_config, "project_use_proxy", lambda pid: False)
    calls = []
    st.session_state.clear()

    cap_off = {}
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap_off)
    monkeypatch.setattr(sa, "outbound_ip", lambda addr: (calls.append(addr) or ("1.1.1.1", 1, None)))
    sa.render_proxy_toggle("smu")  # выключен

    cap_on = {}
    _stub_streamlit(monkeypatch, checkbox_return=True, capture=cap_on)
    monkeypatch.setattr(sa, "outbound_ip", lambda addr: (calls.append(addr) or ("1.1.1.1", 1, None)))
    sa.render_proxy_toggle("smu")  # включили - другой ключ кеша

    assert len(calls) == 2, f'выкл→вкл должны дать ДВА разных запроса IP: {calls}'
    assert calls[0] is None       # выключен → без прокси
    assert calls[1] == "http://p:1"  # включён → через прокси
    print('✓ переключение чек-бокса само обновляет IP, без кнопки')


def test_resolve_proxy_exception_shows_error_not_silence(monkeypatch):
    """Раньше вызывающая страница глотала любое исключение (except: pass) -
    сайдбар просто оставался пустым без следа причины. Теперь ошибка видна."""
    def _boom(pid):
        raise RuntimeError('нет сети')
    monkeypatch.setattr(proxy_config, "resolve_proxy", _boom)
    cap = {}
    _stub_streamlit(monkeypatch, checkbox_return=False, capture=cap)
    st.session_state.clear()
    result = sa.render_proxy_toggle("smu")
    assert result is None
    assert cap.get("errors"), 'ошибка должна быть видна пользователю, не проглочена молча'
    print('✓ ошибка resolve_proxy показана явно, не проглочена')


# ── auth.fill_proxy_slot: чек-бокс дорисовывается в плейсхолдер ─────────────
# render_account_ui выполняется РАНЬШЕ скрипта страницы (app.py зовёт её до
# st.navigation().run()), поэтому не может сразу знать pid страницы - вместо
# прямой отрисовки оставляет st.empty() и запоминает его в auth.ui._proxy_slot.
# Страница, уже зная свой pid, вызывает fill_proxy_slot(pid) - он дорисовывает
# содержимое в ТО ЖЕ место (см. auth/ui.py). render_proxy_toggle НЕ открывает
# свой st.sidebar (раньше открывала - и вложенный sidebar внутри чужого
# st.empty().container() уводил курсор записи в конец сайдбара, под кнопку
# «Выйти», а не в отведённое место - см. докстринг render_proxy_toggle).


def test_fill_proxy_slot_without_render_account_ui_returns_none():
    """render_account_ui почему-то не вызывалась в этом запуске - плейсхолдера
    нет, fill_proxy_slot тихо возвращает None, не падает."""
    import auth.ui as ui
    ui._proxy_slot = None
    assert ui.fill_proxy_slot("smu") is None
    print('✓ без плейсхолдера - None, не падение')


def test_fill_proxy_slot_delegates_to_render_proxy_toggle(monkeypatch):
    """fill_proxy_slot должен вызвать render_proxy_toggle с ТЕМ ЖЕ pid и
    вернуть ровно то, что она вернула - сам никакой логики не добавляет."""
    import auth.ui as ui

    class _FakeSlot:
        def container(self):
            return nullcontext()
    ui._proxy_slot = _FakeSlot()

    captured_pid = []
    # fill_proxy_slot делает ленивый `import site_access` внутри себя - это
    # ТОТ ЖЕ объект модуля, что sa (sys.modules общий), поэтому патч sa.* виден.
    monkeypatch.setattr(sa, "render_proxy_toggle",
                        lambda pid: captured_pid.append(pid) or "http://p:1")
    result = ui.fill_proxy_slot("imp")
    assert result == "http://p:1"
    assert captured_pid == ["imp"]
    print('✓ fill_proxy_slot передаёт pid как есть и возвращает результат')
