"""Общие мелкие UI-хелперы для страниц проверок (не про шаблоны - см.
page_templates.py для этого)."""
import streamlit as st


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
