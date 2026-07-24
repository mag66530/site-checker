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
    @import url('https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=Hanken+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

    /* ── Единая серо-бежевая тема (как в «Проверке форм»), для ВСЕХ вкладок ── */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    section.main {
        background-color: #F3F2EE !important;
    }
    [data-testid="stHeader"] { background: transparent !important; }

    html, body, .stApp, .stMarkdown, p, span, label,
    div[data-testid="stMarkdownContainer"], [data-baseweb="select"], li {
        font-family: 'Hanken Grotesk', system-ui, -apple-system, sans-serif;
    }
    h1, h2, h3, h4 {
        font-family: 'Newsreader', Georgia, serif !important;
        font-weight: 500 !important;
        letter-spacing: -0.01em;
        color: #1A1A1A !important;
    }
    h1 { font-size: 2.4rem !important; }
    .stApp, .stApp p, .stApp span, .stApp div, .stApp label, .stApp li,
    [data-testid="stMarkdownContainer"] { color: #1A1A1A; }
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {
        color: #5B5853 !important;
    }

    /* Единые отступы контента от боковой панели - одинаково на всех вкладках */
    .block-container {
        padding-top: 2.6rem !important;
        max-width: 1280px !important;
    }

    /* Уведомления info/success/warning/error */
    [data-testid="stAlert"] { background-color: #ECEAE4 !important; }
    [data-testid="stAlert"] * { color: #1A1A1A !important; }

    /* Кнопки */
    .stButton > button, .stDownloadButton > button {
        font-family: 'Hanken Grotesk', sans-serif; font-weight: 600;
        border-radius: 10px; padding: 0.6rem 1.1rem;
        transition: background .18s, transform .15s;
    }
    /* Тёмные (primary) кнопки. Streamlit меняет разметку кнопок между версиями
       (kind="primary" → data-testid="stBaseButton-primary"), поэтому ловим и
       через [data-testid*="rimary"]. */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid*="rimary"] {
        background: #1A1A1A !important; color: #fff !important; border: 1px solid #1A1A1A !important;
    }
    /* Текст и иконка ВНУТРИ тёмной кнопки - всегда белые. Без этого правило
       .stApp div{color:#1A1A1A} перебивало наследование и текст выходил
       чёрным на чёрном (кнопка «Запустить», «Сохранить шаблон» и т.п.). */
    .stButton > button[kind="primary"] *,
    .stButton > button[data-testid*="rimary"] * {
        color: #fff !important; fill: #fff !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid*="rimary"]:hover {
        background: #000 !important; transform: translateY(-1px);
    }
    /* Светлые (secondary) кнопки + скачивание: прозрачный фон, тёмный текст. */
    .stButton > button[kind="secondary"],
    .stButton > button[data-testid*="econdary"],
    .stDownloadButton > button {
        background: transparent !important; color: #1A1A1A !important;
        border: 1px solid rgba(26,26,26,.18) !important;
    }
    .stButton > button[kind="secondary"] *,
    .stButton > button[data-testid*="econdary"] * {
        color: #1A1A1A !important;
    }
    .stButton > button[kind="secondary"]:hover,
    .stButton > button[data-testid*="econdary"]:hover,
    .stDownloadButton > button:hover {
        background: rgba(26,26,26,.05) !important;
    }
    /* Кнопки отправки формы (st.form_submit_button): без этих правил текст
       невидим - тёмный на тёмном фоне. Приводим к обычному светлому виду. */
    [data-testid="stFormSubmitButton"] > button {
        background: transparent !important; color: #1A1A1A !important;
        border: 1px solid rgba(26,26,26,.18) !important;
        font-family: 'Hanken Grotesk', sans-serif; font-weight: 600;
        border-radius: 10px; padding: 0.6rem 1.1rem;
    }
    [data-testid="stFormSubmitButton"] > button:hover {
        background: rgba(26,26,26,.05) !important;
    }
    [data-testid="stFormSubmitButton"] > button * { color: #1A1A1A !important; }

    /* Поля, список, редактор */
    [data-baseweb="select"] > div, .stTextArea textarea, .stTextInput input {
        background: #FFFFFF !important; border-radius: 10px !important;
        border-color: rgba(26,26,26,.14) !important; color: #1A1A1A !important;
    }
    /* Числовые поля (Параметры выборки) - белые, чтобы не сливались с фоном */
    [data-testid="stNumberInput"] input {
        background: #FFFFFF !important; border-radius: 10px !important;
        border-color: rgba(26,26,26,.14) !important; color: #1A1A1A !important;
    }
    /* Кнопки-стрелки +/- - мягкие, в тон теме (не чёрные) */
    [data-testid="stNumberInput"] button {
        background: #ECEAE4 !important;
        border: 1px solid rgba(26,26,26,.12) !important;
        color: #1A1A1A !important;
        width: 42px !important;
        border-radius: 8px !important;
    }
    [data-testid="stNumberInput"] button:hover {
        background: #DEDBD4 !important;
    }
    /* Иконки +/- - ТОЛЬКО сами степперы (по их testid), чтобы заливка не
       попадала на значок подсказки «?», который тоже лежит в <button>. */
    [data-testid="stNumberInputStepUp"] svg, [data-testid="stNumberInputStepUp"] svg path,
    [data-testid="stNumberInputStepDown"] svg, [data-testid="stNumberInputStepDown"] svg path {
        fill: #1A1A1A !important; stroke: none !important;
    }

    /* Значок подсказки «?» убираем совсем - он выглядел «кружком в кружке» и
       размазанно, а подписи полей и так понятны. Если где-то нужна подсказка -
       вынесем текстом, а не иконкой. */
    [data-testid="stTooltipIcon"] { display: none !important; }
    /* Английская техподсказка «Press Enter to apply» у полей ввода - прячем,
       значение применяется по Enter или клику мимо поля. */
    [data-testid="InputInstructions"] { display: none !important; }
    .stTextArea textarea { font-family: 'JetBrains Mono', monospace !important; font-size: 13px !important; }

    /* Карточки-контейнеры (с рамкой) - белые, чтобы блоки не сливались с фоном */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: #FFFFFF !important;
        border: 1px solid #DEDBD4 !important;
        border-radius: 12px !important;
    }

    /* Expander */
    [data-testid="stExpander"] {
        background: #FFFFFF; border: 1px solid rgba(26,26,26,.12); border-radius: 12px;
    }
    [data-testid="stExpander"] summary { font-weight: 600; }

    /* Таблица */
    [data-testid="stDataFrame"] { border: 1px solid rgba(26,26,26,.12); border-radius: 12px; }

    /* Код/лог */
    [data-testid="stCode"], pre, code { font-family: 'JetBrains Mono', monospace !important; }
    [data-testid="stCode"] { border-radius: 12px; }

    /* Select крупнее и чётче */
    div[data-baseweb="select"] > div { min-height: 48px; display: flex !important; align-items: center !important; }
    div[data-baseweb="select"] > div > div {
        font-size: 16px !important; color: #1A1A1A !important; font-weight: 500 !important;
    }
    /* Выпадающий список селекта: тёмный читаемый текст опций (и вложенных
       span'ов!). Без правила на «li *» текст опций оставался бледным. */
    ul[role="listbox"] li, li[role="option"], div[role="option"],
    div[data-baseweb="popover"] li, div[data-baseweb="popover"] [role="option"],
    div[data-baseweb="popover"] ul > li {
        font-size: 15.5px !important; color: #1A1A1A !important;
        background: #FFFFFF !important;
        padding-top: 9px !important; padding-bottom: 9px !important;
    }
    ul[role="listbox"] li *, li[role="option"] *,
    div[data-baseweb="popover"] li *,
    div[data-baseweb="popover"] [role="option"] * {
        color: #1A1A1A !important; background: transparent !important;
    }
    /* Список - одна белая рамка, без «матрёшки» вложенных рамок popover */
    div[data-baseweb="popover"] ul, div[data-baseweb="popover"] [role="listbox"] {
        background: #FFFFFF !important; border: 1px solid #DEDBD4 !important;
        border-radius: 8px !important; box-shadow: 0 8px 24px rgba(26,26,26,.12) !important;
    }
    /* Наведение / выбранная опция - мягкая подсветка, текст остаётся тёмным */
    div[data-baseweb="popover"] li:hover, div[data-baseweb="popover"] [role="option"]:hover,
    div[data-baseweb="popover"] li[aria-selected="true"],
    div[data-baseweb="popover"] [role="option"][aria-selected="true"] {
        background: #ECEAE4 !important;
    }
    div[data-baseweb="popover"] li:hover *, div[data-baseweb="popover"] [role="option"]:hover *,
    div[data-baseweb="popover"] li[aria-selected="true"] *,
    div[data-baseweb="popover"] [role="option"][aria-selected="true"] * {
        color: #1A1A1A !important;
    }
    .stSelectbox label, .stCheckbox label, [data-testid="stWidgetLabel"] p {
        font-size: 14px !important; color: #5B5853 !important;
    }
    /* Чёткий текст: НЕ используем grayscale-сглаживание (antialiased), от него
       на Windows/Chrome буквы становятся тоньше и «размытыми». Оставляем
       субпиксельное сглаживание браузера + оптимизацию читаемости. */
    .stApp, .stApp * {
        -webkit-font-smoothing: subpixel-antialiased;
        text-rendering: optimizeLegibility;
    }

    /* ── Боковая панель ── */
    [data-testid="stSidebar"] {
        background-color: #E8E5DF !important;
        border-right: 1px solid #DEDBD4 !important;
    }
    [data-testid="stSidebar"] * { color: #1A1A1A !important; }
    /* Заголовок «Панель проверок» над пунктами навигации */
    [data-testid="stSidebarNav"]::before {
        content: "Панель проверок";
        display: block;
        font-family: 'Newsreader', Georgia, serif;
        font-size: 1.3rem;
        font-weight: 600;
        color: #1A1A1A;
        padding: 0.4rem 0.95rem 0.85rem;
    }
    [data-testid="stSidebarNav"] { padding-top: 0.6rem !important; }
    [data-testid="stSidebarNav"] a {
        padding: 0.55rem 0.95rem !important;
        border-radius: 10px !important;
        margin: 0.12rem 0.45rem !important;
    }
    [data-testid="stSidebarNav"] a span,
    [data-testid="stSidebarNav"] a p {
        font-family: 'Hanken Grotesk', sans-serif !important;
        font-size: 1.02rem !important; font-weight: 500 !important;
    }
    [data-testid="stSidebarNav"] a:hover { background-color: rgba(26,26,26,.06) !important; }
    [data-testid="stSidebarNav"] a[aria-current="page"] { background-color: rgba(26,26,26,.10) !important; }

    /* Мультиселект (напр. «Проекты» в форме входа): не обрезать названия в
       плашках-тегах - показываем целиком, переносим при нехватке места. */
    [data-baseweb="tag"] { max-width: none !important; height: auto !important; }
    [data-baseweb="tag"] span {
        max-width: none !important; overflow: visible !important;
        text-overflow: clip !important; white-space: normal !important;
    }
    /* Читаемость плашек: вместо синих с белым текстом - светлые в тон темы
       с тёмным текстом и тёмным крестиком. */
    [data-baseweb="tag"] {
        background: #E3E0D9 !important;
        border: 1px solid rgba(26,26,26,.14) !important;
    }
    [data-baseweb="tag"], [data-baseweb="tag"] * {
        color: #1A1A1A !important;
    }
    [data-baseweb="tag"] svg, [data-baseweb="tag"] svg path {
        fill: #1A1A1A !important;
    }
    /* Плейсхолдер/значение селекта не должны подрезаться сверху ("текст не
       влазит"): достаточная высота, вертикальное центрирование, нормальный
       интерлиньяж и без обрезки по высоте. */
    div[data-baseweb="select"] > div {
        min-height: 50px !important; height: auto !important;
        align-items: center !important; overflow: visible !important;
        padding-top: 4px !important; padding-bottom: 4px !important;
    }
    div[data-baseweb="select"] > div > div {
        overflow: visible !important; line-height: 1.6 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Личный кабинет: вход обязателен перед доступом к панели проверок ──
# require_login() рисует экран логина/регистрации и возвращает False, пока
# пользователь не вошёл; render_account_ui() добавляет в сайдбар блок аккаунта
# (кто я / выйти / кабинет руководителя / админ-панель). См. auth/INSTRUCTIONS.md.
import auth

try:
    _logged_in = auth.require_login()
except Exception as _auth_err:  # noqa: BLE001
    # Понятное сообщение вместо трейсбека, если секреты/подключение не настроены
    # (самая частая причина - неверная строка подключения). Так апп не пугает
    # пользователя стеком и подсказывает, что именно проверить.
    st.error(
        "⚠️ Авторизация не поднялась — проверьте секреты приложения "
        "(Streamlit Cloud → Manage app → Settings → Secrets):\n\n"
        "• `[supabase] db_url` — строка **Transaction pooler** "
        "(хост …pooler.supabase.com, порт **6543**) с реальным паролем базы "
        "вместо `[YOUR-PASSWORD]`;\n"
        "• блоки `[seed_admin]` и `[app]` присутствуют.\n\n"
        f"Тех. детали: `{_auth_err}`"
    )
    st.stop()

if not _logged_in:
    # Экран входа: НЕ оставляем боковое меню страниц от прошлого рана (после
    # выхода из аккаунта оно продолжало висеть). Пустая скрытая навигация
    # заменяет прежнюю и прячет сайдбар-меню целиком.
    def _login_screen() -> None:
        pass  # формы входа уже нарисовал require_login()

    st.navigation([st.Page(_login_screen, title='Вход')], position='hidden').run()
    st.stop()

auth.render_account_ui()

_user = auth.current_user()

# Вкладки панели по правам юзера (настраиваются в админке/кабинете; пусто в
# БД = все). Ключи и порядок — auth.APP_TABS.
_PAGE_DEFS = {
    'checklist':    ('checklists/checklist_30min.py', 'Чек-лист', '🔎'),
    'autoclickers': ('checklists/autoclickers.py', 'Автокликеры', '🖱'),
    'forms':        ('checklists/forms_check.py', 'Проверка форм', '📝'),
    'goals':        ('checklists/goals_check.py', 'Проверка целей', '🎯'),
    'kp':           ('checklists/variables_check.py', 'Проверка КП', '🗺️'),
    'pagespeed':    ('checklists/pagespeed_check.py', 'Скорость страниц', '⚡'),
}
_allowed = set(auth.live_allowed_tabs(_user))
pages = []
for _key in auth.APP_TAB_KEYS:
    if _key in _allowed and _key in _PAGE_DEFS:
        _path, _title, _icon = _PAGE_DEFS[_key]
        pages.append(st.Page(_path, title=_title, icon=_icon, default=not pages))
if not pages:   # подстраховка: совсем без страниц не оставляем
    pages = [st.Page(_PAGE_DEFS['checklist'][0], title='Чек-лист', icon='🔎',
                     default=True)]

# Кабинеты — обычные страницы навигации (переключение работает в обе стороны).
_mgmt_pages = []
if _user and _user.get('role') in ('manager', 'admin'):
    _mgmt_pages.append(st.Page(auth.manager_cabinet_page,
                               title='Кабинет руководителя', icon='🗂',
                               url_path='cabinet'))
# «Настройки проекта» видят админ, руководители и специалисты с делегированным
# правом (страница сама ре-проверяет права по живым данным).
if _user and auth.live_settings_projects(_user):
    _mgmt_pages.append(st.Page(auth.project_settings_page,
                               title='Настройки проекта', icon='🔑',
                               url_path='project-settings'))
if _user and _user.get('role') == 'admin':
    _mgmt_pages.append(st.Page(auth.admin_panel_page, title='Админ-панель',
                               icon='⚙️', url_path='admin'))

if _mgmt_pages:
    st.navigation({'Проверки': pages, 'Управление': _mgmt_pages}).run()
else:
    st.navigation(pages).run()
