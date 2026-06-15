"""
reporter.py – формирование xlsx-отчёта.

Структура (как в Node.js версии):
  • Лист «Обзор» – метрики, сводка, параметры прогона
  • Лист «Все детали» – каждая проверка отдельной строкой
  • Лист «Битые тексты» – добавляется ТОЛЬКО если есть находки

Колонки в «Все детали»:
  Город | Поддомен | Тип | URL | Код | Статус |
  Скорость, с | Оценка скорости | Битые переменные | Откуда перешли
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter


# ── Стили (цвета как в Node.js версии) ──────────────────────────────


class C:
    text = '09090B'
    text_soft = '3F3F46'
    text_muted = '71717A'
    # Раньше border_light был 'E4E4E7' – настолько светлый, что в Excel
    # границы данных не было видно («почему в отчёте нет границ»).
    # Делаем оба варианта заметнее.
    border = 'A8B2BD'
    border_light = 'C7D0DA'
    surface = 'FAFAFA'
    bg_elev = 'FFFFFF'
    accent = '0052CC'
    accent_soft = 'EEF3FB'
    ok = '15803D'
    ok_soft = 'F0FDF4'
    warn = 'B45309'
    warn_soft = 'FFFBEB'
    err = 'B91C1C'
    err_soft = 'FEF2F2'


# Метки статусов на русском
STATUS_LABEL = {
    'ok': 'Работает',
    'redirect': 'Перенаправление',
    'not_found': 'Страница не найдена',
    'client_error': 'Ошибка на сайте',
    'server_error': 'Сервер не отвечает',
    'timeout': 'Нет ответа',
    'network_error': 'Нет соединения',
}

SPEED_LABEL = {
    'fast': 'ОК',
    'normal': 'ОК',
    'slow': 'Медленно',
    'very_slow': 'Долгий ответ сервера',
}

SPEED_COLOR = {
    'fast': C.ok,
    'normal': C.ok,
    'slow': C.warn,
    'very_slow': C.err,
}

_NOTIF_CAT_DEPT = {
    'server':    ['разработка'],
    'speed':     ['разработка'],
    'security':  ['разработка'],
    'indexing':  ['SEO'],
    'coverage':  ['SEO'],
    'structure': ['SEO'],
    'other':     ['SEO'],
}


def _dept_result(r) -> str:
    """Отдел для колонки «Отдел» листа «Все детали».

    Тег ставим ТОЛЬКО при проблеме со статусом или скоростью.
    Если статус «Работает» и скорость «ОК» – поле пустое, всё в порядке.
    (Битые переменные и контент-баги показаны в своих колонках/листе,
    здесь их не дублируем – иначе тег появлялся бы у рабочих страниц.)

    Карта:
      • сервер не отвечает / таймаут / нет соединения (5xx) → разработка
      • прочие ошибки на сайте (4xx, кроме 404)              → разработка
      • долгий ответ сервера (медленно)                      → разработка
      • 404 / страница не найдена                            → SEO
      • редиректы (предупреждение)                           → SEO
    """
    tags: list[str] = []
    if r.is_error:
        if r.status == 'not_found':
            tags.append('SEO')
        else:  # server_error, timeout, network_error, client_error
            tags.append('разработка')
    elif r.is_warning:
        tags.append('SEO')
    if r.speed_rating in ('slow', 'very_slow') and 'разработка' not in tags:
        tags.append('разработка')
    return ', '.join(dict.fromkeys(tags))


def _dept_notif(n) -> str:
    return ', '.join(_NOTIF_CAT_DEPT.get(n.category, ['SEO']))


def _font(size=10, bold=False, italic=False, underline=None, color=C.text, name='Arial'):
    return Font(
        name=name, size=size, bold=bold, italic=italic,
        underline=underline, color=color,
    )


def _border(color=C.border):
    side = Side(style='thin', color=color)
    return Border(top=side, left=side, bottom=side, right=side)


def _fill(color):
    return PatternFill(start_color=color, end_color=color, fill_type='solid')


def _align(horizontal='left', vertical='center', wrap=False, indent=1):
    return Alignment(
        horizontal=horizontal, vertical=vertical,
        wrap_text=wrap, indent=indent,
    )


# ── Описание пути для 404 ──────────────────────────────────────────


def _build_path_description(result) -> str:
    """Колонка «Откуда перешли»: пусто / прямая ссылка / цепочка редиректов."""
    chain = result.redirect_chain or []
    if not chain:
        if not result.is_ok:
            return 'Прямая ссылка из каталога (без переходов)'
        return ''

    # Цепочка редиректов: 301: from → to → to2
    steps = []
    for i, hop in enumerate(chain):
        if i == 0:
            steps.append(f"{hop['code']}: {hop['from']}")
        steps.append(f"→ {hop['to']}")
    return '  '.join(steps)


# ── Лист «Структура страниц» ───────────────────────────────────────

# Порядок и подписи групп страниц. Категории/теги делятся по факту наполнения:
# страница с товарами → «Листинг», страница-витрина/пустая → «Разделы каталога».
def _grp_listing(r):
    return (r.type_code in ('category', 'filter')
            and getattr(r.content, 'page_kind', '') == 'listing')


def _grp_section(r):
    return (r.type_code in ('category', 'filter')
            and getattr(r.content, 'page_kind', '') in ('section', 'empty'))


_STRUCT_GROUPS = [
    ('Главная',           lambda r: r.type_code == 'main'),
    ('Каталог',           lambda r: r.type_code == 'catalog'),
    ('Листинг',           _grp_listing),
    ('Разделы каталога',  _grp_section),
    ('Карточки товаров',  lambda r: r.type_code == 'product'),
    ('Прочие страницы',   lambda r: r.type_code == 'custom'),
]


# «Схлопнутые» столбцы грида: 3 столбца цены и 3 столбца кнопок сводим в один
# смысловой каждый – так таблица читается, а тип цены/кнопки виден в ячейке.

def _price_cell(bk):
    price = bk.get('price'); real = bk.get('price_real'); req = bk.get('price_request')
    if price and price.required and not price.present:
        return ('БАГ', 'bug')
    has_real = bool(real and real.present); has_req = bool(req and req.present)
    if has_real and has_req:
        return ('₽ + запрос', 'okinfo')
    if has_real:
        return ('₽', 'ok')
    if has_req:
        return ('по запросу', 'okinfo')
    if price and price.present:
        return ('✓', 'ok')
    return ('–', 'absent')


def _btn_cell(bk):
    order = bk.get('btn_order'); cart = bk.get('btn_cart'); one = bk.get('btn_oneclick')
    if order and order.required and not order.present:
        return ('БАГ', 'bug')
    has_cart = bool(cart and cart.present); has_one = bool(one and one.present)
    if has_cart and has_one:
        return ('в корзину + 1 клик', 'okinfo')
    if has_cart:
        return ('в корзину', 'okinfo')
    if has_one:
        return ('1 клик', 'okinfo')
    if order and order.present:
        return ('✓', 'ok')
    return ('–', 'absent')


_COLLAPSE = [
    {'trigger': 'price', 'label': 'Цена',
     'desc': 'Цена на карточках: «₽» – рублёвая, «по запросу» – цена по запросу. '
             '«БАГ» – цены нет вовсе.',
     'keys': {'price', 'price_real', 'price_request'}, 'fn': _price_cell},
    {'trigger': 'btn_order', 'label': 'Кнопка заказа',
     'desc': 'Кнопка заказа: «в корзину» (товар с ценой) или «1 клик» (по запросу). '
             '«БАГ» – нет ни одной.',
     'keys': {'btn_order', 'btn_cart', 'btn_oneclick'}, 'fn': _btn_cell},
]


def _grid_columns(blocks):
    """Столбцы грида: реальные блоки + схлопнутые «Цена»/«Кнопка заказа»."""
    by_trigger = {c['trigger']: c for c in _COLLAPSE}
    consumed = set().union(*(c['keys'] for c in _COLLAPSE))
    cols = []
    for b in blocks:
        if b.key in by_trigger:
            c = by_trigger[b.key]
            cols.append({'kind': 'virtual', 'label': c['label'],
                         'desc': c['desc'], 'fn': c['fn']})
        elif b.key in consumed:
            continue                       # под-блок схлопнут – пропускаем
        else:
            cols.append({'kind': 'block', 'key': b.key, 'label': b.label,
                         'desc': getattr(b, 'description', '')})
    return cols


def _cell_state(col, by_key):
    """(значение, состояние) для ячейки грида."""
    if col['kind'] == 'virtual':
        return col['fn'](by_key)
    b = by_key.get(col['key'])
    if b is None:
        return ('', 'absent')
    if b.required and not b.present:
        return ('БАГ', 'bug')
    if b.present:
        if b.count is not None:
            return (b.count, 'count')
        return ('✓', 'ok')
    return ('–', 'absent')


def _style_cell(cell, value, state):
    cell.value = value
    if state == 'bug':
        cell.font = _font(size=10, bold=True, color=C.err); cell.fill = _fill(C.err_soft)
    elif state == 'ok':
        cell.font = _font(size=10, bold=True, color=C.ok); cell.fill = _fill(C.ok_soft)
    elif state == 'okinfo':       # значение-текст (по запросу / в корзину…)
        cell.font = _font(size=9, color=C.ok)
    elif state == 'count':
        cell.font = _font(size=10, color=C.text_soft)
    else:                          # absent
        cell.value = '–'
        cell.font = _font(size=10, color=C.text_muted)


def _build_structure_sheet(wb, results):
    """Лист структурной проверки – рассчитан на читателя без подготовки."""
    pages = [r for r in results if getattr(r, 'content', None) is not None]
    if not pages:
        return

    ws = wb.create_sheet('Структура страниц')
    ws.sheet_view.showGridLines = False

    total_pages = len(pages)
    pages_with_bugs = sum(1 for r in pages if r.content_bugs > 0)
    total_bugs = sum(r.content_bugs for r in pages)
    ws.sheet_properties.tabColor = C.err if total_bugs else C.accent

    # ── Заголовок + пояснение простым языком ──
    ws.column_dimensions['A'].width = 3
    ws.merge_cells('B2:H2')
    c = ws['B2']
    c.value = 'Структура страниц'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 24

    ws.merge_cells('B3:N3')
    c = ws['B3']
    c.value = ('Проверяем, что на каждой странице есть всё нужное для продаж: заголовок, хлебные '
               'крошки, цена, кнопки заказа, формы. Красным помечено то, что НУЖНО ЧИНИТЬ. '
               'Серым прочерком – то, чего просто нет (это не ошибка). '
               'Наведите курсор на заголовок столбца – всплывёт пояснение, что именно проверяется.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 30

    # ── Сводка: три плитки ──
    tiles = [
        ('Проверено страниц', total_pages, C.accent,
         C.accent_soft),
        ('Страниц с проблемами', pages_with_bugs,
         C.err if pages_with_bugs else C.ok, C.err_soft if pages_with_bugs else C.ok_soft),
        ('Всего проблем', total_bugs,
         C.err if total_bugs else C.ok, C.err_soft if total_bugs else C.ok_soft),
    ]
    srow = 5
    col = 2
    for label, value, color, bg in tiles:
        ws.merge_cells(start_row=srow, start_column=col, end_row=srow, end_column=col + 1)
        ws.merge_cells(start_row=srow + 1, start_column=col, end_row=srow + 1, end_column=col + 1)
        vc = ws.cell(row=srow, column=col)
        vc.value = value
        vc.font = _font(size=22, bold=True, color=color)
        vc.fill = _fill(bg)
        vc.alignment = _align(horizontal='center')
        vc.border = _border(color=C.border_light)
        ws.cell(row=srow, column=col + 1).fill = _fill(bg)
        ws.cell(row=srow, column=col + 1).border = _border(color=C.border_light)
        lc = ws.cell(row=srow + 1, column=col)
        lc.value = label
        lc.font = _font(size=9, color=C.text_muted)
        lc.fill = _fill(bg)
        lc.alignment = _align(horizontal='center')
        lc.border = _border(color=C.border_light)
        ws.cell(row=srow + 1, column=col + 1).fill = _fill(bg)
        ws.cell(row=srow + 1, column=col + 1).border = _border(color=C.border_light)
        col += 3
    ws.row_dimensions[srow].height = 32

    # ── Легенда ──
    lrow = srow + 3
    lh = ws.cell(row=lrow, column=2)
    lh.value = 'Обозначения:'
    lh.font = _font(size=10, bold=True, color=C.text_soft)
    legend = [
        ('✓',     'блок есть',                                C.ok,        C.ok_soft),
        ('БАГ',   'обязательного блока нет – нужно починить', C.err,       C.err_soft),
        ('–',     'необязательного блока нет – это норма',    C.text_muted, C.surface),
        ('число', 'сколько найдено (карточек товаров, форм)', C.text_soft, C.bg_elev),
    ]
    lr = lrow + 1
    for sym, desc, color, bg in legend:
        sc = ws.cell(row=lr, column=2)
        sc.value = sym
        sc.font = _font(size=10, bold=True, color=color)
        sc.fill = _fill(bg)
        sc.alignment = _align(horizontal='center')
        sc.border = _border(color=C.border_light)
        ws.merge_cells(start_row=lr, start_column=3, end_row=lr, end_column=6)
        dc = ws.cell(row=lr, column=3)
        dc.value = desc
        dc.font = _font(size=10, color=C.text_soft)
        dc.alignment = _align(horizontal='left')
        lr += 1

    # ── «Что чинить» – понятный список проблем сразу под сводкой ──
    # Главное для читателя: не грид целиком, а короткий список «где и что
    # сломано». Грид ниже – для деталей.
    _kind_label = {'listing': 'Листинг', 'section': 'Раздел каталога',
                   'empty': 'Пустой раздел'}
    bug_pages = [r for r in pages if r.content_bugs > 0]
    row = lr + 2
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
    hc = ws.cell(row=row, column=2)
    if bug_pages:
        hc.value = f'Что чинить – {total_bugs} на {len(bug_pages)} стр.'
        hc.font = _font(size=13, bold=True, color=C.err)
    else:
        hc.value = '✓ Структурных проблем не найдено – чинить нечего'
        hc.font = _font(size=13, bold=True, color=C.ok)
    hc.fill = _fill(C.err_soft if bug_pages else C.ok_soft)
    hc.alignment = _align(indent=1)
    ws.row_dimensions[row].height = 24
    row += 1

    if bug_pages:
        # сортируем: больше всего проблем – выше
        bug_pages.sort(key=lambda r: -r.content_bugs)
        ws.column_dimensions['B'].width = 18
        for r in bug_pages[:40]:
            kind = _kind_label.get(getattr(r.content, 'page_kind', ''), r.type_label)
            if getattr(r.content, 'is_soft_404', False):
                # Страница отдала 200, но это «не найдена» — суть проблемы 404,
                # а не «нет цены». Так и пишем.
                problem_text = ('страница отдаёт 404 (не найдена) — проверить '
                                'ссылку/убрать из каталога')
            else:
                problem_text = 'нет: ' + ', '.join(b.label for b in r.content.bugs)

            cc = ws.cell(row=row, column=2, value=f'[{r.city}] {kind}')
            cc.font = _font(size=10, bold=True)
            cc.alignment = _align(indent=1)
            cc.border = _border(color=C.border_light)

            uc = ws.cell(row=row, column=3, value='открыть')
            uc.hyperlink = r.url
            uc.font = _font(size=10, color=C.accent, underline='single')
            uc.alignment = _align(horizontal='center', indent=0)
            uc.border = _border(color=C.border_light)

            ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=8)
            mc = ws.cell(row=row, column=4, value=problem_text)
            mc.font = _font(size=10, color=C.err)
            mc.alignment = _align(indent=1, wrap=True)
            mc.border = _border(color=C.border_light)
            row += 1
        if len(bug_pages) > 40:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
            ws.cell(row=row, column=2,
                    value=f'… и ещё {len(bug_pages) - 40} страниц – см. таблицы ниже').font = \
                _font(size=10, italic=True, color=C.text_muted)
            row += 1

    # ── Подробные таблицы по группам страниц ──
    row += 2
    sect_title = ws.cell(row=row, column=2, value='Подробно по типам страниц')
    sect_title.font = _font(size=12, bold=True, color=C.text_soft)
    row += 2
    for group_label, predicate in _STRUCT_GROUPS:
        group_pages = [r for r in pages if predicate(r)]
        if not group_pages:
            continue
        columns = _grid_columns(group_pages[0].content.blocks)
        n_cols = len(columns)
        g_bugs = sum(r.content_bugs for r in group_pages)

        # Заголовок секции
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4 + n_cols)
        gc = ws.cell(row=row, column=2)
        gc.value = (f'{group_label} – {len(group_pages)} стр.'
                    + (f' · проблем: {g_bugs}' if g_bugs else ''))
        gc.font = _font(size=12, bold=True, color=C.err if g_bugs else C.text)
        gc.fill = _fill(C.accent_soft)
        gc.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 22
        row += 1

        # Шапка таблицы. К каждому столбцу – комментарий-пояснение (по наведению).
        headers = (
            [('Город', ''), ('Открыть', ''), ('Проблем', '')]
            + [(c['label'], c['desc']) for c in columns]
        )
        hdr_row = row
        for ci, (h, desc) in enumerate(headers, start=2):
            cell = ws.cell(row=hdr_row, column=ci)
            cell.value = h
            cell.font = _font(size=9, bold=True, color=C.text_muted)
            cell.fill = _fill(C.surface)
            cell.alignment = _align(horizontal='center', wrap=True, indent=0)
            cell.border = _border()
            if desc:
                cell.comment = Comment(desc, 'Site Checker', height=120, width=260)
        ws.row_dimensions[hdr_row].height = 56
        row += 1

        # Строки
        for r in group_pages:
            by_key = {b.key: b for b in r.content.blocks}

            cc = ws.cell(row=row, column=2)
            cc.value = r.city
            cc.font = _font(size=10)
            cc.alignment = _align(indent=1)
            cc.border = _border(color=C.border_light)

            uc = ws.cell(row=row, column=3)
            uc.value = 'открыть'
            uc.hyperlink = r.url
            uc.font = _font(size=10, color=C.accent, underline='single')
            uc.alignment = _align(horizontal='center', indent=0)
            uc.border = _border(color=C.border_light)

            pc = ws.cell(row=row, column=4)
            pc.value = r.content_bugs if r.content_bugs else ''
            pc.font = _font(size=11, bold=True, color=C.err)
            pc.alignment = _align(horizontal='center', indent=0)
            pc.fill = _fill(C.err_soft) if r.content_bugs else _fill(C.bg_elev)
            pc.border = _border(color=C.border_light)

            # Soft-404: не сыплем БАГ по каждому столбцу — одна заметка на строку.
            if getattr(r.content, 'is_soft_404', False) and n_cols:
                ws.merge_cells(start_row=row, start_column=5,
                               end_row=row, end_column=4 + n_cols)
                cell = ws.cell(row=row, column=5,
                               value='Страница отдаёт 404 (не найдена)')
                cell.font = _font(size=10, bold=True, color=C.err)
                cell.fill = _fill(C.err_soft)
                cell.alignment = _align(indent=1)
                cell.border = _border(color=C.border_light)
                for k in range(1, n_cols):
                    ws.cell(row=row, column=5 + k).border = _border(color=C.border_light)
                row += 1
                continue

            for ci, col in enumerate(columns):
                cell = ws.cell(row=row, column=5 + ci)
                cell.alignment = _align(horizontal='center', indent=0)
                cell.border = _border(color=C.border_light)
                value, state = _cell_state(col, by_key)
                _style_cell(cell, value, state)
            row += 1
        row += 1  # пробел между секциями

    # ── Ширины колонок ──
    # Город / Открыть / Проблем, дальше – столбцы блоков. Их стало больше
    # (шапка и подвал разбиты на элементы), поэтому ширину задаём с запасом
    # по самому широкому набору столбцов на листе.
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 10
    ws.column_dimensions['D'].width = 9
    max_block_cols = max(
        (len(r.content.blocks) for r in pages if getattr(r, 'content', None)),
        default=13,
    )
    for col_idx in range(5, 5 + max_block_cols + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 13


# ── Лист уведомлений ──────────────────────────────────────────────

_NOTIF_PRIORITY_ORDER = ['critical', 'important', 'recommendation', 'info']
_NOTIF_PRIORITY_LABEL = {
    'critical':       '🔴 Критические',
    'important':      '🟠 Важные',
    'recommendation': '🟡 Рекомендации',
    'info':           '⚪ Инфо',
}
_NOTIF_PRIORITY_COLOR = {
    'critical':       C.err,
    'important':      C.warn,
    'recommendation': 'CA8A04',
    'info':           C.text_muted,
}
_NOTIF_PRIORITY_BG = {
    'critical':       C.err_soft,
    'important':      C.warn_soft,
    'recommendation': 'FEFCE8',
    'info':           C.surface,
}
_NOTIF_CATEGORY_LABEL = {
    'server':     'Сервер',
    'indexing':   'Индексирование',
    'speed':      'Скорость',
    'security':   'Безопасность',
    'structure':  'Структура',
    'coverage':   'Покрытие',
    'other':      'Прочее',
}

# Секции в порядке убывания релевантности:
# (source_key, title, has_priority)
_NOTIF_SECTIONS = [
    ('yandex_webmaster', 'Яндекс.Вебмастер',       True),
    ('gsc',              'Google Search Console',   True),
    ('ya_business',      'Я.Бизнес',                False),
    ('twogis',           '2ГИС',                    False),
    ('google_accounts',  'Google',                  False),
]


def _build_notifications_sheet(wb, notifications):
    """Лист «Уведомления» – письма по источникам, структурированные секциями.
    Лист добавляется всегда: при пустом списке показывает заглушку."""
    notifications = notifications or []
    ws = wb.create_sheet('Уведомления')
    ws.sheet_view.showGridLines = False

    has_critical = any(n.priority == 'critical' for n in notifications)
    ws.sheet_properties.tabColor = C.err if has_critical else C.accent

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 14   # Дата
    ws.column_dimensions['C'].width = 20   # Приоритет / пусто
    ws.column_dimensions['D'].width = 20   # Категория / пусто
    ws.column_dimensions['E'].width = 58   # Тема
    ws.column_dimensions['F'].width = 70   # Превью
    ws.column_dimensions['G'].width = 22   # Отдел

    # ── Заголовок листа ──
    ws.merge_cells('B2:G2')
    c = ws['B2']
    c.value = 'Уведомления из почты'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:G3')
    c = ws['B3']
    c.value = (
        'Письма от Яндекс.Вебмастера, GSC, Я.Бизнеса, 2ГИС и Google '
        'за период проверки. Красная вкладка = есть критические уведомления.'
    )
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 24

    # Пустой список – показываем заглушку и выходим
    if not notifications:
        ws.merge_cells('B5:G5')
        c = ws['B5']
        c.value = ('За период проверки писем не найдено. '
                   'Если ждёте уведомления – проверьте секреты почты и пароли приложений '
                   '(Gmail требует App Password), затем запустите прогон с галкой '
                   '«Собрать уведомления из почты».')
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True, vertical='top')
        ws.row_dimensions[5].height = 60
        return

    # Разбиваем по источникам
    from collections import defaultdict
    by_source = defaultdict(list)
    for n in notifications:
        by_source[n.source].append(n)

    row = 5

    for source_key, section_title, has_priority in _NOTIF_SECTIONS:
        items = by_source.get(source_key, [])
        if not items:
            continue

        # ── Заголовок секции ──
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
        sc = ws.cell(row=row, column=2)
        sc.value = f'{section_title}  ({len(items)})'
        sc.font = _font(size=13, bold=True, color=C.accent)
        sc.fill = _fill(C.accent_soft)
        sc.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1

        if has_priority:
            # ── Источник с классификацией: группируем по приоритету ──
            p_groups = defaultdict(list)
            for n in items:
                p_groups[n.priority].append(n)

            for priority in _NOTIF_PRIORITY_ORDER:
                p_items = p_groups.get(priority, [])
                if not p_items:
                    continue

                p_color = _NOTIF_PRIORITY_COLOR[priority]
                p_bg = _NOTIF_PRIORITY_BG[priority]
                p_label = _NOTIF_PRIORITY_LABEL[priority]

                # Подзаголовок приоритета
                ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
                pc = ws.cell(row=row, column=2)
                pc.value = f'  {p_label}  ({len(p_items)})'
                pc.font = _font(size=10, bold=True, color=p_color)
                pc.fill = _fill(p_bg)
                pc.alignment = _align(indent=2)
                ws.row_dimensions[row].height = 20
                row += 1

                # Шапка
                for ci, h in enumerate(['Дата', 'Приоритет', 'Категория', 'Тема', 'Превью', 'Отдел'], 2):
                    cell = ws.cell(row=row, column=ci)
                    cell.value = h
                    cell.font = _font(size=9, bold=True, color=C.text_muted)
                    cell.fill = _fill(C.surface)
                    cell.alignment = _align()
                    cell.border = _border()
                ws.row_dimensions[row].height = 20
                row += 1

                # Строки
                for n in sorted(p_items, key=lambda x: x.date, reverse=True):
                    ws.row_dimensions[row].height = 44

                    for ci, (val, kw) in enumerate([
                        (n.date, {'color': C.text_soft}),
                        (_NOTIF_PRIORITY_LABEL[n.priority], {'bold': priority == 'critical', 'color': p_color}),
                        (_NOTIF_CATEGORY_LABEL.get(n.category, n.category), {'color': C.text_soft}),
                        (n.subject, {'bold': priority == 'critical', 'color': p_color}),
                        ((n.body_preview or '')[:400], {'size': 9, 'color': C.text_soft}),
                        (_dept_notif(n), {'size': 9, 'color': C.text_soft}),
                    ], 2):
                        cell = ws.cell(row=row, column=ci)
                        cell.value = val
                        cell.font = _font(**kw)
                        cell.alignment = _align(wrap=True)
                        cell.border = _border(color=C.border_light)
                        if priority == 'critical' and ci in (5, 6):
                            cell.fill = _fill(p_bg)

                    row += 1

                row += 1  # пробел между приоритетами

        else:
            # ── Источник без классификации: плоский список ──
            # Шапка
            for ci, h in enumerate(['Дата', '', '', 'Тема', 'Превью', 'Отдел'], 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = h
                cell.font = _font(size=9, bold=True, color=C.text_muted)
                cell.fill = _fill(C.surface)
                cell.alignment = _align()
                cell.border = _border()
            ws.row_dimensions[row].height = 20
            row += 1

            for n in sorted(items, key=lambda x: x.date, reverse=True):
                ws.row_dimensions[row].height = 44

                for ci, (val, kw) in enumerate([
                    (n.date, {'color': C.text_soft}),
                    ('', {}),
                    ('', {}),
                    (n.subject, {'bold': False, 'color': C.text}),
                    ((n.body_preview or '')[:400], {'size': 9, 'color': C.text_soft}),
                    (_dept_notif(n), {'size': 9, 'color': C.text_soft}),
                ], 2):
                    cell = ws.cell(row=row, column=ci)
                    cell.value = val
                    cell.font = _font(**kw)
                    cell.alignment = _align(wrap=True)
                    cell.border = _border(color=C.border_light)

                row += 1

        row += 2  # пробел между секциями


# ── Лист «Контакты по городам» (сверка с КП) ───────────────────────


def _build_kp_sheet(wb, results):
    """
    Сверка контактов (телефон / почта / адрес) на главных страницах
    поддоменов с «Картой присутствия». По одному городу в строке –
    наглядно видно, где номер/почта/адрес не совпали с КП.
    """
    rows = [r for r in results if getattr(r, 'kp_result', None)]
    if not rows:
        return

    # Сортировка: сначала города с проблемами, потом по алфавиту
    rows.sort(key=lambda r: (not r.kp_result.get('has_issues'),
                             r.kp_result.get('city') or ''))

    total = len(rows)
    with_problems = sum(1 for r in rows if r.kp_result.get('has_issues'))

    ws = wb.create_sheet('Контакты по городам')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if with_problems else C.accent

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 22   # Город
    ws.column_dimensions['C'].width = 10   # Открыть
    ws.column_dimensions['D'].width = 12   # Телефон
    ws.column_dimensions['E'].width = 12   # Почта
    ws.column_dimensions['F'].width = 12   # Адрес
    ws.column_dimensions['G'].width = 78   # Что не так

    # Заголовок + пояснение
    ws.merge_cells('B2:G2')
    c = ws['B2']
    c.value = 'Контакты по городам – сверка с КП'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 24

    ws.merge_cells('B3:G3')
    c = ws['B3']
    c.value = ('Сверяем телефон, почту и адрес на главной каждого города с '
               '«Картой присутствия». Телефон: ожидается SEO-номер (если нет – '
               'рекламный, затем общий). Зелёное – совпало, красное – нет. '
               'Что именно не так – в последнем столбце.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 30

    # Плитки сводки
    tiles = [
        ('Проверено городов', total, C.accent, C.accent_soft),
        ('С расхождениями', with_problems,
         C.err if with_problems else C.ok, C.err_soft if with_problems else C.ok_soft),
    ]
    col = 2
    for label, value, color, bg in tiles:
        ws.merge_cells(start_row=5, start_column=col, end_row=5, end_column=col + 1)
        ws.merge_cells(start_row=6, start_column=col, end_row=6, end_column=col + 1)
        vc = ws.cell(row=5, column=col, value=value)
        vc.font = _font(size=22, bold=True, color=color)
        vc.fill = _fill(bg); vc.alignment = _align(horizontal='center')
        vc.border = _border(color=C.border_light)
        ws.cell(row=5, column=col + 1).fill = _fill(bg)
        ws.cell(row=5, column=col + 1).border = _border(color=C.border_light)
        lc = ws.cell(row=6, column=col, value=label)
        lc.font = _font(size=9, color=C.text_muted)
        lc.fill = _fill(bg); lc.alignment = _align(horizontal='center')
        lc.border = _border(color=C.border_light)
        ws.cell(row=6, column=col + 1).fill = _fill(bg)
        ws.cell(row=6, column=col + 1).border = _border(color=C.border_light)
        col += 3
    ws.row_dimensions[5].height = 30

    # Шапка таблицы
    hdr_row = 8
    headers = ['Город', 'Открыть', 'Телефон', 'Почта', 'Адрес', 'Что не так']
    for ci, h in enumerate(headers, start=2):
        cell = ws.cell(row=hdr_row, column=ci, value=h)
        cell.font = _font(size=10, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align(horizontal='center' if ci > 3 else 'left')
        cell.border = _border()
    ws.row_dimensions[hdr_row].height = 24
    ws.freeze_panes = f'B{hdr_row + 1}'

    field_to_col = {'Телефон': 4, 'Почта': 5, 'Адрес': 6}
    row = hdr_row + 1
    for r in rows:
        kp = r.kp_result
        issues = {i['field']: i for i in kp.get('issues', [])}

        cc = ws.cell(row=row, column=2, value=kp.get('city') or r.city)
        cc.font = _font(size=10); cc.alignment = _align(indent=1)
        cc.border = _border(color=C.border_light)

        uc = ws.cell(row=row, column=3, value='открыть')
        uc.hyperlink = r.url
        uc.font = _font(size=10, color=C.accent, underline='single')
        uc.alignment = _align(horizontal='center', indent=0)
        uc.border = _border(color=C.border_light)

        # Ячейки статусов
        for field, col_idx in field_to_col.items():
            cell = ws.cell(row=row, column=col_idx)
            cell.alignment = _align(horizontal='center', indent=0)
            cell.border = _border(color=C.border_light)
            iss = issues.get(field)
            if iss is None:
                cell.value = '–'           # поле в КП не задано – не сверяем
                cell.font = _font(size=10, color=C.text_muted)
            elif iss['status'] == 'ok':
                cell.value = '✓'
                cell.font = _font(size=10, bold=True, color=C.ok)
                cell.fill = _fill(C.ok_soft)
            elif iss['status'] == 'critical':
                cell.value = 'КРИТ'
                cell.font = _font(size=9, bold=True, color=C.err)
                cell.fill = _fill(C.err_soft)
            else:
                cell.value = 'БАГ'
                cell.font = _font(size=10, bold=True, color=C.err)
                cell.fill = _fill(C.err_soft)

        # Что не так – комментарии по проблемным полям
        problems = [f'{i["field"]}: {i["comment"]}'
                    for i in kp.get('issues', [])
                    if i['status'] in ('bug', 'critical') and i.get('comment')]
        wc = ws.cell(row=row, column=7, value='\n'.join(problems))
        wc.font = _font(size=9, color=C.err if problems else C.text_muted)
        wc.alignment = _align(wrap=True, vertical='top')
        wc.border = _border(color=C.border_light)
        ws.row_dimensions[row].height = max(22, 15 * (len(problems) or 1))
        row += 1


# ── Главная функция ────────────────────────────────────────────────


def build_report(
    *,
    project_name: str,
    started_at_ms: int,
    finished_at_ms: int,
    selected_subdomains: list,    # список Subdomain
    results: list,                 # список CheckResult
    output_path: Path | str,
    metrika_reports: list = None,  # список Report404 – добавит лист «404 из Метрики»
    metrika_data_date: str = None, # дата отчёта Метрики (YYYY-MM-DD)
    metrika_is_stale: bool = False,# True если данные не за вчера, а за более ранний день
    notifications: list = None,    # список WebmasterNotification – добавит лист «Уведомления»
) -> Path:
    """Сформировать xlsx-отчёт и сохранить в output_path."""
    wb = Workbook()
    # Удаляем дефолтный пустой лист
    wb.remove(wb.active)

    # ── Подсчёт метрик ─────────────────────────────────────────────
    total = len(results)
    ok_count = sum(1 for r in results if r.is_ok)
    warn_count = sum(1 for r in results if r.is_warning)
    err_count = total - ok_count - warn_count
    duration_sec = (finished_at_ms - started_at_ms) // 1000

    pages_with_issues = [r for r in results if r.has_text_issues]
    total_text_issues = sum(len(r.text_issues) for r in pages_with_issues)

    # Структурные проблемы (баги контента: нет цены, кнопок, H1 и т.п.)
    pages_with_content = [r for r in results if getattr(r, 'content', None) is not None]
    pages_with_content_bugs = [r for r in pages_with_content if r.content_bugs > 0]
    total_content_bugs = sum(r.content_bugs for r in pages_with_content)

    # ═══════════════════════════════════════════════════════════════
    # ЛИСТ 1: Обзор
    # ═══════════════════════════════════════════════════════════════
    ws1 = wb.create_sheet('Обзор')
    ws1.sheet_view.showGridLines = False

    # Ширины колонок
    ws1.column_dimensions['A'].width = 3
    for col in ('B', 'C', 'D', 'E'):
        ws1.column_dimensions[col].width = 22
    ws1.column_dimensions['F'].width = 3

    # Заголовок
    ws1.merge_cells('B2:E2')
    c = ws1['B2']
    c.value = 'Отчёт по проверке сайта'
    c.font = _font(size=20, bold=True)
    ws1.row_dimensions[2].height = 30

    ws1.merge_cells('B3:E3')
    started_dt = datetime.fromtimestamp(started_at_ms / 1000)
    c = ws1['B3']
    c.value = f'{project_name} · {started_dt.strftime("%d.%m.%Y, %H:%M:%S")}'
    c.font = _font(size=11, color=C.text_muted)
    ws1.row_dimensions[3].height = 20

    # ─── 4 карточки метрик ─────────────────────────────────────────
    card_row = 6
    ws1.row_dimensions[card_row].height = 22
    ws1.row_dimensions[card_row + 1].height = 38

    metrics = [
        ('B', 'ВСЕГО ПРОВЕРОК', total, C.text),
        ('C', 'РАБОТАЕТ', ok_count, C.ok),
        ('D', 'НЕ РАБОТАЕТ', err_count, C.err),
        ('E', 'ПРЕДУПРЕЖДЕНИЯ', warn_count, C.warn),
    ]
    for col, label, value, color in metrics:
        top = ws1[f'{col}{card_row}']
        top.value = label
        top.font = _font(size=9, bold=True, color=C.text_muted)
        top.alignment = _align()
        top.fill = _fill(C.surface)
        top.border = _border()

        bot = ws1[f'{col}{card_row + 1}']
        bot.value = value
        bot.font = _font(size=26, bold=True, color=color)
        bot.alignment = _align()
        bot.fill = _fill(C.bg_elev)
        bot.border = _border()

    # ─── Сводка ────────────────────────────────────────────────────
    sum_row = card_row + 3
    ws1.row_dimensions[sum_row].height = 26
    ws1.merge_cells(f'B{sum_row}:E{sum_row}')
    c = ws1[f'B{sum_row}']
    c.value = 'Сводка'
    c.font = _font(size=12, bold=True)
    c.alignment = _align()
    c.fill = _fill(C.surface)
    c.border = _border()

    sum_body_row = sum_row + 1
    _extra = (1 if total_text_issues > 0 else 0) + (1 if total_content_bugs > 0 else 0)
    ws1.row_dimensions[sum_body_row].height = 44 + _extra * 17
    ws1.merge_cells(f'B{sum_body_row}:E{sum_body_row}')
    c = ws1[f'B{sum_body_row}']
    summary_text = (
        f'Из {total} проверенных страниц: '
        f'{ok_count} работают, {warn_count} с перенаправлениями, {err_count} не открываются.'
    )
    if total_text_issues > 0:
        summary_text += (
            f'\nДополнительно: на {len(pages_with_issues)} страницах найдено '
            f'{total_text_issues} битых переменных в текстах – см. лист «Битые тексты».'
        )
    if total_content_bugs > 0:
        summary_text += (
            f'\nВ контенте {total_content_bugs} проблем на {len(pages_with_content_bugs)} страницах '
            f'(нет цены, кнопок заказа или заголовка) – см. лист «Структура страниц».'
        )
    summary_text += '\nПодробности – на листе «Все детали» (фильтр по колонке «Статус»).'
    c.value = summary_text
    c.font = _font(size=11, color=C.text_soft)
    c.alignment = _align(wrap=True)
    c.fill = _fill(C.bg_elev)
    c.border = _border()

    # ─── Параметры прогона ─────────────────────────────────────────
    param_row = sum_body_row + 2
    ws1.row_dimensions[param_row].height = 22
    ws1.merge_cells(f'B{param_row}:E{param_row}')
    c = ws1[f'B{param_row}']
    c.value = 'Параметры прогона'
    c.font = _font(size=10, bold=True, color=C.text_muted)
    c.alignment = _align()

    params = [('Длительность', f'{duration_sec} сек')]
    if selected_subdomains:
        cities = ', '.join(s.city for s in selected_subdomains)
        params.append(('Поддоменов', f'{len(selected_subdomains)} ({cities})'))

    for i, (key, value) in enumerate(params):
        r = param_row + 1 + i
        ws1.row_dimensions[r].height = 22
        k = ws1[f'B{r}']
        k.value = key
        k.font = _font(size=10, color=C.text_muted)
        k.alignment = Alignment(horizontal='left', vertical='top', indent=1)

        ws1.merge_cells(f'C{r}:E{r}')
        v = ws1[f'C{r}']
        v.value = value
        v.font = _font(size=10, color=C.text_soft)
        v.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)

    # ─── Навигация по отчёту (для тех, кто открыл впервые) ──────────
    nav_row = param_row + len(params) + 2
    ws1.row_dimensions[nav_row].height = 22
    ws1.merge_cells(f'B{nav_row}:E{nav_row}')
    c = ws1[f'B{nav_row}']
    c.value = 'Из чего состоит отчёт'
    c.font = _font(size=10, bold=True, color=C.text_muted)
    c.alignment = _align()

    nav_items = [
        ('Обзор', 'эта страница: сколько проверено, сколько работает и сколько сломано.'),
        ('Структура страниц', 'что чинить в контенте – где нет цены, кнопок заказа, заголовка. Красное = баг.'),
        ('Все детали', 'каждая проверенная страница: адрес, код ответа, статус, скорость.'),
        ('Битые тексты', 'если есть лист – страницы с незаменёнными переменными ({{city}} и т.п.).'),
        ('404 из Метрики', 'если есть лист – страницы, куда заходили люди и упёрлись в 404.'),
        ('Уведомления', 'если есть лист – письма от Яндекс.Вебмастера и GSC за выбранный период.'),
    ]
    for i, (sheet_name, desc) in enumerate(nav_items):
        r = nav_row + 1 + i
        ws1.row_dimensions[r].height = 30
        k = ws1[f'B{r}']
        k.value = sheet_name
        k.font = _font(size=10, bold=True, color=C.accent)
        k.alignment = Alignment(horizontal='left', vertical='top', indent=1)
        ws1.merge_cells(f'C{r}:E{r}')
        v = ws1[f'C{r}']
        v.value = desc
        v.font = _font(size=10, color=C.text_soft)
        v.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)

    # ─── Лист структурной проверки (идёт сразу после «Обзора») ──────
    _build_structure_sheet(wb, results)

    # ─── Лист сверки контактов с КП (если были главные с kp_result) ──
    _build_kp_sheet(wb, results)

    # ═══════════════════════════════════════════════════════════════
    # ЛИСТ 2: Все детали
    # ═══════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet('Все детали')
    ws2.sheet_view.showGridLines = False
    ws2.freeze_panes = 'A2'

    headers = [
        ('Город', 18), ('Поддомен', 28), ('Тип', 12), ('URL', 55),
        ('Код', 8), ('Статус', 22), ('Скорость, с', 12),
        ('Оценка скорости', 18), ('Отдел', 22),
        ('Битые переменные', 18), ('Откуда перешли', 50),
    ]
    for i, (header, width) in enumerate(headers, 1):
        col_letter = get_column_letter(i)
        ws2.column_dimensions[col_letter].width = width
        c = ws2.cell(row=1, column=i)
        c.value = header
        c.font = _font(size=10, bold=True, color=C.text_muted)
        c.alignment = _align()
        c.fill = _fill(C.surface)
        c.border = _border()
    ws2.row_dimensions[1].height = 28

    # Сортировка: сначала ошибки, потом предупреждения, потом с битыми текстами, потом ОК
    def sort_key(r):
        score = 0 if r.is_error else 1 if r.is_warning else 2 if r.has_text_issues else 3
        return (score, r.city or '')

    sorted_results = sorted(results, key=sort_key)

    for row_idx, r in enumerate(sorted_results, 2):
        ws2.row_dimensions[row_idx].height = 22

        # Скорость с запятой (Excel в РФ ожидает запятую)
        speed_sec = ''
        if r.elapsed_ms is not None:
            speed_sec = f'{r.elapsed_ms / 1000:.2f}'.replace('.', ',')

        speed_label = SPEED_LABEL.get(r.speed_rating, '') if r.speed_rating else ''

        text_issue_text = ''
        if r.has_text_issues:
            n = len(r.text_issues)
            text_issue_text = f'{n} {"находка" if n == 1 else "находок"}'

        values = [
            r.city,                            # 1 Город
            r.subdomain,                       # 2 Поддомен
            r.type_label,                      # 3 Тип
            r.url,                             # 4 URL
            r.http_code if r.http_code else '–',  # 5 Код
            STATUS_LABEL.get(r.status, r.status),  # 6 Статус
            speed_sec,                         # 7 Скорость, с
            speed_label,                       # 8 Оценка скорости
            _dept_result(r),                   # 9 Отдел
            text_issue_text,                   # 10 Битые переменные
            _build_path_description(r),        # 11 Откуда перешли
        ]

        for col_idx, value in enumerate(values, 1):
            cell = ws2.cell(row=row_idx, column=col_idx)
            cell.value = value
            cell.font = _font(size=10)
            cell.alignment = _align(wrap=True)
            cell.border = _border(color=C.border_light)

        # Спец-шрифты для отдельных колонок
        ws2.cell(row=row_idx, column=2).font = _font(name='Consolas', size=10, color=C.text_muted)

        # URL – кликабельная гиперссылка
        url_cell = ws2.cell(row=row_idx, column=4)
        url_cell.hyperlink = r.url
        url_cell.font = _font(name='Consolas', size=10, color=C.accent, underline='single')

        # Откуда перешли – моноширинный для цепочек, курсивный для прямых
        path_cell = ws2.cell(row=row_idx, column=11)
        if r.redirect_chain:
            path_cell.font = _font(name='Consolas', size=9, color=C.text_soft)
        elif not r.is_ok:
            path_cell.font = _font(size=10, italic=True, color=C.text_muted)

        # Битые переменные – подсветка
        if r.has_text_issues:
            issue_cell = ws2.cell(row=row_idx, column=10)
            issue_cell.font = _font(size=10, bold=True, color=C.warn)
            issue_cell.fill = _fill(C.warn_soft)

        # Оценка скорости – цвет по уровню
        if r.speed_rating:
            speed_cell = ws2.cell(row=row_idx, column=8)
            color = SPEED_COLOR[r.speed_rating]
            bold = r.speed_rating in ('slow', 'very_slow')
            speed_cell.font = _font(size=10, bold=bold, color=color)

        # Статус – цвет по результату
        status_color = C.ok if r.is_ok else C.warn if r.is_warning else C.err
        ws2.cell(row=row_idx, column=6).font = _font(size=10, bold=True, color=status_color)

    ws2.auto_filter.ref = f'A1:K{len(sorted_results) + 1}'

    # ═══════════════════════════════════════════════════════════════
    # ЛИСТ 3: Битые тексты (только если есть)
    # ═══════════════════════════════════════════════════════════════
    if total_text_issues > 0:
        ws3 = wb.create_sheet('Битые тексты')
        ws3.sheet_view.showGridLines = False
        ws3.sheet_properties.tabColor = C.warn
        ws3.freeze_panes = 'A5'

        ws3.column_dimensions['A'].width = 18
        ws3.column_dimensions['B'].width = 50
        ws3.column_dimensions['C'].width = 18
        ws3.column_dimensions['D'].width = 24
        ws3.column_dimensions['E'].width = 80

        # Заголовок и пояснение
        ws3.merge_cells('A1:E1')
        c = ws3['A1']
        c.value = 'Битые переменные в текстах страниц'
        c.font = _font(size=14, bold=True)
        ws3.row_dimensions[1].height = 26

        ws3.merge_cells('A2:E2')
        c = ws3['A2']
        c.value = (
            'Шаблонизатор сайта не подставил значение, и фрагмент шаблона '
            '({{city}}, %price%, undefined и т.п.) остался виден пользователю в тексте страницы. '
            'Чтобы увидеть проблему – откройте URL и поищите по странице (Ctrl+F) то, '
            'что в колонке «Что нашлось».'
        )
        c.font = _font(size=10, italic=True, color=C.text_soft)
        c.alignment = _align(wrap=True, vertical='top')
        ws3.row_dimensions[2].height = 36

        # Шапка таблицы на строке 4
        ws3.merge_cells('A3:E3')  # пустая разделительная

        hdr_row = 4
        ws3.row_dimensions[hdr_row].height = 28
        hdrs = ['Город', 'Открыть страницу', 'Тип шаблона', 'Что нашлось', 'Где это в тексте']
        for col_idx, label in enumerate(hdrs, 1):
            c = ws3.cell(row=hdr_row, column=col_idx)
            c.value = label
            c.font = _font(size=10, bold=True, color=C.text_muted)
            c.alignment = _align()
            c.fill = _fill(C.surface)
            c.border = _border()

        row_idx = hdr_row + 1
        for page in pages_with_issues:
            for issue in page.text_issues:
                ws3.row_dimensions[row_idx].height = 30

                # Город
                c = ws3.cell(row=row_idx, column=1)
                c.value = page.city
                c.font = _font(size=10)
                c.alignment = _align(wrap=True)
                c.border = _border(color=C.border_light)

                # URL – кликабельный
                c = ws3.cell(row=row_idx, column=2)
                c.value = page.url
                c.hyperlink = page.url
                c.font = _font(name='Consolas', size=10, color=C.accent, underline='single')
                c.alignment = _align(wrap=True)
                c.border = _border(color=C.border_light)

                # Тип шаблона
                c = ws3.cell(row=row_idx, column=3)
                c.value = issue.pattern
                c.font = _font(size=10, color=C.text_soft)
                c.alignment = _align()
                c.border = _border(color=C.border_light)

                # Что нашлось
                c = ws3.cell(row=row_idx, column=4)
                c.value = issue.match
                c.font = _font(name='Consolas', size=10, bold=True, color=C.warn)
                c.alignment = _align()
                c.border = _border(color=C.border_light)

                # Контекст
                c = ws3.cell(row=row_idx, column=5)
                c.value = issue.context
                c.font = _font(name='Consolas', size=9, color=C.text_muted)
                c.alignment = _align(wrap=True)
                c.border = _border(color=C.border_light)

                row_idx += 1

        ws3.auto_filter.ref = f'A{hdr_row}:E{hdr_row}'

    # ═══════════════════════════════════════════════════════════════
    # ЛИСТ 4: «404 из Метрики» – если есть данные
    # ═══════════════════════════════════════════════════════════════
    if metrika_reports:
        # Собираем все страницы из всех стран, считаем сшивку с Site Checker
        # Множество URL'ов которые упали в Site Checker (404 или 5xx)
        sc_failed_urls = set()
        sc_failed_paths = set()  # для сравнения по pathname (без поддомена)
        from urllib.parse import urlparse as _urlparse
        for r in results:
            if r.is_error and r.http_code in (404, 410):
                sc_failed_urls.add(r.url)
                try:
                    p = _urlparse(r.url).path
                    if p:
                        sc_failed_paths.add(p)
                except ValueError:
                    pass

        ws4 = wb.create_sheet('404 из Метрики')
        ws4.sheet_view.showGridLines = False
        ws4.sheet_properties.tabColor = C.err if metrika_is_stale else C.accent
        ws4.freeze_panes = 'A6'  # шапка фиксируется

        # Колонки и их ширина
        ws4.column_dimensions['A'].width = 14   # Дата отчёта
        ws4.column_dimensions['B'].width = 16   # Страна
        ws4.column_dimensions['C'].width = 18   # Статус сшивки
        ws4.column_dimensions['D'].width = 55   # URL
        ws4.column_dimensions['E'].width = 12   # Просмотры
        ws4.column_dimensions['F'].width = 12   # Посетители
        ws4.column_dimensions['G'].width = 40   # Реферер
        ws4.column_dimensions['H'].width = 40   # Заголовок страницы

        # ─── Заголовок и пояснение ─────────────────────────────────
        ws4.merge_cells('A1:H1')
        c = ws4['A1']
        c.value = '404-страницы по данным Яндекс.Метрики'
        c.font = _font(size=14, bold=True)
        ws4.row_dimensions[1].height = 26

        # Информация о дате данных
        ws4.merge_cells('A2:H2')
        c = ws4['A2']
        # Форматируем дату красиво
        try:
            d_obj = datetime.strptime(metrika_data_date or '', '%Y-%m-%d')
            date_display = d_obj.strftime('%d.%m.%Y')
        except ValueError:
            date_display = metrika_data_date or '–'

        if metrika_is_stale:
            c.value = (
                f'⚠ Внимание: данные за {date_display}. '
                f'Свежий отчёт Метрики (за вчерашний день) ещё не пришёл – '
                f'используем последний доступный.'
            )
            c.font = _font(size=10, italic=True, bold=True, color=C.err)
            c.fill = _fill(C.err_soft)
        else:
            c.value = f'Данные за {date_display}'
            c.font = _font(size=10, color=C.text_soft)
        c.alignment = _align(wrap=True)
        ws4.row_dimensions[2].height = 30 if metrika_is_stale else 20

        # 3-я строка – пустая. Раньше тут была длинная пояснительная
        # строка про «🔴 Точно сломан / ⚠ Только в Метрике / Сортировка».
        # Убрана по требованию: цвета в колонке «Статус» интуитивно понятны,
        # а лишний текст загромождал шапку.
        ws4.row_dimensions[3].height = 8

        # ─── Шапка таблицы на 5-й строке ───────────────────────────
        # 4-я строка – пустая разделительная
        hdr_row = 5
        ws4.row_dimensions[hdr_row].height = 28
        hdrs = ['Дата', 'Страна', 'Статус', 'URL страницы', 'Просмотры', 'Посетители', 'Реферер', 'Заголовок страницы']
        for col_idx, label in enumerate(hdrs, 1):
            cell = ws4.cell(row=hdr_row, column=col_idx)
            cell.value = label
            cell.font = _font(size=10, bold=True, color=C.text_muted)
            cell.alignment = _align()
            cell.fill = _fill(C.surface)
            cell.border = _border()

        # ─── Собираем плоский список страниц со статусом сшивки ────
        flat_rows = []
        for report in metrika_reports:
            for page in report.pages:
                # Проверяем сшивку: URL из метрики совпадает с упавшим в Site Checker?
                is_confirmed = False
                if page.page_url:
                    if page.page_url in sc_failed_urls:
                        is_confirmed = True
                    else:
                        # Также сравним по path – если в Метрике URL без поддомена, в SC с поддоменом
                        try:
                            p = _urlparse(page.page_url).path
                            if p and p in sc_failed_paths:
                                is_confirmed = True
                        except ValueError:
                            pass

                flat_rows.append({
                    'date': report.report_date,
                    'country_code': report.country_code,
                    'country_name': report.country_name,
                    'url': page.page_url or '',
                    'title': page.page_title,
                    'views': page.views,
                    'visitors': page.visitors,
                    'referer': page.referer or '',
                    'confirmed': is_confirmed,
                })

        # Сортируем: сначала подтверждённые, потом по убыванию просмотров
        flat_rows.sort(key=lambda r: (not r['confirmed'], -r['views']))

        # ─── Предупреждение о пустых URL ──────────────────────────
        # Если у всех или у большинства строк нет page_url – Метрика
        # отдала только заголовки страниц. Так бывает, если в шаблоне
        # рассылки не настроена группировка «Адрес страницы». Чинить
        # это в Метрике, не в коде. Помечаем это прямо в xlsx, чтобы
        # пользователь сразу понял что происходит.
        rows_with_url = sum(1 for fr in flat_rows if fr['url'])
        if flat_rows and rows_with_url == 0:
            warn_row = hdr_row - 1  # 4-я строка (там сейчас пусто)
            ws4.merge_cells(f'A{warn_row}:H{warn_row}')
            wc = ws4.cell(row=warn_row, column=1)
            wc.value = (
                '⚠ Колонка «URL страницы» пустая: в текущем шаблоне рассылки '
                'Метрики нет «Адреса страницы» – приходят только заголовки. '
                'Чтобы получать URL: Метрика → Содержание → Страницы → 404 → '
                '«Группировки» → добавить «Адрес страницы» → сохранить шаблон '
                'рассылки. Со следующего письма URL начнут приходить.'
            )
            wc.font = _font(size=10, bold=True, color=C.warn)
            wc.fill = _fill(C.warn_soft)
            wc.alignment = _align(wrap=True, vertical='top')
            ws4.row_dimensions[warn_row].height = 48

        # ─── Если в почте есть отчёты но 404 не нашлось – короткое сообщение ──
        if not flat_rows:
            ws4.merge_cells(f'A{hdr_row + 1}:H{hdr_row + 1}')
            cell = ws4.cell(row=hdr_row + 1, column=1)
            cell.value = '✓ За эту дату Метрика не зафиксировала ни одной 404-страницы по проекту'
            cell.font = _font(size=11, bold=True, color=C.ok)
            cell.alignment = _align()
            cell.fill = _fill(C.ok_soft)
            ws4.row_dimensions[hdr_row + 1].height = 32
        else:
            row_idx = hdr_row + 1
            for fr in flat_rows:
                ws4.row_dimensions[row_idx].height = 22

                # Дата
                try:
                    d_obj = datetime.strptime(fr['date'], '%Y-%m-%d')
                    date_str = d_obj.strftime('%d.%m.%Y')
                except ValueError:
                    date_str = fr['date']
                cell = ws4.cell(row=row_idx, column=1)
                cell.value = date_str
                cell.font = _font(size=10, color=C.text_soft)
                cell.alignment = _align()
                cell.border = _border(color=C.border_light)

                # Страна
                cell = ws4.cell(row=row_idx, column=2)
                cell.value = f'{fr["country_code"]} – {fr["country_name"]}'
                cell.font = _font(size=10)
                cell.alignment = _align()
                cell.border = _border(color=C.border_light)

                # Статус сшивки
                cell = ws4.cell(row=row_idx, column=3)
                if fr['confirmed']:
                    cell.value = '🔴 Точно сломан'
                    cell.font = _font(size=10, bold=True, color=C.err)
                    cell.fill = _fill(C.err_soft)
                else:
                    cell.value = '⚠ В Метрике'
                    cell.font = _font(size=10, color=C.warn)
                cell.alignment = _align()
                cell.border = _border(color=C.border_light)

                # URL – кликабельный
                cell = ws4.cell(row=row_idx, column=4)
                cell.value = fr['url'] or '–'
                if fr['url']:
                    cell.hyperlink = fr['url']
                    cell.font = _font(name='Consolas', size=10, color=C.accent, underline='single')
                else:
                    cell.font = _font(size=10, color=C.text_muted, italic=True)
                cell.alignment = _align(wrap=True)
                cell.border = _border(color=C.border_light)

                # Просмотры
                cell = ws4.cell(row=row_idx, column=5)
                cell.value = fr['views']
                cell.font = _font(size=10, bold=fr['confirmed'])
                cell.alignment = _align(horizontal='right')
                cell.border = _border(color=C.border_light)

                # Посетители
                cell = ws4.cell(row=row_idx, column=6)
                cell.value = fr['visitors']
                cell.font = _font(size=10)
                cell.alignment = _align(horizontal='right')
                cell.border = _border(color=C.border_light)

                # Реферер
                cell = ws4.cell(row=row_idx, column=7)
                cell.value = fr['referer'] or '–'
                if fr['referer']:
                    cell.font = _font(name='Consolas', size=9, color=C.text_soft)
                else:
                    cell.font = _font(size=10, color=C.text_muted, italic=True)
                cell.alignment = _align(wrap=True)
                cell.border = _border(color=C.border_light)

                # Заголовок страницы
                cell = ws4.cell(row=row_idx, column=8)
                cell.value = fr['title']
                cell.font = _font(size=9, color=C.text_soft)
                cell.alignment = _align(wrap=True)
                cell.border = _border(color=C.border_light)

                row_idx += 1

            ws4.auto_filter.ref = f'A{hdr_row}:H{row_idx - 1}'

    # ═══════════════════════════════════════════════════════════════
    # ЛИСТ 5: Уведомления (Вебмастер + GSC) – если есть данные
    # ═══════════════════════════════════════════════════════════════
    # Лист «Уведомления» добавляем всегда (при пустом списке – заглушка).
    _build_notifications_sheet(wb, notifications)

    # ── Сохраняем ──────────────────────────────────────────────────
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


# ── Утилита для имени файла ─────────────────────────────────────────


def make_report_filename(project_id: str, started_at_ms: int, reports_dir: Path) -> str:
    """
    Имя файла: smu-21.05.2026.xlsx
    Если уже есть – smu-21.05.2026_2.xlsx, _3 и т.д.
    """
    d = datetime.fromtimestamp(started_at_ms / 1000)
    date_part = d.strftime('%d.%m.%Y')
    prefix = f'{project_id}-{date_part}'

    base_name = f'{prefix}.xlsx'
    if not (reports_dir / base_name).exists():
        return base_name

    n = 2
    while (reports_dir / f'{prefix}_{n}.xlsx').exists():
        n += 1
    return f'{prefix}_{n}.xlsx'
