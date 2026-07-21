"""
Страница «Скорость страниц» - проверка скорости через Google PageSpeed Insights
(Lighthouse) с разбивкой по типам страниц и сравнением с прошлым периодом.

Для выбранного проекта собирает выборку URL (по типам из каталогов проекта или
свой список), гоняет каждую страницу через PageSpeed (desktop+mobile), показывает
средние по типам с Δ к прошлому снятию, детальную таблицу и КОНКРЕТНЫЕ
рекомендации (что и где чинить). Скачивается Excel-отчёт. История прогонов
хранится локально (pagespeed_data/{project}.csv) и переносится кнопками
выгрузки/загрузки.

Прогон идёт отдельным процессом (pagespeed_run.py), как «Проверка форм» и
«Проверка КП» - страница тайлит лог и показывает прогресс.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable

OUT_ROOT = ROOT / 'cache' / 'pagespeed'
LOG_FILE = OUT_ROOT / 'run.log'
PID_FILE = OUT_ROOT / 'run.pid'

PROJECTS = {
    'smu': 'СМУ - Стальметурал', 'imp': 'ИМП - Инметпром',
    'mpe': 'МПЭ - Мепэн',
}

# цвета (в тон приложению + пороги Google)
C_GOOD, C_OK, C_POOR = '#1F9D2F', '#E08600', '#D03B3B'
C_UP, C_DOWN, C_FLAT = '#006300', '#C0392B', '#8A8781'


# ── Секреты ──────────────────────────────────────────────────────────
def _secret(key: str, default: str = '') -> str:
    try:
        if hasattr(st, 'secrets') and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default


def _api_key(pid: str) -> str:
    """Ключ PageSpeed: сперва per-project (pagespeed_api_key_<pid>), затем общий
    (pagespeed_api_key), затем PAGESPEED_API_KEY из окружения, затем то, что
    введено в поле на этой сессии."""
    return (_secret(f'pagespeed_api_key_{pid}') or _secret('pagespeed_api_key')
            or os.environ.get('PAGESPEED_API_KEY', '')
            or st.session_state.get(f'ps_key_{pid}', '')).strip()


# ── Управление фоновым процессом ─────────────────────────────────────
def _read_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == 'nt':
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                 capture_output=True, text=True).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill(pid):
    if not pid:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def _launch(args, extra_env=None):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v})
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    f = open(LOG_FILE, 'a', encoding='utf-8')
    proc = subprocess.Popen([PY, *args], cwd=str(ROOT), stdout=f,
                            stderr=subprocess.STDOUT, env=env, creationflags=flags)
    try:
        PID_FILE.write_text(str(proc.pid), encoding='utf-8')
    except Exception:
        pass
    return proc.pid


# ── Рендер результата ────────────────────────────────────────────────
def _score_badge(score) -> str:
    if score is None:
        return f'<span style="background:{C_FLAT};color:#fff;padding:2px 8px;border-radius:6px;font-weight:700">–</span>'
    color = C_GOOD if score >= 90 else (C_OK if score >= 50 else C_POOR)
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:6px;font-weight:700">{score:g}</span>')


def _delta_chip(v) -> str:
    if v is None:
        return f'<span style="color:{C_FLAT}">–</span>'
    if v > 0:
        return f'<span style="color:{C_UP};font-weight:700">▲ +{v:g}</span>'
    if v < 0:
        return f'<span style="color:{C_DOWN};font-weight:700">▼ {v:g}</span>'
    return f'<span style="color:{C_FLAT};font-weight:700">= 0</span>'


def _render_summary(data: dict):
    ov = data.get('overall', {})
    do = data.get('deltas_overall', {})
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric('🖥 Desktop AVG', ov.get('desktop_avg', '–'),
                  delta=(f"{do.get('desktop'):+g}" if do.get('desktop') is not None else None))
    with c2:
        st.metric('📱 Mobile AVG', ov.get('mobile_avg', '–'),
                  delta=(f"{do.get('mobile'):+g}" if do.get('mobile') is not None else None))
    with c3:
        st.metric('Проверено страниц', ov.get('count', '–'))
    with c4:
        bad = sum(1 for r in data.get('rows', [])
                  if isinstance(r.get('m_score'), (int, float)) and r['m_score'] < 50)
        st.metric('Mobile ниже 50', bad)

    # Сводка по типам - HTML-таблица с бейджами и стрелками
    rows_html = []
    for b in data.get('by_type', []):
        rows_html.append(
            f'<tr><td style="text-align:left"><b>{escape(str(b.get("label","")))}</b></td>'
            f'<td>{b.get("count","")}</td>'
            f'<td>{_score_badge(b.get("desktop_avg"))}</td><td>{_delta_chip(b.get("d_desktop"))}</td>'
            f'<td>{_score_badge(b.get("mobile_avg"))}</td><td>{_delta_chip(b.get("d_mobile"))}</td></tr>')
    table = (
        '<table style="width:100%;border-collapse:collapse;text-align:center">'
        '<thead><tr style="color:#5B5853;font-size:.85rem">'
        '<th style="text-align:left">Тип страницы</th><th>Кол-во</th>'
        '<th>🖥 Desktop</th><th>Δ</th><th>📱 Mobile</th><th>Δ</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>')
    st.markdown(table, unsafe_allow_html=True)


def _render_detail(data: dict):
    def _mcell(disp):
        return escape(str(disp or '–'))
    rows_html = []
    for r in data.get('rows', []):
        err = r.get('error', '')
        rows_html.append(
            f'<tr><td style="text-align:left;word-break:break-all">{escape(r.get("url",""))}'
            f'<br><span style="color:#5B5853;font-size:.8rem">{escape(r.get("label",""))}</span></td>'
            f'<td>{_score_badge(r.get("d_score"))}</td>'
            f'<td>{_mcell(r.get("d_fcp"))}</td><td>{_mcell(r.get("d_lcp"))}</td>'
            f'<td>{_mcell(r.get("d_cls"))}</td><td>{_mcell(r.get("d_tbt"))}</td>'
            f'<td>{_score_badge(r.get("m_score"))}</td>'
            f'<td>{_mcell(r.get("m_fcp"))}</td><td>{_mcell(r.get("m_lcp"))}</td>'
            f'<td>{_mcell(r.get("m_cls"))}</td><td>{_mcell(r.get("m_tbt"))}</td>'
            + (f'<td style="color:{C_POOR};font-size:.8rem">{escape(err)}</td>' if err else '<td></td>')
            + '</tr>')
    table = (
        '<div style="overflow-x:auto"><table style="border-collapse:collapse;text-align:center;font-size:.9rem">'
        '<thead><tr style="color:#5B5853;font-size:.8rem">'
        '<th style="text-align:left">Страница</th>'
        '<th>🖥</th><th>FCP</th><th>LCP</th><th>CLS</th><th>TBT</th>'
        '<th>📱</th><th>FCP</th><th>LCP</th><th>CLS</th><th>TBT</th><th>Ошибка</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table></div>')
    st.markdown(table, unsafe_allow_html=True)


def _render_recs(data: dict):
    """Список рекомендаций сплошным списком (без вложенных экспандеров - их
    нельзя вкладывать в внешний раскрывающийся блок «Что чинить»)."""
    recs = data.get('top_recs', [])
    if not recs:
        st.success('Критичных замечаний Lighthouse не найдено 👍')
        return
    for n, rec in enumerate(recs, 1):
        title = rec.get('title', '')
        pages = rec.get('pages', 0)
        savings = rec.get('savings', '')
        head = f'**{n}. {title}**  ·  на {pages} стр.' + (f'  ·  {savings}' if savings else '')
        st.markdown(head)
        items = rec.get('items', [])
        if items:
            shown = items[:10]
            lines = ['Что и где конкретно:']
            for it in shown:
                info = f' – {it["info"]}' if it.get('info') else ''
                lines.append(f'- `{escape(it.get("url",""))}`{escape(info)}')
            if len(items) > len(shown):
                lines.append(f'- …и ещё {len(items) - len(shown)} ресурс(ов)')
            st.markdown('  \n'.join(lines))
        ex = rec.get('example_pages', [])
        if ex:
            st.caption('Примеры страниц: ' + ', '.join(escape(u) for u in ex))
        if n < len(recs):
            st.divider()


def _dnum(cur, prev):
    """Δ оценки (cur-prev) или None, если чего-то нет."""
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    return round(cur - prev, 1)


def _render_all_runs(runs_agg: list):
    """Таблица ВСЕХ прогонов проекта (новые сверху): дата, кол-во страниц,
    средние desktop/mobile и Δ к предыдущему по времени прогону."""
    ordered = list(reversed(runs_agg))   # новые сверху
    rows_html = []
    for i, r in enumerate(ordered):
        ov = r['agg'].get('overall', {})
        prev_ov = ordered[i + 1]['agg'].get('overall', {}) if i + 1 < len(ordered) else {}
        cnt = ov.get('count')
        rows_html.append(
            f'<tr><td style="text-align:left"><b>{escape(PH.fmt_ts(r["run_ts"]))}</b></td>'
            f'<td>{cnt if cnt is not None else "–"}</td>'
            f'<td>{_score_badge(ov.get("desktop_avg"))}</td>'
            f'<td>{_delta_chip(_dnum(ov.get("desktop_avg"), prev_ov.get("desktop_avg")))}</td>'
            f'<td>{_score_badge(ov.get("mobile_avg"))}</td>'
            f'<td>{_delta_chip(_dnum(ov.get("mobile_avg"), prev_ov.get("mobile_avg")))}</td></tr>')
    table = (
        '<table style="width:100%;border-collapse:collapse;text-align:center">'
        '<thead><tr style="color:#5B5853;font-size:.85rem">'
        '<th style="text-align:left">Прогон</th><th>Стр.</th>'
        '<th>🖥 Desktop</th><th>Δ</th><th>📱 Mobile</th><th>Δ</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>')
    st.markdown(table, unsafe_allow_html=True)


def _render_run_compare(cur_agg: dict, base_agg: dict, ts_base: str):
    """Сверка двух выбранных прогонов: оценки текущего по типам и Δ к базовому."""
    import pagespeed_checker as PC
    cur_agg = cur_agg or {}
    deltas = PC.compute_deltas(cur_agg, base_agg or {})
    ov, do = cur_agg.get('overall', {}), deltas.get('overall', {})
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric('🖥 Desktop AVG', ov.get('desktop_avg', '–'),
                  delta=(f"{do.get('desktop'):+g}" if do.get('desktop') is not None else None))
    with c2:
        st.metric('📱 Mobile AVG', ov.get('mobile_avg', '–'),
                  delta=(f"{do.get('mobile'):+g}" if do.get('mobile') is not None else None))
    with c3:
        st.metric('Проверено страниц', ov.get('count', '–'))
    st.caption(f'Δ показан относительно базового прогона {PH.fmt_ts(ts_base)}.')

    bt, d_by = cur_agg.get('by_type', {}), deltas.get('by_type', {})
    ordered = [tc for tc in PC.TYPE_ORDER if tc in bt] + \
              [tc for tc in bt if tc not in PC.TYPE_ORDER]
    rows_html = []
    for tc in ordered:
        b = bt[tc]
        rows_html.append(
            f'<tr><td style="text-align:left"><b>{escape(PC.TYPE_LABELS.get(tc, tc))}</b></td>'
            f'<td>{b.get("count","")}</td>'
            f'<td>{_score_badge(b.get("desktop_avg"))}</td><td>{_delta_chip(d_by.get(tc,{}).get("desktop"))}</td>'
            f'<td>{_score_badge(b.get("mobile_avg"))}</td><td>{_delta_chip(d_by.get(tc,{}).get("mobile"))}</td></tr>')
    table = (
        '<table style="width:100%;border-collapse:collapse;text-align:center">'
        '<thead><tr style="color:#5B5853;font-size:.85rem">'
        '<th style="text-align:left">Тип страницы</th><th>Кол-во</th>'
        '<th>🖥 Desktop</th><th>Δ</th><th>📱 Mobile</th><th>Δ</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>')
    st.markdown(table, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════
st.markdown(
    """<style>
    [data-testid="stDownloadButton"] button { background:#1E8E3E !important;
        border:1px solid #1E8E3E !important; }
    [data-testid="stDownloadButton"] button * { color:#FFF !important; }
    </style>""", unsafe_allow_html=True)

st.title('⚡ Скорость страниц (PageSpeed Insights)')
st.caption('Google PageSpeed / Lighthouse: оценка скорости (desktop + mobile) по '
           'типам страниц, сравнение с прошлым периодом и конкретные рекомендации '
           '(что и где чинить). Отчёт - Excel. История хранится локально - файл '
           'лежит на «облачном» компьютере Streamlit, он временный: при '
           'перезапуске приложения история сбрасывается (выгружайте её, '
           'чтобы не потерять сравнение периодов).')

pid = st.selectbox('Проект', list(PROJECTS.keys()),
                   format_func=lambda k: PROJECTS[k], index=None,
                   placeholder='- выберите проект -')
if not pid:
    st.info('Выберите проект, чтобы запустить проверку скорости.')
    st.stop()

OUT_DIR = OUT_ROOT / pid

# ── Ключ PageSpeed ───────────────────────────────────────────────────
key = _api_key(pid)
with st.expander('🔑 Ключ PageSpeed API', expanded=not key):
    if key:
        st.markdown('✅ Ключ задан (из секретов/окружения).')
    else:
        st.markdown('❌ Ключ не задан. Без ключа Google даёт очень жёсткий лимит - '
                    'проверка почти наверняка упрётся в ошибку лимита.')
        st.caption('Постоянный ключ задаётся секретом `pagespeed_api_key` (или '
                   '`pagespeed_api_key_<проект>`). Получить: Google Cloud Console → '
                   'включить «PageSpeed Insights API» → Credentials → API key.')
        _typed = st.text_input('Или вставьте ключ на эту сессию', type='password',
                               key=f'ps_key_input_{pid}')
        if _typed:
            st.session_state[f'ps_key_{pid}'] = _typed.strip()
            st.rerun()
    st.caption('⚠ Если сайт блокирует зарубежные IP - серверы Google могут не '
               'открыть страницу, и проверка вернёт ошибку (ограничение PageSpeed, '
               'не скрипта).')
key = _api_key(pid)

# ── Что проверяем ────────────────────────────────────────────────────
st.divider()
st.subheader('Что проверяем')
scope = st.radio('Охват', ['Выборка по типам (из каталогов проекта)', 'Свой список URL'],
                 label_visibility='collapsed')
urls_file_arg = None
if scope.startswith('Выборка'):
    c1, c2 = st.columns(2)
    with c1:
        per_type = st.number_input('Страниц каждого типа', 1, 30, 5,
                                   help='Главная/Каталог/Категории/Фильтры (+Товары). '
                                        'PageSpeed медленный, поэтому берём выборку.')
    with c2:
        want_products = st.checkbox(
            'Включить товары', value=True,
            help='Товары берутся из базы листингов проекта '
                 '(catalogs/<проект>-products.csv), при её отсутствии - из sitemap.')
    st.caption('Проверяются главный домен проекта: главная, каталог, N категорий, '
               'N фильтров и (опц.) N товаров.')
else:
    urls_text = st.text_area('Список URL (по одному в строке)', height=160,
                             placeholder='https://stalmetural.ru/\nhttps://stalmetural.ru/catalog/armatura/')
    st.caption('Тип каждой страницы определится по адресу. Удобно для точечной '
               'перепроверки конкретных страниц.')

compare = st.selectbox('Сравнивать с периодом',
                       ['prev', 'week', 'month'], index=0,
                       format_func=lambda m: {'prev': 'прошлым прогоном',
                                              'week': 'прогоном ~неделю назад',
                                              'month': 'прогоном ~месяц назад'}[m])

# ── История: сколько снятий уже есть ─────────────────────────────────
try:
    import pagespeed_history as PH
    _runs = PH.run_timestamps(pid)
except Exception:
    PH, _runs = None, []
st.caption(f'В истории проекта: **{len(_runs)}** прошлых снятий'
           + (f' (последнее {PH.fmt_ts(_runs[-1])})' if _runs else ' - это будет первое.'))

# ── Запуск ───────────────────────────────────────────────────────────
st.divider()
alive = _pid_alive(_read_pid())
_no_urls = (not scope.startswith('Выборка')) and not (locals().get('urls_text') or '').strip()

c1, c2 = st.columns([3, 1])
with c1:
    if st.button('▶ Запустить проверку скорости', use_container_width=True,
                 disabled=alive or _no_urls):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text('', encoding='utf-8')
        args = ['pagespeed_run.py', '--project', pid, '--compare', compare]
        if scope.startswith('Выборка'):
            args += ['--scope', 'sample', '--per-type', str(int(per_type))]
            if not want_products:
                args += ['--no-products']
        else:
            uf = OUT_DIR / 'urls_input.txt'
            uf.write_text(urls_text, encoding='utf-8')
            args += ['--scope', 'list', '--urls-file', str(uf)]
        _launch(args, extra_env={'PAGESPEED_API_KEY': key})
        st.session_state['ps_started'] = datetime.now().strftime('%H:%M:%S')
        st.rerun()
with c2:
    if st.button('⛔ Отменить', use_container_width=True, disabled=not alive):
        _kill(_read_pid())
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        st.rerun()

if _no_urls:
    st.warning('Вставьте хотя бы один URL или переключитесь на «Выборку по типам».')

# ── Прогресс ─────────────────────────────────────────────────────────
st.divider()
st.subheader('Прогресс')
_log = LOG_FILE.read_text(encoding='utf-8', errors='ignore') if LOG_FILE.exists() else ''
_done = '✅ ГОТОВО' in _log or '✗ ОШИБКА' in _log
last_run = OUT_DIR / 'last_run.json'

if alive and not _done:
    import re as _re
    m = None
    for ln in _log.splitlines():
        mm = _re.search(r'\[(\d+)/(\d+)\]', ln)
        if mm:
            m = mm
    if m:
        i, n = int(m.group(1)), int(m.group(2))
        st.progress(min(i / max(n, 1), 0.99), text=f'Проверено {i} из {n} (страница × устройство)')
    else:
        st.progress(0.05, text='Готовлю выборку…')
    with st.expander('Подробный лог', expanded=True):
        st.code('\n'.join(_log.splitlines()[-200:]) or '…', language='text')
    time.sleep(2)
    st.rerun()
else:
    if st.session_state.get('ps_started'):
        st.caption(f'Последний запуск: {st.session_state["ps_started"]}')
    if _log.strip():
        with st.expander('Подробный лог', expanded=False):
            st.code('\n'.join(_log.splitlines()[-200:]), language='text')

    # ── Результат ──
    if last_run.exists():
        try:
            data = json.loads(last_run.read_text(encoding='utf-8'))
        except Exception:
            data = None
        if data and data.get('project') == pid:
            st.divider()
            _fmt = PH.fmt_ts if PH else (lambda x: x or '')
            _prev = data.get('prev_ts')
            _cmp = (f'сравнение со снятием {_fmt(_prev)}'
                    if _prev else 'первый прогон - сравнивать не с чем')
            st.markdown(f'#### Результат · {_fmt(data.get("run_ts",""))}  \n'
                        f'<span style="color:#5B5853">{_cmp}</span>', unsafe_allow_html=True)
            _render_summary(data)

            # скачать Excel
            xlsx_name = data.get('xlsx_name', '')
            xlsx_path = OUT_DIR / xlsx_name if xlsx_name else None
            if xlsx_path and xlsx_path.exists():
                st.download_button(
                    f'⬇ Скачать отчёт «{xlsx_name}»', data=xlsx_path.read_bytes(),
                    file_name=xlsx_name, use_container_width=True,
                    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

            with st.expander('📋 Детально по страницам', expanded=False):
                _render_detail(data)

            # «Что чинить в первую очередь» - один раскрывающийся список.
            _recs = data.get('top_recs', [])
            if not _recs:
                st.markdown('#### Что чинить в первую очередь')
                st.success('Критичных замечаний Lighthouse не найдено 👍')
            else:
                with st.expander(f'🔧 Что чинить в первую очередь · {len(_recs)} замеч.',
                                 expanded=False):
                    _render_recs(data)
    else:
        st.caption('Результат появится после запуска.')

# ── Все прогоны: обзор и сверка любых двух периодов ───────────────────
st.divider()
with st.expander('📊 Все прогоны – сравнение со всеми прошлыми снятиями'):
    runs_agg = PH.all_run_aggregates(pid) if PH is not None else []
    if not runs_agg:
        st.caption('Пока нет ни одного прогона в истории этого проекта – '
                   'запустите проверку или загрузите историю ниже.')
    else:
        st.caption(f'Всего снятий в истории: **{len(runs_agg)}**. Каждая строка – '
                   'отдельный прогон и его изменение к предыдущему по времени.')
        _render_all_runs(runs_agg)

        if len(runs_agg) >= 2:
            st.markdown('#### Сверить любые два прогона')
            st.caption('Выберите базовый прогон и прогон для сравнения – увидите Δ по '
                       'типам страниц между ними.')
            ts_list = [r['run_ts'] for r in runs_agg]
            agg_map = {r['run_ts']: r['agg'] for r in runs_agg}
            csel1, csel2 = st.columns(2)
            with csel1:
                base_ts = st.selectbox('База (с чем сравниваем)', ts_list,
                                       index=len(ts_list) - 2,
                                       format_func=PH.fmt_ts, key=f'ps_cmp_base_{pid}')
            with csel2:
                cur_ts = st.selectbox('Прогон', ts_list, index=len(ts_list) - 1,
                                      format_func=PH.fmt_ts, key=f'ps_cmp_cur_{pid}')
            _render_run_compare(agg_map.get(cur_ts), agg_map.get(base_ts), base_ts)

# ── История: перенос ─────────────────────────────────────────────────
st.divider()
with st.expander('🗂 История прогонов (перенос между машинами)'):
    st.caption('История хранится локально в pagespeed_data/. На облаке файловая '
               'система эфемерна - выгружайте историю, чтобы не потерять сравнение '
               'периодов, и загружайте её на другой машине.')
    if PH is not None:
        cc1, cc2 = st.columns(2)
        with cc1:
            st.download_button('⬇ Скачать историю (CSV)', data=PH.export_csv(pid),
                               file_name=f'pagespeed-history-{pid}.csv', mime='text/csv',
                               use_container_width=True)
        with cc2:
            ups = st.file_uploader('⬆ Загрузить историю (CSV) – можно несколько файлов',
                                   type=['csv'], accept_multiple_files=True,
                                   key=f'ps_up_{pid}')
            _replace = st.checkbox('Заменить целиком (иначе добавить недостающее)',
                                   key=f'ps_rep_{pid}')
            if _replace:
                st.caption('«Заменить целиком»: история очищается и наполняется '
                           'объединением всех выбранных файлов.')
            _msg_key = f'ps_up_msg_{pid}'
            if ups:
                st.caption(f'Выбрано файлов: {len(ups)}. Нажмите «Применить загрузку» – '
                           'их прогоны добавятся в историю проекта.')
            if ups and st.button(
                    'Применить загрузку', key=f'ps_apply_{pid}',
                    help='Читает выбранные CSV и добавляет прогоны в историю проекта '
                         '(pagespeed_data/). После загрузки они появятся выше – в блоке '
                         '«📊 Все прогоны» и в сравнении периодов.'):
                try:
                    total = 0
                    for i, f in enumerate(ups):
                        # первый файл – в выбранном режиме, остальные всегда домешиваем,
                        # чтобы «заменить целиком» = заменить объединением всех файлов
                        mode = ('replace' if _replace else 'merge') if i == 0 else 'merge'
                        total += PH.import_csv(pid, f.getvalue(), mode=mode)
                    runs_now = len(PH.run_timestamps(pid))
                    st.session_state[_msg_key] = (
                        'ok', f'✅ Загружено: файлов – {len(ups)}, новых строк – {total}, '
                              f'всего снятий в истории – {runs_now}. Разверните блок '
                              '«📊 Все прогоны» выше, чтобы сравнить периоды.')
                except Exception as e:  # noqa: BLE001
                    st.session_state[_msg_key] = ('err', f'Не удалось загрузить историю: {e}')
                # rerun – чтобы обновить таблицу «Все прогоны» и счётчик снятий выше.
                st.rerun()
            # Итог показываем ПОСЛЕ rerun (иначе st.rerun гасит сообщение) – он
            # переживает перезапуск через session_state и виден прямо у кнопки.
            _msg = st.session_state.pop(_msg_key, None)
            if _msg:
                kind, text = _msg
                (st.success if kind == 'ok' else st.error)(text)
                try:
                    st.toast(text, icon='✅' if kind == 'ok' else '⚠️')
                except Exception:
                    pass
