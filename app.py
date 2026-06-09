import streamlit as st

st.set_page_config(
    page_title='Site Checker',
    page_icon='🔎',
    layout='wide',
    initial_sidebar_state='expanded',
)

st.markdown(
    """
    <style>
    /* ── Светлая тема для ВСЕХ страниц (общая) ── */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    section.main {
        background-color: #FFFFFF !important;
    }
    [data-testid="stHeader"] {
        background: transparent !important;
    }
    .stApp, .stApp p, .stApp span, .stApp div, .stApp label, .stApp li,
    .stApp h1, .stApp h2, .stApp h3, .stApp h4,
    [data-testid="stMarkdownContainer"] {
        color: #1E212E;
    }
    /* Уведомления info/success/warning/error — светлый фон, тёмный текст */
    [data-testid="stAlert"] {
        background-color: #EEF4FB !important;
    }
    [data-testid="stAlert"] * {
        color: #1E212E !important;
    }
    /* Поля ввода и виджеты — на светлом */
    [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-baseweb="select"] > div {
        background-color: #FFFFFF !important;
        color: #1E212E !important;
    }

    /* ── Боковая панель ── */
    [data-testid="stSidebar"] {
        background-color: #F7FBFE !important;
        border-right: 1px solid #E1E8F0 !important;
    }
    [data-testid="stSidebar"] * {
        color: #1E212E !important;
    }
    [data-testid="stSidebarNav"] {
        padding-top: 0.5rem !important;
    }
    [data-testid="stSidebarNav"] a {
        padding: 0.5rem 0.85rem !important;
        border-radius: 8px !important;
        margin: 0.1rem 0.4rem !important;
    }
    [data-testid="stSidebarNav"] a span,
    [data-testid="stSidebarNav"] a p {
        font-size: 1.05rem !important;
        font-weight: 500 !important;
    }
    [data-testid="stSidebarNav"] a:hover {
        background-color: rgba(26, 86, 232, 0.06) !important;
    }
    [data-testid="stSidebarNav"] a[aria-current="page"] {
        background-color: rgba(26, 86, 232, 0.10) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

pages = [
    st.Page('checklists/checklist_15min.py', title='Чек-лист 15 мин', icon='🔎', default=True),
    st.Page('checklists/checklist_30min.py', title='Чек-лист 30 мин', icon='📋'),
]

st.navigation(pages).run()
