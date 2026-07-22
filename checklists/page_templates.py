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


def _collect_opts(key_list, extra=None) -> dict:
    """Собирает {ключ: значение} из session_state по списку ключей (или callable,
    его возвращающему), сохраняя только JSON-безопасные значения. + extra."""
    kl = key_list() if callable(key_list) else (key_list or [])
    opts = {kk: st.session_state[kk] for kk in kl
            if kk in st.session_state and _json_ok(st.session_state[kk])}
    if extra:
        opts.update({kk: vv for kk, vv in extra.items() if _json_ok(vv)})
    return opts


def _do_save(scope, pid, name, key_list, extra=None) -> bool:
    """Пишет шаблон name на диск. Возвращает True при успехе.

    ВАЖНО: НЕ трогает виджеты и НЕ делает st.rerun(). rerun из верхнего блока
    (render_panel стоит ВВЕРХУ страницы) прервал бы прогон ДО отрисовки галочек,
    которые идут НИЖЕ - а Streamlit чистит состояние виджетов, не отрисованных в
    прерванном прогоне. Из-за этого после «Сохранить» все галочки слетали в
    дефолт. Поэтому пишем файл здесь же (синхронно, надёжно) и rerun не делаем."""
    all_t = load_all(scope, pid)
    all_t[name] = {'options': _collect_opts(key_list, extra)}
    return save_all(scope, pid, all_t)


def render_panel(scope, pid, *, on_apply=None, on_reset=None, help_text=None,
                 save_keys=None, save_extra=None):
    """Верхний блок «Проектные шаблоны». Ставить ПОСЛЕ выбора проекта и ДО
    отрисовки настроек страницы.

    save_keys — список ключей session_state (или callable, его возвращающий),
    которые надо СОХРАНИТЬ. Если задан — шаблон пишется ПРЯМО по клику «Сохранить»
    (надёжно: значения виджетов уже лежат в session_state с прошлой отрисовки, и
    не важно, дойдёт ли выполнение до commit_pending внизу — раньше во время
    прогона / зависшего PID низ страницы не рисовался и шаблон молча не
    сохранялся). Если save_keys не задан — старая двухфазная схема (commit_pending
    внизу). save_extra — доп. пары для сохранения.

    on_apply(tpl: dict) — необязательный колбэк; вызывается при «Загрузить» ПОСЛЕ
    того, как сохранённые значения проставлены в session_state (для страничных
    нюансов — напр., отфильтровать невалидные города или сбросить флаг пресета).
    После него делается st.rerun().

    on_reset() — необязательный колбэк «вернуть страницу к стандартным настройкам».
    Вызывается, когда пользователь ОЧИЩАЕТ выбор шаблона крестиком (×) в поле
    «Загрузить шаблон». Должен сбросить настройки страницы к дефолту (обычно —
    удалить из session_state ключи-виджеты / флаги инициализации, чтобы дефолты
    проставились заново). После него делается st.rerun()."""
    k = f'{scope}_{pid}'
    just_saved = st.session_state.pop(f'tpl_saved_{k}', '')
    open_flag = bool(st.session_state.pop(f'tpl_open_{k}', False))
    with st.expander('📁 Проектные шаблоны (сохранить/загрузить настройки страницы)',
                     expanded=open_flag):
        if just_saved:      # для старой двухфазной схемы (commit_pending с rerun)
            st.toast(f'Шаблон «{just_saved}» сохранён', icon='✅')
        st.caption('Как это работает: **1)** настройте страницу как нужно → '
                   '**2)** впишите название → **3)** нажмите «💾 Сохранить». '
                   'Потом эти же настройки вернёте кнопкой «Загрузить».')

        # ── СОХРАНИТЬ (идёт ПЕРВЫМ: файл пишется прямо здесь, поэтому список
        # «Загрузить» ниже сразу видит новый шаблон - без перезагрузки). ──
        st.markdown('**Сохранить текущие настройки как новый шаблон**')
        s1, s2 = st.columns([3, 1], vertical_alignment='bottom')
        new_name = s1.text_input('Название шаблона', key=f'tpl_name_{k}',
                                 label_visibility='collapsed',
                                 placeholder='Например: быстрая проверка')
        _saved_now = ''
        if s2.button('💾 Сохранить', use_container_width=True, type='primary',
                     disabled=not (new_name or '').strip(), key=f'tpl_save_{k}'):
            _nm = new_name.strip()
            if save_keys is not None:
                # Сохраняем СРАЗУ и БЕЗ st.rerun() - иначе галочки, отрисованные
                # ниже по странице, слетали бы в дефолт (см. _do_save).
                if _do_save(scope, pid, _nm, save_keys, extra=save_extra):
                    _saved_now = _nm
            else:                       # старая двухфазная схема (commit_pending)
                st.session_state[f'tpl_pending_{k}'] = _nm
                st.rerun()
        if _saved_now:
            st.success(f'✓ Готово! Шаблон «{_saved_now}» сохранён - он уже в списке '
                       '«Загрузить шаблон» ниже. Настройки на странице НЕ '
                       'изменились, галочки на месте.')
        st.caption(help_text or _HELP_DEFAULT)

        st.divider()

        # ── ЗАГРУЗИТЬ (список читаем ПОСЛЕ сохранения - уже с новым шаблоном). ──
        tpls = load_all(scope, pid)
        if tpls:
            c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment='bottom')
            pick = c1.selectbox('Загрузить шаблон', list(tpls.keys()), index=None,
                                placeholder='— выберите шаблон —',
                                key=f'tpl_pick_{k}')
            # Крестик (×) в поле «Загрузить шаблон» = «вернуть к стандартным
            # настройкам». Ловим переход «был выбран шаблон → стало пусто» и зовём
            # on_reset. (_prev - что было в поле на прошлом прогоне.)
            _prevk = f'tpl_pick_prev_{k}'
            if on_reset is not None and st.session_state.get(_prevk) and not pick:
                st.session_state[_prevk] = None
                on_reset()
                st.toast('Настройки страницы сброшены к стандартным', icon='🔄')
                st.rerun()
            st.session_state[_prevk] = pick
            if c2.button('Загрузить', use_container_width=True, disabled=not pick,
                         key=f'tpl_apply_{k}'):
                tpl = tpls.get(pick) or {}
                for _key, _val in (tpl.get('options') or {}).items():
                    st.session_state[_key] = _val
                if on_apply:
                    on_apply(tpl)
                st.toast(f'Шаблон «{pick}» загружен', icon='📥')
                st.rerun()
            if c3.button('Удалить', use_container_width=True, disabled=not pick,
                         key=f'tpl_del_{k}'):
                tpls.pop(pick, None)
                save_all(scope, pid, tpls)
                st.rerun()
            if on_reset is not None:
                st.caption('💡 Крестик (×) в поле «Загрузить шаблон» вернёт страницу '
                           'к стандартным настройкам.')
        else:
            st.caption('Пока сохранённых шаблонов нет - создайте первый выше.')


def commit_pending(scope, pid, keys, *, extra=None):
    """Нижний блок. Если жали «Сохранить» — собирает значения ключей keys из
    session_state (+ произвольный extra-словарь) и пишет шаблон. Ставить ПОСЛЕ
    отрисовки всех виджетов страницы. keys может быть списком или вызываемым,
    возвращающим список (для динамических наборов ключей)."""
    k = f'{scope}_{pid}'
    pending = st.session_state.pop(f'tpl_pending_{k}', '')
    if not pending:
        return
    _do_save(scope, pid, pending, keys, extra=extra)
    # commit_pending вызывается ВНИЗУ страницы (все виджеты уже отрисованы),
    # поэтому здесь rerun безопасен - галочки не слетят. Ставим флаг для верхнего
    # подтверждения на следующем прогоне.
    st.session_state[f'tpl_saved_{k}'] = pending
    st.session_state[f'tpl_open_{k}'] = True
    st.rerun()
