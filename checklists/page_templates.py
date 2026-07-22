"""Общие «Проектные шаблоны» для страниц проверок.

Шаблон — именованный набор настроек страницы (галочки / режимы / числа / выбор
городов), привязанный к проекту. Пароли, логины и API-ключи в шаблон НЕ пишутся —
их всегда вводят вручную.

Двухфазная схема (ограничение Streamlit: значение виджета можно проставить только
ДО его отрисовки):
  • render_panel() ставится ВВЕРХУ страницы, сразу после выбора проекта. Он
    показывает загрузку/удаление/сохранение и по «Загрузить» проставляет
    сохранённые значения в session_state и делает rerun — тогда виджеты ниже
    отрисуются уже с ними.
  • commit_pending() ставится ВНИЗУ страницы, после отрисовки всех виджетов
    (их значения к этому моменту лежат в session_state). Если жали «Сохранить» —
    собирает нужные ключи и пишет шаблон.

Хранение: cache/templates/<scope>/<pid>/templates.json — как у форм: переживает
перезагрузку страницы, но может сброситься при перезапуске приложения.
"""
from pathlib import Path
import json

import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent

_HELP_DEFAULT = (
    'Шаблон запоминает все галочки и режимы этой страницы (кроме паролей, '
    'логинов и API-ключей). Хранится на сервере проекта **до перезапуска '
    'приложения** — после может сброситься.')


def _file(scope: str, pid) -> Path:
    return _ROOT / 'cache' / 'templates' / str(scope) / str(pid) / 'templates.json'


def load_all(scope, pid) -> dict:
    """Все шаблоны проекта: {имя: {'options': {ключ: значение}}}."""
    try:
        return json.loads(_file(scope, pid).read_text(encoding='utf-8')) or {}
    except Exception:
        return {}


def save_all(scope, pid, data: dict) -> bool:
    try:
        f = _file(scope, pid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding='utf-8')
        return True
    except Exception:
        return False


def _json_ok(value) -> bool:
    """Сохраняем только то, что переживёт JSON (str/int/float/bool/list/dict/None).
    Даты, файлы и прочие объекты тихо пропускаем — иначе весь шаблон не сохранится."""
    try:
        json.dumps(value)
        return True
    except Exception:
        return False


def render_panel(scope, pid, *, on_apply=None, help_text=None):
    """Верхний блок «Проектные шаблоны». Ставить ПОСЛЕ выбора проекта и ДО
    отрисовки настроек страницы.

    on_apply(tpl: dict) — необязательный колбэк; вызывается при «Загрузить» ПОСЛЕ
    того, как сохранённые значения проставлены в session_state (для страничных
    нюансов — напр., отфильтровать невалидные города или сбросить флаг пресета).
    После него делается st.rerun()."""
    k = f'{scope}_{pid}'
    just_saved = st.session_state.pop(f'tpl_saved_{k}', '')
    open_flag = bool(st.session_state.pop(f'tpl_open_{k}', False))
    with st.expander('📁 Проектные шаблоны (сохранить/загрузить настройки страницы)',
                     expanded=open_flag):
        if just_saved:
            st.success(f'Шаблон «{just_saved}» сохранён — теперь он в списке ниже.')
        tpls = load_all(scope, pid)
        if tpls:
            c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment='bottom')
            pick = c1.selectbox('Загрузить шаблон', list(tpls.keys()), index=None,
                                placeholder='— выберите шаблон —',
                                key=f'tpl_pick_{k}')
            if c2.button('Загрузить', use_container_width=True, disabled=not pick,
                         key=f'tpl_apply_{k}'):
                tpl = tpls.get(pick) or {}
                for _key, _val in (tpl.get('options') or {}).items():
                    st.session_state[_key] = _val
                if on_apply:
                    on_apply(tpl)
                st.rerun()
            if c3.button('Удалить', use_container_width=True, disabled=not pick,
                         key=f'tpl_del_{k}'):
                tpls.pop(pick, None)
                save_all(scope, pid, tpls)
                st.rerun()
        else:
            st.caption('Пока нет сохранённых шаблонов. Настройте страницу как нужно '
                       'и сохраните текущие настройки ниже.')
        s1, s2 = st.columns([3, 1], vertical_alignment='bottom')
        new_name = s1.text_input('Сохранить текущие настройки как шаблон',
                                 key=f'tpl_name_{k}',
                                 placeholder='Например: быстрая проверка')
        if s2.button('Сохранить', use_container_width=True,
                     disabled=not (new_name or '').strip(), key=f'tpl_save_{k}'):
            st.session_state[f'tpl_pending_{k}'] = new_name.strip()
            st.rerun()
        st.caption(help_text or _HELP_DEFAULT)


def commit_pending(scope, pid, keys, *, extra=None):
    """Нижний блок. Если жали «Сохранить» — собирает значения ключей keys из
    session_state (+ произвольный extra-словарь) и пишет шаблон. Ставить ПОСЛЕ
    отрисовки всех виджетов страницы. keys может быть списком или вызываемым,
    возвращающим список (для динамических наборов ключей)."""
    k = f'{scope}_{pid}'
    pending = st.session_state.pop(f'tpl_pending_{k}', '')
    if not pending:
        return
    key_list = keys() if callable(keys) else keys
    opts = {kk: st.session_state[kk] for kk in key_list
            if kk in st.session_state and _json_ok(st.session_state[kk])}
    if extra:
        opts.update({kk: vv for kk, vv in extra.items() if _json_ok(vv)})
    all_t = load_all(scope, pid)
    all_t[pending] = {'options': opts}
    save_all(scope, pid, all_t)
    st.session_state[f'tpl_saved_{k}'] = pending
    st.session_state[f'tpl_open_{k}'] = True
    st.rerun()
