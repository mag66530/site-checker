import streamlit as st

st.set_page_config(
    page_title='Site Checker',
    page_icon='🔎',
    layout='wide',
    initial_sidebar_state='expanded',
)

pages = [
    st.Page('checklists/checklist_15min.py', title='Чек-лист 15 мин', icon='🔎', default=True),
    st.Page('checklists/checklist_30min.py', title='Чек-лист 30 мин', icon='📋'),
]

st.navigation(pages).run()
