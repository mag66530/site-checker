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
import re
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
    # Одна галочка: есть цена в любом виде (₽ ИЛИ «по запросу») → ✓; нет ни того
    # ни другого или скрыто стилями → БАГ. Без «₽ + запрос» – это лишний шум.
    price = bk.get('price')
    if price and price.required and not price.present:
        return ('БАГ', 'bug')
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


def _plural_pages(n):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return 'страница'
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return 'страницы'
    return 'страниц'


_KIND_LABEL = {'listing': 'Листинг', 'section': 'Раздел каталога',
               'empty': 'Пустой раздел'}


def _contacts_problem_text(r):
    """Текст расхождений контактов с КП (адреса всех городов / телефон страницы)."""
    parts = []
    ca = getattr(r, 'contacts_addr', None)
    if ca and ca.get('mismatched'):
        mm = ca['mismatched']
        ex = '; '.join(f'{m["city"]}: сайт «{m["site"]}» / КП «{m["kp"]}»' for m in mm[:5])
        parts.append('адреса не совпадают с КП – ' + ex
                     + (f' и ещё {len(mm) - 5}' if len(mm) > 5 else ''))
    pp = getattr(r, 'page_phone', None)
    if pp and pp.get('status') in ('bug', 'critical'):
        parts.append(f'телефон: {pp.get("comment", "не совпадает с КП")}')
    return '; '.join(parts)


def _broken_links_text(r):
    """Битые ссылки (404/410) в контенте страницы – краткий текст для отчёта."""
    bl = getattr(r, 'broken_links', None)
    if not bl or not bl.get('broken'):
        return ''
    items = bl['broken']
    ex = '; '.join(f'{b["code"]} {b["url"]}' for b in items[:3])
    more = f' и ещё {len(items) - 3}' if len(items) > 3 else ''
    return f'битые ссылки ({len(items)}): ' + ex + more


def _problem_text(r):
    """Понятная формулировка проблемы страницы для списка «Что чинить»."""
    parts = []
    _ct = _contacts_problem_text(r)
    if _ct:
        parts.append(_ct)
    content = getattr(r, 'content', None)
    if content is not None:
        if getattr(content, 'is_soft_404', False):
            parts.append('страница отдаёт 404 (не найдена) – проверить ссылку или убрать из каталога')
        elif getattr(content, 'page_kind', '') == 'empty':
            parts.append('раздел пуст – нет ни товаров, ни подразделов')
        else:
            # У бага может быть пояснение (напр. «в коде есть, но покупатель не
            # видит») – показываем его рядом с названием блока.
            bugs = [f'{b.label} – {b.note}' if getattr(b, 'note', '') else b.label
                    for b in content.bugs]
            if bugs:
                parts.append('нет: ' + ', '.join(bugs))
    _bl = _broken_links_text(r)
    if _bl:
        parts.append(_bl)
    return '; '.join(parts) if parts else 'проблема'


def _build_structure_sheet(wb, results):
    """Лист структурной проверки – дашборд, что чинить, сводка и детали."""
    # Тех. страницы выносим отдельной секцией (у них нет структуры — только
    # доступность), чтобы они не искажали статистику структурной проверки.
    pages = [r for r in results if getattr(r, 'content', None) is not None
             and getattr(r, 'type_code', '') != 'tech']
    if not pages:
        return

    ws = wb.create_sheet('Структура страниц')
    ws.sheet_view.showGridLines = False

    total_pages = len(pages)
    pages_with_bugs = sum(1 for r in pages if r.content_bugs > 0)
    ok_pages = total_pages - pages_with_bugs
    total_bugs = sum(r.content_bugs for r in pages)
    ws.sheet_properties.tabColor = C.err if total_bugs else C.ok

    # ── Ширины ──
    ws.column_dimensions['A'].width = 2.5
    ws.column_dimensions['B'].width = 24
    ws.column_dimensions['C'].width = 17
    ws.column_dimensions['D'].width = 11
    max_block_cols = max((len(_grid_columns(r.content.blocks)) for r in pages), default=12)
    for col_idx in range(5, 5 + max(max_block_cols, 9) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 13
    last_col = 13                       # карточки дашборда занимают B..M
    LASTL = get_column_letter(last_col)

    def fill_block(r1, c1, r2, c2, bg, bc=C.border_light):
        for rr in range(r1, r2 + 1):
            for cc in range(c1, c2 + 1):
                cell = ws.cell(row=rr, column=cc)
                if bg:
                    cell.fill = _fill(bg)
                cell.border = _border(color=bc)

    # ── Заголовок ──
    ws.merge_cells(f'B2:{LASTL}2')
    c = ws['B2']
    c.value = 'Структура страниц'
    c.font = _font(size=20, bold=True, color=C.text)
    ws.row_dimensions[2].height = 30

    ws.merge_cells(f'B3:{LASTL}3')
    c = ws['B3']
    c.value = ('Что должно быть на каждой странице для продаж – и чего не хватает. '
               'Красное нужно чинить, серый прочерк – этого просто нет (норма).')
    c.font = _font(size=11, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='center')
    ws.row_dimensions[3].height = 18

    # ── Дашборд: 3 карточки на всю ширину (B-E, F-I, J-M) ──
    cards = [
        (total_pages, 'ПРОВЕРЕНО СТРАНИЦ', C.accent, C.accent_soft),
        (ok_pages, 'БЕЗ ПРОБЛЕМ', C.ok, C.ok_soft),
        (pages_with_bugs, 'НУЖНО ПОЧИНИТЬ',
         C.err if pages_with_bugs else C.ok, C.err_soft if pages_with_bugs else C.ok_soft),
    ]
    ws.row_dimensions[5].height = 30
    ws.row_dimensions[6].height = 16
    for i, (value, label, color, bg) in enumerate(cards):
        c1 = 2 + i * 4
        c2 = c1 + 3
        fill_block(5, c1, 6, c2, bg)
        ws.merge_cells(start_row=5, start_column=c1, end_row=5, end_column=c2)
        v = ws.cell(row=5, column=c1, value=value)
        v.font = _font(size=26, bold=True, color=color)
        v.alignment = _align(horizontal='center', vertical='center')
        ws.merge_cells(start_row=6, start_column=c1, end_row=6, end_column=c2)
        l = ws.cell(row=6, column=c1, value=label)
        l.font = _font(size=9, bold=True, color=C.text_muted)
        l.alignment = _align(horizontal='center')

    # ── «Что чинить» – главный блок ──
    bug_pages = [r for r in pages if r.content_bugs > 0]
    # Тех. страницы с расхождением контактов с КП (адреса городов / телефон) –
    # тоже выводим наверх как ошибку.
    for r in results:
        if getattr(r, 'type_code', '') == 'tech' and (
                (getattr(r, 'content_bugs', 0) or 0) > 0
                or _contacts_problem_text(r) or _broken_links_text(r)):
            bug_pages.append(r)
    bug_pages = sorted(bug_pages, key=lambda r: -(getattr(r, 'content_bugs', 0) or 0))
    row = 8
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last_col)
    hc = ws.cell(row=row, column=2)
    if bug_pages:
        hc.value = f'  Что чинить – {len(bug_pages)} {_plural_pages(len(bug_pages))}'
        hc.font = _font(size=14, bold=True, color=C.err)
        hc.fill = _fill(C.err_soft)
    else:
        hc.value = '  ✓ Всё в порядке – структурных проблем не найдено'
        hc.font = _font(size=14, bold=True, color=C.ok)
        hc.fill = _fill(C.ok_soft)
    hc.alignment = _align(indent=1, vertical='center')
    for cc in range(2, last_col + 1):
        ws.cell(row=row, column=cc).fill = _fill(C.err_soft if bug_pages else C.ok_soft)
    ws.row_dimensions[row].height = 26
    row += 1

    if bug_pages:
        # Шапка списка
        for ci, h in [(2, 'Город'), (3, 'Тип страницы'), (4, 'Открыть'),
                      (5, 'Что не так')]:
            cell = ws.cell(row=row, column=ci, value=h)
            cell.font = _font(size=9, bold=True, color=C.text_muted)
            cell.fill = _fill(C.surface)
            cell.alignment = _align(indent=1)
            cell.border = _border()
        ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=last_col)
        for cc in range(5, last_col + 1):
            ws.cell(row=row, column=cc).fill = _fill(C.surface)
            ws.cell(row=row, column=cc).border = _border()
        row += 1
        for idx, r in enumerate(bug_pages[:50]):
            band = C.surface if idx % 2 else C.bg_elev
            kind = _KIND_LABEL.get(getattr(r.content, 'page_kind', ''), r.type_label)
            cc = ws.cell(row=row, column=2, value=r.city)
            cc.font = _font(size=10, bold=True); cc.fill = _fill(band)
            cc.alignment = _align(indent=1); cc.border = _border(color=C.border_light)
            kc = ws.cell(row=row, column=3, value=kind)
            kc.font = _font(size=10, color=C.text_soft); kc.fill = _fill(band)
            kc.alignment = _align(indent=1); kc.border = _border(color=C.border_light)
            uc = ws.cell(row=row, column=4, value='открыть')
            uc.hyperlink = r.url
            uc.font = _font(size=10, color=C.accent, underline='single')
            uc.fill = _fill(band)
            uc.alignment = _align(horizontal='center'); uc.border = _border(color=C.border_light)
            ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=last_col)
            mc = ws.cell(row=row, column=5, value=_problem_text(r))
            mc.font = _font(size=10, color=C.err)
            mc.alignment = _align(indent=1, wrap=True)
            for cc2 in range(5, last_col + 1):
                ws.cell(row=row, column=cc2).fill = _fill(band)
                ws.cell(row=row, column=cc2).border = _border(color=C.border_light)
            row += 1
        if len(bug_pages) > 50:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last_col)
            ws.cell(row=row, column=2,
                    value=f'… и ещё {len(bug_pages) - 50} – см. таблицы ниже').font = \
                _font(size=10, italic=True, color=C.text_muted)
            row += 1

    # ── Подробные таблицы по типам ──
    row += 2
    ws.cell(row=row, column=2, value='Подробно по типам страниц').font = \
        _font(size=13, bold=True, color=C.text)
    ws.cell(row=row + 1, column=2,
            value='✓ есть · БАГ обязательного нет · «–» необязательного нет (норма) · '
                  'число = сколько найдено. Наведите курсор на заголовок столбца – пояснение.').font = \
        _font(size=9, italic=True, color=C.text_muted)
    ws.merge_cells(start_row=row + 1, start_column=2, end_row=row + 1, end_column=last_col)
    row += 3

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
        gc.value = (f'  {group_label} – {len(group_pages)} стр.'
                    + (f'  ·  проблем: {g_bugs}' if g_bugs else '  ·  все в порядке'))
        gc.font = _font(size=11, bold=True, color=C.err if g_bugs else C.ok)
        gc.fill = _fill(C.accent_soft)
        gc.alignment = _align(indent=1, vertical='center')
        for cc in range(2, 5 + n_cols):
            ws.cell(row=row, column=cc).fill = _fill(C.accent_soft)
        ws.row_dimensions[row].height = 22
        row += 1

        # Шапка таблицы
        headers = ([('Город', ''), ('Открыть', ''), ('Проблем', '')]
                   + [(c['label'], c['desc']) for c in columns])
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
        ws.row_dimensions[hdr_row].height = 54
        row += 1

        for idx, r in enumerate(group_pages):
            by_key = {b.key: b for b in r.content.blocks}
            band = C.surface if idx % 2 else C.bg_elev

            cc = ws.cell(row=row, column=2, value=r.city)
            cc.font = _font(size=10); cc.fill = _fill(band)
            cc.alignment = _align(indent=1); cc.border = _border(color=C.border_light)

            uc = ws.cell(row=row, column=3, value='открыть')
            uc.hyperlink = r.url
            uc.font = _font(size=10, color=C.accent, underline='single')
            uc.fill = _fill(band)
            uc.alignment = _align(horizontal='center', indent=0)
            uc.border = _border(color=C.border_light)

            pc = ws.cell(row=row, column=4)
            pc.value = r.content_bugs if r.content_bugs else ''
            pc.font = _font(size=11, bold=True, color=C.err)
            pc.alignment = _align(horizontal='center', indent=0)
            pc.fill = _fill(C.err_soft) if r.content_bugs else _fill(band)
            pc.border = _border(color=C.border_light)

            if getattr(r.content, 'is_soft_404', False) and n_cols:
                ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=4 + n_cols)
                cell = ws.cell(row=row, column=5, value='Страница отдаёт 404 (не найдена)')
                cell.font = _font(size=10, bold=True, color=C.err)
                cell.alignment = _align(indent=1)
                for k in range(n_cols):
                    cm = ws.cell(row=row, column=5 + k)
                    cm.fill = _fill(C.err_soft); cm.border = _border(color=C.border_light)
                row += 1
                continue

            for ci, col in enumerate(columns):
                cell = ws.cell(row=row, column=5 + ci)
                cell.alignment = _align(horizontal='center', indent=0)
                cell.border = _border(color=C.border_light)
                value, state = _cell_state(col, by_key)
                _style_cell(cell, value, state)
                if state in ('absent', 'count', 'okinfo'):
                    cell.fill = _fill(band)
            row += 1
        row += 2  # пробел между секциями

    # ── Технические страницы (оплата, доставка, контакты, реквизиты, политики,
    # карта сайта) ── Проверяем их «как все»: доступность (открывается / 404 /
    # ошибка) + структуру (H1, хлебные крошки) + битые переменные. H1 обязателен;
    # крошки справочно (их отсутствие на служебной странице багом не считаем).
    from urllib.parse import urlparse as _urlparse
    from sources import tech_page_label as _tech_label
    tech = [r for r in results if getattr(r, 'type_code', '') == 'tech']
    if tech:
        def _tech_bad(r):
            if not r.is_ok:
                return True
            if getattr(r.content, 'is_soft_404', False):
                return True
            return bool(r.content_bugs or r.has_text_issues
                        or _broken_links_text(r))
        _bad = sum(1 for r in tech if _tech_bad(r))
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=10)
        gc = ws.cell(row=row, column=2)
        gc.value = (f'  Технические страницы – {len(tech)} стр.'
                    + (f'  ·  проблем: {_bad}' if _bad else '  ·  все в порядке'))
        gc.font = _font(size=11, bold=True, color=C.err if _bad else C.ok)
        gc.fill = _fill(C.accent_soft)
        gc.alignment = _align(indent=1, vertical='center')
        for cc in range(2, 11):
            ws.cell(row=row, column=cc).fill = _fill(C.accent_soft)
        ws.row_dimensions[row].height = 22
        row += 1
        _tech_headers = [
            (2, 'Страница', 'Название страницы – кликабельная ссылка, ведёт на страницу.'),
            (3, 'Статус', 'Открывается ли страница: «Работает» / код ответа (404 и т.п.) / «404-заглушка» (отдаёт 200, но контент «страница не найдена»).'),
            (4, 'Проблем', 'Сколько проблем на странице: структурные баги, битые переменные, расхождения контактов с КП и битые ссылки (404).'),
            (5, 'H1', 'Заголовок H1. Обязателен – у нормальной страницы он есть.'),
            (6, 'Крошки', 'Хлебные крошки. Справочно: показываем есть/нет, отсутствие на служебной странице не баг.'),
            (7, 'Текст', 'Есть ли на странице собственный текст (помимо сквозных шапки и подвала). Обязателен.'),
            (8, 'Битые перем.', 'Битые шаблонные переменные ({{…}}, %name% и т.п.). Число = сколько найдено.'),
            (9, 'Элементы страницы', 'Спец-проверки в зависимости от страницы: картинки, ссылка на каталог, карта, форма обратной связи, строка поиска (✓ есть / – нет / БАГ – обязательного нет). Обязательны: карта на «Контактах», картинки на «О компании», строка поиска на странице поиска. Если включена проверка ссылок – тут же «Ссылки: N ✓» или «N битых» (404/410).'),
            (10, 'Что не так', 'Подробно: структурные баги (нет карты/картинок/строки поиска и т.п.) и расхождения контактов с КП (адреса городов / телефон страницы).'),
        ]
        hdr_row = row
        for ci, h, desc in _tech_headers:
            cell = ws.cell(row=hdr_row, column=ci, value=h)
            cell.font = _font(size=9, bold=True, color=C.text_muted)
            cell.fill = _fill(C.surface)
            cell.alignment = _align(horizontal='center', wrap=True, indent=0)
            cell.border = _border()
            if desc:
                cell.comment = Comment(desc, 'Site Checker', height=120, width=260)
        ws.row_dimensions[hdr_row].height = 40
        row += 1
        for idx, r in enumerate(tech):
            band = C.surface if idx % 2 else C.bg_elev

            # Страница – человеческое название (Оплата, Доставка…) как ссылка.
            try:
                _path = _urlparse(r.url).path or r.url
            except Exception:
                _path = r.url
            pgc = ws.cell(row=row, column=2, value=_tech_label(_path))
            pgc.hyperlink = r.url
            pgc.font = _font(size=10, color=C.accent, underline='single')
            pgc.fill = _fill(band)
            pgc.alignment = _align(indent=1)
            pgc.border = _border(color=C.border_light)

            _soft = getattr(r.content, 'is_soft_404', False)
            if not r.is_ok:
                _status = str(r.http_code) if r.http_code else 'не открылась'
            elif _soft:
                _status = '404-заглушка'
            else:
                _status = 'Работает'
            _status_ok = r.is_ok and not _soft
            sc = ws.cell(row=row, column=3, value=_status)
            sc.font = _font(size=10, bold=not _status_ok, color=C.ok if _status_ok else C.err)
            sc.fill = _fill(band if _status_ok else C.err_soft)
            sc.alignment = _align(horizontal='center', indent=0)
            sc.border = _border(color=C.border_light)

            _probs = (r.content_bugs or 0) + len(r.text_issues or [])
            _ca = getattr(r, 'contacts_addr', None)
            if _ca:
                _probs += len(_ca.get('mismatched') or [])
            _pp = getattr(r, 'page_phone', None)
            if _pp and _pp.get('status') in ('bug', 'critical'):
                _probs += 1
            _blk = getattr(r, 'broken_links', None)
            _broken_n = len(_blk['broken']) if (_blk and _blk.get('broken')) else 0
            _probs += _broken_n
            pc = ws.cell(row=row, column=4)
            pc.value = _probs if _probs else ''
            pc.font = _font(size=11, bold=True, color=C.err)
            pc.alignment = _align(horizontal='center', indent=0)
            pc.fill = _fill(C.err_soft) if _probs else _fill(band)
            pc.border = _border(color=C.border_light)

            # H1 / Крошки: если страница не открылась или это 404-заглушка –
            # структуры нет, ставим «–». Иначе берём из блоков контента.
            by_key = {b.key: b for b in r.content.blocks} if (r.is_ok and r.content) else {}
            for ci, key in ((5, 'h1'), (6, 'breadcrumbs'), (7, 'content_text')):
                cell = ws.cell(row=row, column=ci)
                cell.alignment = _align(horizontal='center', indent=0)
                cell.border = _border(color=C.border_light)
                if not by_key or _soft:
                    cell.value = '–'; cell.font = _font(size=10, color=C.text_muted)
                    cell.fill = _fill(band)
                else:
                    value, state = _cell_state({'kind': 'block', 'key': key}, by_key)
                    _style_cell(cell, value, state)
                    if state in ('absent', 'count', 'okinfo'):
                        cell.fill = _fill(band)

            # Битые переменные – число найденных.
            _ti = len(r.text_issues or []) if r.is_ok else 0
            vc = ws.cell(row=row, column=8)
            vc.alignment = _align(horizontal='center', indent=0)
            vc.border = _border(color=C.border_light)
            if _ti:
                vc.value = _ti; vc.font = _font(size=10, bold=True, color=C.err)
                vc.fill = _fill(C.err_soft)
            else:
                vc.value = '–'; vc.font = _font(size=10, color=C.text_muted)
                vc.fill = _fill(band)

            # Элементы страницы – спец-проверки (картинки/каталог-ссылка/карта/форма)
            # + краткий итог сверки адресов/телефона с КП.
            _spec = [b for b in (r.content.blocks if (r.is_ok and r.content) else [])
                     if b.key.startswith('tech_')]
            _addr_bad = False
            _parts = []
            for b in _spec:
                if b.required and not b.present:
                    _parts.append(f'{b.label}: БАГ')   # обязательный элемент, а его нет
                    _addr_bad = True
                else:
                    _parts.append(f'{b.label} {"✓" if b.present else "–"}')
            if _ca:
                _mm = _ca.get('mismatched') or []
                _txt = f'Адреса городов {_ca.get("matched", 0)}/{_ca.get("on_page", 0)}'
                if _mm:
                    _txt += f' · расхождений {len(_mm)}'
                    _addr_bad = True
                _parts.append(_txt)
            if _pp:
                _ps = _pp.get('status')
                _parts.append('Телефон ' + {'ok': '✓', 'info': 'инфо'}.get(_ps, 'расхождение'))
                if _ps in ('bug', 'critical'):
                    _addr_bad = True
            if _broken_n:
                _parts.append(f'Ссылки: {_broken_n} битых')
                _addr_bad = True
            elif _blk:                       # проверяли – все ссылки открылись
                _parts.append(f'Ссылки: {_blk.get("checked", 0)} ✓')
            ec = ws.cell(row=row, column=9)
            ec.alignment = _align(indent=1)
            ec.border = _border(color=C.border_light)
            ec.fill = _fill(C.err_soft if _addr_bad else band)
            ec.value = ' · '.join(_parts) if _parts else '–'
            ec.font = _font(size=9, color=C.err if _addr_bad else
                            (C.text_soft if _parts else C.text_muted))

            # Что не так – подробно: структурные баги (нет карты/картинок/строки
            # поиска и т.п.) и расхождения контактов с КП. Пусто, если проблем нет.
            _has_problem = ((r.content_bugs or 0) > 0
                            or bool(_contacts_problem_text(r)) or _broken_n > 0)
            _wn = _problem_text(r) if _has_problem else ''
            wn = ws.cell(row=row, column=10, value=_wn or '–')
            wn.alignment = _align(indent=1, wrap=True)
            wn.border = _border(color=C.border_light)
            wn.fill = _fill(C.err_soft if _wn else band)
            wn.font = _font(size=9, color=C.err if _wn else C.text_muted)
            row += 1
        row += 2


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

# Группировка уведомлений по теме (один и тот же текст письма приходит по
# каждому домену отдельно – схлопываем в одну строку, домены в список).
_DOMAIN_TLDS = (
    # рф/ru/su + СНГ/региональные зоны (.kz/.kg/.uz/.ua и т.д.) — чтобы один
    # бренд в разных зонах не дробил тему на отдельные строки + gTLD.
    'ru|рф|su|by|kz|kg|uz|ua|am|az|ge|md|tj|tm|ee|lv|lt|'
    'com|net|org|info|biz|pro|online|store|site|shop|me|cc|io'
)
# URL c путём целиком (group1 = host+path) – для извлечения режем по '/'.
_URL_RE = re.compile(r'https?://([^\s,;()<>"\']+)', re.IGNORECASE)
_HOST_RE = re.compile(
    r'\b((?:[a-zа-я0-9](?:[a-zа-я0-9-]*[a-zа-я0-9])?\.)+(?:' + _DOMAIN_TLDS + r'))\b',
    re.IGNORECASE,
)


def _extract_domains(text: str) -> list:
    """Вытащить хосты/домены из текста темы письма (URL и «голые» хосты)."""
    if not text:
        return []
    raw = [m.group(1).split('/')[0] for m in _URL_RE.finditer(text)]
    raw += [m.group(1) for m in _HOST_RE.finditer(text)]
    seen, out = set(), []
    for d in raw:
        d = d.strip('.').lower()
        if d.startswith('www.'):
            d = d[4:]
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _canon_theme(subject: str) -> str:
    """Тема без конкретного домена/URL – ключ группировки и текст для отчёта."""
    s = subject or ''
    s = _URL_RE.sub('', s)
    s = _HOST_RE.sub('', s)
    s = re.sub(r'\s+', ' ', s).strip(' .,:;/––-«»"\'')
    return s or (subject or '').strip()


def _group_notifs_by_theme(items: list) -> list:
    """Схлопнуть письма с одинаковой темой в группы.

    Возвращает список dict: theme, date (минимальная), domains (список),
    first (репрезентативное письмо), count. Порядок – первое появление темы.
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for n in items:
        key = _canon_theme(n.subject)
        g = groups.get(key)
        if g is None:
            g = {'theme': key, 'date': n.date, 'first': n,
                 'domains': [], 'count': 0}
            groups[key] = g
        g['count'] += 1
        if n.date and (not g['date'] or n.date < g['date']):
            g['date'] = n.date
        for d in _extract_domains(n.subject):
            if d not in g['domains']:
                g['domains'].append(d)
    return list(groups.values())


def _notif_row_height(domains_str: str, preview: str) -> float:
    """Высота строки под перенос длинного списка доменов / превью."""
    import math
    dom_lines = max(1, math.ceil(len(domains_str or '') / 50))
    prev_lines = max(1, math.ceil(len((preview or '')[:400]) / 70))
    lines = max(dom_lines, prev_lines)
    return min(300, max(44, lines * 14))


# Оценка отзыва 2ГИС → текст звёзд + ярлык качества + цвет.
def _review_rating_cell(rating):
    """(текст, цвет) для колонки «Оценка». rating: 1..5 или None."""
    if not rating:
        return '–', C.text_muted
    stars = '★' * int(rating)
    if rating >= 4:
        return f'{stars} Хороший', C.ok
    if rating == 3:
        return f'{stars} Средний', C.warn
    return f'{stars} Плохой', C.err


# Отдел для ошибки сервиса (Вебмастер-API): серверное → разработка, иначе SEO.
def _dept_service_issue(i) -> str:
    code = (getattr(i, 'code', '') or '').upper()
    if any(k in code for k in ('SERVER', 'DNS', 'SLOW', 'RESPONSE', 'THREAT',
                               'SITE_NOT_LOADED', 'SITE_ERROR', '5XX')):
        return 'разработка'
    return 'SEO'


_SEV2PRIO = {'fatal': 'critical', 'critical': 'critical', 'possible': 'important',
             'recommendation': 'recommendation', 'info': 'info'}


def _group_service_issues(items: list) -> list:
    """Схлопнуть ошибки сервиса по одной проблеме: один и тот же тип проблемы
    приходит по каждому сайту отдельно – собираем сайты в список.
    Возвращает dict: title, code, hosts (список), date (мин), count, first."""
    from collections import OrderedDict, Counter
    groups = OrderedDict()
    for i in items:
        title = getattr(i, 'title', '') or getattr(i, 'code', '')
        key = (title, getattr(i, 'code', ''))
        g = groups.get(key)
        if g is None:
            g = {'title': title, 'code': getattr(i, 'code', ''),
                 'hosts': [], 'date': getattr(i, 'date', ''),
                 'count': 0, 'first': i, 'states': Counter()}
            groups[key] = g
        g['count'] += 1
        host = getattr(i, 'host', '')
        if host and host not in g['hosts']:
            g['hosts'].append(host)
        st = getattr(i, 'state', '') or '—'
        g['states'][st] += 1
        d = getattr(i, 'date', '')
        if d and (not g['date'] or d < g['date']):
            g['date'] = d
    return list(groups.values())


# Коды состояния проблемы Вебмастера → человекочитаемо.
_WM_STATE_LABELS = {
    'IN_PROGRESS': 'на проверке',
    'CHECKING': 'на проверке',
    'PROBLEM_ACTUAL': 'проблема актуальна',
    'PRESENT': 'проблема актуальна',
    'ACTUAL': 'проблема актуальна',
    'NEW': 'новая',
}


def _state_human(code: str):
    """Код состояния → текст. Пусто/«—» → None (не выводим).
    Старый кеш с уже-человеческим текстом — отдаём как есть."""
    s = (code or '').strip()
    if not s or s == '—':
        return None
    up = s.upper()
    if up in _WM_STATE_LABELS:
        return _WM_STATE_LABELS[up]
    return s.lower()


def _format_states(states) -> str:
    """Counter кодов состояния → «16 - на проверке. 45 - проблема актуальна».
    Коды агрегируются по человекочитаемой метке."""
    from collections import Counter
    agg = Counter()
    for code, n in states.items():
        h = _state_human(code)
        if h:
            agg[h] += n
    return '\n'.join(f'{n} — {label}' for label, n in agg.most_common())


# Секции в порядке убывания релевантности:
# (source_key, title, has_priority)
_NOTIF_SECTIONS = [
    ('yandex_webmaster', 'Вебмастер. Почта',        True),
    ('gsc',              'Google Search Console',   True),
    ('ya_business',      'Я.Бизнес',                False),
    ('twogis',           '2ГИС',                    False),
    ('google_accounts',  'Google',                  False),
]


def _build_notifications_sheet(wb, notifications, service_issues=None):
    """Лист «Уведомления» – письма по источникам + ошибки прямо из сервисов
    (Вебмастер по API). Структурирован секциями. Добавляется всегда: при
    пустых данных показывает заглушку."""
    notifications = notifications or []
    service_issues = service_issues or []
    ws = wb.create_sheet('Уведомления')
    ws.sheet_view.showGridLines = False

    has_critical = (any(n.priority == 'critical' for n in notifications)
                    or any(getattr(i, 'severity', '') in ('fatal', 'critical')
                           for i in service_issues))
    ws.sheet_properties.tabColor = C.err if has_critical else C.accent

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 14   # Дата
    ws.column_dimensions['C'].width = 18   # Приоритет / пусто
    ws.column_dimensions['D'].width = 18   # Категория / пусто
    ws.column_dimensions['E'].width = 50   # Тема
    ws.column_dimensions['F'].width = 42   # Домены / Сайты
    ws.column_dimensions['G'].width = 60   # Превью / Состояние (Вебмастер-API)
    ws.column_dimensions['H'].width = 22   # Отдел / Кол-во
    ws.column_dimensions['I'].width = 22   # Отдел (секция Вебмастер-API)

    # ── Заголовок листа ──
    ws.merge_cells('B2:H2')
    c = ws['B2']
    c.value = 'Уведомления'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:H3')
    c = ws['B3']
    c.value = (
        'Письма от Яндекс.Вебмастера, GSC, Я.Бизнеса, 2ГИС и Google '
        'за период проверки. Красная вкладка = есть критические уведомления.'
    )
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 24

    # Нет ни писем, ни ошибок сервисов – показываем заглушку и выходим
    if not notifications and not service_issues:
        ws.merge_cells('B5:H5')
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
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
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
                ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
                pc = ws.cell(row=row, column=2)
                pc.value = f'  {p_label}  ({len(p_items)})'
                pc.font = _font(size=10, bold=True, color=p_color)
                pc.fill = _fill(p_bg)
                pc.alignment = _align(indent=2)
                ws.row_dimensions[row].height = 20
                row += 1

                # Шапка: одна строка на тему, домены списком + их количество
                for ci, h in enumerate(['Дата', 'Серьёзность', 'Категория', 'Тема',
                                        'Сайты', 'Кол-во', 'Отдел'], 2):
                    cell = ws.cell(row=row, column=ci)
                    cell.value = h
                    cell.font = _font(size=9, bold=True, color=C.text_muted)
                    cell.fill = _fill(C.surface)
                    cell.alignment = _align()
                    cell.border = _border()
                ws.row_dimensions[row].height = 20
                row += 1

                # Строки — одна на уникальную тему (без учёта доменной зоны),
                # все домены в колонке «Сайты», их число — в «Кол-во».
                groups = _group_notifs_by_theme(p_items)
                for g in sorted(groups, key=lambda x: len(x['domains']), reverse=True):
                    n0 = g['first']
                    domains_str = ', '.join(g['domains'])
                    ws.row_dimensions[row].height = _notif_row_height(domains_str, '')

                    for ci, (val, kw) in enumerate([
                        (g['date'], {'color': C.text_soft}),
                        (_NOTIF_PRIORITY_LABEL[priority], {'bold': priority == 'critical', 'color': p_color}),
                        (_NOTIF_CATEGORY_LABEL.get(n0.category, n0.category), {'color': C.text_soft}),
                        (g['theme'], {'bold': priority == 'critical', 'color': p_color}),
                        (domains_str, {'size': 9, 'color': C.text_soft}),
                        (len(g['domains']), {'size': 10, 'bold': True, 'color': C.text_soft}),
                        (_dept_notif(n0), {'size': 9, 'color': C.text_soft}),
                    ], 2):
                        cell = ws.cell(row=row, column=ci)
                        cell.value = val
                        cell.font = _font(**kw)
                        cell.alignment = _align(
                            wrap=True, vertical='top',
                            horizontal='center' if ci == 7 else 'general')
                        cell.border = _border(color=C.border_light)
                        if priority == 'critical' and ci in (5, 6, 7):
                            cell.fill = _fill(p_bg)

                    row += 1

                row += 1  # пробел между приоритетами

        elif source_key == 'twogis':
            # ── 2ГИС: одна строка на отзыв (без группировки), колонка
            # «Оценка» (★ + качество), превью = только ссылка «Читать». ──
            for ci, h in enumerate(['Дата', 'Оценка', '', 'Тема', '', 'Ссылка', 'Отдел'], 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = h
                cell.font = _font(size=9, bold=True, color=C.text_muted)
                cell.fill = _fill(C.surface)
                cell.alignment = _align()
                cell.border = _border()
            ws.row_dimensions[row].height = 20
            row += 1

            for n in sorted(items, key=lambda x: x.date or '', reverse=True):
                ws.row_dimensions[row].height = 30
                rating_txt, rating_color = _review_rating_cell(getattr(n, 'rating', None))
                review_url = getattr(n, 'review_url', None)

                for ci, (val, kw) in enumerate([
                    (n.date, {'color': C.text_soft}),
                    (rating_txt, {'bold': True, 'color': rating_color}),
                    ('', {}),
                    (n.subject, {'color': C.text}),
                    ('', {}),
                    ('', {}),   # ссылка проставляется ниже
                    (_dept_notif(n), {'size': 9, 'color': C.text_soft}),
                ], 2):
                    cell = ws.cell(row=row, column=ci)
                    cell.value = val
                    cell.font = _font(**kw)
                    cell.alignment = _align(wrap=True, vertical='top')
                    cell.border = _border(color=C.border_light)

                # Колонка «Ссылка» (G = 7): кликабельная «Читать полностью»
                link_cell = ws.cell(row=row, column=7)
                if review_url:
                    link_cell.value = 'Читать полностью'
                    link_cell.hyperlink = review_url
                    link_cell.font = _font(size=9, color=C.accent, underline='single')
                else:
                    link_cell.value = '–'
                    link_cell.font = _font(size=9, color=C.text_muted)
                link_cell.alignment = _align(vertical='top')
                link_cell.border = _border(color=C.border_light)

                row += 1

        else:
            # ── Источник без классификации: плоский список ──
            # Шапка
            for ci, h in enumerate(['Дата', '', '', 'Тема', 'Домены', 'Превью', 'Отдел'], 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = h
                cell.font = _font(size=9, bold=True, color=C.text_muted)
                cell.fill = _fill(C.surface)
                cell.alignment = _align()
                cell.border = _border()
            ws.row_dimensions[row].height = 20
            row += 1

            groups = _group_notifs_by_theme(items)
            for g in sorted(groups, key=lambda x: x['date'] or '', reverse=True):
                n0 = g['first']
                domains_str = ', '.join(g['domains'])
                theme = g['theme']
                if g['count'] > 1:
                    theme = f'{theme}  ×{g["count"]}'
                ws.row_dimensions[row].height = _notif_row_height(
                    domains_str, n0.body_preview)

                for ci, (val, kw) in enumerate([
                    (g['date'], {'color': C.text_soft}),
                    ('', {}),
                    ('', {}),
                    (theme, {'bold': False, 'color': C.text}),
                    (domains_str, {'size': 9, 'color': C.text_soft}),
                    ((n0.body_preview or '')[:400], {'size': 9, 'color': C.text_soft}),
                    (_dept_notif(n0), {'size': 9, 'color': C.text_soft}),
                ], 2):
                    cell = ws.cell(row=row, column=ci)
                    cell.value = val
                    cell.font = _font(**kw)
                    cell.alignment = _align(wrap=True, vertical='top')
                    cell.border = _border(color=C.border_light)

                row += 1

        row += 2  # пробел между секциями

    # ── Секция «Вебмастер» – ошибки прямо из сервиса (API), не из почты ──
    if service_issues:
        from collections import defaultdict as _dd
        _n_problems = len(_group_service_issues(service_issues))
        _n_hosts = len({getattr(i, 'host', '') for i in service_issues})
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
        sc = ws.cell(row=row, column=2)
        sc.value = (f'Вебмастер  ({_n_problems} проблем на {_n_hosts} сайтах, '
                    f'{len(service_issues)} всего)')
        sc.font = _font(size=13, bold=True, color=C.accent)
        sc.fill = _fill(C.accent_soft)
        sc.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1

        prio_groups = _dd(list)
        for i in service_issues:
            prio_groups[_SEV2PRIO.get(getattr(i, 'severity', 'info'), 'info')].append(i)

        for priority in _NOTIF_PRIORITY_ORDER:
            p_items = prio_groups.get(priority, [])
            if not p_items:
                continue
            p_color = _NOTIF_PRIORITY_COLOR[priority]
            p_bg = _NOTIF_PRIORITY_BG[priority]
            p_label = _NOTIF_PRIORITY_LABEL[priority]

            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
            pc = ws.cell(row=row, column=2)
            pc.value = f'  {p_label}  ({len(p_items)})'
            pc.font = _font(size=10, bold=True, color=p_color)
            pc.fill = _fill(p_bg)
            pc.alignment = _align(indent=2)
            ws.row_dimensions[row].height = 20
            row += 1

            # Шапка: одна строка на проблему, сайты – списком + их состояния
            for ci, h in enumerate(['Дата', 'Серьёзность', 'Категория', 'Проблема',
                                    'Сайты', 'Состояние', 'Кол-во', 'Отдел'], 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = h
                cell.font = _font(size=9, bold=True, color=C.text_muted)
                cell.fill = _fill(C.surface)
                cell.alignment = _align()
                cell.border = _border()
            ws.row_dimensions[row].height = 20
            row += 1

            groups = _group_service_issues(p_items)
            for g in sorted(groups, key=lambda x: x['count'], reverse=True):
                hosts_str = ', '.join(g['hosts'])
                ws.row_dimensions[row].height = _notif_row_height(hosts_str, '')
                for ci, (val, kw) in enumerate([
                    (g['date'], {'color': C.text_soft}),
                    (p_label, {'bold': priority == 'critical', 'color': p_color}),
                    ('Диагностика', {'color': C.text_soft}),
                    (g['title'], {'bold': priority == 'critical', 'color': p_color}),
                    (hosts_str, {'size': 9, 'color': C.text_soft}),
                    (_format_states(g['states']), {'size': 9, 'color': C.text_soft}),
                    (len(g['hosts']), {'size': 10, 'bold': True, 'color': C.text_soft}),
                    (_dept_service_issue(g['first']), {'size': 9, 'color': C.text_soft}),
                ], 2):
                    cell = ws.cell(row=row, column=ci)
                    cell.value = val
                    cell.font = _font(**kw)
                    cell.alignment = _align(
                        wrap=True, vertical='top',
                        horizontal='center' if ci == 8 else 'general')
                    cell.border = _border(color=C.border_light)
                row += 1

            row += 1
        row += 2


# ── Лист «Ошибки сервисов» (Вебмастер/GSC/Метрика – из API) ─────────

_SVC_SECTION = [
    ('webmaster', 'Яндекс.Вебмастер'),
    ('gsc',       'Google Search Console'),
    ('metrika',   'Яндекс.Метрика'),
]
_SVC_SEV_LABEL = {
    'fatal': '🔴 Фатальная', 'critical': '🔴 Критическая',
    'possible': '🟠 Возможная', 'recommendation': '🟡 Рекомендация',
    'info': '⚪ Инфо',
}
_SVC_SEV_COLOR = {
    'fatal': C.err, 'critical': C.err, 'possible': C.warn,
    'recommendation': 'CA8A04', 'info': C.text_muted,
}
_SVC_SEV_ORDER = {'fatal': 0, 'critical': 1, 'possible': 2,
                  'recommendation': 3, 'info': 4}


def _build_service_issues_sheet(wb, service_issues):
    """Лист «Ошибки сервисов» – проблемы сайтов прямо из сервисов (не из почты).
    Добавляется только если есть данные."""
    issues = service_issues or []
    if not issues:
        return

    ws = wb.create_sheet('Ошибки сервисов')
    ws.sheet_view.showGridLines = False
    has_crit = any(getattr(i, 'severity', '') in ('fatal', 'critical') for i in issues)
    ws.sheet_properties.tabColor = C.err if has_crit else C.accent

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 28   # Сайт
    ws.column_dimensions['C'].width = 18   # Серьёзность
    ws.column_dimensions['D'].width = 50   # Проблема
    ws.column_dimensions['E'].width = 13   # Дата
    ws.column_dimensions['F'].width = 10   # Открыть

    ws.merge_cells('B2:F2')
    c = ws['B2']
    c.value = 'Ошибки сайтов из сервисов'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:F3')
    c = ws['B3']
    c.value = ('Проблемы напрямую из Яндекс.Вебмастера / GSC / Метрики (диагностика: '
               'сайтмапы, дубли, мусорные ссылки, ошибки сервера и индексации). '
               'Не из почты – из самих сервисов по API.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 30

    from collections import defaultdict
    by_service = defaultdict(list)
    for i in issues:
        by_service[getattr(i, 'service', 'webmaster')].append(i)

    row = 5
    for svc_key, svc_title in _SVC_SECTION:
        svc_items = by_service.get(svc_key, [])
        if not svc_items:
            continue

        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        sc = ws.cell(row=row, column=2)
        sc.value = f'{svc_title}  ({len(svc_items)})'
        sc.font = _font(size=13, bold=True, color=C.accent)
        sc.fill = _fill(C.accent_soft)
        sc.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1

        for ci, h in enumerate(['Сайт', 'Серьёзность', 'Проблема', 'Дата', 'Открыть'], 2):
            cell = ws.cell(row=row, column=ci)
            cell.value = h
            cell.font = _font(size=9, bold=True, color=C.text_muted)
            cell.fill = _fill(C.surface)
            cell.alignment = _align()
            cell.border = _border()
        ws.row_dimensions[row].height = 20
        row += 1

        for i in sorted(svc_items, key=lambda x: (_SVC_SEV_ORDER.get(
                getattr(x, 'severity', 'info'), 9), getattr(x, 'host', ''))):
            sev = getattr(i, 'severity', 'info')
            sev_color = _SVC_SEV_COLOR.get(sev, C.text_muted)
            ws.row_dimensions[row].height = 30

            for ci, (val, kw) in enumerate([
                (getattr(i, 'host', ''), {'size': 10, 'color': C.text}),
                (_SVC_SEV_LABEL.get(sev, sev),
                 {'size': 9, 'bold': sev in ('fatal', 'critical'), 'color': sev_color}),
                (getattr(i, 'title', '') or getattr(i, 'code', ''),
                 {'size': 10, 'color': C.text_soft}),
                (getattr(i, 'date', ''), {'size': 9, 'color': C.text_muted}),
            ], 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = val
                cell.font = _font(**kw)
                cell.alignment = _align(wrap=True, vertical='top')
                cell.border = _border(color=C.border_light)

            # «Открыть» – ссылка в панель сервиса
            link_cell = ws.cell(row=row, column=6)
            _u = getattr(i, 'url', '')
            if _u:
                link_cell.value = 'открыть'
                link_cell.hyperlink = _u
                link_cell.font = _font(size=9, color=C.accent, underline='single')
            else:
                link_cell.value = '–'
                link_cell.font = _font(size=9, color=C.text_muted)
            link_cell.alignment = _align(horizontal='center')
            link_cell.border = _border(color=C.border_light)

            row += 1

        row += 2


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
    c.value = ('Сверяем телефон, почту и адрес на главной каждого города (шапка + '
               'подвал) с «Картой присутствия». Телефон: ожидается SEO-номер (если '
               'нет – рекламный, затем общий). Зелёное «✓» – совпало с КП, красное – '
               'нет. «есть» (серое) – на сайте есть, но в КП этого поля нет (сверять '
               'не с чем, дополнить КП). «–» – нет ни в КП, ни на сайте. '
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

        cc = ws.cell(row=row, column=2, value=r.city or kp.get('city'))
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
                cell.value = '–'           # и в КП нет, и на сайте нет – нечего показать
                cell.font = _font(size=10, color=C.text_muted)
            elif iss['status'] == 'ok':
                cell.value = '✓'
                cell.font = _font(size=10, bold=True, color=C.ok)
                cell.fill = _fill(C.ok_soft)
            elif iss['status'] == 'info':
                # на сайте есть, но в КП нет – не сверка, но и не «нет». «есть».
                cell.value = 'есть'
                cell.font = _font(size=9, color=C.text_soft)
                if iss.get('comment'):
                    cell.comment = Comment(iss['comment'], 'Site Checker',
                                           height=80, width=240)
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


# Страна по доменной зоне URL (для листа «404 из Метрики»).
_TLD_COUNTRY = {
    'ru': 'Россия', 'kz': 'Казахстан', 'by': 'Беларусь', 'kg': 'Кыргызстан',
    'uz': 'Узбекистан', 'am': 'Армения', 'az': 'Азербайджан', 'ua': 'Украина',
}


def _country_by_url(url: str) -> str:
    """Страна по TLD домена URL. Неизвестно → прочерк."""
    from urllib.parse import urlparse as _up
    host = (url or '').strip()
    try:
        netloc = _up(host).netloc or host.split('/')[0]
    except ValueError:
        netloc = host.split('/')[0]
    netloc = netloc.split(':')[0].strip('.')   # без порта
    tld = netloc.rsplit('.', 1)[-1].lower() if '.' in netloc else ''
    return _TLD_COUNTRY.get(tld, '–')


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
    service_issues: list = None,   # список ServiceIssue – добавит лист «Ошибки сервисов»
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

                # Страна — определяем по доменной зоне URL
                cell = ws4.cell(row=row_idx, column=2)
                cell.value = _country_by_url(fr['url'])
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
    # Лист «Уведомления» добавляем всегда (при пустых данных – заглушка).
    # Сюда же идут ошибки из Вебмастера по API (секция «Вебмастер»).
    _build_notifications_sheet(wb, notifications, service_issues)

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
