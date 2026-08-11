"""Общие мелкие UI-хелперы для страниц проверок (не про шаблоны - см.
page_templates.py для этого)."""
import time
from pathlib import Path

import streamlit as st


def _мм_сс(секунд) -> str:
    с = max(int(секунд or 0), 0)
    return f'{с // 60}:{с % 60:02d}'


def estimate_badge(текст: str, подробности: str = '') -> None:
    """Прогноз времени - заметной плашкой, а не серым мелким текстом.

    Раньше это был st.caption: строку просто не замечали, и люди запускали
    часовой прогон, думая, что он на пять минут."""
    st.markdown(
        f'<div style="background:#EEF4FF;border-left:4px solid #2563EB;'
        f'border-radius:6px;padding:10px 14px;margin:6px 0 10px 0">'
        f'<span style="font-size:15px;font-weight:600;color:#1E3A8A">'
        f'⏱ Примерное время: {текст}</span>'
        + (f'<br><span style="font-size:12px;color:#475569">{подробности}</span>'
           if подробности else '')
        + '</div>',
        unsafe_allow_html=True)


def run_started_at(pid_file, log_file=None):
    """Когда начался ИДУЩИЙ прогон (unix-время) или None.

    Берём время создания pid-файла, а не session_state: прогон живёт в
    отдельном процессе и виден из любой сессии - секундомер должен работать и
    после перезагрузки страницы, и у коллеги на другом компьютере."""
    for f in (pid_file, log_file):
        try:
            p = Path(f)
            if p.is_file():
                return p.stat().st_mtime
        except Exception:
            continue
    return None


def elapsed_caption(pid_file, log_file=None, *, running: bool,
                    estimate_low=None, estimate_high=None) -> None:
    """Секундомер прогона: «идёт 4:32» и «заняло 12:07» после завершения.

    Пока прогон идёт, показываем и остаток по прогнозу - иначе непонятно,
    ждать ещё минуту или полчаса. Когда закончился, сравниваем факт с
    прогнозом: так видно, насколько оценка врёт, и её можно поправить."""
    старт = run_started_at(pid_file, log_file)
    if not старт:
        return
    if running:
        прошло = time.time() - старт
        текст = f'⏱ идёт **{_мм_сс(прошло)}**'
        if estimate_high:
            осталось = estimate_high - прошло
            if осталось > 0:
                текст += f' · осталось примерно {_мм_сс(осталось)}'
            else:
                текст += ' · дольше прогноза'
        st.caption(текст)
        return

    # Завершился: конец - время последней записи лога (pid-файл к тому моменту
    # уже мог быть удалён).
    конец = None
    try:
        p = Path(log_file) if log_file else None
        if p and p.is_file():
            конец = p.stat().st_mtime
    except Exception:
        pass
    if not конец or конец < старт:
        return
    заняло = конец - старт
    текст = f'⏱ заняло **{_мм_сс(заняло)}**'
    if estimate_low and estimate_high:
        if заняло < estimate_low:
            текст += f' (прогноз был {_мм_сс(estimate_low)}–{_мм_сс(estimate_high)} - быстрее)'
        elif заняло > estimate_high:
            текст += f' (прогноз был {_мм_сс(estimate_low)}–{_мм_сс(estimate_high)} - дольше)'
        else:
            текст += ' (уложился в прогноз)'
    st.caption(текст)


def multiselect_grows_css() -> None:
    """CSS-фикс для st.multiselect с большим числом выбранных чипов (напр.
    выбор городов): по умолчанию у BaseWeb Select один из контейнеров-предков
    держит max-height, посчитанный под ОДНУ строку - при переносе чипов на
    несколько строк они обрезались этим max-height и рисовались НАЛЕЗАЯ на
    остальную страницу (снятия одной только height недостаточно - max-height
    обрезал независимо от неё; проверено вживую через Playwright на
    изолированном стенде). :has() находит все такие контейнеры-предки
    независимо от вложенности - строка честно растягивается по высоте,
    вместо обрезки/наложения на остальной контент.

    Вызывать ПЕРЕД отрисовкой multiselect, на каждой странице, где он есть -
    инъекция CSS дешёвая и идемпотентная (можно звать хоть на каждый rerun)."""
    st.markdown(
        """<style>
        div[data-testid="stMultiSelect"] div:has(span[data-baseweb="tag"]) {
            height: auto !important;
            max-height: none !important;
            min-height: 42px;
            overflow: visible !important;
        }
        div[data-testid="stMultiSelect"] div:has(> span[data-baseweb="tag"]) {
            flex-wrap: wrap !important;
        }
        </style>""", unsafe_allow_html=True)
