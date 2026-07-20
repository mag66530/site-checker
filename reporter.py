"""
reporter.py - формирование xlsx-отчёта.

Структура (как в Node.js версии):
  • Лист «Обзор» - метрики, сводка, параметры прогона
  • Лист «Все детали» - каждая проверка отдельной строкой
  • Лист «Битые тексты» - добавляется ТОЛЬКО если есть находки

Колонки в «Все детали»:
  Город | Поддомен | Тип | URL | Код | Статус |
  Скорость, с | Оценка скорости | Битые переменные | Откуда перешли
"""
import re
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter, range_boundaries


# ── Стили (цвета как в Node.js версии) ──────────────────────────────


class C:
    text = '09090B'
    text_soft = '3F3F46'
    text_muted = '71717A'
    # Раньше border_light был 'E4E4E7' - настолько светлый, что в Excel
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
    'redirect_loop': 'Циклический редирект',
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
    Если статус «Работает» и скорость «ОК» - поле пустое, всё в порядке.
    (Битые переменные и контент-баги показаны в своих колонках/листе,
    здесь их не дублируем - иначе тег появлялся бы у рабочих страниц.)

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
# смысловой каждый - так таблица читается, а тип цены/кнопки виден в ячейке.

def _price_cell(bk):
    # Одна галочка: есть цена в любом виде (₽ ИЛИ «по запросу») → ✓; нет ни того
    # ни другого или скрыто стилями → БАГ. Без «₽ + запрос» - это лишний шум.
    price = bk.get('price')
    if price and price.required and not price.present:
        return ('БАГ', 'bug')
    if price and price.present:
        return ('✓', 'ok')
    return ('-', 'absent')


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
    return ('-', 'absent')


_COLLAPSE = [
    {'trigger': 'price', 'label': 'Цена',
     'desc': 'Цена на карточках: «₽» - рублёвая, «по запросу» - цена по запросу. '
             '«БАГ» - цены нет вовсе.',
     'keys': {'price', 'price_real', 'price_request'}, 'fn': _price_cell},
    {'trigger': 'btn_order', 'label': 'Кнопка заказа',
     'desc': 'Кнопка заказа: «в корзину» (товар с ценой) или «1 клик» (по запросу). '
             '«БАГ» - нет ни одной.',
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
            continue                       # под-блок схлопнут - пропускаем
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
    # Жёлтое предупреждение (не красный баг), напр. «Фото товаров»: стоит заглушка.
    if getattr(b, 'warn', False):
        return (f'Заглушка ({b.count})' if b.count else 'Заглушка', 'warn')
    if b.required and not b.present:
        if b.count:
            return (f'БАГ ({b.count})', 'bug')
        return ('БАГ', 'bug')
    if b.present:
        if b.count is not None:
            return (b.count, 'count')
        return ('✓', 'ok')
    return ('-', 'absent')


def _style_cell(cell, value, state):
    cell.value = value
    if state == 'bug':
        cell.font = _font(size=10, bold=True, color=C.err); cell.fill = _fill(C.err_soft)
    elif state == 'warn':          # жёлтое предупреждение (заглушка фото и т.п.)
        cell.font = _font(size=10, bold=True, color=C.warn); cell.fill = _fill(C.warn_soft)
    elif state == 'ok':
        cell.font = _font(size=10, bold=True, color=C.ok); cell.fill = _fill(C.ok_soft)
    elif state == 'okinfo':       # значение-текст (по запросу / в корзину…)
        cell.font = _font(size=9, color=C.ok)
    elif state == 'count':
        cell.font = _font(size=10, color=C.text_soft)
    else:                          # absent
        cell.value = '-'
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
        parts.append('адреса не совпадают с КП - ' + ex
                     + (f' и ещё {len(mm) - 5}' if len(mm) > 5 else ''))
    pp = getattr(r, 'page_phone', None)
    if pp and pp.get('status') in ('bug', 'critical'):
        parts.append(f'телефон: {pp.get("comment", "не совпадает с КП")}')
    return '; '.join(parts)


def _broken_links_text(r):
    """Битые ссылки (404/410) в контенте страницы - краткий текст для отчёта."""
    bl = getattr(r, 'broken_links', None)
    if not bl or not bl.get('broken'):
        return ''
    items = bl['broken']
    ex = '; '.join(f'{b["code"]} {b["url"]}' for b in items[:3])
    more = f' и ещё {len(items) - 3}' if len(items) > 3 else ''
    return f'битые ссылки ({len(items)}): ' + ex + more


# Человеческие формулировки багов для «Что чинить» / «Что не так». Иначе из
# машинного названия столбца получалось коряво: «нет: Цена (есть)».
_BUG_PHRASES = {
    'price': 'нет цены',
    'price_real': 'нет цены суммой',
    'btn_order': 'нет кнопки заказа',
    'product_cards': 'нет карточек товаров',
    'photos': 'нет фото у части товаров',
    'h1': 'нет заголовка H1',
    'breadcrumbs': 'нет хлебных крошек',
    'img_alt': 'картинки без alt',
    'content_text': 'нет текста на странице',
    'rec_price': 'нет цен в нижних блоках',
    'form_nf': 'нет формы «Не нашли что искали»',
    'tech_map': 'нет карты',
    'tech_images': 'нет картинок',
    'tech_search': 'нет строки поиска',
    'hdr_phone': 'нет телефона в шапке',
    'hdr_callback': 'нет «Заказать звонок» в шапке',
    'hdr_request': 'нет «Оставить заявку» в шапке',
    'hdr_city': 'нет выбора города в шапке',
    'ftr_phone': 'нет телефона в подвале',
    'ftr_email': 'нет e-mail в подвале',
    'ftr_writeus': 'нет «Написать нам» в подвале',
    'ftr_address': 'нет адреса в подвале',
}


def _problem_text(r):
    """Понятная формулировка проблемы страницы для списка «Что чинить»."""
    parts = []
    _ct = _contacts_problem_text(r)
    if _ct:
        parts.append(_ct)
    content = getattr(r, 'content', None)
    if content is not None:
        if getattr(content, 'is_soft_404', False):
            parts.append('страница отдаёт 404 (не найдена) - проверить ссылку или убрать из каталога')
        elif getattr(content, 'page_kind', '') == 'empty':
            parts.append('раздел пуст - нет ни товаров, ни подразделов')
        else:
            # Человеческая фраза по каждому багу (+ число для фото, + пояснение,
            # напр. «в коде есть, но покупатель не видит»).
            bugs = []
            for b in content.bugs:
                phrase = _BUG_PHRASES.get(b.key, b.label)
                if b.key in ('photos', 'img_alt') and getattr(b, 'count', None):
                    phrase += f' ({b.count})'
                if getattr(b, 'note', ''):
                    # У картинок без alt пояснение - список адресов: через «:»
                    if b.key == 'img_alt':
                        phrase += f': {b.note}'
                    else:
                        phrase += f' ({b.note})'
                bugs.append(phrase)
            if bugs:
                parts.append(', '.join(bugs))
    _bl = _broken_links_text(r)
    if _bl:
        parts.append(_bl)
    return '; '.join(parts) if parts else 'проблема'


def _build_structure_sheet(wb, results):
    """Лист структурной проверки - дашборд, что чинить, сводка и детали."""
    # Тех. страницы выносим отдельной секцией (у них нет структуры - только
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
    c.value = ('Что должно быть на каждой странице для продаж - и чего не хватает. '
               'Красное нужно чинить, серый прочерк - этого просто нет (норма).')
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

    # ── «Что чинить» - главный блок ──
    bug_pages = [r for r in pages if r.content_bugs > 0]
    # Тех. страницы с расхождением контактов с КП (адреса городов / телефон) -
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
        hc.value = f'  Что чинить - {len(bug_pages)} {_plural_pages(len(bug_pages))}'
        hc.font = _font(size=14, bold=True, color=C.err)
        hc.fill = _fill(C.err_soft)
    else:
        hc.value = '  ✓ Всё в порядке - структурных проблем не найдено'
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
            _ptext = _problem_text(r)
            mc = ws.cell(row=row, column=5, value=_ptext)
            mc.font = _font(size=10, color=C.err)
            # Одна строка фиксированной высоты: длинный текст (списки адресов)
            # визуально обрезается, НЕ раздувая таблицу. Полный текст - в
            # тултипе (навести курсор), в строке формул (клик по ячейке) или
            # растянув строку вручную. Данные не меняем - только отображение.
            mc.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 20
            if len(_ptext) > 100:
                mc.comment = Comment(_ptext, 'Site Checker', height=260, width=420)
            for cc2 in range(5, last_col + 1):
                ws.cell(row=row, column=cc2).fill = _fill(band)
                ws.cell(row=row, column=cc2).border = _border(color=C.border_light)
            row += 1
        if len(bug_pages) > 50:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last_col)
            ws.cell(row=row, column=2,
                    value=f'… и ещё {len(bug_pages) - 50} - см. таблицы ниже').font = \
                _font(size=10, italic=True, color=C.text_muted)
            row += 1

    # ── Подробные таблицы по типам ──
    row += 2
    ws.cell(row=row, column=2, value='Подробно по типам страниц').font = \
        _font(size=13, bold=True, color=C.text)
    ws.cell(row=row + 1, column=2,
            value='✓ есть · БАГ обязательного нет · «-» необязательного нет (норма) · '
                  'число = сколько найдено. Наведите курсор на заголовок столбца - пояснение.').font = \
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
        gc.value = (f'  {group_label} - {len(group_pages)} стр.'
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
                # У заглушки фото - всплывающая подсказка с названиями товаров.
                if state == 'warn' and col.get('kind') == 'block':
                    _b = by_key.get(col.get('key'))
                    _nm = getattr(_b, 'note', '') if _b else ''
                    if _nm:
                        cell.comment = Comment('Стоит заглушка «нет фото» у товаров: '
                                               + _nm, 'Site Checker', height=120, width=300)
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
        gc.value = (f'  Технические страницы - {len(tech)} стр.'
                    + (f'  ·  проблем: {_bad}' if _bad else '  ·  все в порядке'))
        gc.font = _font(size=11, bold=True, color=C.err if _bad else C.ok)
        gc.fill = _fill(C.accent_soft)
        gc.alignment = _align(indent=1, vertical='center')
        for cc in range(2, 11):
            ws.cell(row=row, column=cc).fill = _fill(C.accent_soft)
        ws.row_dimensions[row].height = 22
        row += 1
        _tech_headers = [
            (2, 'Страница', 'Название страницы - кликабельная ссылка, ведёт на страницу.'),
            (3, 'Статус', 'Открывается ли страница: «Работает» / код ответа (404 и т.п.) / «404-заглушка» (отдаёт 200, но контент «страница не найдена»).'),
            (4, 'Проблем', 'Сколько проблем на странице: структурные баги, битые переменные, расхождения контактов с КП и битые ссылки (404).'),
            (5, 'H1', 'Заголовок H1. Обязателен - у нормальной страницы он есть.'),
            (6, 'Крошки', 'Хлебные крошки. Справочно: показываем есть/нет, отсутствие на служебной странице не баг.'),
            (7, 'Текст', 'Есть ли на странице собственный текст (помимо сквозных шапки и подвала). Обязателен.'),
            (8, 'Битые перем.', 'Битые шаблонные переменные ({{…}}, %name% и т.п.). Число = сколько найдено.'),
            (9, 'Элементы страницы', 'Спец-проверки в зависимости от страницы: картинки, ссылка на каталог, карта, форма обратной связи, строка поиска (✓ есть / - нет / БАГ - обязательного нет). Обязательны: карта на «Контактах», картинки на «О компании», строка поиска на странице поиска. Если включена проверка ссылок - тут же «Ссылки: N ✓» или «N битых» (404/410).'),
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

            # Страница - человеческое название (Оплата, Доставка…) как ссылка.
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

            # H1 / Крошки: если страница не открылась или это 404-заглушка -
            # структуры нет, ставим «-». Иначе берём из блоков контента.
            by_key = {b.key: b for b in r.content.blocks} if (r.is_ok and r.content) else {}
            for ci, key in ((5, 'h1'), (6, 'breadcrumbs'), (7, 'content_text')):
                cell = ws.cell(row=row, column=ci)
                cell.alignment = _align(horizontal='center', indent=0)
                cell.border = _border(color=C.border_light)
                if not by_key or _soft:
                    cell.value = '-'; cell.font = _font(size=10, color=C.text_muted)
                    cell.fill = _fill(band)
                else:
                    value, state = _cell_state({'kind': 'block', 'key': key}, by_key)
                    _style_cell(cell, value, state)
                    if state in ('absent', 'count', 'okinfo'):
                        cell.fill = _fill(band)

            # Битые переменные - число найденных.
            _ti = len(r.text_issues or []) if r.is_ok else 0
            vc = ws.cell(row=row, column=8)
            vc.alignment = _align(horizontal='center', indent=0)
            vc.border = _border(color=C.border_light)
            if _ti:
                vc.value = _ti; vc.font = _font(size=10, bold=True, color=C.err)
                vc.fill = _fill(C.err_soft)
            else:
                vc.value = '-'; vc.font = _font(size=10, color=C.text_muted)
                vc.fill = _fill(band)

            # Элементы страницы - спец-проверки (картинки/каталог-ссылка/карта/форма)
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
                    _parts.append(f'{b.label} {"✓" if b.present else "-"}')
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
            elif _blk:                       # проверяли - все ссылки открылись
                _parts.append(f'Ссылки: {_blk.get("checked", 0)} ✓')
            ec = ws.cell(row=row, column=9)
            ec.alignment = _align(indent=1)
            ec.border = _border(color=C.border_light)
            ec.fill = _fill(C.err_soft if _addr_bad else band)
            ec.value = ' · '.join(_parts) if _parts else '-'
            ec.font = _font(size=9, color=C.err if _addr_bad else
                            (C.text_soft if _parts else C.text_muted))

            # Что не так - подробно: структурные баги (нет карты/картинок/строки
            # поиска и т.п.) и расхождения контактов с КП. Пусто, если проблем нет.
            _has_problem = ((r.content_bugs or 0) > 0
                            or bool(_contacts_problem_text(r)) or _broken_n > 0)
            _wn = _problem_text(r) if _has_problem else ''
            wn = ws.cell(row=row, column=10, value=_wn or '-')
            # Одна строка: длинные списки не растягивают таблицу; полный текст -
            # в тултипе / строке формул / при растяжении строки вручную.
            wn.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 20
            if len(_wn) > 100:
                wn.comment = Comment(_wn, 'Site Checker', height=260, width=420)
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
# каждому домену отдельно - схлопываем в одну строку, домены в список).
_DOMAIN_TLDS = (
    # рф/ru/su + СНГ/региональные зоны (.kz/.kg/.uz/.ua и т.д.) - чтобы один
    # бренд в разных зонах не дробил тему на отдельные строки + gTLD.
    'ru|рф|su|by|kz|kg|uz|ua|am|az|ge|md|tj|tm|ee|lv|lt|'
    'com|net|org|info|biz|pro|online|store|site|shop|me|cc|io'
)
# URL c путём целиком (group1 = host+path) - для извлечения режем по '/'.
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
    """Тема без конкретного домена/URL - ключ группировки и текст для отчёта."""
    s = subject or ''
    s = _URL_RE.sub('', s)
    s = _HOST_RE.sub('', s)
    s = re.sub(r'\s+', ' ', s).strip(' .,:;/---«»"\'')
    return s or (subject or '').strip()


def _group_notifs_by_theme(items: list) -> list:
    """Схлопнуть письма с одинаковой темой в группы.

    Возвращает список dict: theme, date (минимальная), domains (список),
    first (репрезентативное письмо), count. Порядок - первое появление темы.
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
        return '-', C.text_muted
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
    приходит по каждому сайту отдельно - собираем сайты в список.
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
        st = getattr(i, 'state', '') or '-'
        g['states'][st] += 1
        d = getattr(i, 'date', '')
        if d and (not g['date'] or d < g['date']):
            g['date'] = d
    return list(groups.values())


# Коды состояния проблемы Вебмастера → человекочитаемо.
_WM_STATE_LABELS = {
    'IN_PROGRESS': 'на проверке',
    'CHECKING': 'на проверке',
    'UNDEFINED': 'на проверке',   # состояние не определено = идёт перепроверка
    'PROBLEM_ACTUAL': 'проблема актуальна',
    'PRESENT': 'проблема актуальна',
    'ACTUAL': 'проблема актуальна',
    'NEW': 'новая',
}


def _state_human(code: str):
    """Код состояния → текст. Пусто/«-» → None (не выводим).
    Старый кеш с уже-человеческим текстом - отдаём как есть."""
    s = (code or '').strip()
    if not s or s == '-':
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
    return '\n'.join(f'{n} - {label}' for label, n in agg.most_common())


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
    """Лист «Уведомления» - письма по источникам + ошибки прямо из сервисов
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

    # Нет ни писем, ни ошибок сервисов - показываем заглушку и выходим
    if not notifications and not service_issues:
        ws.merge_cells('B5:H5')
        c = ws['B5']
        c.value = ('За период проверки писем не найдено. '
                   'Если ждёте уведомления - проверьте секреты почты и пароли приложений '
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

                # Строки - одна на уникальную тему (без учёта доменной зоны),
                # все домены в колонке «Сайты», их число - в «Кол-во».
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
                    link_cell.value = '-'
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

    # ── Секция «Вебмастер» - ошибки прямо из сервиса (API), не из почты ──
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

            # Шапка: одна строка на проблему, сайты - списком + их состояния
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


# ── Лист «Ошибки сервисов» (Вебмастер/GSC/Метрика - из API) ─────────

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
    """Лист «Ошибки сервисов» - проблемы сайтов прямо из сервисов (не из почты).
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
               'Не из почты - из самих сервисов по API.')
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

            # «Открыть» - ссылка в панель сервиса
            link_cell = ws.cell(row=row, column=6)
            _u = _wm_alive_url(getattr(i, 'url', ''))
            if _u:
                link_cell.value = 'открыть'
                link_cell.hyperlink = _u
                link_cell.font = _font(size=9, color=C.accent, underline='single')
            else:
                link_cell.value = '-'
                link_cell.font = _font(size=9, color=C.text_muted)
            link_cell.alignment = _align(horizontal='center')
            link_cell.border = _border(color=C.border_light)

            row += 1

        row += 2


# ── Лист «Индексация» (п.1.7: robots.txt / noindex / canonical) ─────


def _idx_signals_text(ix):
    """Краткая сводка сигналов индексации страницы для колонки «Сигналы»."""
    parts = []
    if ix.get('robots_disallowed'):
        parts.append(f'robots.txt: Disallow {ix.get("robots_rule")}')
    if ix.get('meta_noindex'):
        parts.append(f'meta: {ix.get("meta_robots")}')
    if ix.get('x_robots_noindex'):
        parts.append(f'X-Robots-Tag: {ix.get("x_robots")}')
    if ix.get('canonical_disallowed'):
        parts.append(f'canonical → закрытый URL: {ix.get("canonical")}')
    elif ix.get('canonical_self') is False:
        parts.append(f'canonical → {ix.get("canonical")}')
    return '; '.join(parts)


def _build_indexing_sheet(wb, results, indexing_summary):
    """Лист проверки индексации: расхождения сигналов страниц с robots.txt
    (noindex на открытой в robots странице, canonical на закрытый URL)
    + противоречия sitemap ↔ robots.txt.
    Добавляется только если проверка индексации выполнялась."""
    checked = [r for r in results if getattr(r, 'indexing', None)]
    if not checked and not indexing_summary:
        return

    bad = [r for r in checked if r.indexing.get('issues')]
    warned = [r for r in checked if (not r.indexing.get('issues')
                                     and r.indexing.get('warnings'))]
    sm_dis = (indexing_summary or {}).get('disallowed') or []
    _blanket = (indexing_summary or {}).get('blanket_disallow') or []
    _assets_closed = (indexing_summary or {}).get('assets_closed') or []
    _aud_mc = (((indexing_summary or {}).get('sitemap_audit') or {})
               .get('missing_catalog') or {})
    _aud_missing = ((_aud_mc.get('categories') or [])
                    + (_aud_mc.get('filters') or [])
                    + (_aud_mc.get('services') or []))
    _hm_junk_top = (((indexing_summary or {}).get('html_sitemap') or {})
                    .get('junk_links') or [])
    has_bugs = bool(bad or sm_dis or _blanket or _assets_closed
                    or _aud_missing or _hm_junk_top)

    ws = wb.create_sheet('Индексация')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 18   # Город
    ws.column_dimensions['C'].width = 14   # Тип
    ws.column_dimensions['D'].width = 62   # URL / путь
    ws.column_dimensions['E'].width = 60   # Сигналы / правило
    ws.column_dimensions['F'].width = 3

    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = 'Проверка индексации (п.1.7)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('Эталон - robots.txt сайта. Ошибка = РАСХОЖДЕНИЕ сигналов страницы '
               'с robots: в robots страница открыта, а на ней noindex (meta или '
               'X-Robots-Tag), либо canonical ведёт на закрытый в robots URL. '
               'Закрыта в robots и noindex - согласовано, так задумано, не '
               'показываем. Плюс «верно настроен rel=canonical»: ровно один тег, '
               'указывает на себя, не на чужой домен; отсутствие тега - '
               'предупреждение. hreflang: если теги есть - валидируем (коды '
               'языков, абсолютные URL, self-reference); отсутствие - не '
               'ошибка (одноязычному сайту не нужен). Отдельно: пути из '
               'sitemap/каталога, закрытые Disallow, - противоречие (sitemap '
               'говорит «в индекс», robots - «нельзя»).')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 44

    row = 5

    # ── Сводка ──
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    _rs = (indexing_summary or {}).get('robots_status')
    _sm = (indexing_summary or {}).get('sitemaps') or []
    bits = [f'Проверено страниц: {len(checked)}',
            f'расхождений с robots: {len(bad)}',
            f'предупреждений: {len(warned)}']
    if indexing_summary:
        bits.append(f'путей каталога проверено по robots: '
                    f'{indexing_summary.get("checked", 0)}, '
                    f'под Disallow: {len(sm_dis)}')
        if _rs is not None:
            bits.append(f'robots.txt: HTTP {_rs}'
                        + (f', Sitemap-директив: {len(_sm)}' if _rs == 200 else ''))
    # hreflang: подтверждение, что проверка была (отсутствие тегов - не ошибка).
    _hl_pages = sum(1 for r in checked if r.indexing.get('hreflang_count'))
    _hl_bad = sum(1 for r in checked
                  if any('hreflang' in w for w in r.indexing.get('warnings') or []))
    if _hl_pages:
        bits.append(f'hreflang: на {_hl_pages} страницах'
                    + (f', с ошибками: {_hl_bad}' if _hl_bad else ', ок'))
    else:
        bits.append('hreflang: не используется (одноязычный сайт - ок)')
    c.value = ' · '.join(bits)
    c.font = _font(size=10, bold=True,
                   color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 30
    row += 2

    # ── Секция 1: закрытые страницы выборки (сгруппированы по проблеме) ──
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = f'Расхождения с robots.txt  ({len(bad)})'
    c.font = _font(size=13, bold=True, color=C.err if bad else C.ok)
    c.fill = _fill(C.accent_soft)
    c.alignment = _align(indent=1)
    ws.row_dimensions[row].height = 24
    row += 1

    if not bad:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = '✅ Расхождений с robots.txt нет - сигналы страниц согласованы.'
        c.font = _font(size=10, color=C.ok)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 22
        row += 2
    else:
        row = _render_issue_groups(
            ws, row, _issue_groups(bad, 'indexing', 'issues'), C.err)

    # ── Секция 2: предупреждения (сгруппированы по замечанию) ──
    if warned:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = f'Предупреждения  ({len(warned)})'
        c.font = _font(size=13, bold=True, color=C.warn)
        c.fill = _fill(C.accent_soft)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1
        def _idx_extra(r):
            _ext = (getattr(r, 'indexing', None) or {}).get('ext_nofollow') or []
            return ('внешние: ' + ', '.join(_ext[:4])
                    + (f' … +{len(_ext) - 4}' if len(_ext) > 4 else '')
                    if _ext else '')
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'indexing', 'warnings'), C.warn,
            extra=_idx_extra)

    # ── Секция 3: sitemap ↔ robots противоречия ──
    if indexing_summary:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = (f'Противоречия sitemap ↔ robots.txt '
                   f'({len(sm_dis)}) - {indexing_summary.get("host", "")}')
        c.font = _font(size=13, bold=True, color=C.err if sm_dis else C.ok)
        c.fill = _fill(C.accent_soft)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1
        if indexing_summary.get('error'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = f'⚠ Проверка не выполнена: {indexing_summary["error"]}'
            c.font = _font(size=10, color=C.warn)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 22
            row += 1
        elif not sm_dis:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = ('✅ Все пути каталога (категории, фильтры, товары) '
                       'открыты в robots.txt.')
            c.font = _font(size=10, color=C.ok)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 22
            row += 1
        else:
            # Группируем по правилу: одно правило Disallow бьёт сотни путей -
            # без группировки каждая строка повторяет одно и то же правило.
            _by_rule = {}
            for d in sm_dis:
                _agent = d.get('agent') or '*'
                _rule = f'Disallow: {d.get("rule")}'
                if _agent != '*':
                    _rule += f' (User-agent: {_agent})'
                _by_rule.setdefault(_rule, []).append(d.get('path', ''))
            _MAX_PATHS = 100
            for _rule, _paths in sorted(_by_rule.items(),
                                        key=lambda kv: -len(kv[1])):
                ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
                c = ws.cell(row=row, column=2)
                c.value = (f'{_rule}  -  путей из sitemap/каталога: {len(_paths)}')
                c.font = _font(size=10, bold=True, color=C.err)
                c.fill = _fill(C.surface)
                c.alignment = _align(wrap=True, indent=1)
                c.border = _border()
                ws.row_dimensions[row].height = 22
                row += 1
                for _p in _paths[:_MAX_PATHS]:
                    ws.merge_cells(start_row=row, start_column=2,
                                   end_row=row, end_column=5)
                    c = ws.cell(row=row, column=2)
                    c.value = _p
                    c.font = _font(size=9, color=C.text_soft)
                    c.alignment = _align(indent=2)
                    c.border = _border(color=C.border_light)
                    ws.row_dimensions[row].height = 16
                    row += 1
                if len(_paths) > _MAX_PATHS:
                    ws.merge_cells(start_row=row, start_column=2,
                                   end_row=row, end_column=5)
                    c = ws.cell(row=row, column=2)
                    c.value = f'… и ещё {len(_paths) - _MAX_PATHS} путей'
                    c.font = _font(size=9, italic=True, color=C.text_muted)
                    c.alignment = _align(indent=2)
                    ws.row_dimensions[row].height = 16
                    row += 1
                row += 1
        row += 1

        def _line(text, color, bold=False):
            nonlocal row
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = text
            c.font = _font(size=10, bold=bold, color=color)
            c.alignment = _align(wrap=True, indent=1)
            ws.row_dimensions[row].height = 20
            row += 1

        # ── Секция 3а: соблюдение директив - заблокированные страницы вживую ──
        # Disallow сам по себе не мешает URL попасть в индекс без сниппета,
        # если на него где-то есть ссылка - надёжна защита с доп. noindex.
        _dc = indexing_summary.get('directive_check')
        if _dc and not indexing_summary.get('error'):
            _dc_finds = _dc.get('findings') or []
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = f'Заблокированные страницы: проверено вживую  ({len(_dc_finds)})'
            c.font = _font(size=13, bold=True,
                           color=C.warn if _dc_finds else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            if not _dc_finds:
                _line(f'✅ Заблокированные в robots.txt страницы (проверено вживую '
                      f'{_dc.get("checked", 0)}) либо недоступны напрямую, либо '
                      f'дополнительно закрыты noindex.', C.ok)
            else:
                _line('Эти страницы реально отвечают 200 и БЕЗ собственного '
                      'noindex - держатся только на честном слове robots.txt:',
                      C.text_muted)
                for _f in _dc_finds:
                    _line(f'⚠ {_f.get("path", "")}: Disallow: {_f.get("rule", "")} - '
                          f'отвечает 200, noindex не стоит', C.warn)
            row += 1

        # ── Секция 4: мусор не закрыт в robots (ТЗ 3.3.4.2) ──
        junk = indexing_summary.get('junk_open')
        if junk is not None and not indexing_summary.get('error'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = f'Служебные страницы не закрыты в robots.txt  ({len(junk)})'
            c.font = _font(size=13, bold=True, color=C.err if junk else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            if not junk:
                _line('✅ Пагинация, сортировки, метки UTM/печати, AJAX-попапы, '
                      'служебные экшены и каталоги, поиск, корзина, сравнение, '
                      'оформление заказа, личный кабинет и админ. панель '
                      'закрыты в robots.txt (или не существуют на сайте).', C.ok)
            else:
                _line('Эти страницы отвечают 200, но НЕ закрыты в robots - '
                      'мусор попадает в обход робота:', C.text_muted)
                for j in junk:
                    _line(f'{j.get("label", "")}: {j.get("path", "")}', C.err)
            row += 1

        # ── Секция 4.1: пагинация (canonical для Яндекса, JS для Google) ──
        _pg = indexing_summary.get('pagination')
        if _pg:
            _pg_bad = _pg.get('status') == 200 and _pg.get('canon_ok') is False
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = f'Пагинация  ({_pg.get("base", "")})'
            c.font = _font(size=13, bold=True, color=C.warn if _pg_bad else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            if _pg.get('status') != 200:
                _line('✓ Вторая страница пагинации (?PAGEN_1=2) не отдаёт 200 '
                      '(редирект/404) - дублей пагинации нет.', C.ok)
            elif _pg.get('canon_ok'):
                _line(f'✅ Для Яндекса: на странице пагинации rel=canonical '
                      f'без номера страницы ({_pg.get("canonical", "")}).', C.ok)
            elif _pg.get('canonical'):
                _line(f'⚠ canonical пагинации содержит номер страницы '
                      f'({_pg.get("canonical", "")}) - для Яндекса нужен '
                      f'canonical на категорию без номера.', C.warn)
            else:
                _line('⚠ На странице пагинации (?PAGEN_1=2) нет rel=canonical '
                      '- для Яндекса нужен canonical на категорию.', C.warn)
            if _pg.get('loadmore') is True:
                _line('✅ Для Google: на категории найдена JS-подгрузка '
                      '(«показать ещё») - контент дозагружается на одной '
                      'странице.', C.ok)
                # Бесконечная прокрутка обязана дублироваться ссылками
                # пагинации в HTML - JS-подгрузку роботы не крутят.
                if _pg.get('pag_links') is True:
                    _line('✅ Ссылки пагинации есть в HTML - краулер дойдёт '
                          'до товаров дальше первой страницы.', C.ok)
                elif _pg.get('pag_links') is False:
                    _line('⚠ Бесконечная прокрутка БЕЗ ссылок пагинации в '
                          'HTML - роботы не увидят товары дальше первой '
                          'страницы; добавить <a href> на страницы пагинации.',
                          C.warn)
            elif _pg.get('loadmore') is False:
                _line('⚠ Маркеры JS-подгрузки («показать ещё»/load-more) на '
                      'категории не найдены - проверить вручную, как Google '
                      'видит остальные товары.', C.warn)
            row += 1

        # ── Секция 4.2: спорные для индекса страницы (noindex-кандидаты) ──
        _adv = indexing_summary.get('advisory_open')
        if _adv is not None and not indexing_summary.get('error'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = f'Спорные для индекса страницы  ({len(_adv)})'
            c.font = _font(size=13, bold=True,
                           color=C.warn if _adv else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            if not _adv:
                _line('✅ Типовые разделы (новости, акции, блог, политика '
                      'конфиденциальности) не существуют либо уже закрыты '
                      'noindex/robots.', C.ok)
            else:
                _line('Чек-лист советует noindex для старых акций/новостей/'
                      'политик. Эти разделы открыты - решить, нужны ли они '
                      'в индексе (полезный раздел можно оставить):',
                      C.text_muted)
                for a in _adv:
                    _line(f'⚠ {a.get("label", "")}: {a.get("path", "")} - '
                          f'отвечает 200, noindex не стоит', C.warn)
            row += 1

        # ── Секция 4.2а: обязательные страницы + раздел «Отгрузки» ──
        _rq = indexing_summary.get('required_pages')
        if _rq:
            _rq_missing = [r_ for r_ in _rq if not r_.get('found')]
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = (f'Обязательные страницы  '
                       f'(не найдено: {len(_rq_missing)})')
            c.font = _font(size=13, bold=True,
                           color=C.err if _rq_missing else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            for r_ in _rq:
                if r_.get('found'):
                    _line(f'✅ {r_["label"]}: {r_["found"]}', C.ok)
                else:
                    _line(f'❌ {r_["label"]}: страница не найдена (проверены '
                          f'типовые адреса) - создать', C.err)
            # Раздел «Отгрузки» - опционален; если есть, должна быть
            # перелинковка на каталог.
            _otg = indexing_summary.get('otgruzki') or {}
            if _otg.get('found'):
                if _otg.get('catalog_links'):
                    _line(f'✅ Раздел «Отгрузки» ({_otg["found"]}) - с '
                          f'перелинковкой на каталог '
                          f'({_otg["catalog_links"]} ссылок).', C.ok)
                else:
                    _line(f'⚠ Раздел «Отгрузки» ({_otg["found"]}) есть, но '
                          f'ссылок на каталог в нём нет - добавить '
                          f'перелинковку.', C.warn)
            else:
                _line('· Раздел «Отгрузки» не найден - опционален по '
                      'проекту, не находка.', C.text_muted)
            # Даты публикации/обновления у статей и новостей.
            _nd = indexing_summary.get('news_dates')
            if _nd is None:
                _line('· Новости/статьи: раздел не найден - проверка дат '
                      'не применима.', C.text_muted)
            elif not _nd.get('article'):
                _line(f'· Новости/статьи ({_nd.get("section", "")}): '
                      f'статью в разделе не распознали - даты проверить '
                      f'вручную.', C.text_muted)
            elif _nd.get('published') and _nd.get('modified'):
                _line(f'✅ Новости/статьи: дата публикации и обновления '
                      f'размечены (datePublished/dateModified) - '
                      f'{_nd.get("article", "")}', C.ok)
            elif _nd.get('published'):
                _line(f'⚠ Новости/статьи: есть дата публикации, но нет '
                      f'даты ОБНОВЛЕНИЯ (dateModified) - '
                      f'{_nd.get("article", "")}', C.warn)
            else:
                _line(f'⚠ Новости/статьи: на статье нет даты публикации '
                      f'(datePublished / <time datetime>) - '
                      f'{_nd.get("article", "")}', C.warn)
            row += 1

        # ── Секция 4.3: перелинковка (внутренний вес) ──
        # Прокси по выборке прогона (не полный PageRank): классифицируем
        # внутренние ссылки каждой страницы по цели.
        _il_pages = [r for r in checked if r.indexing.get('int_links')]
        if _il_pages:
            _tot = {'home': 0, 'catalog': 0, 'tech': 0, 'other': 0}
            _tt_sum: dict = {}
            for r in _il_pages:
                for k, v in r.indexing['int_links'].items():
                    _tot[k] = _tot.get(k, 0) + v
                for p, n in (r.indexing.get('tech_targets') or {}).items():
                    _tt_sum[p] = _tt_sum.get(p, 0) + n
            _all = sum(_tot.values()) or 1
            _cat_share = _tot['catalog'] * 100 // _all
            _tech_share = _tot['tech'] * 100 // _all
            _il_bad = _tot['tech'] >= _tot['catalog']
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = 'Перелинковка (внутренний вес)'
            c.font = _font(size=13, bold=True, color=C.warn if _il_bad else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            _line(f'Прокси по выборке прогона ({len(_il_pages)} страниц, не '
                  f'полный расчёт веса): внутренних ссылок {_all}, из них на '
                  f'каталог/категории {_cat_share}% · на главную '
                  f'{_tot["home"] * 100 // _all}% · на тех/инфо {_tech_share}% '
                  f'· прочее {_tot["other"] * 100 // _all}%.', C.text_muted)
            if _il_bad:
                _line('⚠ Ссылок на тех/инфо-страницы не меньше, чем на каталог '
                      '- внутренний вес льётся на страницы-«паразиты» (обычно '
                      'распухший футер). Каталог и категории должны получать '
                      'больше всего ссылок.', C.warn)
            else:
                _line('✅ Каталог и категории получают больше внутренних '
                      'ссылок, чем тех/инфо-страницы.', C.ok)
            if _tt_sum:
                _top = sorted(_tt_sum.items(), key=lambda kv: -kv[1])[:5]
                _line('Топ тех/инфо-получателей ссылок: '
                      + ' · '.join(f'{p} ({n})' for p, n in _top),
                      C.text_soft)
            row += 1

        # ── Секция 4а: гигиена robots.txt (доп. чек-лист) ──
        # Показываем только если проверка выполнялась (нет error и robots 200)
        if not indexing_summary.get('error'):
            _ua = indexing_summary.get('ua_groups')
            _hyg_bad = bool(_blanket or _assets_closed)
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = 'Гигиена robots.txt'
            c.font = _font(size=13, bold=True, color=C.err if _hyg_bad else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            # 1. Disallow: / - сайт закрыт целиком
            if _blanket:
                _grp = ', '.join(f'User-agent: {a}' for a in _blanket)
                _line(f'❌ В robots.txt есть директива «Disallow: /» ({_grp}) - '
                      f'сайт закрыт от индексации целиком.', C.err, bold=True)
            else:
                _line('✅ Директивы «Disallow: /» нет.', C.ok)
            # 2. Отдельные группы User-agent для Яндекса и Google
            if _ua:
                _missing = [n for n, k in (('Yandex', 'yandex'),
                                           ('Googlebot', 'google'))
                            if not _ua.get(k)]
                if _missing:
                    _line(f'⚠ Нет отдельных групп User-agent: '
                          f'{", ".join(_missing)} - роботы работают по общей '
                          f'группе «*».', C.warn)
                else:
                    _line('✅ Отдельные группы User-agent для Яндекса и Google '
                          'заданы.', C.ok)
                # прочие роботы = группа «*» (правила для всех остальных)
                if not _ua.get('star'):
                    _line('⚠ Нет группы User-agent: * - для прочих роботов '
                          '(кроме Яндекса/Google) правил не задано.', C.warn)
            # 3. CSS/JS открыты для роботов
            _n_assets = indexing_summary.get('assets_checked', 0)
            if _assets_closed:
                _line(f'❌ Файлы .css/.js закрыты в robots.txt '
                      f'({len(_assets_closed)} из {_n_assets}) - Google не '
                      f'сможет отрендерить страницы:', C.err, bold=True)
                for _a in _assets_closed[:10]:
                    _line(f'{_a.get("url", "")}  (Disallow: {_a.get("rule", "")})',
                          C.err)
                if len(_assets_closed) > 10:
                    _line(f'… и ещё {len(_assets_closed) - 10}', C.text_muted)
            elif _n_assets:
                _line(f'✅ Файлы .css/.js главной ({_n_assets} шт.) открыты '
                      f'для роботов.', C.ok)
            row += 1

        # ── Секция 4б: ЧПУ и формат адресов (по всем путям каталога) ──
        _uf = indexing_summary.get('url_format')
        if _uf:
            _uf_bad = bool(_uf.get('total_bad'))
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = (f'ЧПУ и формат адресов  '
                       f'(проверено {_uf.get("checked", 0)} путей)')
            c.font = _font(size=13, bold=True, color=C.warn if _uf_bad else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            if not _uf_bad:
                _line('✅ Все адреса каталога - ЧПУ: латиница/цифры/дефис в '
                      'нижнем регистре, без технических параметров.', C.ok)
            else:
                for kind, label in (
                        ('non_sef', 'технические адреса (не ЧПУ: ?ID=, .php)'),
                        ('cyrillic', 'кириллица в адресе'),
                        ('uppercase', 'ЗАГЛАВНЫЕ буквы в адресе'),
                        ('underscore', 'подчёркивания вместо дефисов'),
                        ('junk_chars', 'пробелы/спецсимволы в адресе')):
                    _n = _uf.get(kind + '_n', 0)
                    if not _n:
                        continue
                    _line(f'⚠ {label}: {_n} шт.', C.warn, bold=True)
                    for _p in (_uf.get(kind) or [])[:5]:
                        _line(_p, C.text_muted)
                    if _n > 5:
                        _line(f'… и ещё {_n - 5}', C.text_muted)
            row += 1

        # ── Секция 5: sitemap-директивы в robots (ТЗ 3.3.6) ──
        smc = indexing_summary.get('sitemap_checks')
        if smc is not None:
            _sm_bad = (not smc.get('has_directive')
                       or any((d.get('status') or 0) != 200
                              for d in smc.get('directives') or [])
                       or smc.get('matches_project') is False)
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = 'Sitemap в robots.txt'
            c.font = _font(size=13, bold=True, color=C.err if _sm_bad else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1
            if not smc.get('has_directive'):
                _line('❌ В robots.txt нет директивы Sitemap - роботы не видят '
                      'карту сайта.', C.err, bold=True)
            else:
                for d in (smc.get('directives') or []):
                    _st = d.get('status')
                    if _st == 200:
                        _line(f'✅ {d.get("url", "")} - открывается (HTTP 200)', C.ok)
                    else:
                        _line(f'❌ {d.get("url", "")} - не открывается '
                              f'(HTTP {_st if _st is not None else "нет ответа"})',
                              C.err, bold=True)
                if smc.get('matches_project') is False:
                    _line('⚠ Ни одна директива не совпадает с sitemap проекта '
                          '(sitemap_url из настроек) - проверьте, тот ли адрес '
                          'указан.', C.warn)
            row += 1

        def _sec_title(text, bad):
            nonlocal row
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = text
            c.font = _font(size=13, bold=True, color=C.err if bad else C.ok)
            c.fill = _fill(C.accent_soft)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 24
            row += 1

        # ── Секция 6: структура sitemap (ТЗ 3.4.2) ──
        aud = indexing_summary.get('sitemap_audit')
        if aud is not None:
            _bad_urls = aud.get('bad_urls') or []
            _tot = aud.get('total', 0)
            _fld_missing = [
                (name, aud.get(key, 0))
                for name, key in (('lastmod', 'with_lastmod'),
                                  ('changefreq', 'with_changefreq'),
                                  ('priority', 'with_priority'))
                if _tot and aud.get(key, 0) == 0
            ]
            # лимиты допа (10k/10МБ = предупр.) и протокола (50k/50МБ = баг)
            _fstats = aud.get('file_stats') or []
            _over_proto = [f for f in _fstats
                           if f.get('urls', 0) > 50000
                           or f.get('bytes', 0) > 50 * 1024 * 1024]
            _over_dop = [f for f in _fstats if f not in _over_proto
                         and (f.get('urls', 0) > 10000
                              or f.get('bytes', 0) > 10 * 1024 * 1024)]
            _mc = aud.get('missing_catalog') or {}
            _miss = ((_mc.get('categories') or [])
                     + (_mc.get('filters') or [])
                     + (_mc.get('services') or []))
            _sec_title('Sitemap: структура записей (ТЗ 3.4.2)',
                       bool(aud.get('error') or _bad_urls or _over_proto
                            or _miss))
            if aud.get('error'):
                _line(f'⚠ Аудит не выполнен: {aud["error"]}', C.warn)
            else:
                _line(f'Файлов sitemap: {aud.get("files", 0)} · записей URL: '
                      f'{_tot} · с lastmod: {aud.get("with_lastmod", 0)} · '
                      f'с changefreq: {aud.get("with_changefreq", 0)} · '
                      f'с priority: {aud.get("with_priority", 0)}', C.text_muted)
                # структура: индекс или одиночный файл
                _itypes = aud.get('index_types') or []
                # опознанные типы (без «прочее») - по ним судим о разбивке
                _named = [t for t in _itypes if t != 'прочее']
                if aud.get('is_index'):
                    _line(f'✅ Sitemap - индекс-файл, внутри '
                          f'{len(aud.get("index_children") or [])} файлов.',
                          C.ok)
                    if _named:
                        _line(f'Разбивка по типам: {", ".join(_named)}'
                              + (' + прочие' if 'прочее' in _itypes else '')
                              + '.', C.text_muted)
                    # разбивки по типам нет (всё «прочее» / один тип), а
                    # каталог большой - предупреждение (п.5)
                    if _tot > 10000 and len(_named) < 2:
                        _line('⚠ Индекс не разбит по типам страниц '
                              '(категории/фильтры/товары в отдельные файлы) - '
                              'при большом каталоге так рекомендуется.', C.warn)
                elif _tot > 10000:
                    _line(f'⚠ Записей {_tot}, но sitemap - одиночный файл '
                          f'без индекса; нужен индекс-файл с разбивкой '
                          f'по типам страниц.', C.warn)
                else:
                    _line('✅ Одиночный sitemap - допустимо, страниц немного.',
                          C.ok)
                # лимиты на файл
                for f in _over_proto:
                    _line(f'❌ {f.get("url", "")} - {f.get("urls", 0)} URL, '
                          f'{f.get("bytes", 0) // 1048576} МБ: нарушен лимит '
                          f'протокола (50 000 / 50 МБ).', C.err, bold=True)
                for f in _over_dop:
                    _line(f'⚠ {f.get("url", "")} - {f.get("urls", 0)} URL, '
                          f'{f.get("bytes", 0) // 1048576} МБ: больше '
                          f'рекомендуемого лимита (10 000 ссылок / 10 МБ '
                          f'на файл).', C.warn)
                if _fstats and not _over_proto and not _over_dop:
                    _line('✅ Лимиты на файл соблюдены (до 10 000 ссылок '
                          'и 10 МБ).', C.ok)
                if _bad_urls:
                    _line(f'❌ Неправильные URL в sitemap ({len(_bad_urls)}):',
                          C.err, bold=True)
                    for b in _bad_urls[:20]:
                        _line(f'{b.get("why", "")}: {b.get("url", "")}', C.err)
                    if len(_bad_urls) > 20:
                        _line(f'… и ещё {len(_bad_urls) - 20}', C.text_muted)
                else:
                    _line('✅ Все URL абсолютные, https и своего хоста.', C.ok)
                for name, _n in _fld_missing:
                    _line(f'⚠ Ни у одной записи нет <{name}> - ТЗ требует '
                          f'заполнять.', C.warn)
                # полнота: категории/фильтры/услуги из выгрузки каталога
                if aud.get('truncated'):
                    _line('- Полнота (категории/фильтры/услуги в sitemap) не '
                          'проверена: обход упёрся в лимит файлов/записей.',
                          C.text_muted)
                elif _miss:
                    _line(f'❌ В sitemap нет {len(_miss)} важных ссылок '
                          f'(категории/фильтры/услуги) из выгрузки - страницы '
                          f'не попадут в индекс:', C.err, bold=True)
                    for _p in _miss[:20]:
                        _line(_p, C.err)
                    if len(_miss) > 20:
                        _line(f'… и ещё {len(_miss) - 20}', C.text_muted)
                elif aud.get('missing_catalog') is not None:
                    _line('✅ Все категории, фильтры и услуги из выгрузки '
                          'есть в sitemap.', C.ok)
            row += 1

            # ── Секция 7: даты lastmod (ТЗ 3.4.3) ──
            la = aud.get('lastmod_analysis') or {}
            _la_warn = la.get('warnings') or []
            _sec_title('Sitemap: даты обновления (ТЗ 3.4.3)', bool(_la_warn))
            if _la_warn:
                for w in _la_warn:
                    _line(f'⚠ {w}', C.warn)
            elif aud.get('with_lastmod'):
                _cr = la.get('changed_ratio')
                _extra = (f' С прошлого прогона изменилось '
                          f'{int(_cr * 100)}% дат.' if _cr is not None else
                          ' Сравнение с прошлым прогоном появится со '
                          'следующего запуска.')
                _line('✅ Признаков динамической генерации дат нет.' + _extra,
                      C.ok)
            else:
                _line('- Дат lastmod в sitemap нет - проверять нечего '
                      '(см. секцию структуры выше).', C.text_muted)
            row += 1

        # ── Секция 8: sitemap в Яндекс.Вебмастере (ТЗ 3.4.4) ──
        wm = indexing_summary.get('wm_sitemaps')
        if wm is not None:
            _wm_list = wm.get('sitemaps') or []
            _wm_bad = (bool(wm.get('error')) or not _wm_list
                       or any((s.get('errors') or 0) for s in _wm_list))
            _sec_title('Sitemap в Яндекс.Вебмастере (ТЗ 3.4.4)', _wm_bad)
            if wm.get('error'):
                _line(f'⚠ Не удалось получить: {wm["error"]}', C.warn)
            elif not _wm_list:
                _line(f'❌ У хоста {wm.get("host", "")} в Вебмастере нет '
                      f'sitemap-файлов - карта не добавлена.', C.err, bold=True)
            else:
                for s in _wm_list:
                    _err = s.get('errors')
                    if _err:
                        _line(f'❌ {s.get("url", "")} - ошибок: {_err}', C.err,
                              bold=True)
                    else:
                        _n = s.get('urls_count')
                        _line(f'✅ {s.get("url", "")} - без ошибок'
                              + (f', URL: {_n}' if _n else ''), C.ok)

        # ── Секция 9: HTML-карта сайта (доп. чек-лист) ──
        hm = indexing_summary.get('html_sitemap')
        if hm is not None:
            _hm_junk = hm.get('junk_links') or []
            _sec_title('HTML-карта сайта', bool(_hm_junk))
            if hm.get('status') != 200:
                _line(f'⚠ HTML-карта не найдена по типовым адресам '
                      f'(/sitemap/, /sitemap.html) - последний ответ '
                      f'HTTP {hm.get("status") if hm.get("status") is not None else "нет"}'
                      f'{"; " + hm["error"] if hm.get("error") else ""}. '
                      f'Если карта живёт по другому адресу - проверить '
                      f'руками.', C.warn)
            elif _hm_junk:
                _line(f'❌ {hm.get("url", "")} - в HTML-карте служебные '
                      f'ссылки ({len(_hm_junk)}):', C.err, bold=True)
                for j in _hm_junk[:15]:
                    _line(f'{j.get("url", "")}', C.err)
                if len(_hm_junk) > 15:
                    _line(f'… и ещё {len(_hm_junk) - 15}', C.text_muted)
            else:
                _line(f'✅ {hm.get("url", "")} - существует, служебных '
                      f'ссылок нет.', C.ok)


# ── Группировка «одна проблема - одна строка + список URL» ─────────
# Как на листе «Уведомления»: не плодим милион одинаковых строк, а
# группируем страницы по тексту проблемы.


def _issue_groups(pages, attr, key):
    """[(текст проблемы, [CheckResult])] по убыванию количества страниц."""
    groups = {}
    for r in pages:
        data = getattr(r, attr, None) or {}
        for t in (data.get(key) or []):
            groups.setdefault(t, []).append(r)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def _render_issue_groups(ws, row, groups, color, max_urls=100, extra=None):
    """Строка-проблема (текст + сколько страниц), под ней - город/тип/URL.
    extra(r) - необязательный текст в последнюю колонку (что нашлось на
    странице), чтобы было видно контекст проблемы, а не только URL."""
    for text, rs in groups:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = f'{text}  -  {len(rs)} {_plural_pages(len(rs))}'
        c.font = _font(size=10, bold=True, color=color)
        c.fill = _fill(C.surface)
        c.alignment = _align(wrap=True, indent=1)
        c.border = _border()
        ws.row_dimensions[row].height = 22
        row += 1
        for r in rs[:max_urls]:
            ws.row_dimensions[row].height = 18
            for ci, (val, kw) in enumerate([
                (r.city or '-', {'size': 9, 'color': C.text_muted}),
                (r.type_label, {'size': 9, 'color': C.text_muted}),
                (r.url, {'size': 9, 'color': C.accent, 'underline': 'single'}),
                (extra(r) if extra else '', {'size': 9, 'color': C.text_soft}),
            ], 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = val
                if kw:
                    cell.font = _font(**kw)
                cell.alignment = _align(vertical='top')
                cell.border = _border(color=C.border_light)
                if ci == 4 and val:
                    cell.hyperlink = val
            row += 1
        if len(rs) > max_urls:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = f'… и ещё {len(rs) - max_urls} {_plural_pages(len(rs) - max_urls)}'
            c.font = _font(size=9, italic=True, color=C.text_muted)
            c.alignment = _align(indent=2)
            ws.row_dimensions[row].height = 16
            row += 1
        row += 1
    return row


# ── Лист «Вёрстка» (п.1.11, ТЗ 2.1/2.1.1: viewport, стили, @media) ──


def _build_layout_sheet(wb, results, filters_test=None, search_check=None):
    """Лист вёрстки и адаптивности: страницы без viewport, битые CSS,
    отсутствие @media. Плюс секция «Фильтрация товаров» (браузерный тест
    фильтра). Добавляется, если выполнялась вёрстка ИЛИ фильтр-тест."""
    checked = [r for r in results if getattr(r, 'layout', None)]
    if not checked and not filters_test:
        return

    bad = [r for r in checked if r.layout.get('issues')]
    warned = [r for r in checked if (not r.layout.get('issues')
                                     and r.layout.get('warnings'))]
    has_bugs = bool(bad)

    ws = wb.create_sheet('Вёрстка')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 18   # Город
    ws.column_dimensions['C'].width = 14   # Тип
    ws.column_dimensions['D'].width = 62   # URL
    ws.column_dimensions['E'].width = 60
    ws.column_dimensions['F'].width = 3

    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = 'Вёрстка, адаптивность и навигация (п.1.11)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('ТЗ 2.1/2.1.1: страницы выводятся со стилями на ПК и мобильных - '
               'задан тег viewport, каждый подключённый CSS-файл реально '
               'грузится (4xx/5xx = страница без вёрстки), в стилях есть '
               '@media-запросы по ширине (признак адаптивности; отсутствие - '
               'предупреждение). ТЗ 2.2/2.3: переходы из меню шапки работают - '
               'все ссылки меню (тех. страницы и каталог) прозваниваются с '
               'главной каждого поддомена, 404/410 = баг. Favicon установлен '
               'и реально грузится (проверка с главной поддомена). Плюс: свои '
               'CSS/JS минифицированы и объединены (много отдельных файлов / '
               'лишние пробелы = предупреждение); семантическая разметка '
               '(<header>/<footer>/<main>) и инлайн-стили (много style="…" '
               'в HTML = предупреждение); единый протокол - http-ресурсы на '
               'https-странице (mixed content, браузер блокирует = баг) и '
               'внутренние ссылки по http (предупреждение); вынос стилей/'
               'скриптов во внешние файлы (большие inline-блоки = '
               'предупреждение) и отложенный рендеринг (скрипты в <head> без '
               'async/defer = предупреждение); псевдоссылки (button/div с '
               'onclick вместо <a href>) и шрифты без font-display: swap '
               '(сдвиг макета). Полный визуальный рендер это не заменяет - '
               'выборочный ручной просмотр остаётся.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 60

    row = 5

    # Сводка
    _no_vp = sum(1 for r in checked if not r.layout.get('viewport'))
    _css_broken_pages = sum(1 for r in checked if r.layout.get('css_broken'))
    _menu_checked = sum((r.layout.get('menu') or {}).get('checked', 0)
                        for r in checked)
    _menu_broken = sum(len((r.layout.get('menu') or {}).get('broken') or [])
                       for r in checked)
    # Favicon: проверяется с главной каждого поддомена; ок = без favicon-бага.
    _fav_checked = [r for r in checked if r.layout.get('favicon')]
    _fav_bad = sum(1 for r in _fav_checked
                   if any('favicon' in t for t in r.layout.get('issues') or []))
    _fav_txt = ''
    if _fav_checked:
        _fav_txt = (f' · favicon: ✅ ок на {len(_fav_checked)} поддоменах'
                    if not _fav_bad else
                    f' · favicon: ❌ битый на {_fav_bad} из {len(_fav_checked)}')
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = (f'Проверено страниц: {len(checked)} · без viewport: {_no_vp} · '
               f'с битыми CSS: {_css_broken_pages} · '
               f'ссылок меню прозвонено: {_menu_checked}, битых: {_menu_broken} · '
               f'предупреждений: {len(warned)}{_fav_txt}')
    c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 2

    # Секция 1: проблемы (сгруппированы по тексту)
    _meta_section_title(ws, row, f'Проблемы вёрстки  ({len(bad)})',
                        C.err if bad else C.ok)
    row += 1
    if not bad:
        _meta_ok_line(ws, row, '✅ На всех проверенных страницах viewport задан, '
                               'все CSS-файлы грузятся.')
        row += 2
    else:
        row = _render_issue_groups(
            ws, row, _issue_groups(bad, 'layout', 'issues'), C.err)

    # Секция 2: битые CSS-файлы (по файлу: какой файл на каких страницах)
    _by_css = {}
    for r in checked:
        for b in (r.layout.get('css_broken') or []):
            _key = f'{b.get("url", "")} (HTTP {b.get("status")})'
            _by_css.setdefault(_key, []).append(r)
    if _by_css:
        _meta_section_title(ws, row, f'Битые CSS-файлы  ({len(_by_css)})', C.err)
        row += 1
        row = _render_issue_groups(
            ws, row, sorted(_by_css.items(), key=lambda kv: -len(kv[1])), C.err)

    # Секция 3: битые ссылки меню шапки (ТЗ 2.2/2.3) - по ссылке: где битая
    _by_link = {}
    for r in checked:
        for b in ((r.layout.get('menu') or {}).get('broken') or []):
            _key = f'{b.get("url", "")} (HTTP {b.get("code")})'
            _by_link.setdefault(_key, []).append(r)
    if _by_link:
        _meta_section_title(ws, row,
                            f'Битые ссылки в меню шапки  ({len(_by_link)})', C.err)
        row += 1
        row = _render_issue_groups(
            ws, row, sorted(_by_link.items(), key=lambda kv: -len(kv[1])), C.err)

    # Секция 4: предупреждения (нет @media)
    if warned:
        _meta_section_title(ws, row, f'Предупреждения  ({len(warned)})', C.warn)
        row += 1
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'layout', 'warnings'), C.warn)

    # Секция 5: поиск по сайту находит категории (чек-лист)
    if search_check:
        row += 1
        _sc_bad = (search_check.get('available')
                   and search_check.get('found_category') is False)
        _meta_section_title(ws, row, 'Поиск по сайту',
                            C.warn if _sc_bad or not search_check.get(
                                'available') else C.ok)
        row += 1
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        if not search_check.get('available'):
            c.value = ('⚠ Поиск не проверился: '
                       + (search_check.get('error') or 'неизвестная причина')
                       + ' - проверить вручную.')
            c.font = _font(size=10, color=C.warn)
        elif search_check.get('found_category'):
            from urllib.parse import unquote as _unq
            c.value = (f'✅ Поиск находит категории: по запросу '
                       f'«{search_check.get("query", "")}» в выдаче есть '
                       f'ссылка на саму категорию '
                       f'({_unq(search_check.get("search_url", ""))}).')
            c.font = _font(size=10, color=C.ok)
        else:
            from urllib.parse import unquote as _unq
            c.value = (f'⚠ По запросу «{search_check.get("query", "")}» в '
                       f'СТАТИКЕ выдачи нет ссылки на категорию. Либо поиск '
                       f'ищет только товары, либо блок категорий дорисовывает '
                       f'JS (как на СМУ) - открыть выдачу и проверить кликом: '
                       f'{_unq(search_check.get("search_url", ""))}')
            c.font = _font(size=10, color=C.warn)
        c.alignment = _align(indent=1, wrap=True)
        ws.row_dimensions[row].height = 30
        row += 1
        # Тег (страница-фильтр) - вторая проба.
        if search_check.get('tag_note'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row,
                           end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = '· Тег: ' + search_check['tag_note'] + '.'
            c.font = _font(size=10, color=C.text_muted)
            c.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 18
            row += 1
        if search_check.get('tag_query'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row,
                           end_column=5)
            c = ws.cell(row=row, column=2)
            if search_check.get('found_tag'):
                c.value = (f'✅ Теги тоже находятся: по запросу '
                           f'«{search_check["tag_query"]}» в выдаче есть '
                           f'ссылка на страницу-фильтр.')
                c.font = _font(size=10, color=C.ok)
            else:
                c.value = (f'⚠ Тег «{search_check["tag_query"]}» в выдаче '
                           f'не найден (ссылки на страницу-фильтр нет) - '
                           f'типично для штатного поиска Bitrix, проверить '
                           f'при желании вручную.')
                c.font = _font(size=10, color=C.warn)
            c.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 26
            row += 1

    # Секция 6: фильтрация товаров (браузерный тест) - если запускался
    if filters_test:
        row += 1
        row = _render_filters_section(ws, row, filters_test)


# ── Лист «Разметка» (п.1.12, ТЗ 3.5: Schema.org + OpenGraph) ────────


def _build_markup_sheet(wb, results):
    """Лист микроразметки: OG-теги и Schema.org-типы на основных типах
    страниц. Добавляется только если проверка выполнялась."""
    checked = [r for r in results if getattr(r, 'markup', None)]
    if not checked:
        return

    bad = [r for r in checked if r.markup.get('issues')]
    warned = [r for r in checked if (not r.markup.get('issues')
                                     and r.markup.get('warnings'))]
    has_bugs = bool(bad)

    ws = wb.create_sheet('Разметка')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 14
    ws.column_dimensions['D'].width = 62
    ws.column_dimensions['E'].width = 60
    ws.column_dimensions['F'].width = 3

    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = 'Микроразметка и OpenGraph (п.1.12)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('ТЗ 3.5: OpenGraph (og:url/title/description/image/type - все '
               'обязательны) и Schema.org на основных типах страниц: данные '
               'компании (Organization/LocalBusiness) везде, хлебные крошки '
               '(BreadcrumbList) на вложенных, листинги (OfferCatalog/ItemList/'
               'CollectionPage), на товаре - Product, характеристики '
               '(PropertyValue), фото (itemprop=image). Основной формат - '
               'microdata: тип только в JSON-LD = предупреждение. Цена не '
               'размечена = предупреждение (товары «по запросу»). Плюс '
               'проверка обязательных полей в объекте: Product без '
               'offers/name/image, Offer без цены/валюты, крошки без '
               'элементов = баг; желательные (логотип, описание) = '
               'предупреждение. Условные типы: видео на странице → '
               'VideoObject, FAQ-блок → FAQPage, адрес/контакты → '
               'PostalAddress (нет = предупреждение).')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 68

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = (f'Проверено страниц: {len(checked)} · с багами разметки: '
               f'{len(bad)} · с предупреждениями: {len(warned)}')
    c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 2

    # Детали полей («Offer/цена: 21 из 60») - в колонке-контексте: тексты
    # проблем без чисел, иначе группировка дробится на страницы.
    def _markup_extra(r):
        d = (getattr(r, 'markup', None) or {}).get('field_details') or []
        return '; '.join(d[:3]) + (f' … +{len(d) - 3}' if len(d) > 3 else '')

    # Что реально нашлось на странице - чтобы было видно: «нет разметки»
    # значит нет НИ ОДНОГО типа из требуемых, а не «часть есть».
    _meta_section_title(ws, row, f'Проблемы разметки  ({len(bad)})',
                        C.err if bad else C.ok)
    row += 1
    if not bad:
        _meta_ok_line(ws, row, '✅ OG-теги и обязательная Schema.org-разметка '
                               'на месте у всех проверенных страниц.')
        row += 2
    else:
        row = _render_issue_groups(
            ws, row, _issue_groups(bad, 'markup', 'issues'), C.err,
            extra=_markup_extra)

    if warned:
        _meta_section_title(ws, row, f'Предупреждения  ({len(warned)})', C.warn)
        row += 1
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'markup', 'warnings'), C.warn,
            extra=_markup_extra)


# ── Лист «Безопасность» (доп. 1.8: заголовки безопасности HTTP) ────


def _build_security_sheet(wb, results):
    """Лист заголовков безопасности: HSTS/CSP/X-Frame и т.п. по ответу
    сервера. Добавляется только если проверка выполнялась."""
    checked = [r for r in results if getattr(r, 'security', None)]
    if not checked:
        return

    bad = [r for r in checked if r.security.get('issues')]
    warned = [r for r in checked if (not r.security.get('issues')
                                     and r.security.get('warnings'))]
    has_bugs = bool(bad)

    ws = wb.create_sheet('Безопасность')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 14
    ws.column_dimensions['D'].width = 62
    ws.column_dimensions['E'].width = 60
    ws.column_dimensions['F'].width = 3

    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = 'Заголовки безопасности (1.8)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('HTTP-заголовки безопасности ответа сервера. Нет HSTS, '
               'Content-Security-Policy, X-Content-Type-Options: nosniff или '
               'защиты от кликджекинга (X-Frame-Options / CSP '
               'frame-ancestors) - предупреждение. '
               'Битое значение (HSTS max-age=0, устаревший ALLOW-FROM, '
               'X-Content-Type-Options не nosniff, конфликт дублей, CSP с '
               'unsafe-inline+unsafe-eval) - баг/предупреждение: заголовок '
               'есть, но работает во вред или впустую. '
               'Полную оценку даёт securityheaders.com.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 60

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    # Оценка A-F (в стиле securityheaders.com) - по главным страницам.
    _grades = []
    for r in checked:
        if r.type_code == 'main' and r.security.get('grade'):
            _grades.append(f'{r.security["grade"]} ({r.city})')
    _gr_txt = (' · оценка (в стиле securityheaders.com): '
               + ', '.join(_grades[:4]) if _grades else '')
    c.value = (f'Проверено страниц: {len(checked)} · с багами: {len(bad)} · '
               f'с предупреждениями: {len(warned)}{_gr_txt}')
    c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 1

    # Ссылка на точную оценку (их API - только по ключу, считаем локально).
    _main_url = next((r.url for r in checked if r.type_code == 'main'), None)
    if _main_url:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        _sh = f'https://securityheaders.com/?q={_main_url}&followRedirects=on'
        c.value = ('Точная оценка: securityheaders.com (клик) - наша считается '
                   'локально по наличию 6 заголовков.')
        c.hyperlink = _sh
        c.font = _font(size=9, color=C.accent, underline='single')
        c.alignment = _align(wrap=True)
        ws.row_dimensions[row].height = 16
        row += 1
    row += 1

    def _sec_found(r):
        present = (getattr(r, 'security', None) or {}).get('present') or []
        return ('есть на странице: ' + ', '.join(present)
                if present else 'заголовков безопасности нет вообще')

    _meta_section_title(ws, row, f'Ошибки заголовков  ({len(bad)})',
                        C.err if bad else C.ok)
    row += 1
    if not bad:
        _meta_ok_line(ws, row, '✅ Битых значений заголовков безопасности нет.')
        row += 2
    else:
        row = _render_issue_groups(
            ws, row, _issue_groups(bad, 'security', 'issues'), C.err,
            extra=_sec_found)

    if warned:
        _meta_section_title(ws, row, f'Рекомендации  ({len(warned)})', C.warn)
        row += 1
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'security', 'warnings'), C.warn,
            extra=_sec_found)


# ── Лист «Изображения» (п.1.15: alt, webp/avif, вес) ───────────────

_IMG_HEAVY_KB = 300      # порог «тяжёлой» картинки (синхронно с image_checker)


def _build_images_sheet(wb, results):
    """Лист изображений: alt у картинок, современные форматы (webp/avif),
    вес (оптимизация). Добавляется только если проверка выполнялась."""
    checked = [r for r in results if getattr(r, 'images', None)]
    if not checked:
        return
    bad = [r for r in checked if r.images.get('issues')]
    warned = [r for r in checked if (not r.images.get('issues')
                                     and r.images.get('warnings'))]
    has_bugs = bool(bad)

    ws = wb.create_sheet('Изображения')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok
    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 14
    ws.column_dimensions['D'].width = 60
    ws.column_dimensions['E'].width = 60
    ws.column_dimensions['F'].width = 3

    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = 'Изображения (п.1.15)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('Проверки по картинкам страницы: (1) Alt - у каждого <img> '
               'есть атрибут alt (пустой alt="" ок для декоративных; баг - '
               'полное отсутствие). (2) Современные форматы - используются '
               'webp/avif, а не только jpg/png/gif (устаревшие без webp/avif = '
               'предупреждение). (3) Оптимизация - вес ≤150 КБ; '
               f'два порога: тяжелее 150 КБ - замечание, тяжелее {_IMG_HEAVY_KB} '
               'КБ - «тяжёлые» с именами файлов. (4) Lazy loading - у '
               'картинок/видео есть ленивая загрузка (loading="lazy"/data-src/'
               'preload="none"). (5) Имена файлов - транслит из alt; хеш-имена '
               'CMS (/upload/iblock/…) - одно предупреждение на страницу. '
               '(6) Уникальные картинки категорий - «главная» картинка '
               'категории (og:image / первая после h1) не повторяется на '
               'других категориях того же поддомена и не заглушка. '
               '(7) Уникальные фото товаров - изображения товаров в разных '
               'категориях не дублируются: одно фото не встречается у товаров '
               'из разных категорий (внутри одной категории общее фото - '
               'норма для металлопроката; один товар в нескольких категориях '
               'тоже норма, ему нужен rel=canonical). Вес берётся по '
               'Content-Length.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = (f'Проверено страниц: {len(checked)} · без alt: {len(bad)} · '
               f'с предупреждениями (форматы/вес/lazy): {len(warned)}')
    c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 2

    def _cat_extra(r):
        """Контекст находки по картинке категории: какая картинка."""
        im = getattr(r, 'images', None) or {}
        if im.get('cat_dup'):
            return (f'та же картинка: {im["cat_dup"]["name"]} '
                    f'(на {im["cat_dup"]["n"]} категориях)')
        if im.get('cat_img'):
            return f'заглушка: {im["cat_img"]["name"]}'
        return ''

    def _prod_extra(r):
        """Контекст находки по фото товара: какое фото, в скольких категориях
        и у скольких товаров оно встретилось."""
        im = getattr(r, 'images', None) or {}
        if im.get('prod_dup'):
            d = im['prod_dup']
            cats = d.get('cats')
            cat_txt = f'в {cats} категориях, ' if cats else ''
            return (f'та же картинка: {d["name"]} '
                    f'({cat_txt}у {d["n"]} товаров)')
        if im.get('prod_img'):
            return f'заглушка: {im["prod_img"]["name"]}'
        return ''

    def _img_extra(r):
        im = getattr(r, 'images', None) or {}
        bits = []
        if im.get('no_alt'):
            bits.append('без alt: ' + ', '.join(im['no_alt'][:3])
                        + (f' … +{len(im["no_alt"]) - 3}'
                           if len(im['no_alt']) > 3 else ''))
        if im.get('broken_imgs'):
            bits.append('битые: ' + ', '.join(
                b['url'].rsplit('/', 1)[-1]
                for b in im['broken_imgs'][:3]))
        if im.get('legacy'):
            bits.append(f'устаревших: {len(im["legacy"])}')
        if im.get('heavy'):
            bits.append('тяжёлые: ' + ', '.join(
                f'{h["url"].rsplit("/", 1)[-1]} {h["kb"]}КБ'
                for h in im['heavy'][:3]))
        if im.get('mid_heavy'):
            bits.append(f'тяжелее 150КБ: {im["mid_heavy"]}')
        _nm = im.get('names') or {}
        if _nm.get('hashed', 0) >= 3 and _nm['hashed'] > _nm.get('readable', 0):
            bits.append(f'хеш-имена: {_nm["hashed"]}')
        if _nm.get('mismatch_n'):
            bits.append('не по alt: ' + ', '.join(_nm.get('mismatch', [])[:2])
                        + (f' … +{_nm["mismatch_n"] - 2}'
                           if _nm['mismatch_n'] > 2 else ''))
        if im.get('no_size') and im.get('img_total', 0) >= 4 \
                and im['no_size'] > im['img_total'] // 2:
            bits.append(f'без width/height: {im["no_size"]} из {im["img_total"]}')
        if im.get('img_total') and not im.get('lazy_imgs'):
            bits.append(f'без lazy: {im["img_total"]} картинок')
        if im.get('media_total') and not im.get('lazy_media'):
            bits.append(f'видео/iframe: {im["media_total"]}')
        return ' · '.join(bits)

    _meta_section_title(ws, row, f'Проблемы (alt, битые картинки)  ({len(bad)})',
                        C.err if bad else C.ok)
    row += 1
    if not bad:
        _meta_ok_line(ws, row, '✅ У всех картинок на проверенных страницах '
                               'есть атрибут alt, битых картинок (404) нет.')
        row += 2
    else:
        row = _render_issue_groups(
            ws, row, _issue_groups(bad, 'images', 'issues'), C.err,
            extra=_img_extra)

    if warned:
        _meta_section_title(ws, row,
                            f'Форматы, вес, lazy loading (предупреждения)  '
                            f'({len(warned)})', C.warn)
        row += 1
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'images', 'warnings'), C.warn,
            extra=_img_extra)

    # ── Уникальные картинки категорий/разделов ──
    # Секция появляется, когда в прогоне были страницы категорий.
    cats = [r for r in checked if getattr(r, 'type_code', '') == 'category']
    if cats:
        cat_bad = [r for r in cats if r.images.get('cat_warnings')]
        recognized = sum(1 for r in cats if r.images.get('cat_img'))
        _meta_section_title(
            ws, row,
            f'Картинки категорий/разделов - уникальность  ({len(cat_bad)})',
            C.warn if cat_bad else C.ok)
        row += 1
        if cat_bad:
            row = _render_issue_groups(
                ws, row, _issue_groups(cat_bad, 'images', 'cat_warnings'),
                C.warn, extra=_cat_extra)
        elif recognized:
            _meta_ok_line(ws, row,
                          f'✅ У каждой категории своя картинка, дублей и '
                          f'заглушек нет (категорий: {len(cats)}, картинка '
                          f'распознана у {recognized}).')
            row += 2
        else:
            ws.merge_cells(start_row=row, start_column=2,
                           end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = ('· Картинка категории (og:image / первая после h1) '
                       'не распознана ни на одной категории - пропуск.')
            c.font = _font(size=10, color=C.text_muted)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 22
            row += 2

    # ── Фото товаров в разных категориях не дублируются ──
    # Секция появляется, когда в прогоне были карточки товаров. Дубль - одно
    # фото у товаров из РАЗНЫХ категорий; внутри одной категории общее фото -
    # норма (металлопрокат: арматура/лист разных размеров с одним фото). Один
    # товар в нескольких категориях (тот же slug) тоже не дубль - ему нужен
    # rel=canonical.
    prods = [r for r in checked if getattr(r, 'type_code', '') == 'product']
    if prods:
        prod_bad = [r for r in prods if r.images.get('prod_warnings')]
        recognized_p = sum(1 for r in prods if r.images.get('prod_img'))
        _meta_section_title(
            ws, row,
            f'Фото товаров в разных категориях - уникальность  '
            f'({len(prod_bad)})',
            C.warn if prod_bad else C.ok)
        row += 1
        if prod_bad:
            row = _render_issue_groups(
                ws, row, _issue_groups(prod_bad, 'images', 'prod_warnings'),
                C.warn, extra=_prod_extra)
        elif recognized_p:
            _meta_ok_line(ws, row,
                          f'✅ Фото товаров не дублируются между категориями, '
                          f'заглушек нет (товаров: {len(prods)}, фото '
                          f'распознано у {recognized_p}).')
            row += 2
        else:
            ws.merge_cells(start_row=row, start_column=2,
                           end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = ('· Фото товара (og:image / первое после h1) не '
                       'распознано ни на одной карточке - пропуск.')
            c.font = _font(size=10, color=C.text_muted)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 22
            row += 2


# ── Лист «Валидация и скорость» (п.1.16: W3C HTML/CSS + время ресурсов) ─


def _build_gsc_pages_sheet(wb, gsc_pages):
    """Лист «Страницы в ГСК»: проиндексировано / просканировано-не-индексировано /
    сумма + Δ к прошлому снятию (пункт «Количество страниц в ГСК»)."""
    if not gsc_pages or not gsc_pages.get('available'):
        return
    ws = wb.create_sheet('Страницы в ГСК')
    d = gsc_pages.get('deltas') or {}

    for i, t in enumerate(('Показатель', 'Значение', 'Δ к прошлому'), 1):
        c = ws.cell(row=1, column=i, value=t)
        c.font = _font(bold=True)
        c.fill = _fill('ECEAE4')
        c.border = _border()
        c.alignment = _align('center' if i > 1 else 'left')

    def _row(r, label, key):
        c1 = ws.cell(row=r, column=1, value=label)
        c1.font = _font()
        c1.border = _border()
        val = gsc_pages.get(key)
        c2 = ws.cell(row=r, column=2, value=(val if val is not None else '—'))
        c2.font = _font(bold=True)
        c2.alignment = _align('center')
        c2.border = _border()
        dv = d.get(key)
        c3 = ws.cell(row=r, column=3)
        c3.border = _border()
        c3.alignment = _align('center')
        if dv is not None:
            if dv > 0:
                c3.value, _col = f'▲ +{dv:g}', '006300'
            elif dv < 0:
                c3.value, _col = f'▼ {dv:g}', 'C0392B'
            else:
                c3.value, _col = '= 0', '8A8781'
            c3.font = _font(bold=True, color=_col)
        else:
            c3.value = '—'
            c3.font = _font(color='8A8781')

    _row(2, 'Проиндексировано', 'indexed')
    _row(3, 'Просканировано, но пока не проиндексировано', 'crawled_not_indexed')
    _row(4, 'Сумма', 'total')

    note = 'Числа из отчёта GSC «Индексирование → Страницы».'
    if gsc_pages.get('manual'):
        note += ' Введены вручную.'
    ws.cell(row=6, column=1, value=note).font = _font(italic=True, color='5B5853')

    ws.column_dimensions['A'].width = 44
    ws.column_dimensions['B'].width = 14
    ws.column_dimensions['C'].width = 16


_HOME_DUPES_VERDICT = {
    'main': ('✔ это главная', '006300'),
    'redirect': ('✔ склеено (редирект)', '006300'),
    'canonical': ('✔ склеено (canonical)', '006300'),
    'duplicate': ('✖ ДУБЛЬ', 'C0392B'),
    'absent': ('— адреса нет', '8A8781'),
    'error': ('⚠ недоступно', 'B9770E'),
}


def _build_home_dupes_sheet(wb, home_dupes):
    """Лист «Дубли главной»: одна и та же главная не должна открываться по разным
    адресам с кодом 200 (www/без, http/https, слэши, index.php, ?параметр).
    Строится, только если проверка выполнялась."""
    if not home_dupes or not home_dupes.get('available'):
        return
    variants = home_dupes.get('variants') or []
    ws = wb.create_sheet('Дубли главной')
    ws.sheet_view.showGridLines = False

    dupes = int(home_dupes.get('dupes', 0) or 0)
    c = ws.cell(row=1, column=1, value='Каноническая главная:')
    c.font = _font(bold=True)
    ws.cell(row=1, column=2, value=home_dupes.get('home', '—')).font = _font()
    c = ws.cell(row=2, column=1, value='Реальных дублей:')
    c.font = _font(bold=True)
    c2 = ws.cell(row=2, column=2, value=dupes)
    c2.font = _font(bold=True, color=('C0392B' if dupes else '006300'))

    head_row = 4
    for i, t in enumerate(('Адрес', 'Ответ', 'Что происходит', 'Вердикт'), 1):
        cell = ws.cell(row=head_row, column=i, value=t)
        cell.font = _font(bold=True)
        cell.fill = _fill('ECEAE4')
        cell.border = _border()
        cell.alignment = _align('center' if i > 1 else 'left')

    # дубли - вверх списка, дальше по осмысленному порядку
    order = {'duplicate': 0, 'error': 1, 'canonical': 2, 'main': 3,
             'redirect': 4, 'absent': 5}
    rows = sorted(variants, key=lambda v: order.get(v.get('verdict'), 9))
    r = head_row + 1
    for v in rows:
        verdict = v.get('verdict', 'error')
        label, color = _HOME_DUPES_VERDICT.get(verdict, ('?', C.text))
        ca = ws.cell(row=r, column=1, value=v.get('url', ''))
        ca.font = _font()
        ca.border = _border()
        cs = ws.cell(row=r, column=2, value=str(v.get('status', '')))
        cs.font = _font()
        cs.border = _border()
        cs.alignment = _align('center')
        cn = ws.cell(row=r, column=3, value=v.get('note', ''))
        cn.font = _font()
        cn.border = _border()
        cv = ws.cell(row=r, column=4, value=label)
        cv.font = _font(bold=True, color=color)
        cv.border = _border()
        if verdict == 'duplicate':
            for col in range(1, 5):
                ws.cell(row=r, column=col).fill = _fill('FBE9E7')
        r += 1

    note = ('Дубль = главная открывается по этому адресу с кодом 200, а поисковик '
            'не склеивает его с главной (нет редиректа и canonical не на главную). '
            'Лечится 301-редиректом на главную или тегом canonical.')
    ws.cell(row=r + 1, column=1, value=note).font = _font(italic=True, color='5B5853')

    ws.column_dimensions['A'].width = 48
    ws.column_dimensions['B'].width = 9
    ws.column_dimensions['C'].width = 40
    ws.column_dimensions['D'].width = 24


def _build_arsenkin_sheet(wb, arsenkin):
    """Лист «Индексация (Арсенкин)»: есть ли URL в индексе Яндекса и Google
    (через API Арсенкина). Строится, только если проверка выполнялась."""
    if not arsenkin or not arsenkin.get('available'):
        return
    rows = arsenkin.get('rows') or []
    eng = arsenkin.get('engines') or {'yandex': True, 'google': True}
    ws = wb.create_sheet('Индексация (Арсенкин)')
    ws.sheet_view.showGridLines = False

    ni = int(arsenkin.get('not_indexed', 0) or 0)
    ws.cell(row=1, column=1, value='Проверено URL:').font = _font(bold=True)
    ws.cell(row=1, column=2, value=arsenkin.get('checked', len(rows))).font = _font()
    ws.cell(row=2, column=1, value='Не в индексе:').font = _font(bold=True)
    c2 = ws.cell(row=2, column=2, value=ni)
    c2.font = _font(bold=True, color=('C0392B' if ni else '006300'))
    _det = []
    if eng.get('yandex'):
        _det.append(f'Яндекс: {arsenkin.get("not_indexed_yandex", 0)}')
    if eng.get('google'):
        _det.append(f'Google: {arsenkin.get("not_indexed_google", 0)}')
    ws.cell(row=2, column=3, value='  '.join(_det)).font = _font(color='5B5853')

    head_row = 4
    for i, t in enumerate(('URL', 'В Яндексе', 'В Google'), 1):
        cell = ws.cell(row=head_row, column=i, value=t)
        cell.font = _font(bold=True)
        cell.fill = _fill('ECEAE4')
        cell.border = _border()
        cell.alignment = _align('center' if i > 1 else 'left')

    def _cell(row, col, flag, checked):
        c = ws.cell(row=row, column=col)
        c.border = _border()
        c.alignment = _align('center')
        if not checked:
            c.value, c.font = '—', _font(color='8A8781')
        elif flag is True:
            c.value, c.font = 'Да', _font(bold=True, color='006300')
        elif flag is False:
            c.value, c.font = 'Нет', _font(bold=True, color='C0392B')
            c.fill = _fill('FBE9E7')
        else:
            c.value, c.font = '?', _font(color='B9770E')

    # не в индексе - вверх списка
    def _rank(r):
        bad = ((eng.get('yandex') and r.get('yandex') is False)
               or (eng.get('google') and r.get('google') is False))
        return 0 if bad else 1
    r = head_row + 1
    for row in sorted(rows, key=_rank):
        cu = ws.cell(row=r, column=1, value=row.get('url', ''))
        cu.font = _font()
        cu.border = _border()
        _cell(r, 2, row.get('yandex'), eng.get('yandex'))
        _cell(r, 3, row.get('google'), eng.get('google'))
        r += 1

    ws.column_dimensions['A'].width = 70
    ws.column_dimensions['B'].width = 12
    ws.column_dimensions['C'].width = 12


def _build_w3c_sheet(wb, w3c_check):
    """Лист валидации W3C (HTML/CSS) и скорости загрузки ресурсов по выборке
    страниц. Добавляется, только если проверка выполнялась."""
    if not w3c_check:
        return
    pages = w3c_check.get('pages') or []
    show = w3c_check.get('show') or {'valid': True, 'static': True}
    _sv, _ss = show.get('valid', True), show.get('static', True)

    ws = wb.create_sheet('Валидация и скорость')
    ws.sheet_view.showGridLines = False
    def _page_fail(p):
        """Причина, по которой валидность не проверена (403/429/502…), или ''."""
        return (str((p.get('html') or {}).get('error') or '')
                or str((p.get('css') or {}).get('error') or ''))

    def _perf_warn(p):
        """Проблема со сжатием/кешем статики (для окраски вкладки)."""
        t = p.get('timings') or {}
        cp = t.get('compression') or {}
        ca = t.get('caching') or {}
        return ((cp.get('checked') and cp.get('ok', 0) < cp['checked'])
                or (ca.get('checked') and ca.get('ok', 0) < ca['checked']))

    _any_err = ((_sv and any((p.get('html') or {}).get('errors')
                             or (p.get('css') or {}).get('errors')
                             for p in pages))
                or (_ss and any(_perf_warn(p) for p in pages)))
    _any_blocked = _sv and any(_page_fail(p) for p in pages)
    ws.sheet_properties.tabColor = C.warn if (_any_err or _any_blocked) else C.ok

    for col, w in (('A', 3), ('B', 50), ('C', 20), ('D', 20), ('E', 46), ('F', 3)):
        ws.column_dimensions[col].width = w

    # Заголовок и вводка зависят от того, какие пункты включены (1.16 / 1.17).
    if _sv and _ss:
        _title = 'Валидация W3C, скорость и доставка статики (пп.1.16-1.17)'
    elif _sv:
        _title = 'Валидация W3C и скорость ресурсов (п.1.16)'
    else:
        _title = 'Сжатие и кеширование статики (п.1.17)'
    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = _title
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    _intro = []
    if _sv:
        _intro.append('(1.16) HTML валиден - W3C Nu (validator.w3.org); CSS '
                      'валиден - W3C CSS Validator (jigsaw.w3.org); время '
                      'загрузки ресурсов - качаем HTML/CSS/JS/шрифты/картинки '
                      'и суммируем по типам. Ошибки валидатора = предупреждение '
                      '(у боевых сайтов их часто много).')
    if _ss:
        _intro.append('(1.17) сжатие своей статики (Gzip/Brotli, по '
                      'Content-Encoding) и кеш (Cache-Control/ETag/Expires) - '
                      'по заголовкам ответа CSS/JS того же домена.')
    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('По ВЫБОРКЕ страниц (главная/категория/товар). '
               + ' '.join(_intro))
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5
    if not w3c_check.get('available') or not pages:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = w3c_check.get('note') or 'Проверка не выполнялась.'
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True)
        return

    # Баннер: W3C не проверил валидность (403 Cloudflare / 429 лимит / 502 сбой).
    _fails = [_page_fail(p) for p in pages if _page_fail(p)] if _sv else []
    _blocked = len(_fails)
    _reason = _fails[0] if _fails else ''
    if _blocked:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = (f'⚠ W3C не проверил валидность HTML/CSS на '
                   f'{_blocked} из {len(pages)} страниц ({_reason}). Это не '
                   f'ошибка сайта, а лимит/сбой бесплатного сервиса W3C: '
                   f'повторить проверку 1.16 позже (через час/на следующий '
                   f'день) и реже включать. Время загрузки ресурсов ниже '
                   f'измерено корректно.')
        c.font = _font(size=10, bold=True, color=C.warn)
        c.fill = _fill(C.warn_soft)
        c.alignment = _align(wrap=True, vertical='top')
        ws.row_dimensions[row].height = 56
        row += 2

    for p in pages:
        # Заголовок страницы
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = p.get('url', '')
        c.hyperlink = p.get('url', '')
        c.font = _font(size=11, bold=True, color=C.accent, underline='single')
        c.fill = _fill(C.surface)
        c.alignment = _align(indent=1)
        c.border = _border()
        ws.row_dimensions[row].height = 22
        row += 1

        if p.get('error'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = '⚠ ' + p['error']
            c.font = _font(size=10, color=C.warn)
            c.alignment = _align(indent=2)
            ws.row_dimensions[row].height = 18
            row += 2
            continue

        def _line(label, value, color=C.text):
            nonlocal row
            k = ws.cell(row=row, column=2, value=label)
            k.font = _font(size=10, color=C.text_muted)
            k.alignment = _align(indent=2)
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
            v = ws.cell(row=row, column=3, value=value)
            v.font = _font(size=10, color=color)
            v.alignment = _align(wrap=True)
            ws.row_dimensions[row].height = 18
            row += 1

        _t = p.get('timings') or {}
        # ── п.1.16: валидация HTML/CSS + скорость ──
        if _sv:
            # HTML
            _h = p.get('html') or {}
            if _h.get('error'):
                _line('HTML (W3C Nu):', f'не проверено — {_h["error"]}',
                      C.text_muted)
            else:
                _he = _h.get('errors')
                _line('HTML (W3C Nu):',
                      ('✅ валиден (0 ошибок)' if _he == 0
                       else f'⚠ {_he} ошибок, {_h.get("warnings", 0)} замечаний'),
                      C.ok if _he == 0 else C.warn)
                if _he and _h.get('samples'):
                    _line('  примеры:', '; '.join(_h['samples'][:2]), C.text_soft)
            # CSS
            _cs = p.get('css') or {}
            if _cs.get('error'):
                _line('CSS (W3C):', f'не проверено — {_cs["error"]}', C.text_muted)
            else:
                _ce = _cs.get('errors')
                _line('CSS (W3C):',
                      ('✅ валиден (0 ошибок)' if _ce == 0
                       else f'⚠ {_ce} ошибок, {_cs.get("warnings", 0)} замечаний'),
                      C.ok if _ce == 0 else C.warn)
            # Скорость
            if _t:
                bt = _t.get('by_type', {})
                _parts = [f'HTML {_t.get("html_ms", 0)}мс']
                for k, ru in (('css', 'CSS'), ('js', 'JS'), ('font', 'шрифты'),
                              ('img', 'картинки')):
                    d = bt.get(k) or {}
                    if d.get('count'):
                        _parts.append(
                            f'{ru} {d["ms"]}мс/{d["count"]}шт/{d["kb"]}КБ')
                _line('Скорость ресурсов:', ' · '.join(_parts), C.text)
                _sl = _t.get('slowest') or {}
                if _sl.get('url'):
                    _line('  самый долгий:',
                          f'{_sl["ms"]}мс — {_sl["url"].rsplit("/", 1)[-1]} '
                          f'({_sl.get("kind", "")})', C.text_soft)
                _line('  итого загрузка:', f'{_t.get("total_ms", 0)} мс',
                      C.warn if _t.get('total_ms', 0) > 8000 else C.text)

        # ── п.1.17: сжатие и кеш статики ──
        if _ss and _t:
            # Сжатие статики (Gzip/Brotli) - по CSS/JS.
            _cp = _t.get('compression') or {}
            if _cp.get('checked'):
                _ok, _n = _cp.get('ok', 0), _cp['checked']
                _enc = ', '.join(_cp.get('enc') or []) or '—'
                if _ok >= _n:
                    _line('Сжатие CSS/JS:',
                          f'✅ включено ({_enc}) — {_ok} из {_n}', C.ok)
                elif _ok:
                    _line('Сжатие CSS/JS:',
                          f'⚠ частично ({_enc}): сжато {_ok} из {_n}. Без сжатия: '
                          + '; '.join(u.rsplit('/', 1)[-1]
                                      for u in _cp.get('missing', [])[:4]),
                          C.warn)
                else:
                    _line('Сжатие CSS/JS:',
                          f'⚠ НЕ включено (Gzip/Brotli) — 0 из {_n}. Включите '
                          'сжатие статики на сервере (ускорит загрузку).', C.warn)
            # Кеширование статики (Cache-Control/ETag/Expires).
            _ca = _t.get('caching') or {}
            if _ca.get('checked'):
                _ok, _n = _ca.get('ok', 0), _ca['checked']
                if _ok >= _n:
                    _line('Кеш статики:',
                          f'✅ настроен (Cache-Control/ETag) — {_ok} из {_n}', C.ok)
                elif _ok:
                    _line('Кеш статики:',
                          f'⚠ частично: с кешем {_ok} из {_n}. Без кеша: '
                          + '; '.join(u.rsplit('/', 1)[-1]
                                      for u in _ca.get('missing', [])[:4]),
                          C.warn)
                else:
                    _line('Кеш статики:',
                          f'⚠ НЕ настроен (Cache-Control/ETag/Expires) — 0 из '
                          f'{_n}. Настройте заголовки кеша статики.', C.warn)
        row += 1


# ── Лист «Страница 404» (п.1.18) ────────────────────────────────────


def _build_404_sheet(wb, p404_check):
    """Лист проверки 404-страницы: код ответа, дизайн, title/description,
    ссылки на разделы и форма. Добавляется, только если проверка выполнялась."""
    if not p404_check:
        return
    hosts = p404_check.get('hosts') or []
    has_bugs = any(h.get('issues') for h in hosts)
    has_warns = any(h.get('warnings') for h in hosts)

    ws = wb.create_sheet('Страница 404')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if has_bugs
                                    else C.warn if has_warns else C.ok)

    for col, w in (('A', 3), ('B', 24), ('C', 60), ('D', 60), ('E', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:D2')
    c = ws['B2']
    c.value = 'Страница 404 (п.1.18)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:D3')
    c = ws['B3']
    c.value = ('Запрашиваем заведомо несуществующий адрес и проверяем: '
               '(1) код ответа ровно 404 (200 = soft-404 шаблон, баг; '
               'редирект = предупреждение); (2) дизайн совпадает с главной - '
               'косвенно, по общим CSS-файлам и шапке/подвалу шаблона '
               '(пиксельное сравнение без браузера невозможно); '
               '(3) уникальный <title> (не как у главной) и meta description; '
               '(4) есть ссылки на основные разделы и форма заявки/телефон; '
               '(5) несуществующие служебные адреса тоже отдают 404: '
               'пагинация ?PAGEN_1=999999 (200 прощаем при canonical без '
               'номера) и мусорный фильтр /filter/…/. Шаблон 404 сквозной - '
               'проверяются главный домен и один поддомен.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5
    if not p404_check.get('available') or not hosts:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        c = ws.cell(row=row, column=2)
        c.value = 'Проверка не выполнялась.'
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True)
        return

    for h in hosts:
        # Заголовок хоста
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        c = ws.cell(row=row, column=2)
        _st = h.get('status')
        c.value = (f'{h.get("city", "")} — {h.get("host", "")}   '
                   f'(проба: HTTP {_st if _st is not None else "—"})')
        c.font = _font(size=11, bold=True)
        c.fill = _fill(C.surface)
        c.alignment = _align(indent=1)
        c.border = _border()
        ws.row_dimensions[row].height = 22
        row += 1

        def _line(text, color):
            nonlocal row
            ws.merge_cells(start_row=row, start_column=2,
                           end_row=row, end_column=4)
            c = ws.cell(row=row, column=2)
            c.value = text
            c.font = _font(size=10, color=color)
            c.alignment = _align(indent=2, wrap=True)
            ws.row_dimensions[row].height = 18
            row += 1

        if h.get('error'):
            _line('⚠ ' + h['error'], C.text_muted)
        elif not h.get('issues') and not h.get('warnings'):
            _line('✅ Код 404, дизайн шаблона, свой заголовок, ссылки на '
                  'разделы и форма - всё на месте.', C.ok)
        else:
            for t in h.get('issues') or []:
                _line('❌ ' + t, C.err)
            for t in h.get('warnings') or []:
                _line('⚠ ' + t, C.warn)
        # Подтверждение проб пагинации/фильтра (проблемные уже выше текстом).
        for p in h.get('probes') or []:
            if p.get('ok'):
                _note = f' ({p["note"]})' if p.get('note') else ''
                _line(f'✓ {p["kind"]}: HTTP {p["status"]}{_note} - корректно',
                      C.text_muted)
        row += 1


def _wm_alive_url(url, section='optimization/checklist/'):
    """Живая ссылка в панель Вебмастера. Старые кеши хранят мёртвые пути
    (/diagnostics/ и /links/external/ отдают 404 - панель переехала) -
    подменяем хвост на актуальный раздел."""
    if not url:
        return None
    for dead in ('diagnostics/', 'links/external/'):
        if url.endswith(dead):
            return url[:-len(dead)] + section
    return url


# ── Лист «404 в индексе» (регулярный мониторинг страниц в поиске) ───


def _index_404_code(status) -> int:
    try:
        return int(str(status).strip() or 0)
    except (ValueError, TypeError):
        return 0


def _build_index_404_sheet(wb, index_404_check):
    """Лист «404 в индексе» - понятная таблица битых страниц из поиска.
    Каждая строка: сайт, адрес, что не так (простыми словами), код, источник.
    Источники комбо (Sitemap / Яндекс / Google) сливаются, дубли по URL
    схлопываются. Сортируется/фильтруется. Добавляется, если проверка была."""
    if not index_404_check:
        return
    hosts = index_404_check.get('hosts') or []
    _MAX_ROWS = 2000                 # предохранитель от гигантского файла
    _RANK = {'dead': 0, 'server': 1, 'client': 2}

    # Собираем проблемы, схлопывая по URL и объединяя источники.
    by_url = {}

    def _add(site, r, kind):
        url = r.get('url', '')
        if not url:
            return
        code = _index_404_code(r.get('status'))
        # Код для показа: число, если известно; иначе сырой статус (GSC 5xx).
        code_txt = str(code) if code else (str(r.get('status') or '').strip() or '—')
        src = r.get('source', '')
        p = by_url.get(url)
        if p is None:
            by_url[url] = {'site': site, 'url': url, 'code': code,
                           'code_txt': code_txt, 'kind': kind,
                           'sources': ({src} if src else set())}
        else:
            if src:
                p['sources'].add(src)
            if _RANK.get(kind, 9) < _RANK.get(p['kind'], 9):
                p.update(site=site, code=code, code_txt=code_txt, kind=kind)

    for h in hosts:
        site = h.get('host', '')
        for r in h.get('dead') or []:
            _add(site, r, 'dead')
        for r in h.get('errors') or []:
            code = _index_404_code(r.get('status'))
            _add(site, r, 'server' if (code >= 500 or code == 0) else 'client')

    problems = list(by_url.values())
    n_dead = sum(1 for p in problems if p['kind'] == 'dead')
    n_err = len(problems) - n_dead
    bad_sites = len({p['site'] for p in problems})
    has_any = bool(problems)
    src_list = [s for s in (index_404_check.get('sources') or []) if s]

    ws = wb.create_sheet('404 в индексе')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if n_dead
                                    else C.warn if n_err else C.ok)
    for col, w in (('A', 2), ('B', 18), ('C', 82), ('D', 20), ('E', 7),
                   ('F', 16)):
        ws.column_dimensions[col].width = w

    # Заголовок.
    ws.merge_cells('B2:F2')
    c = ws['B2']
    c.value = '404 в индексе — страницы в поиске, которые открываются с ошибкой'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    # Короткий подзаголовок (перепроверено — в списке нет рабочих ссылок).
    ws.merge_cells('B3:F3')
    c = ws['B3']
    c.value = ('Страницы, которые Яндекс/Google держат в поиске, и которые ПРЯМО '
               'СЕЙЧАС отдают ошибку — каждая перепроверена живым запросом, '
               'рабочих ссылок в списке нет. Столбец «Источник» — откуда узнали.')
    c.font = _font(size=10, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 28

    row = 4

    # Проверка не выполнилась / нет данных.
    if index_404_check.get('error') and not hosts:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2)
        c.value = '⚠ Проверка не выполнилась: ' + str(index_404_check['error'])
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True)
        return
    if not index_404_check.get('available') or not hosts:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2)
        c.value = 'Проверка не выполнялась.'
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True)
        return

    # Крупная сводка.
    total_checked = index_404_check.get('total_checked', 0)
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    c = ws.cell(row=row, column=2)
    _rev = ' · перепроверено живьём' if index_404_check.get('reverified') else ''
    if has_any:
        c.value = (f'Найдено {len(problems)} битых страниц на {bad_sites} '
                   f'сайт(ах):   🔴 удалены (404): {n_dead}   '
                   f'🟠 ошибка сервера: {n_err}{_rev}')
        c.font = _font(size=13, bold=True, color=C.err)
    else:
        c.value = ('✅ Битых страниц в поиске не найдено — все, что поисковики '
                   'считали битыми, при живой перепроверке открылись нормально.')
        c.font = _font(size=13, bold=True, color=C.ok)
    ws.row_dimensions[row].height = 22
    row += 1

    _src_txt = f'источники: {", ".join(src_list)}' if src_list else ''
    if _src_txt:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2)
        c.value = _src_txt
        c.font = _font(size=10, color=C.text_muted)
        ws.row_dimensions[row].height = 14
        row += 1

    if not has_any:
        return

    # Что делать — простыми словами, один раз.
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    c = ws.cell(row=row, column=2)
    c.value = ('Что делать:  🔴 404 — 301-редирект на живой раздел или убрать из '
               'индекса.   🟠 5xx — проверить, почему сервер отваливается (часто '
               'тяжёлые страницы фильтров).')
    c.font = _font(size=10, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[row].height = 28
    row += 1

    # Инсайт: почти все битые — страницы фильтров.
    n_filter = sum(1 for p in problems if '/filter/' in (p['url'] or ''))
    if len(problems) >= 10 and n_filter / len(problems) >= 0.6:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2)
        c.value = (f'⚠ {n_filter} из {len(problems)} — страницы фильтров каталога '
                   f'(…/filter/…): фильтры плодят ссылки на несуществующие '
                   f'комбинации, их стоит закрыть от индексации.')
        c.font = _font(size=10, color=C.warn)
        c.alignment = _align(wrap=True, vertical='top')
        ws.row_dimensions[row].height = 26
        row += 1

    # Шапка таблицы.
    hdr = row
    for col, title in (('B', 'Сайт'), ('C', 'Адрес страницы'),
                       ('D', 'Проблема'), ('E', 'Код'), ('F', 'Источник')):
        cell = ws[f'{col}{hdr}']
        cell.value = title
        cell.font = _font(size=10, bold=True, color=C.text)
        cell.fill = _fill(C.surface)
        cell.border = _border()
        cell.alignment = _align(indent=1)
    ws.row_dimensions[hdr].height = 20
    row += 1

    # Сначала 404 (важнее), затем ошибки сервера; внутри - по сайту и адресу.
    problems.sort(key=lambda p: (_RANK.get(p['kind'], 9), p['site'], p['url']))
    _KIND = {'dead': ('Страница удалена', C.err),
             'server': ('Сервер не ответил', C.warn),
             'client': ('Страница недоступна', C.warn)}

    for p in problems[:_MAX_ROWS]:
        label, color = _KIND.get(p['kind'], ('Ошибка', C.warn))
        b = ws.cell(row=row, column=2)
        b.value = p['site']
        b.font = _font(size=10)
        b.alignment = _align(indent=1)
        u = ws.cell(row=row, column=3)
        u.value = p['url']
        u.font = _font(size=10, color=C.accent, underline='single')
        if p['url']:
            u.hyperlink = p['url']
        u.alignment = _align(indent=1)
        pc = ws.cell(row=row, column=4)
        pc.value = label
        pc.font = _font(size=10, bold=True, color=color)
        pc.alignment = _align(indent=1)
        e = ws.cell(row=row, column=5)
        e.value = p.get('code_txt') or '—'
        e.font = _font(size=10, color=color)
        e.alignment = _align(horizontal='center')
        s = ws.cell(row=row, column=6)
        s.value = ', '.join(sorted(p['sources'])) or '—'
        s.font = _font(size=10, color=C.text_soft)
        s.alignment = _align(indent=1)
        for cc in (2, 3, 4, 5, 6):
            ws.cell(row=row, column=cc).border = _border()
        ws.row_dimensions[row].height = 15
        row += 1

    last = row - 1
    ws.auto_filter.ref = f'B{hdr}:F{last}'
    ws.freeze_panes = f'B{hdr + 1}'

    if len(problems) > _MAX_ROWS:
        row += 1
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2)
        c.value = (f'…показаны первые {_MAX_ROWS} из {len(problems)} — '
                   f'остальные того же типа, чинятся так же.')
        c.font = _font(size=10, italic=True, color=C.text_muted)


# ── Лист «Фильтры ПС» (п.1.19: санкции поисковых систем) ───────────


def _build_ps_filters_sheet(wb, ps_filters):
    """Лист санкций/фильтров поисковых систем: диагностика Яндекс.Вебмастера
    (санкционные коды) + маркеры ручных мер в почте GSC. Добавляется, только
    если проверка выполнялась."""
    if not ps_filters:
        return
    sanc = ps_filters.get('yandex') or []
    gsc_hits = ps_filters.get('gsc_hits') or []
    has_bugs = bool(sanc or gsc_hits)

    ws = wb.create_sheet('Фильтры ПС')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

    for col, w in (('A', 3), ('B', 30), ('C', 60), ('D', 46), ('E', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:D2')
    c = ws['B2']
    c.value = 'Фильтры поисковых систем (п.1.19)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:D3')
    c = ws['B3']
    c.value = ('Санкции за переоптимизацию/угрозы. Яндекс: санкционные '
               'сигналы из диагностики Вебмастера (FATAL и коды угроз/'
               'качества) - надёжный официальный источник, виден только при '
               'подтверждённых правах. Google: API ручных мер не существует - '
               'сканируем почтовые уведомления GSC за 90 дней по маркерам '
               '(«ручные меры», «security issue» и т.п.) и даём ссылку для '
               'ручной сверки в Search Console.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5

    def _line(text, color, bold=False, link=None):
        nonlocal row
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        c = ws.cell(row=row, column=2)
        c.value = text
        c.font = _font(size=10, bold=bold, color=color,
                       underline='single' if link else None)
        if link:
            c.hyperlink = link
        c.alignment = _align(indent=1, wrap=True)
        ws.row_dimensions[row].height = 18
        row += 1

    # ── Яндекс ──
    _line('Яндекс (диагностика Вебмастера)', C.text, bold=True)
    if not ps_filters.get('wm_collected'):
        _line('⚠ Сбор диагностики Вебмастера в этом прогоне выключен - '
              'данные из прошлого кеша либо отсутствуют. Включите галочку '
              'Вебмастера для свежей проверки.', C.warn)
    if sanc:
        for s in sanc:
            _line(f'❌ {s.get("host", "")}: {s.get("title", s.get("code", ""))} '
                  f'({s.get("date", "")}) - открыть панель',
                  C.err, link=_wm_alive_url(s.get('url')))
    else:
        _line(f'✅ Санкций/угроз в диагностике Вебмастера нет '
              f'(хостов: {ps_filters.get("wm_hosts", 0)}, '
              f'проблем всего: {ps_filters.get("wm_issues_total", 0)} - '
              f'несанкционные см. лист «Ошибки сервисов»).', C.ok)
    row += 1

    # ── Google ──
    _line('Google (уведомления GSC + ручная сверка)', C.text, bold=True)
    if gsc_hits:
        for h in gsc_hits:
            _line(f'❌ {h.get("date", "")}: {h.get("subject", "")}', C.err)
    else:
        _line(f'✅ В почтовых уведомлениях GSC за 90 дней маркеров ручных '
              f'мер/безопасности нет (писем просмотрено: '
              f'{ps_filters.get("gsc_scanned", 0)}).', C.ok)
    _line('Ручная сверка (1 клик): Search Console → «Меры, принятые '
          'вручную» и «Проблемы безопасности».', C.accent,
          link='https://search.google.com/search-console/manual-actions')


# ── Лист «Нагрузка и парсинг» (ошибки сервера: парсинг/нагрузка/дубли) ──


def _build_stress_sheet(wb, stress_check):
    """Лист «Нагрузка и парсинг»: нет ли ошибок сервера (5xx/обрывы) при
    быстром обходе-парсинге, параллельной нагрузке и кривых дублях URL.
    Добавляется, только если стресс-пробы выполнялись."""
    if not stress_check or not stress_check.get('available'):
        return
    parsing = stress_check.get('parsing') or {}
    load = stress_check.get('load') or {}
    dups = stress_check.get('duplicates') or {}

    parse_5xx = parsing.get('server_errors') or []
    parse_net = parsing.get('network_errors') or []
    banned = parsing.get('banned')
    load_pages = load.get('pages') or []
    load_5xx = sum(p.get('server_5xx', 0) for p in load_pages)
    load_net = sum(p.get('network_errors', 0) for p in load_pages)
    load_degraded = [p for p in load_pages if p.get('degraded')]
    dup_5xx = dups.get('server_errors') or []

    total_5xx = len(parse_5xx) + load_5xx + len(dup_5xx)
    has_bugs = bool(total_5xx or parse_net or load_net or banned)
    has_warn = bool(load_degraded or parsing.get('rate_limited'))

    ws = wb.create_sheet('Нагрузка и парсинг')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if has_bugs
                                    else C.warn if has_warn else C.ok)
    for col, w in (('A', 3), ('B', 30), ('C', 64), ('D', 44), ('E', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:D2')
    c = ws['B2']
    c.value = 'Нагрузка и парсинг (ошибки сервера)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:D3')
    c = ws['B3']
    c.value = ('Сервер не должен отдавать ошибки (5xx) при: (1) быстром '
               'обходе страниц парсингом; (2) высокой параллельной нагрузке; '
               '(3) кривых дублях адресов категорий/фильтров/товаров '
               '(сдвоенный сегмент, двойной слэш, глубокая пагинация, '
               'сдвоенный GET-параметр). Пробы идут в конце прогона; при '
               'первых же 5xx/обрывах проба останавливается, чтобы не '
               'добивать сервер. Бан на парсинге - нагрузку и дубли '
               'пропускаем (их результат стал бы недостоверным).')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 66

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    c = ws.cell(row=row, column=2)
    c.value = (f'Ошибок сервера (5xx) всего: {total_5xx}  ·  обрывов связи: '
               f'{len(parse_net) + load_net}'
               + ('  ·  БАН на парсинге' if banned else ''))
    c.font = _font(size=10, bold=True,
                   color=C.err if has_bugs else C.warn if has_warn else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 24
    row += 2

    def _line(text, color, bold=False, link=None):
        nonlocal row
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        c = ws.cell(row=row, column=2)
        c.value = text
        c.font = _font(size=10, bold=bold, color=color,
                       underline='single' if link else None)
        if link:
            c.hyperlink = link
        c.alignment = _align(indent=1, wrap=True)
        ws.row_dimensions[row].height = 18
        row += 1

    # ── 1. Парсинг ──
    _line(f'1. Парсинг - быстрый обход (проверено {parsing.get("checked", 0)} '
          f'из {parsing.get("total", 0)})', C.text, bold=True)
    if banned:
        _line(f'❌ Защита закрыла доступ (код {banned.get("code")}) после '
              f'{banned.get("after", 0)} успешных страниц - сайт принял бота '
              f'за парсера: {banned.get("url", "")}', C.err)
    if parse_5xx:
        for e in parse_5xx[:20]:
            _line(f'❌ {e.get("code")} на {e.get("url", "")}', C.err)
    if parse_net:
        _line(f'❌ Обрывы связи при обходе: {len(parse_net)} '
              f'(сервер не отвечал под последовательной нагрузкой)', C.err)
    if parsing.get('rate_limited'):
        _line(f'⚠ Rate-limit (429): {parsing["rate_limited"]} - сервер '
              f'притормаживал частые запросы', C.warn)
    if not (banned or parse_5xx or parse_net):
        _line('✅ Быстрый обход парсингом прошёл без ошибок сервера и бана.',
              C.ok)
    row += 1

    # ── 2. Высокая нагрузка ──
    if load.get('skipped') == 'ban':
        _line('2. Высокая нагрузка - пропущено из-за бана на парсинге.',
              C.text_muted, bold=True)
    else:
        _line(f'2. Высокая нагрузка - на каждую страницу залп '
              f'{load.get("concurrency", 0)} одновременных запросов × '
              f'{load.get("waves", 0)} волны '
              f'(итого {load.get("concurrency", 0) * load.get("waves", 0)} '
              f'запросов на страницу)', C.text, bold=True)
        for p in load_pages:
            _base = p.get('baseline_ms')
            _med = p.get('median_ms')
            _t = (f' · медиана {_med} мс'
                  + (f' против {_base} мс в прогоне' if _base else '')
                  ) if _med else ''
            if p.get('server_5xx') or p.get('network_errors'):
                _line(f'❌ {p.get("url", "")}: 5xx {p.get("server_5xx", 0)}, '
                      f'обрывов {p.get("network_errors", 0)} из '
                      f'{p.get("sent", 0)} запросов'
                      + ('  (проба остановлена - не добивали сервер)'
                         if p.get('stopped') else '') + _t, C.err)
            elif p.get('degraded'):
                _line(f'⚠ {p.get("url", "")}: под нагрузкой ответ замедлился '
                      f'более чем в 3 раза{_t}', C.warn)
            else:
                _line(f'✅ {p.get("url", "")}: держит нагрузку без 5xx{_t}',
                      C.ok)
        if not load_pages:
            _line('· Репрезентативных страниц для залпа не набралось - '
                  'пропуск.', C.text_muted)
    row += 1

    # ── 3. Дубли URL ──
    if dups.get('skipped') == 'ban':
        _line('3. Дубли URL - пропущено из-за бана на парсинге.',
              C.text_muted, bold=True)
    else:
        _line(f'3. Дубли категорий/фильтров/товаров - кривые вариации URL '
              f'(проверено {dups.get("checked", 0)} по '
              f'{dups.get("samples", 0)} страницам)', C.text, bold=True)
        if dup_5xx:
            for e in dup_5xx[:20]:
                _line(f'❌ {e.get("code")} на «{e.get("kind", "")}»: '
                      f'{e.get("url", "")}', C.err)
        else:
            _line('✅ На кривых дублях адресов сервер отвечает штатно '
                  '(200/301/404), 5xx нет.', C.ok)


# ── Лист «Ссылочный профиль» (lite-проверка беклинков, Вебмастер) ──


def _lp_rank(h):
    """Сортировка хостов: сначала самые проблемные. Группы: 0 - обвал массы,
    1 - спам-доноры/всплеск, 2 - прочие предупреждения, 3 - профиля нет,
    4 - норма. Внутри группы - по глубине просадки, затем по числу спама."""
    hist = h.get('history') or {}
    if hist.get('dropped'):
        grp = 0
    elif h.get('spam_count') or hist.get('spiked'):
        grp = 1
    elif h.get('warnings'):
        grp = 2
    elif h.get('infos'):
        grp = 3
    else:
        grp = 4
    return (grp, -(hist.get('drop_pct') or 0),
            -(h.get('recent_spam_count') or 0),
            -(h.get('spam_count') or 0), h.get('host') or '')


def _build_link_profile_sheet(wb, link_profile):
    """Лист «Ссылочный профиль»: таблица по всем хостам (объём/доноры/
    динамика/спам, данные Яндекс.Вебмастера), самые проблемные - сверху.
    Добавляется, только если проверка выполнялась."""
    if not link_profile:
        return
    hosts = sorted(link_profile.get('hosts') or [], key=_lp_rank)
    n_drop = sum(1 for h in hosts if (h.get('history') or {}).get('dropped'))
    n_spam = sum(1 for h in hosts
                 if h.get('spam_count') or (h.get('history') or {}).get('spiked'))
    n_warn = sum(1 for h in hosts if h.get('warnings'))
    n_empty = sum(1 for h in hosts
                  if h.get('infos') and not h.get('warnings'))
    n_recent = sum(1 for h in hosts if h.get('recent_spam_count'))

    ws = wb.create_sheet('Ссылочный профиль')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if n_drop or n_spam
                                    else C.warn if n_warn else C.ok)
    for col, w in (('A', 3), ('B', 28), ('C', 11), ('D', 11), ('E', 20),
                   ('F', 11), ('G', 16), ('H', 66), ('I', 10), ('J', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:H2')
    c = ws['B2']
    c.value = 'Ссылочный профиль (lite)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:H3')
    c = ws['B3']
    c.value = ('Беклинки по официальным данным Яндекс.Вебмастера (API v4). '
               'Смотрим: объём (всего внешних ссылок и доноров), динамику '
               '(резкий обвал = потеря ссылок; резкий всплеск = возможный '
               'спам/накрутка) и подозрительных доноров (мусорные зоны, '
               'gambling/adult), в т.ч. ВНЕЗАПНЫХ - появившихся за последние '
               '~30 дней (по discovery_date Яндекса) - это сигнал негативного '
               'SEO / закупки мусорных ссылок. Таблица отсортирована: самые проблемные '
               'хосты сверху. Глубокий аудит (Ahrefs/Majestic) - платный, '
               'здесь его нет. У Google API беклинков нет - внизу ссылка '
               'на ручную сверку в GSC.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 62

    if not link_profile.get('available'):
        ws.merge_cells('B5:H5')
        c = ws['B5']
        c.value = f'⚪ {link_profile.get("note", "Проверка не выполнена.")}'
        c.font = _font(size=10, color=C.text_muted)
        c.alignment = _align(indent=1, wrap=True)
        return
    if not hosts:
        ws.merge_cells('B5:H5')
        c = ws['B5']
        c.value = ('⚪ Верифицированных в Вебмастере хостов проекта не '
                   'нашлось - привяжите сайт в Вебмастере под тем же '
                   'аккаунтом.')
        c.font = _font(size=10, color=C.text_muted)
        c.alignment = _align(indent=1, wrap=True)
        return

    # Сводка: сколько хостов в какой группе
    ws.merge_cells('B5:H5')
    c = ws['B5']
    c.value = (f'Хостов: {len(hosts)} · обвал массы: {n_drop} · '
               f'спам/всплеск: {n_spam} · внезапные мусорные доноры: {n_recent} · '
               f'прочие предупреждения: '
               f'{max(n_warn - n_drop - n_spam, 0)} · без профиля: {n_empty} · '
               f'в норме: {len(hosts) - n_warn - n_empty}')
    c.font = _font(size=10, bold=True,
                   color=C.err if n_drop or n_spam else C.text)
    c.alignment = _align(indent=1)
    ws.row_dimensions[5].height = 20

    # Шапка таблицы
    hdr_row = 7
    headers = [('B', 'Хост'), ('C', 'Ссылок'), ('D', 'Доноров'),
               ('E', 'Динамика (было → сейчас)'), ('F', 'Просадка'),
               ('G', 'Статус'), ('H', 'Что не так'), ('I', 'Панель')]
    for col, title in headers:
        cell = ws[f'{col}{hdr_row}']
        cell.value = title
        cell.font = _font(size=10, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align(horizontal='center' if col in 'CDFI' else 'left',
                                indent=0 if col in 'CDFI' else 1)
        cell.border = _border()
    ws.row_dimensions[hdr_row].height = 24
    ws.freeze_panes = f'A{hdr_row + 1}'
    ws.auto_filter.ref = f'B{hdr_row}:I{hdr_row + len(hosts)}'

    _STATUS = {0: ('❌ обвал', C.err, C.err_soft),
               1: ('⚠ спам/всплеск', C.err, C.err_soft),
               2: ('⚠ внимание', C.warn, C.warn_soft),
               3: ('· нет профиля', C.text_muted, None),
               4: ('✅ норма', C.ok, None)}

    row = hdr_row + 1
    for h in hosts:
        hist = h.get('history') or {}
        grp = _lp_rank(h)[0]
        label, color, bg = _STATUS[grp]

        # Что не так: предупреждения + примеры спам-доноров + инфо
        problems = list(h.get('warnings') or [])
        if h.get('spam_hosts'):
            more = (f' … +{h["spam_count"] - len(h["spam_hosts"])}'
                    if h.get('spam_count', 0) > len(h['spam_hosts']) else '')
            problems.append('спам-доноры: ' + ', '.join(h['spam_hosts']) + more)
        problems.extend(h.get('infos') or [])
        problems_text = ('; '.join(problems) if problems
                         else 'динамика стабильна, спам-доноров в выборке нет')

        dyn = (f'{hist.get("first")} → {hist.get("latest")} '
               f'(пик {hist.get("peak")})' if hist.get('points') else '-')
        drop = (f'−{hist.get("drop_pct")}%'
                if (hist.get('drop_pct') or 0) > 0 else '-')

        cells = [
            ('B', h.get('host', ''), _font(size=10, color=C.text),
             _align(indent=1)),
            ('C', h.get('total', 0), _font(size=10, color=C.text_soft),
             _align(horizontal='center')),
            ('D', h.get('distinct_hosts', 0),
             _font(size=10, color=C.text_soft), _align(horizontal='center')),
            ('E', dyn, _font(size=10, color=C.text_soft), _align(indent=1)),
            ('F', drop,
             _font(size=10, bold=hist.get('dropped', False),
                   color=C.err if hist.get('dropped') else C.text_muted),
             _align(horizontal='center')),
            ('G', label, _font(size=10, bold=grp <= 1, color=color),
             _align(indent=1)),
            ('H', problems_text,
             _font(size=9, color=color if problems else C.text_muted),
             _align(indent=1, wrap=True)),
        ]
        for col, val, fnt, algn in cells:
            cell = ws[f'{col}{row}']
            cell.value = val
            cell.font = fnt
            cell.alignment = algn
            cell.border = _border(color=C.border_light)
            if bg:
                cell.fill = _fill(bg)

        lc = ws[f'I{row}']
        lc.border = _border(color=C.border_light)
        lc.alignment = _align(horizontal='center')
        purl = _wm_alive_url(h.get('panel_url'), 'links/incoming/')
        if purl:
            lc.value = 'открыть'
            lc.hyperlink = purl
            lc.font = _font(size=9, color=C.accent, underline='single')
        else:
            lc.value = '-'
            lc.font = _font(size=9, color=C.text_muted)
        if bg:
            lc.fill = _fill(bg)
        row += 1

    # ── Google - ручная сверка ──
    row += 1
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
    c = ws.cell(row=row, column=2)
    c.value = ('Google (беклинков по API нет): Search Console → «Ссылки» - '
               'внешние ссылки, топ сайтов-доноров, анкоры.')
    c.font = _font(size=10, color=C.accent, underline='single')
    c.hyperlink = (link_profile.get('gsc_links_url')
                   or 'https://search.google.com/search-console/links')
    c.alignment = _align(indent=1)


# ── Секция «Аномалии» (низ листа «Аналитика») ─────────────────────
# Сводит в одном месте резкие отклонения: аномалии Вебмастера (обход,
# проблемы, страницы/ИКС - Блок B) + внезапные мусорные доноры и скачки
# ссылочной массы (Блок A, детали - на листе «Ссылочный профиль»).

_ANOM_SEV = {'fatal': (0, '🔴 фатально'), 'critical': (1, '🔴 критично'),
             'possible': (2, '⚠ возможно'), 'info': (3, 'инфо')}


def _fmt_ba(before, after):
    """«было → сейчас» для колонки динамики."""
    b = '—' if before is None else str(before)
    a = '—' if after is None else str(after)
    return f'{b} → {a}' if before is not None else a


def _collect_anomaly_rows(wm_metrics, link_profile):
    """Плоский список аномалий из Вебмастера (wm_metrics) и ссылочного
    профиля (link_profile): [{host, metric, before, after, delta_pct,
    severity, text}]."""
    rows = []
    for h in (wm_metrics or {}).get('hosts') or []:
        for a in h.get('anomalies') or []:
            rows.append({**a, 'host': h.get('host', '')})
    # Ссылочный профиль → аномалии (детали на своём листе).
    for h in (link_profile or {}).get('hosts') or []:
        host = h.get('host', '')
        if h.get('recent_spam_count'):
            rows.append({
                'host': host, 'metric': 'Внезапные мусорные доноры',
                'before': None, 'after': h['recent_spam_count'], 'delta_pct': None,
                'severity': 'critical',
                'text': f'{h["recent_spam_count"]} новых спам-доноров за ~30 дн. '
                        f'- негативное SEO? (детали - лист «Ссылочный профиль»)'})
        hist = h.get('history') or {}
        if hist.get('dropped'):
            rows.append({
                'host': host, 'metric': 'Ссылочная масса',
                'before': hist.get('peak'), 'after': hist.get('latest'),
                'delta_pct': -(hist.get('drop_pct') or 0), 'severity': 'possible',
                'text': f'обвал ссылок −{hist.get("drop_pct")}% от пика - потеря доноров'})
        if hist.get('spiked'):
            rows.append({
                'host': host, 'metric': 'Рост ссылок',
                'before': hist.get('first'), 'after': hist.get('latest'),
                'delta_pct': None, 'severity': 'possible',
                'text': f'резкий рост ×{hist.get("spike_factor")} - проверить на спам/накрутку'})
    rows.sort(key=lambda r: (_ANOM_SEV.get(r.get('severity'), (9,))[0],
                             r.get('host', '')))
    return rows


def _render_wm_anomalies(ws, start_row, wm_metrics, link_profile):
    """Часть A секции «Аномалии»: Вебмастер (обход/проблемы/страницы/ИКС) +
    внезапные мусорные доноры. Пишет с start_row, возвращает следующую строку."""
    row = start_row
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    h = ws.cell(row=row, column=2, value='A. Вебмастер и ссылочный профиль')
    h.font = _font(size=12, bold=True, color='FFFFFF')
    h.fill = _fill(C.text_soft)
    h.alignment = _align(indent=1)
    ws.row_dimensions[row].height = 20
    row += 1

    if not wm_metrics.get('available'):
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2,
                    value=f'⚪ {wm_metrics.get("note", "Проверка не выполнялась.")}')
        c.font = _font(size=10, color=C.text_muted)
        c.alignment = _align(indent=1, wrap=True)
        return row + 2

    rows = _collect_anomaly_rows(wm_metrics, link_profile)
    n_red = sum(1 for r in rows if r.get('severity') in ('fatal', 'critical'))
    n_warn = sum(1 for r in rows if r.get('severity') == 'possible')

    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    c = ws.cell(row=row, column=2)
    if rows:
        c.value = (f'⚠ Аномалий: {len(rows)} · фатально/критично: {n_red} · '
                   f'возможных: {n_warn}. Проверьте по каждому.')
        c.font = _font(size=11, bold=True, color=C.err if n_red else C.warn)
    else:
        _hosts = len(wm_metrics.get('hosts') or [])
        c.value = (f'✅ Аномалий Вебмастера/ссылок нет (проверено хостов: '
                   f'{_hosts}). Обход, проблемы, страницы/ИКС и доноры - в норме.')
        c.font = _font(size=11, bold=True, color=C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(indent=1)
    ws.row_dimensions[row].height = 22
    row += 1
    if not rows:
        return row + 1

    hdr_row = row
    for col, title in (('B', 'Хост'), ('C', 'Метрика'),
                       ('D', 'Было → сейчас'), ('E', 'Отклонение'),
                       ('F', 'Что случилось')):
        cell = ws[f'{col}{hdr_row}']
        cell.value = title
        cell.font = _font(size=10, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align(horizontal='center' if col in 'DE' else 'left',
                                indent=0 if col in 'DE' else 1)
        cell.border = _border()
    ws.row_dimensions[hdr_row].height = 22
    row += 1

    for r in rows:
        red = r.get('severity') in ('fatal', 'critical')
        color = C.err if red else C.warn
        sev_label = _ANOM_SEV.get(r.get('severity'), (9, ''))[1]
        dpct = (f'−{abs(r["delta_pct"])}%' if r.get('delta_pct') else
                (sev_label.split(' ', 1)[-1] if sev_label else '-'))
        vals = [
            ('B', r.get('host', ''), _font(size=10), _align(indent=1)),
            ('C', r.get('metric', ''), _font(size=10, bold=True, color=color),
             _align(indent=1)),
            ('D', _fmt_ba(r.get('before'), r.get('after')),
             _font(size=10, color=C.text_soft), _align(horizontal='center')),
            ('E', dpct, _font(size=10, bold=red, color=color),
             _align(horizontal='center')),
            ('F', r.get('text', ''), _font(size=10, color=C.text), _align(wrap=True, indent=1)),
        ]
        for col, val, fnt, algn in vals:
            cell = ws[f'{col}{row}']
            cell.value = val
            cell.font = fnt
            cell.alignment = algn
            cell.border = _border(color=C.border_light)
            if red and col in 'CE':
                cell.fill = _fill(C.err_soft)
        ws.row_dimensions[row].height = 20
        row += 1
    return row + 1


def _build_anomalies_sheet(wb, wm_metrics, link_profile, anomalies):
    """Единый лист «Аномалии» (в конце группы «Аналитика»): (A) Вебмастер
    (обход/проблемы/страницы/ИКС) + внезапные мусорные доноры; (B) ГСК-запросы
    и Метрика-рефералы. Строится, если выполнялась хотя бы одна часть."""
    has_wm = bool(wm_metrics)
    _a = anomalies or {}
    has_q = bool(_a.get('gsc') or _a.get('metrika'))
    if not has_wm and not has_q:
        return
    ws = wb.create_sheet('Аномалии')
    ws.sheet_view.showGridLines = False
    for col, w in (('A', 3), ('B', 26), ('C', 24), ('D', 22), ('E', 12),
                   ('F', 60), ('G', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:F2')
    c = ws['B2']
    c.value = 'Аномалии'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:F3')
    c = ws['B3']
    c.value = ('Резкие отклонения «от себя-прошлого» - часто видны раньше, чем '
               'просядут позиции и трафик. A - Вебмастер (всплеск ошибок обхода '
               '4xx/5xx, просадка страниц, фатальные/критические проблемы, '
               'падение страниц в поиске/ИКС) и ссылочный профиль (внезапные '
               'мусорные доноры, скачки массы). B - всплеск мусорных/иноязычных '
               'запросов в ГСК и переходов со спам-сайтов в Метрике. Пусто - '
               'аномалий нет (норма).')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5
    if has_wm:
        row = _render_wm_anomalies(ws, row, wm_metrics, link_profile)
    if has_q:
        _render_query_anomalies(ws, row, anomalies)


# ── Лист «Настройки в админке» (доп. чек-лист: функции настройки) ──


# ── Лист «Я.Бизнес/GMB» ─────────────────────────────────────────────


def _build_yabusiness_sheet(wb, yabusiness):
    """Лист «Я.Бизнес/GMB»: каждый поддомен зарегистрирован под свой регион
    (Яндекс.Бизнес). Данные из кабинета Справочника на сессии. Добавляется,
    только если проверка выполнялась."""
    if not yabusiness:
        return
    missing = yabusiness.get('missing') or []
    matched = yabusiness.get('matched') or []
    orphans = yabusiness.get('orphan_orgs') or []
    has_problem = bool(missing) or not yabusiness.get('available')

    ws = wb.create_sheet('Я.Бизнес и GMB')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if missing else C.ok
                                    if yabusiness.get('available') else C.warn)
    for col, w in (('A', 3), ('B', 30), ('C', 40), ('D', 60), ('E', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:D2')
    c = ws['B2']
    c.value = 'Я.Бизнес / GMB'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:D3')
    c = ws['B3']
    c.value = ('Каждый поддомен (город) должен быть зарегистрирован в '
               'Яндекс.Бизнесе под своим регионом. Берём организации '
               'аккаунта из кабинета Справочника (город/регион карточки) и '
               'сверяем с городами поддоменов. «Сети» без единого города '
               'пропускаем (это группы). Данные - на сессии Яндекса (как '
               'автокликеры); при партнёрском доступе перейдём на API.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5
    if not yabusiness.get('available'):
        ws.merge_cells(f'B{row}:D{row}')
        cc = ws[f'B{row}']
        cc.value = f'⚪ {yabusiness.get("note", "Проверка не выполнена.")}'
        cc.font = _font(size=10, color=C.text_muted)
        cc.alignment = _align(indent=1, wrap=True)
        return

    n_sub = yabusiness.get('total_subdomains', 0)
    ws.merge_cells(f'B{row}:D{row}')
    cc = ws[f'B{row}']
    cc.value = (f'Поддоменов: {n_sub}  ·  с орг под свой город: '
                f'{len(matched)}  ·  БЕЗ орг: {len(missing)}  ·  активных '
                f'карточек в аккаунте: {yabusiness.get("active_orgs", 0)} '
                f'(сетей/пустых: {yabusiness.get("chains_or_empty", 0)})')
    cc.font = _font(size=11, bold=True, color=C.err if missing else C.ok)
    cc.fill = _fill(C.surface)
    cc.alignment = _align(indent=1, wrap=True)
    ws.row_dimensions[row].height = 22
    row += 2

    def _hdr(text):
        nonlocal row
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        h = ws.cell(row=row, column=2, value=text)
        h.font = _font(size=11, bold=True, color=C.text)
        h.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 20
        row += 1

    # Поддомены без орг - главная находка.
    if missing:
        _hdr(f'❌ Поддомены без организации под их город ({len(missing)})')
        for m in missing:
            ws.cell(row=row, column=2, value=m.get('city') or '').font = _font(
                size=10, color=C.err)
            uc = ws.cell(row=row, column=3, value=m.get('url') or '')
            uc.font = _font(size=9, color=C.accent, underline='single')
            if m.get('url'):
                uc.hyperlink = m['url']
            ws.cell(row=row, column=4,
                    value='нет карточки в Я.Бизнесе под этот город').font = \
                _font(size=9, color=C.text_soft)
            ws.row_dimensions[row].height = 15
            row += 1
        row += 1

    # Поддомены с орг.
    if matched:
        _hdr(f'✅ Поддомены с организацией ({len(matched)})')
        for m in matched:
            o = m.get('org') or {}
            ws.cell(row=row, column=2, value=m.get('city') or '').font = _font(
                size=10, color=C.ok)
            ws.cell(row=row, column=3,
                    value=f'орг {o.get("permalink","")} · регион '
                    f'{o.get("region","")}').font = _font(
                size=9, color=C.text_soft)
            ws.cell(row=row, column=4, value=o.get('addr') or '').font = _font(
                size=9, color=C.text_muted)
            ws.row_dimensions[row].height = 15
            row += 1
        row += 1

    # Организации без поддомена (лишние/чужие города).
    if orphans:
        _hdr(f'⚠ Организации без поддомена ({len(orphans)})')
        for o in orphans:
            ws.cell(row=row, column=2, value=o.get('city') or '').font = _font(
                size=10, color=C.warn)
            ws.cell(row=row, column=3,
                    value=f'орг {o.get("permalink","")} · регион '
                    f'{o.get("region","")}').font = _font(size=9, color=C.text_soft)
            ws.cell(row=row, column=4, value=o.get('addr') or '').font = _font(
                size=9, color=C.text_muted)
            ws.row_dimensions[row].height = 15
            row += 1
        row += 1

    # ── Пункт: все филиалы объединены в Сеть ──
    cch = yabusiness.get('chain_check') or {}
    if cch:
        row += 1
        united = cch.get('united')
        _hdr(('✅ ' if united else '❌ ') + 'Все филиалы объединены в Сеть')
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        cc = ws.cell(row=row, column=2)
        if united:
            cc.value = (f'все филиалы объединены в сети (в сетях '
                        f'{cch.get("chain_members", 0)} филиалов, отдельных '
                        f'компаний нет)')
            cc.font = _font(size=10, color=C.ok)
        else:
            cc.value = (f'НЕ объединены: {cch.get("standalone_companies", 0)} '
                        f'отдельных компаний (карточек) вне сети; в сетях '
                        f'{cch.get("chain_members", 0)} филиалов, сетей '
                        f'{cch.get("chains", 0)} - отдельные свести в Сеть')
            cc.font = _font(size=10, color=C.err)
        cc.alignment = _align(indent=1, wrap=True)
        ws.row_dimensions[row].height = 30
        row += 2

    # ── Пункт: максимально заполнен профиль ──
    pch = yabusiness.get('profile_check') or {}
    porgs = pch.get('orgs') or []
    if porgs:
        row += 1
        _hdr(('✅ ' if pch.get('all_full') else '⚠ ')
             + f'Заполненность профиля организаций ({len(porgs)})')
        for o in porgs:
            miss = o.get('missing') or []
            ws.cell(row=row, column=2, value=o.get('city') or '').font = _font(
                size=10, color=C.ok if not miss else C.warn)
            ws.cell(row=row, column=3,
                    value=f'заполнено {o.get("filled",0)}/{o.get("total",0)}'
                    ).font = _font(size=9, color=C.text_soft)
            ws.cell(row=row, column=4,
                    value=('всё заполнено' if not miss
                           else 'не заполнено: ' + ', '.join(miss))).font = \
                _font(size=9, color=C.text_muted if not miss else C.warn)
            ws.row_dimensions[row].height = 15
            row += 1

    # ── Пункт: закупаются отзывы на важные филиалы (≥1 в месяц) ──
    rch = yabusiness.get('reviews_check') or {}
    rorgs = rch.get('orgs') or []
    if rorgs:
        row += 1
        n_mon = rch.get('months', 3)
        bad = [o for o in rorgs if not o.get('ok')]
        _hdr(('✅ ' if rch.get('all_ok') else '❌ ')
             + f'Отзывы на важные филиалы (≥1/мес за {n_mon} мес) - '
             + (f'без отзыва в срок: {len(bad)} из {len(rorgs)}'
                if bad else 'у всех есть'))
        for o in rorgs:
            miss = o.get('missing_months') or []
            ws.cell(row=row, column=2, value=o.get('city') or '').font = _font(
                size=10, color=C.ok if o.get('ok') else C.err)
            last = o.get('last_review')
            ws.cell(row=row, column=3,
                    value=f'всего отзывов {o.get("total_reviews",0)}'
                    + (f' · последний {last}' if last else ' · отзывов нет')
                    ).font = _font(size=9, color=C.text_soft)
            ws.cell(row=row, column=4,
                    value=('норма' if o.get('ok')
                           else 'нет отзыва за: ' + ', '.join(miss))).font = \
                _font(size=9, color=C.text_muted if o.get('ok') else C.err)
            ws.row_dimensions[row].height = 15
            row += 1


_TRAFFIC_COLS = [
    ('Год', 8), ('Срез', 11),
    ('Итого по каналам', 15), ('Прямые заходы', 13), ('Яндекс', 10),
    ('Google', 10), ('Лиды', 8), ('Конверсия, %', 12), ('Отказы, %', 10),
    ('Глубина', 9), ('Время на сайте', 13),
    ('Главная', 10), ('Категория', 11), ('Услуга', 9), ('Товар', 9),
    ('Фильтр', 9), ('Тег', 8), ('Информационная', 14), ('Техническая', 12),
]


_MONTHS_NOM = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
               'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']


def _fmt_duration(sec):
    """Секунды → «м:сс» (0 → «0:00»)."""
    sec = int(sec or 0)
    return f'{sec // 60}:{sec % 60:02d}'


def _build_traffic_sheet(wb, traffic):
    """Лист «Динамика трафика»: широкая таблица по Яндекс.Метрике - день/
    месяц/год, каждый в двух строках (текущий/прошлый). Каналы, лиды,
    конверсия, поведение, разбивка по типам страниц. Только если выполнялось."""
    if not traffic:
        return
    # Формат: группы по странам (новый) или один плоский список rows (старый).
    groups = traffic.get('groups')
    if not groups:
        rows = traffic.get('rows') or []
        if not rows:
            return
        groups = [{'country': 'Все домены',
                   'counters': traffic.get('counters', 0), 'rows': rows}]
    if not any(g.get('rows') for g in groups):
        return

    # Спад визитов где-либо (по любой стране/периоду) - для цвета вкладки.
    declined = False
    for g in groups:
        by_period = {}
        for r in g.get('rows') or []:
            by_period.setdefault(r['period'], {})[r['kind']] = r.get('visits', 0)
        if any(v.get('текущий', 0) < v.get('прошлый', 0)
               for v in by_period.values()):
            declined = True

    ws = wb.create_sheet('Динамика трафика')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.warn if declined else C.ok
    ws.column_dimensions['A'].width = 3
    for idx, (_name, w) in enumerate(_TRAFFIC_COLS):
        ws.column_dimensions[get_column_letter(2 + idx)].width = w
    last_col = 1 + len(_TRAFFIC_COLS)

    ws.merge_cells(start_row=2, start_column=2, end_row=2, end_column=8)
    c = ws.cell(row=2, column=2, value='Динамика трафика (Метрика)')
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=last_col)
    c = ws.cell(row=3, column=2, value=(
        f'Источник: Яндекс.Метрика, {traffic.get("counters", 0)} счётчиков '
        'проекта, РАЗБИВКА ПО СТРАНАМ/ДОМЕНАМ (каждый блок - свой набор '
        'счётчиков по TLD домена, счётчик учтён ровно в одной стране). '
        'День = сегодня / вчера, Месяц = с 1-го числа до сегодня / тот же '
        'отрезок прошлого месяца, Год = с 1 января / прошлый год до той же даты. '
        'Яндекс и Google - весь трафик источника (органика + реклама ПС). Лиды - '
        'основная цель-лид страны; конверсия = лиды / визиты. Разбивка по типам '
        'страниц - по URL приземления.'))
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 46

    hrow = 5
    for idx, (name, _w) in enumerate(_TRAFFIC_COLS):
        h = ws.cell(row=hrow, column=2 + idx, value=name)
        h.font = _font(size=9, bold=True, color=C.text)
        h.fill = _fill(C.surface)
        h.alignment = _align(wrap=True, vertical='center', horizontal='center')
        h.border = _border()
    ws.row_dimensions[hrow].height = 30

    _PAGES = ('main', 'category', 'service', 'product', 'filter', 'tag',
              'info', 'tech')

    def _fmt(iso):
        return '.'.join(reversed(iso.split('-')))

    def _srez(period, r):
        """Что писать в колонке «Срез»: день - дату, месяц - «Месяц ГГГГ»,
        год - год (вместо слов текущий/прошлый). Заодно заменяет старый
        заголовок блока с датами."""
        y, m, _d = r['d1'].split('-')
        if period == 'Месяц':
            return f'{_MONTHS_NOM[int(m)]} {y}'
        if period == 'Год':
            return y
        return _fmt(r['d1'])

    def _nums(r):
        p = r.get('pages') or {}
        return ([r.get('visits', 0), r.get('direct', 0), r.get('yandex', 0),
                 r.get('google', 0), r.get('leads', 0), r.get('conv', 0),
                 r.get('bounce', 0), r.get('depth', 0), r.get('duration', 0)]
                + [p.get(t, 0) for t in _PAGES])

    def _disp(r):
        n = _nums(r)
        n[8] = _fmt_duration(n[8])   # время: секунды → м:сс
        return n

    def _delta(cur, prev, invert=False):
        # invert=True для «отказов»: рост - плохо (красный), падение - хорошо.
        if not prev:
            return '—', C.text_muted
        pct = round((cur - prev) / prev * 100, 1)
        if pct == 0:
            return '0%', C.text_muted
        up_color = C.err if invert else C.ok
        down_color = C.ok if invert else C.err
        if pct > 0:
            return f'+{pct}%', up_color
        return f'{pct}%', down_color

    def _put(row, idx, value, **fkw):
        cell = ws.cell(row=row, column=2 + idx, value=value)
        cell.font = _font(size=9, **fkw)
        cell.alignment = _align(horizontal='center', vertical='center')
        cell.border = _border()
        return cell

    order = ['День', 'Месяц', 'Год']
    row = hrow + 1
    for g in groups:
        grows = g.get('rows') or []
        if not grows:
            continue
        # Полоса страны/домена (шире и заметнее блоков периода).
        ws.merge_cells(start_row=row, start_column=2, end_row=row,
                       end_column=last_col)
        gb = ws.cell(row=row, column=2,
                     value=f'{g.get("country", "")}   ·   '
                           f'{g.get("counters", 0)} счётчик(ов)')
        gb.font = _font(size=12, bold=True, color='FFFFFF')
        gb.fill = _fill(C.accent)
        gb.alignment = _align(indent=1, vertical='center')
        ws.row_dimensions[row].height = 22
        row += 1

        seen_periods = sorted({r['period'] for r in grows},
                              key=lambda p: order.index(p) if p in order else 9)
        for period in seen_periods:
            prs = [r for r in grows if r['period'] == period]
            cur = next((r for r in prs if r['kind'] == 'текущий'), None)
            prev = next((r for r in prs if r['kind'] == 'прошлый'), None)

            # Полоса-заголовок блока периода (День/Месяц/Год).
            ws.merge_cells(start_row=row, start_column=2, end_row=row,
                           end_column=last_col)
            band = ws.cell(row=row, column=2, value=period)
            band.font = _font(size=11, bold=True, color='FFFFFF')
            band.fill = _fill(C.text_soft)
            band.alignment = _align(indent=1, vertical='center')
            ws.row_dimensions[row].height = 20
            row += 1

            for r in (cur, prev):
                if not r:
                    continue
                is_cur = r['kind'] == 'текущий'
                base = C.text if is_cur else C.text_muted
                _put(row, 0, r.get('year'), color=base)
                _put(row, 1, _srez(period, r), color=base)   # дата/месяц/год
                for j, val in enumerate(_disp(r)):
                    _put(row, 2 + j, val, color=base, bold=(j == 0 and is_cur))
                ws.row_dimensions[row].height = 16
                row += 1

            # Строка динамики (%). Отказы (индекс 6) - рост плохой, наоборот.
            if cur and prev:
                _put(row, 0, '', color=C.text_muted)
                _put(row, 1, 'Δ, %', color=C.text, bold=True)
                cn, pn = _nums(cur), _nums(prev)
                for j in range(len(cn)):
                    txt, clr = _delta(cn[j], pn[j], invert=(j == 6))
                    _put(row, 2 + j, txt, color=clr, bold=True)
                ws.row_dimensions[row].height = 16
                row += 1
            row += 1   # разделитель между периодами
        row += 1       # доп. разделитель между странами


_RP_COLS = [
    ('№', 5), ('Город', 20), ('Страна', 12), ('Нас., тыс', 9),
    ('Рейтинг Я', 10), ('Отз. Я', 8), ('Рейтинг 2ГИС', 12), ('Отз. 2ГИС', 10),
    ('Рейтинг (мин)', 12), ('Докупить', 10), ('В цикл', 8),
]


def _build_review_priority_sheet(wb, rp):
    """Лист «Отзывы (докупка)»: приоритет докупки отзывов на филиалы
    (рейтинг Яндекс/2ГИС, население, сколько докупить, план на цикл).
    Добавляется, только если проверка выполнялась."""
    if not rp:
        return
    ws = wb.create_sheet('Отзывы (докупка)')
    ws.sheet_view.showGridLines = False
    ws.column_dimensions['A'].width = 3
    for idx, (_n, w) in enumerate(_RP_COLS):
        ws.column_dimensions[get_column_letter(2 + idx)].width = w
    last_col = 1 + len(_RP_COLS)

    ws.merge_cells(start_row=2, start_column=2, end_row=2, end_column=8)
    c = ws.cell(row=2, column=2, value='Отзывы: приоритет докупки')
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    if not rp.get('available'):
        ws.merge_cells(start_row=4, start_column=2, end_row=4, end_column=last_col)
        cc = ws.cell(row=4, column=2,
                     value='⚪ ' + (rp.get('note') or 'Проверка не выполнена.'))
        cc.font = _font(size=10, color=C.text_muted)
        cc.alignment = _align(indent=1, wrap=True)
        ws.sheet_properties.tabColor = C.warn
        return

    low = rp.get('low_rating_count', 0)
    ws.sheet_properties.tabColor = C.err if low else C.ok
    ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=last_col)
    c = ws.cell(row=3, column=2, value=(
        f'Филиалов: {rp.get("total_branches", 0)}  ·  с рейтингом < '
        f'{4.7}: {low}  ·  план на цикл: {rp.get("cycle_count", 0)} филиалов / '
        f'{rp.get("cycle_reviews", 0)} отзывов (цель {rp.get("target_min")}–'
        f'{rp.get("target_max")}). Докупаем по 2 отзыва (3 если низкий рейтинг/'
        f'негатив). Приоритет: рейтинг < 4.7, затем города от миллионников к '
        f'меньшим. Рейтинг филиала = худший из Яндекс/2ГИС.'))
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 46

    hrow = 5
    for idx, (name, _w) in enumerate(_RP_COLS):
        h = ws.cell(row=hrow, column=2 + idx, value=name)
        h.font = _font(size=9, bold=True, color=C.text)
        h.fill = _fill(C.surface)
        h.alignment = _align(wrap=True, horizontal='center', vertical='center')
        h.border = _border()
    ws.row_dimensions[hrow].height = 28

    def _r(v):
        return '' if v is None else v

    row = hrow + 1
    for i, b in enumerate(rp.get('branches') or [], 1):
        y = b.get('yandex') or {}
        g = b.get('twogis') or {}
        low_b = b.get('low_rating')
        vals = [i, b.get('city'), b.get('country'), b.get('population') or '',
                _r(y.get('rating')), _r(y.get('count')),
                _r(g.get('rating')), _r(g.get('count')),
                _r(b.get('rating')), b.get('order'),
                '✓' if b.get('in_cycle') else '']
        for idx, val in enumerate(vals):
            cell = ws.cell(row=row, column=2 + idx, value=val)
            clr = C.err if (idx == 8 and low_b) else C.text
            cell.font = _font(size=9, color=clr,
                              bold=(idx in (8, 10) and b.get('in_cycle')))
            cell.alignment = _align(horizontal='center', vertical='center')
            cell.border = _border()
            if b.get('in_cycle'):
                cell.fill = _fill(C.surface)
        ws.row_dimensions[row].height = 15
        row += 1


def _render_query_anomalies(ws, start_row, anomalies):
    """Часть B секции «Аномалии»: ГСК - всплеск показов по мусорным/иноязычным
    запросам; Метрика - переходы со спам-сайтов (спам-домены-рефереры +
    всплеск). Пишет с start_row в переданный лист."""
    _a = anomalies or {}
    gsc = _a.get('gsc') or {}
    mtr = _a.get('metrika') or {}
    if not gsc and not mtr:
        return start_row

    row = [start_row]
    ws.merge_cells(start_row=row[0], start_column=2, end_row=row[0], end_column=6)
    _bh = ws.cell(row=row[0], column=2,
                  value='B. Запросы (ГСК) и переходы (Метрика)')
    _bh.font = _font(size=12, bold=True, color='FFFFFF')
    _bh.fill = _fill(C.text_soft)
    _bh.alignment = _align(indent=1)
    ws.row_dimensions[row[0]].height = 20
    row[0] += 1

    def _hdr(text, color=C.text):
        ws.merge_cells(start_row=row[0], start_column=2, end_row=row[0],
                       end_column=4)
        h = ws.cell(row=row[0], column=2, value=text)
        h.font = _font(size=12, bold=True, color='FFFFFF')
        h.fill = _fill(color)
        h.alignment = _align(indent=1)
        ws.row_dimensions[row[0]].height = 20
        row[0] += 1

    def _line(label, value, color=C.text_soft):
        ws.cell(row=row[0], column=2, value=label).font = _font(
            size=10, color=C.text_soft)
        cc = ws.cell(row=row[0], column=3, value=value)
        cc.font = _font(size=10, color=color)
        row[0] += 1

    # ── ГСК ──
    _hdr('Google Search Console - аномалии запросов', C.text_soft)
    if not gsc.get('available'):
        _line('Статус', gsc.get('note', 'не выполнялось'), C.text_muted)
    else:
        bad = gsc.get('spiked') or gsc.get('spam_queries_count')
        _line('Вердикт', 'ЕСТЬ аномалии' if bad else 'аномалий нет',
              C.err if bad else C.ok)
        _line('Период', f'{gsc.get("cur_period")} vs {gsc.get("prev_period")}')
        _line('Показы (тек / пред)',
              f'{gsc.get("total_impr_cur")} / {gsc.get("total_impr_prev")}'
              + (f'  ·  ×{gsc.get("impr_spike")}' if gsc.get('impr_spike') else ''),
              C.err if gsc.get('spiked') else C.text)
        _line('Мусорные/иноязыч. запросы',
              f'{gsc.get("spam_queries_count", 0)} (показов '
              f'{gsc.get("spam_impr_cur", 0)}, было {gsc.get("spam_impr_prev", 0)})',
              C.err if gsc.get('spam_queries_count') else C.text)
        for q in (gsc.get('spam_queries') or [])[:12]:
            ws.cell(row=row[0], column=2, value='  ' + (q.get('query') or '')
                    ).font = _font(size=9, color=C.err)
            ws.cell(row=row[0], column=3, value=f'{q.get("impressions")} показов'
                    ).font = _font(size=9, color=C.text_soft)
            row[0] += 1
        # Доноры GSC - API не отдаёт, ручная сверка.
        lc = ws.cell(row=row[0], column=2,
                     value='Мусорные доноры (раздел Links) - API не отдаёт, '
                           'проверить вручную →')
        lc.font = _font(size=9, italic=True, color=C.text_muted)
        link = ws.cell(row=row[0], column=3, value='GSC → Ссылки')
        link.font = _font(size=9, color=C.accent, underline='single')
        link.hyperlink = gsc.get('gsc_links_url',
                                 'https://search.google.com/search-console/links')
        row[0] += 1
    row[0] += 1

    # ── Метрика ──
    _hdr('Метрика - переходы с мусорных сайтов', C.text_soft)
    if not mtr.get('available'):
        _line('Статус', mtr.get('note', 'не выполнялось'), C.text_muted)
    else:
        bad = mtr.get('spiked') or mtr.get('spam_domains_count')
        _line('Вердикт', 'ЕСТЬ аномалии' if bad else 'аномалий нет',
              C.err if bad else C.ok)
        _line('Период', f'{mtr.get("cur_period")} vs {mtr.get("prev_period")}')
        _line('Переходы-рефералы (тек / пред)',
              f'{mtr.get("total_cur")} / {mtr.get("total_prev")}'
              + (f'  ·  ×{mtr.get("referral_spike")}'
                 if mtr.get('referral_spike') else ''),
              C.err if mtr.get('spiked') else C.text)
        _line('Спам-домены-рефереры',
              f'{mtr.get("spam_domains_count", 0)} (переходов '
              f'{mtr.get("spam_cur", 0)}, было {mtr.get("spam_prev", 0)})',
              C.err if mtr.get('spam_domains_count') else C.text)
        for d in (mtr.get('spam_domains') or [])[:15]:
            ws.cell(row=row[0], column=2, value='  ' + (d.get('domain') or '')
                    ).font = _font(size=9, color=C.err)
            ws.cell(row=row[0], column=3, value=f'{d.get("visits")} переходов'
                    ).font = _font(size=9, color=C.text_soft)
            row[0] += 1
    return row[0]


def _build_trust_sheet(wb, trust):
    """Лист «Траст проекта»: ИКС (Яндекс) + DR (Open PageRank) по хостам.
    Платные CheckTrust/Ahrefs/Semrush не подключены. Только если выполнялось."""
    if not trust:
        return
    ws = wb.create_sheet('Траст проекта')
    ws.sheet_view.showGridLines = False
    for col, w in (('A', 3), ('B', 36), ('C', 14), ('D', 20), ('E', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:D2')
    c = ws['B2']
    c.value = 'Траст проекта'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    if not trust.get('available'):
        ws.sheet_properties.tabColor = C.warn
        cc = ws.cell(row=4, column=2,
                     value='⚪ ' + (trust.get('note') or 'не выполнялось'))
        cc.font = _font(size=10, color=C.text_muted)
        cc.alignment = _align(indent=1, wrap=True)
        return

    hosts = trust.get('hosts') or []
    low = any((h.get('sqi') or 0) < 10 for h in hosts)
    ws.sheet_properties.tabColor = C.warn if low else C.ok

    ws.merge_cells('B3:D3')
    c = ws.cell(row=3, column=2, value=(
        'ИКС - индекс качества сайта (Яндекс, бесплатно). DR - Domain Rating-'
        'подобный ранг 0-100 (Open PageRank, бесплатно). '
        + (trust.get('note_paid') or '')))
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 42

    row = 5
    for col, txt in ((2, 'Хост'), (3, 'ИКС (Яндекс)'), (4, 'DR (Open PageRank)')):
        h = ws.cell(row=row, column=col, value=txt)
        h.font = _font(size=10, bold=True, color=C.text)
        h.fill = _fill(C.surface)
        h.alignment = _align(indent=1)
        h.border = _border()
    ws.row_dimensions[row].height = 18
    row += 1

    for hh in hosts:
        sqi = hh.get('sqi')
        dr = hh.get('dr')
        ws.cell(row=row, column=2, value=hh.get('host') or '').font = _font(
            size=10, color=C.text)
        sc = ws.cell(row=row, column=3,
                     value='—' if sqi is None else sqi)
        sc.font = _font(size=10, bold=True,
                        color=C.err if (sqi is not None and sqi < 10) else C.text)
        ws.cell(row=row, column=4,
                value=('—' if dr is None
                       else (int(dr) if float(dr).is_integer() else round(dr, 1)))
                ).font = _font(size=10, color=C.text)
        for col in (2, 3, 4):
            ws.cell(row=row, column=col).alignment = _align(indent=1)
            ws.cell(row=row, column=col).border = _border()
        ws.row_dimensions[row].height = 15
        row += 1


def _build_admin_settings_sheet(wb, admin_settings):
    """Лист «Настройки в админке»: работают ли функции настройки поддоменов/
    категорий/товаров/тех.страниц (браузерная проверка + round-trip
    сохранения). Добавляется, только если проверка выполнялась."""
    if not admin_settings:
        return
    checks = admin_settings.get('checks') or []
    verdict = admin_settings.get('verdict') or 'ok'

    ws = wb.create_sheet('Настройки в админке')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if verdict == 'fail'
                                    else C.warn if verdict == 'warn' else C.ok)
    for col, w in (('A', 3), ('B', 22), ('C', 78), ('D', 40), ('E', 3)):
        ws.column_dimensions[col].width = w

    ws.merge_cells('B2:D2')
    c = ws['B2']
    c.value = 'Настройки в админке'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:D3')
    c = ws['B3']
    c.value = ('Браузер заходит в админку Bitrix и проверяет, что функции '
               'настройки работают. Поддомены: создание (симуляция-dry-run), '
               'массовая загрузка, правка, удаление, скрытие. Категории: '
               'полный CRUD на временном скрытом разделе «[ТЕСТ ЧЕКЕРА]» '
               '(создание → правка → скрытие → удаление, удаляется в конце) + '
               'массовая загрузка. Товары (опционально): CRUD + сортировка + '
               'вывод в разные категории на временном скрытом товаре. '
               'Тех.страницы - редактор файлов. Ниже - аудит каждой операции '
               '«было → стало». Боевые данные не меняются: тест-раздел и '
               'тест-товар удаляются, поддомены реально не создаются.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 62

    row = 5
    if not admin_settings.get('available'):
        ws.merge_cells(f'B{row}:D{row}')
        c = ws[f'B{row}']
        c.value = f'⚪ {admin_settings.get("note", "Проверка не выполнена.")}'
        c.font = _font(size=10, color=C.text_muted)
        c.alignment = _align(indent=1, wrap=True)
        return

    ws.merge_cells(f'B{row}:D{row}')
    c = ws[f'B{row}']
    _dom = admin_settings.get('domain') or ''
    c.value = f'Админка: {_dom}'
    c.font = _font(size=10, bold=True, color=C.text)
    c.alignment = _align(indent=1)
    row += 2

    # Пояснение режимов операций (появляется, если есть хоть один аудит).
    _has_ops = any(ch.get('operations') for ch in checks)
    if _has_ops:
        ws.merge_cells(f'B{row}:D{row}')
        c = ws[f'B{row}']
        c.value = ('Режимы операций: «выполнено» - реально сделано и '
                   'откатано (запись в БД проверена); «симуляция» - dry-run '
                   'мастера, на сайте ничего не создаётся; «функция» - '
                   'проверено только наличие (реально не трогаем - боевые '
                   'данные).')
        c.font = _font(size=9, italic=True, color=C.text_muted)
        c.alignment = _align(wrap=True, indent=1)
        ws.row_dimensions[row].height = 30
        row += 2

    _MODE_LABEL = {'executed': 'выполнено', 'simulated': 'симуляция',
                   'ui': 'функция'}
    _RES = {'ok': ('✓', C.ok), 'fail': ('✗', C.err),
            'skip': ('—', C.text_muted)}

    for ch in checks:
        ws.row_dimensions[row].height = 18
        b = ws[f'B{row}']
        b.value = ('✅ ' if ch.get('ok') else '❌ ') + (ch.get('title') or '')
        b.font = _font(size=11, bold=True,
                       color=C.ok if ch.get('ok') else C.err)
        b.alignment = _align(indent=1, vertical='top')
        ws.merge_cells(f'C{row}:D{row}')
        d = ws[f'C{row}']
        d.value = ch.get('detail') or ''
        d.font = _font(size=10,
                       color=C.text_soft if ch.get('ok') else C.err)
        d.alignment = _align(wrap=True, vertical='top')
        row += 1
        for w in ch.get('warnings') or []:
            ws.merge_cells(f'C{row}:D{row}')
            wc = ws[f'C{row}']
            wc.value = f'⚠ {w}'
            wc.font = _font(size=9, color=C.warn)
            wc.alignment = _align(wrap=True, indent=1)
            ws.row_dimensions[row].height = 16
            row += 1

        # Таблица операций (аудит было→стало) - если есть.
        ops = ch.get('operations') or []
        if ops:
            # Шапка мини-таблицы
            for col, title in (('B', 'Операция'), ('C', 'Что менялось (было → стало)'),
                               ('D', 'Режим')):
                hc = ws[f'{col}{row}']
                hc.value = title
                hc.font = _font(size=9, bold=True, color=C.text_muted)
                hc.fill = _fill(C.surface)
                hc.alignment = _align(indent=1)
                hc.border = _border(color=C.border_light)
            ws.row_dimensions[row].height = 16
            row += 1
            for o in ops:
                mark, mcolor = _RES.get(o.get('result'), ('•', C.text_muted))
                oc = ws[f'B{row}']
                oc.value = f'{mark} {o.get("label", "")}'
                oc.font = _font(size=10, color=mcolor)
                oc.alignment = _align(indent=1, vertical='top', wrap=True)
                oc.border = _border(color=C.border_light)
                # было → стало (+ примечание)
                before, after = o.get('before', ''), o.get('after', '')
                if before and after:
                    txt = f'{before}  →  {after}'
                else:
                    txt = after or before or '-'
                if o.get('note'):
                    txt += f'\n({o["note"]})'
                cc = ws[f'C{row}']
                cc.value = txt
                cc.font = _font(size=9, color=C.text_soft)
                cc.alignment = _align(wrap=True, vertical='top', indent=1)
                cc.border = _border(color=C.border_light)
                mc = ws[f'D{row}']
                mc.value = _MODE_LABEL.get(o.get('mode'), o.get('mode', ''))
                mc.font = _font(size=9, color=C.text_muted)
                mc.alignment = _align(vertical='top', horizontal='center')
                mc.border = _border(color=C.border_light)
                ws.row_dimensions[row].height = 30 if o.get('note') else 18
                row += 1
        row += 1


# ── Лист «Ошибки JavaScript» (п.1.14: консоль браузера) ────────────


def _build_console_sheet(wb, console_check):
    """Лист ошибок JS в консоли: браузер открывал страницы прогона и слушал
    console.error / необработанные исключения. Добавляется, только если
    проверка выполнялась (console_check передан)."""
    if not console_check:
        return
    pages = console_check.get('pages') or []
    bad = [p for p in pages if p.get('errors')]

    # Адаптивность: замеры на сетке ширин 1440/768/390 той же поездкой
    # браузера (масштаб Ctrl+/- покрыт той же сеткой - тот же рендер).
    def _mob_issues(p):
        mob = p.get('mobile') or {}
        vps = mob.get('viewports') or ({'390': mob} if mob else {})
        out = []
        for w, m in sorted(vps.items(), key=lambda kv: -int(kv[0])):
            if m.get('overflow', 0) > 8:
                _w = ', '.join(m.get('wide') or [])
                out.append(f'на {w}px: контент шире экрана на '
                           f'{m["overflow"]}px - горизонтальный скролл/обрезка'
                           + (f' ({_w})' if _w else ''))
            if m.get('overlaps'):
                out.append(f'на {w}px: блоки накладываются: '
                           + '; '.join(m['overlaps'][:3]))
        m390 = vps.get('390') or mob
        if m390.get('small', 0) >= 3 \
                and m390['small'] > (m390.get('total') or 1) * 0.2:
            _ex = '; '.join(m390.get('small_examples') or [])
            out.append(f'мелкий шрифт меньше 14px (мобильный): '
                       f'{m390["small"]} из {m390["total"]} текстовых '
                       f'элементов' + (f' (напр. {_ex})' if _ex else ''))
        if mob.get('menu_close') == 'not_closed':
            out.append('меню (мобильное) НЕ закрывается по клику вне области '
                       '- проверить вручную')
        if mob.get('menu_close_d') == 'not_closed':
            out.append('меню/каталог (ПК) НЕ закрывается по клику вне '
                       'области - проверить вручную')
        a = p.get('a11y') or {}
        if a.get('img_broken'):
            out.append('битые картинки (не загрузились в браузере): '
                       + ', '.join(a['img_broken'][:4]))
        if a.get('img_distorted'):
            out.append('картинки с искажёнными пропорциями (сплющены/'
                       'растянуты вёрсткой): ' + ', '.join(a['img_distorted'][:4]))
        return out
    mob_bad = [(p, _mob_issues(p)) for p in pages if _mob_issues(p)]
    mob_checked = sum(1 for p in pages if p.get('mobile'))

    ws = wb.create_sheet('Ошибки JavaScript')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if bad
                                    else C.warn if mob_bad else C.ok)

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 66   # URL
    ws.column_dimensions['C'].width = 80   # Ошибки
    ws.column_dimensions['D'].width = 3

    ws.merge_cells('B2:C2')
    c = ws['B2']
    c.value = 'Ошибки JavaScript в консоли (п.1.14)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:C3')
    c = ws['B3']
    c.value = ('Браузер (Playwright) открывал страницы, по которым прошёл '
               'чек-лист (главная, каталог, категории, фильтры, товары, тех.), '
               'и слушал консоль: console.error и необработанные исключения '
               'JavaScript. Шум сторонних сервисов (Метрика, виджеты, чаты, '
               'reCAPTCHA, блокировщики) отсеивается - показываем ошибки '
               'самого сайта. Страница с ошибками = баг. Той же поездкой '
               'замеряется адаптивность на сетке ширин 1440/768/390: нет '
               'горизонтального скролла ни на одном разрешении, блоки не '
               'накладываются при изменении ширины окна (масштаб Ctrl+/- '
               'покрыт той же сеткой - браузер рисует те же макеты), на '
               '390px шрифт читабелен (мин. 14px).')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 48

    row = 5
    if not console_check.get('available') or not pages:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        c = ws.cell(row=row, column=2)
        c.value = console_check.get('note') or (
            'Проверка консоли не выполнялась (нет страниц / браузер недоступен).')
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True, vertical='top')
        ws.row_dimensions[row].height = 40
        return

    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
    c = ws.cell(row=row, column=2)
    c.value = (f'Проверено страниц: {console_check.get("checked", len(pages))} · '
               f'с ошибками JS: {len(bad)}'
               + (f' · адаптивность: проблемы на {len(mob_bad)} из '
                  f'{mob_checked}' if mob_checked else ''))
    c.font = _font(size=10, bold=True, color=C.err if bad else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 2

    if not bad:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        c = ws.cell(row=row, column=2)
        c.value = '✅ Ошибок JavaScript в консоли ни на одной странице нет.'
        c.font = _font(size=11, color=C.ok)
        c.alignment = _align(indent=1)
        row += 2
    else:
        # Заголовки
        for ci, h in enumerate(['Страница', 'Ошибки в консоли'], 2):
            cell = ws.cell(row=row, column=ci)
            cell.value = h
            cell.font = _font(size=9, bold=True, color=C.text_muted)
            cell.fill = _fill(C.surface)
            cell.alignment = _align()
            cell.border = _border()
        ws.row_dimensions[row].height = 20
        row += 1
        for p in bad:
            errs = p.get('errors') or []
            ws.row_dimensions[row].height = max(20, 14 * min(len(errs), 6))
            # URL
            c = ws.cell(row=row, column=2)
            c.value = p.get('url', '')
            c.hyperlink = p.get('url', '')
            c.font = _font(size=9, color=C.accent, underline='single')
            c.alignment = _align(wrap=True, vertical='top')
            c.border = _border(color=C.border_light)
            # Ошибки
            c = ws.cell(row=row, column=3)
            c.value = '\n'.join(f'• {e}' for e in errs[:6]) + (
                f'\n… и ещё {len(errs) - 6}' if len(errs) > 6 else '')
            c.font = _font(size=9, color=C.err)
            c.alignment = _align(wrap=True, vertical='top')
            c.border = _border(color=C.border_light)
            row += 1
        row += 1

    # ── Мобильная вёрстка (viewport 390px) ──
    if mob_checked:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        c = ws.cell(row=row, column=2)
        c.value = f'Адаптивность (1440 / 768 / 390 px)  ({len(mob_bad)})'
        c.font = _font(size=13, bold=True, color=C.warn if mob_bad else C.ok)
        c.fill = _fill(C.accent_soft)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1
        if not mob_bad:
            ws.merge_cells(start_row=row, start_column=2, end_row=row,
                           end_column=3)
            c = ws.cell(row=row, column=2)
            c.value = ('✅ На всех замеренных ширинах (1440/768/390) нет '
                       'горизонтального скролла и наложений блоков; на '
                       'мобильном шрифт читабелен (≥14px).')
            c.font = _font(size=10, color=C.ok)
            c.alignment = _align(indent=1)
        else:
            for p, probs in mob_bad:
                ws.row_dimensions[row].height = max(20, 14 * len(probs))
                c = ws.cell(row=row, column=2)
                c.value = p.get('url', '')
                c.hyperlink = p.get('url', '')
                c.font = _font(size=9, color=C.accent, underline='single')
                c.alignment = _align(wrap=True, vertical='top')
                c.border = _border(color=C.border_light)
                c = ws.cell(row=row, column=3)
                c.value = '\n'.join(f'⚠ {t}' for t in probs)
                c.font = _font(size=9, color=C.warn)
                c.alignment = _align(wrap=True, vertical='top')
                c.border = _border(color=C.border_light)
                row += 1
        # Тач-таргеты (44x44): метрика шаблонная (одни и те же кнопки на
        # всех страницах) - одна СВОДНАЯ строка, не per-page список.
        _tt = [( (p.get('mobile') or {}).get('viewports', {}).get('390')
                 or p.get('mobile') or {}) for p in pages]
        _tt = [m for m in _tt if m.get('touch_total')]
        if _tt:
            _share = sum(m['touch_small'] for m in _tt) \
                / max(sum(m['touch_total'] for m in _tt), 1)
            ws.merge_cells(start_row=row, start_column=2, end_row=row,
                           end_column=3)
            c = ws.cell(row=row, column=2)
            if _share > 0.5:
                _ex = '; '.join((_tt[0].get('touch_examples') or [])[:3])
                c.value = (f'⚠ Тач-таргеты: {int(_share * 100)}% '
                           f'кнопок/иконок меньше 44x44px на мобильном - '
                           f'неудобно попадать пальцем'
                           + (f' (напр. {_ex})' if _ex else '') + '.')
                c.font = _font(size=10, color=C.warn)
            else:
                c.value = (f'✅ Тач-таргеты: большинство кнопок/иконок '
                           f'({100 - int(_share * 100)}%) не меньше 44x44px.')
                c.font = _font(size=10, color=C.ok)
            c.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 22
            row += 1
        # Контрастность (WCAG): метрика шаблонная - одна сводная строка.
        _ct = [p.get('a11y') or {} for p in pages]
        _ct = [a for a in _ct if a.get('contrast_total')]
        if _ct:
            _low = sum(a['contrast_low'] for a in _ct)
            _tot = sum(a['contrast_total'] for a in _ct)
            _cshare = _low / max(_tot, 1)
            ws.merge_cells(start_row=row, start_column=2, end_row=row,
                           end_column=3)
            c = ws.cell(row=row, column=2)
            if _cshare > 0.15:
                _ex = '; '.join((_ct[0].get('contrast_ex') or [])[:3])
                c.value = (f'⚠ Контрастность (WCAG): {int(_cshare * 100)}% '
                           f'текста ниже нормы (4.5:1, крупный 3:1) - плохо '
                           f'читается' + (f' (напр. {_ex})' if _ex else '')
                           + '.')
                c.font = _font(size=10, color=C.warn)
            else:
                c.value = (f'✅ Контрастность (WCAG): '
                           f'{100 - int(_cshare * 100)}% текста читается '
                           f'нормально (порог 4.5:1).')
                c.font = _font(size=10, color=C.ok)
            c.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 22
            row += 1
        row += 1

    # ── Интерактив (слайдеры / выпадающие меню, первые страницы) ──
    _ux_pages = [p for p in pages if p.get('ux')]
    if _ux_pages:
        _sl_fail = [p for p in _ux_pages
                    if (p['ux'] or {}).get('slider') == 'fail']
        _dd_fail = [p for p in _ux_pages
                    if (p['ux'] or {}).get('dropdown') == 'fail']
        _sl_ok = any((p['ux'] or {}).get('slider') == 'ok' for p in _ux_pages)
        _dd_ok = any((p['ux'] or {}).get('dropdown') == 'ok'
                     for p in _ux_pages)
        _ux_bad = bool(_sl_fail or _dd_fail)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        c = ws.cell(row=row, column=2)
        c.value = 'Интерактив (браузер, первые страницы)'
        c.font = _font(size=13, bold=True, color=C.warn if _ux_bad else C.ok)
        c.fill = _fill(C.accent_soft)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1

        def _ux_line(text, color):
            nonlocal row
            ws.merge_cells(start_row=row, start_column=2,
                           end_row=row, end_column=3)
            c = ws.cell(row=row, column=2)
            c.value = text
            c.font = _font(size=10, color=color)
            c.alignment = _align(indent=1, wrap=True)
            ws.row_dimensions[row].height = 18
            row += 1

        if _sl_fail:
            _ux_line('⚠ Слайдер не отреагировал на стрелку «вперёд» - '
                     'проверить вручную: '
                     + ', '.join(p['url'] for p in _sl_fail[:3]), C.warn)
        elif _sl_ok:
            _ux_line('✅ Слайдер листается по стрелке.', C.ok)
        else:
            _ux_line('· Слайдер на проверенных страницах не распознан - '
                     'пропуск.', C.text_muted)
        if _dd_fail:
            _ux_line('⚠ Выпадающее меню не открылось по наведению - '
                     'проверить вручную: '
                     + ', '.join(p['url'] for p in _dd_fail[:3]), C.warn)
        elif _dd_ok:
            _ux_line('✅ Выпадающее меню открывается по наведению.', C.ok)
        else:
            _ux_line('· Выпадающих подменю в шапке не распознано - пропуск.',
                     C.text_muted)
        # Cookie-popup запоминает выбор минимум неделю.
        _ck = [(p, (p.get('ux') or {}).get('cookie')) for p in pages]
        _ck = [(p, v) for p, v in _ck if v]
        _ck_bad = [(p, v) for p, v in _ck
                   if v.get('status') in ('short', 'not_remembered')]
        _ck_ok = [(p, v) for p, v in _ck if v.get('status') == 'ok']
        if _ck_bad:
            p0, v0 = _ck_bad[0]
            if v0.get('status') == 'short':
                _ux_line(f'⚠ Cookie-баннер запоминает выбор лишь на '
                         f'{v0.get("days")} дн. (нужно ≥7) - '
                         f'{p0["url"]}', C.warn)
            else:
                _ux_line(f'⚠ Cookie-баннер НЕ запоминает выбор (появляется '
                         f'снова после перезагрузки) - {p0["url"]}', C.warn)
        elif _ck_ok:
            v0 = _ck_ok[0][1]
            _d = (f' (срок {v0["days"]} дн.)' if v0.get('days') else '')
            _ux_line(f'✅ Cookie-баннер запоминает выбор минимум неделю{_d}.',
                     C.ok)
        elif not _ck:
            _ux_line('· Cookie-баннер с кнопкой согласия не распознан - '
                     'пропуск.', C.text_muted)
        # Модальная форма: закрывается по клику вне (пункт «меню и формы»).
        # Проверяется на ПК (1440) и на мобильном (390); на странице форм
        # несколько - показываем НАЗВАНИЕ формы + URL.
        def _fc_norm(v):
            if isinstance(v, dict):
                return v.get('status'), v.get('name') or 'модальная форма'
            return v, 'модальная форма'
        _fc = []
        for p in pages:
            mob = p.get('mobile') or {}
            for key, dev in (('form_close', 'ПК'), ('form_close_m', 'моб.')):
                s, n = _fc_norm(mob.get(key))
                if s:
                    _fc.append((p, s, n, dev))
        _fc_fail = [(p, n, d) for p, s, n, d in _fc if s == 'not_closed']
        _fc_ok = [(p, n, d) for p, s, n, d in _fc if s == 'ok']
        if _fc_fail:
            _ux_line('⚠ Модальная форма НЕ закрывается по клику вне неё - '
                     'проверить вручную: '
                     + '; '.join(f'«{n}» [{d}] ({p["url"]})'
                                 for p, n, d in _fc_fail[:3]), C.warn)
        if _fc_ok:
            _devs = ', '.join(sorted({d for _, _, d in _fc_ok}))
            _n0 = _fc_ok[0][1]
            _ux_line(f'✅ Модальная форма «{_n0}» закрывается по клику '
                     f'вне неё ({_devs}).', C.ok)
        if not _fc_fail and not _fc_ok:
            _ux_line('· Модальная форма не распознана (кнопка звонка/заявки '
                     'не найдена или окно на весь экран) - пропуск.',
                     C.text_muted)


# ── Лист «Метаданные» (п.1.8: title/description/H1, дубли, URL) ─────

_META_FIELD_LABEL = {'title': 'title', 'description': 'description', 'h1': 'H1'}


def _meta_table_header(ws, row, headers):
    """Строка заголовков таблицы в стиле остальных листов."""
    for ci, h in enumerate(headers, 2):
        cell = ws.cell(row=row, column=ci)
        cell.value = h
        cell.font = _font(size=9, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align()
        cell.border = _border()
    ws.row_dimensions[row].height = 20


def _meta_section_title(ws, row, text, color):
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = text
    c.font = _font(size=13, bold=True, color=color)
    c.fill = _fill(C.accent_soft)
    c.alignment = _align(indent=1)
    ws.row_dimensions[row].height = 24


def _meta_ok_line(ws, row, text):
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = text
    c.font = _font(size=10, color=C.ok)
    c.alignment = _align(indent=1)
    ws.row_dimensions[row].height = 22


def _build_meta_sheet(wb, results, meta_summary):
    """Лист метаданных: проблемы title/description/H1 на страницах +
    дубли внутри города / между городами + дубли УРЛОВ.
    Добавляется только если проверка метаданных выполнялась."""
    checked = [r for r in results if getattr(r, 'meta', None)]
    # SEO-тексты категорий (галочка 1.6) живут на этом же листе - лист
    # нужен и когда метаданные (1.8) выключены, а 1.6 включена.
    _seo_pages = [r for r in results
                  if getattr(r, 'seo_text', None) is not None]
    if not checked and not meta_summary and not _seo_pages:
        return

    bad = [r for r in checked if r.meta.get('issues')]
    warned = [r for r in checked if (not r.meta.get('issues')
                                     and r.meta.get('warnings'))]
    dups = (meta_summary or {}).get('duplicates') or {}
    same_city = dups.get('same_city') or []
    cross_city = dups.get('cross_city') or []
    url_dups_all = (meta_summary or {}).get('url_duplicates') or []
    url_dups = [d for d in url_dups_all if d.get('problem') != 'not_301']
    url_not301 = [d for d in url_dups_all if d.get('problem') == 'not_301']
    test_domains = (meta_summary or {}).get('test_domains') or []
    td_open = [t for t in test_domains if t.get('state') == 'indexable']
    has_bugs = bool(bad or same_city or cross_city or url_dups or td_open)
    _seo_warned = any((r.seo_text or {}).get('warnings') for r in _seo_pages)

    ws = wb.create_sheet('Метаданные')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = (C.err if has_bugs
                                    else C.warn if _seo_warned else C.ok)

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 18   # Город
    ws.column_dimensions['C'].width = 14   # Тип
    ws.column_dimensions['D'].width = 62   # URL
    ws.column_dimensions['E'].width = 60   # Проблема / значение
    ws.column_dimensions['F'].width = 3

    ws.merge_cells('B2:E2')
    c = ws['B2']
    c.value = 'Метаданные и дубли (п.1.8)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:E3')
    c = ws['B3']
    c.value = ('Каждая страница выборки: title, meta description и H1 есть и не '
               'пустые, город поддомена присутствует в title/description, длины '
               'в рекомендуемых рамках (title 10–70 символов, description '
               '50–160). Выход за рамки - предупреждение (не баг). Дубли: '
               'одинаковые title/description/H1 '
               'у разных страниц одного города - баг; полное совпадение между '
               'городами - город не подставился в шаблон. Дубли УРЛОВ: варианты '
               'адреса (http, без слэша, www, index.php/index.html) должны '
               '301-редиректить - ответ 200 без редиректа = страница доступна '
               'по двум адресам; временный 302 вместо 301 = предупреждение.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 56

    row = 5

    # Мета-секции - только если метаданные (1.8) реально проверялись:
    # при включённой одной 1.6 лист живёт ради SEO-текстов, и пустые
    # «Проверено: 0» не рисуем.
    if checked or meta_summary:
        # ── Сводка ──
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = (f'Проверено страниц: {len(checked)} · с проблемами: {len(bad)} · '
                   f'предупреждений: {len(warned)} · дублей в городе: {len(same_city)} · '
                   f'межгородских: {len(cross_city)} · дублей URL: {len(url_dups)} · '
                   f'временных редиректов: {len(url_not301)}')
        c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
        c.fill = _fill(C.surface)
        c.alignment = _align(wrap=True)
        ws.row_dimensions[row].height = 30
        row += 2

    if checked:
        # ── Секция 1: проблемы на страницах (сгруппированы по проблеме) ──
        _meta_section_title(ws, row, f'Проблемы метаданных на страницах  ({len(bad)})',
                            C.err if bad else C.ok)
        row += 1
        if not bad:
            _meta_ok_line(ws, row, '✅ У всех проверенных страниц метаданные в порядке.')
            row += 2
        else:
            row = _render_issue_groups(
                ws, row, _issue_groups(bad, 'meta', 'issues'), C.err)

        # ── Секция 2: предупреждения (длины; сгруппированы по замечанию) ──
        if warned:
            _meta_section_title(ws, row, f'Предупреждения (длины)  ({len(warned)})', C.warn)
            row += 1

            def _meta_len(r):
                m = getattr(r, 'meta', None) or {}
                return (f'title: {m.get("title_len", 0)} симв. · '
                        f'description: {m.get("desc_len", 0)} симв.')

            row = _render_issue_groups(
                ws, row, _issue_groups(warned, 'meta', 'warnings'), C.warn,
                extra=_meta_len)

    # ── Секции 3-4: дубли метаданных (только если 1.8 выполнялась) ──
    for title_text, groups, note in ((
        (f'Дубли внутри города  ({len(same_city)})', same_city,
         'Одинаковое значение у разных страниц одного поддомена.'),
        (f'Межгородские дубли (город не подставился)  ({len(cross_city)})', cross_city,
         'Полное совпадение между разными городами - шаблон не подставил город.'),
    ) if meta_summary is not None else ()):
        _meta_section_title(ws, row, title_text, C.err if groups else C.ok)
        row += 1
        if not groups:
            _meta_ok_line(ws, row, '✅ Дублей не найдено.')
            row += 1
        else:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = note
            c.font = _font(size=9, italic=True, color=C.text_muted)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 18
            row += 1
            for g in groups:
                fld = _META_FIELD_LABEL.get(g.get('field'), g.get('field'))
                ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
                c = ws.cell(row=row, column=2)
                c.value = f'{fld}: «{g.get("value", "")}»'
                c.font = _font(size=10, bold=True, color=C.text)
                c.fill = _fill(C.surface)
                c.alignment = _align(wrap=True, indent=1)
                ws.row_dimensions[row].height = 22
                row += 1
                for p in g.get('pages', []):
                    ws.row_dimensions[row].height = 18
                    vals = [
                        (p.get('city') or '-', {'size': 9, 'color': C.text_muted}),
                        (p.get('type_label', ''), {'size': 9, 'color': C.text_muted}),
                        (p.get('url', ''), {'size': 9, 'color': C.accent,
                                            'underline': 'single'}),
                        ('', {}),
                    ]
                    for ci, (val, kw) in enumerate(vals, 2):
                        cell = ws.cell(row=row, column=ci)
                        cell.value = val
                        if kw:
                            cell.font = _font(**kw)
                        cell.alignment = _align(vertical='top')
                        cell.border = _border(color=C.border_light)
                        if ci == 4 and val:
                            cell.hyperlink = val
                    row += 1
        row += 1

    # ── Секция 5: дубли УРЛОВ ──
    def _url_dup_table(items, color):
        nonlocal row
        _meta_table_header(ws, row, ['Вариант', 'Код',
                                     'Адрес варианта',
                                     'Канонический адрес'])
        row += 1
        for d in items:
            ws.row_dimensions[row].height = 20
            vals = [
                (d.get('kind', ''), {'size': 9, 'color': C.text_muted}),
                (d.get('code', ''), {'size': 10, 'color': color}),
                (d.get('variant', ''), {'size': 10, 'color': color}),
                (d.get('canonical', ''), {'size': 10, 'color': C.accent,
                                          'underline': 'single'}),
            ]
            for ci, (val, kw) in enumerate(vals, 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = val
                cell.font = _font(**kw)
                cell.alignment = _align(wrap=True, vertical='top')
                cell.border = _border(color=C.border_light)
                if ci == 5 and val:
                    cell.hyperlink = val
            row += 1

    # ── Секция: SEO-тексты категорий (нейроответы / AI overviews) ──
    _st_pages = [r for r in results
                 if getattr(r, 'seo_text', None) is not None]
    if _st_pages:
        _st_warned = [r for r in _st_pages if r.seo_text.get('warnings')]
        _meta_section_title(
            ws, row,
            f'SEO-тексты категорий (нейроответы)  ({len(_st_warned)})',
            C.warn if _st_warned else C.ok)
        row += 1
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = ('Формальные признаки текста для нейроответов/AI overviews: '
                   'текст есть, содержит главный ключ (из H1), фото с alt, '
                   'таблица с caption+thead, структура (h2/h3, таблицы, '
                   'нумерованные списки). Смысловую полноту ответа и '
                   'LSI-слова машина не оценит - это семантическое ядро и '
                   'ручная вычитка.')
        c.font = _font(size=9, italic=True, color=C.text_muted)
        c.alignment = _align(indent=1, wrap=True)
        ws.row_dimensions[row].height = 30
        row += 1
        if not _st_warned:
            _meta_ok_line(ws, row, '✅ На проверенных категориях SEO-тексты '
                                   'с ключом, фото, таблицей и структурой.')
            row += 2
        else:
            row = _render_issue_groups(
                ws, row, _issue_groups(_st_warned, 'seo_text', 'warnings'),
                C.warn)

    if meta_summary is not None:
        _meta_section_title(ws, row, f'Дубли УРЛОВ (нет редиректа)  ({len(url_dups)})',
                            C.err if url_dups else C.ok)
        row += 1
        if not url_dups:
            _meta_ok_line(ws, row, '✅ Все варианты адресов (HTTP→HTTPS, '
                                   'слэш на конце, www/без www, index.php/'
                                   'index.html/home.php) отдают постоянный '
                                   '301-редирект на канонический вид.')
            row += 1
        else:
            _url_dup_table(url_dups, C.err)
        row += 1
        # Временные редиректы: склейка не передаётся, 301 обязателен
        if url_not301:
            _meta_section_title(
                ws, row,
                f'Временный редирект вместо 301  ({len(url_not301)})', C.warn)
            row += 1
            _url_dup_table(url_not301, C.warn)
            row += 1

        # ── Тестовые домены (test./dev./stage.…) ──
        _meta_section_title(
            ws, row, f'Тестовые домены  ({len(td_open)})',
            C.err if td_open else C.ok)
        row += 1
        if not test_domains:
            _meta_ok_line(ws, row, '✅ Типовые тестовые поддомены (test., dev., '
                                   'stage., beta., demo., old., new.) не '
                                   'существуют или редиректят на основной сайт.')
            row += 1
        else:
            for t in test_domains:
                ws.merge_cells(start_row=row, start_column=2,
                               end_row=row, end_column=5)
                c = ws.cell(row=row, column=2)
                if t.get('state') == 'indexable':
                    c.value = (f'❌ https://{t.get("host", "")}/ отвечает 200 и '
                               f'ОТКРЫТ для индексации - дубль всего сайта в '
                               f'индексе; закрыть (noindex / Disallow: /) или '
                               f'убрать')
                    c.font = _font(size=10, color=C.err)
                else:
                    c.value = (f'✓ https://{t.get("host", "")}/ существует, но '
                               f'закрыт от индексации (noindex/robots) - ок')
                    c.font = _font(size=10, color=C.text_muted)
                c.alignment = _align(indent=2, wrap=True)
                ws.row_dimensions[row].height = 18
                row += 1


# ── Лист «Контакты по городам» (сверка с КП) ───────────────────────


def _build_kp_sheet(wb, results):
    """
    Сверка контактов (телефон / почта / адрес) на главных страницах
    поддоменов с «Картой присутствия». По одному городу в строке -
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
    c.value = 'Контакты по городам - сверка с КП'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 24

    ws.merge_cells('B3:G3')
    c = ws['B3']
    c.value = ('Сверяем телефон, почту и адрес на главной каждого города (шапка + '
               'подвал) с «Картой присутствия». Телефон: ожидается SEO-номер (если '
               'нет - рекламный, затем общий). Зелёное «✓» - совпало с КП, красное - '
               'нет. «есть» (серое) - на сайте есть, но в КП этого поля нет (сверять '
               'не с чем, дополнить КП). «-» - нет ни в КП, ни на сайте. '
               'Что именно не так - в последнем столбце.')
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
                cell.value = '-'           # и в КП нет, и на сайте нет - нечего показать
                cell.font = _font(size=10, color=C.text_muted)
            elif iss['status'] == 'ok':
                cell.value = '✓'
                cell.font = _font(size=10, bold=True, color=C.ok)
                cell.fill = _fill(C.ok_soft)
            elif iss['status'] == 'info':
                # на сайте есть, но в КП нет - не сверка, но и не «нет». «есть».
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

        # Что не так - комментарии по проблемным полям
        problems = [f'{i["field"]}: {i["comment"]}'
                    for i in kp.get('issues', [])
                    if i['status'] in ('bug', 'critical') and i.get('comment')]
        wc = ws.cell(row=row, column=7, value='\n'.join(problems))
        wc.font = _font(size=9, color=C.err if problems else C.text_muted)
        wc.alignment = _align(wrap=True, vertical='top')
        wc.border = _border(color=C.border_light)
        ws.row_dimensions[row].height = max(22, 15 * (len(problems) or 1))
        row += 1


# ── Секция «Замена рекл. номера» (в группе «Аналитика», в конце) ────
# Два столбца проверки в одной таблице:
#   • «В конфиге» - СТАТИЧЕСКИ, каждый прогон: рекламный номер в коде
#     коллтрекинга (Sipuni) совпадает с phone_ad из КП;
#   • «Подмена (браузер)» - по галочке: реально ли номер подменяется при
#     рекламном визите (?utm_source=yandex), JS выполняется в браузере.

# статус статической сверки → (метка, цвет)
_CT_CFG = {'ok': ('✓ совпал с КП', 'ok'), 'bug': ('БАГ ≠ КП', 'err'),
           'na': ('нет подмены', 'text_muted')}
# статус браузерной проверки → (метка, цвет)
_CT_BROW = {'replaced_ok': ('✅ работает', 'ok'),
            'not_replaced': ('❌ не работает', 'err'),
            'no_element': ('⚠ номер не найден', 'warn'),
            'error': ('⚠ ошибка загрузки', 'warn')}


def _build_calltracking_sheet(wb, results, calltracking_check):
    """Секция «Замена рекл. номера» (в конце «Аналитики»). Сводит воедино:
    (1) статическую сверку рекламного номера в конфиге коллтрекинга с
    phone_ad из КП - идёт в каждом прогоне (из kp_result.ad_check);
    (2) браузерную проверку реальной подмены - по галочке (calltracking_check).
    Лист не создаётся, если нет ни того, ни другого."""
    from urllib.parse import urlparse as _up

    def _nhost(s):
        h = (s or '').strip().lower()
        if '//' in h:
            h = _up(h).netloc or h
        return h.split(':')[0].lstrip('.').replace('www.', '')

    # Статика (каждый прогон): главные с kp_result.ad_check.
    stat = {}
    for r in (results or []):
        kp = getattr(r, 'kp_result', None)
        if kp and kp.get('ad_check'):
            stat[_nhost(r.subdomain or kp.get('domain'))] = {
                'city': kp.get('city') or getattr(r, 'city', ''),
                'url': getattr(r, 'url', ''), 'ad': kp['ad_check']}
    # Браузер (по галочке).
    brow = {_nhost(b.get('url')): b
            for b in ((calltracking_check or {}).get('results') or [])}
    if not stat and not brow:
        return

    hosts = list(dict.fromkeys(list(stat) + list(brow)))
    cfg_bad = sum(1 for h in hosts
                  if (stat.get(h, {}).get('ad') or {}).get('status') == 'bug')
    brow_bad = sum(1 for h in hosts
                   if (brow.get(h) or {}).get('status') == 'not_replaced')
    brow_used = bool(brow)

    ws = wb.create_sheet('Замена рекл. номера')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if (cfg_bad or brow_bad) else C.ok
    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 22   # Город
    ws.column_dimensions['C'].width = 9    # Открыть
    ws.column_dimensions['D'].width = 16   # Рекл. номер (КП)
    ws.column_dimensions['E'].width = 20   # В конфиге сайта
    ws.column_dimensions['F'].width = 24   # Подмена (браузер)
    ws.column_dimensions['G'].width = 30   # Показал сайт (браузер)

    ws.merge_cells('B2:G2')
    c = ws['B2']
    c.value = 'Замена рекламного номера (коллтрекинг)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 24

    ws.merge_cells('B3:G3')
    c = ws['B3']
    c.value = ('Реклама подменяет номер в шапке на отдельный (для отслеживания '
               'звонков с рекламы). «В конфиге» - СТАТИЧЕСКИ, в каждом прогоне: '
               'рекламный номер в коде коллтрекинга (Sipuni) совпадает с '
               'phone_ad из КП. «Подмена (браузер)» - по галочке: открываем '
               'главную с меткой ?utm_source=yandex и проверяем, реально ли '
               'номер подменяется (JS выполняется). ✅/✓ - ок, ❌/БАГ - '
               'проблема, «нет подмены» - коллтрекинг не найден.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 44

    # Плитки сводки
    tiles = [('Проверено городов', len(hosts), C.accent, C.accent_soft),
             ('В конфиге ≠ КП', cfg_bad, C.err if cfg_bad else C.ok,
              C.err_soft if cfg_bad else C.ok_soft)]
    if brow_used:
        tiles.append(('Подмена не работает', brow_bad,
                      C.err if brow_bad else C.ok,
                      C.err_soft if brow_bad else C.ok_soft))
    col = 2
    for label, value, color, bg in tiles:
        vc = ws.cell(row=5, column=col, value=value)
        vc.font = _font(size=22, bold=True, color=color)
        vc.fill = _fill(bg); vc.alignment = _align(horizontal='center')
        vc.border = _border(color=C.border_light)
        lc = ws.cell(row=6, column=col, value=label)
        lc.font = _font(size=9, color=C.text_muted)
        lc.fill = _fill(bg); lc.alignment = _align(horizontal='center')
        lc.border = _border(color=C.border_light)
        col += 1
    ws.row_dimensions[5].height = 30

    def _fmt_num(n):
        n = re.sub(r'\D', '', str(n or ''))
        if len(n) == 10:
            return f'{n[:3]}-{n[3:6]}-{n[6:8]}-{n[8:]}'
        return n or '—'

    # Сортировка: сначала проблемные (браузер не работает / конфиг ≠ КП).
    def _rank(h):
        b = (brow.get(h) or {}).get('status')
        cfgs = (stat.get(h, {}).get('ad') or {}).get('status')
        return (0 if b == 'not_replaced' else 1 if cfgs == 'bug' else 2,
                (stat.get(h, {}).get('city') or brow.get(h, {}).get('city') or h))
    hosts.sort(key=_rank)

    hdr_row = 8
    hdrs = ['Город', 'Открыть', 'Рекл. номер (КП)', 'В конфиге сайта',
            'Подмена (браузер)', 'Показал сайт']
    for ci, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=hdr_row, column=ci, value=h)
        cell.font = _font(size=10, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align(horizontal='center' if ci > 3 else 'left')
        cell.border = _border()
    ws.row_dimensions[hdr_row].height = 24
    ws.freeze_panes = f'B{hdr_row + 1}'

    row = hdr_row + 1
    for h in hosts:
        s = stat.get(h, {})
        b = brow.get(h)
        ad = s.get('ad') or {}
        city = s.get('city') or (b or {}).get('city') or h
        url = s.get('url') or (b or {}).get('url') or ''
        kp_ad = ad.get('kp') or (b or {}).get('kp') or ''

        cc = ws.cell(row=row, column=2, value=city)
        cc.font = _font(size=10); cc.alignment = _align(indent=1)
        cc.border = _border(color=C.border_light)

        uc = ws.cell(row=row, column=3, value='открыть')
        uc.hyperlink = url or None
        uc.font = _font(size=10, color=C.accent, underline='single')
        uc.alignment = _align(horizontal='center')
        uc.border = _border(color=C.border_light)

        kc = ws.cell(row=row, column=4, value=_fmt_num(kp_ad))
        kc.font = _font(size=10, color=C.text_soft)
        kc.alignment = _align(horizontal='center')
        kc.border = _border(color=C.border_light)

        # В конфиге (статически)
        cfg_label, cfg_ck = _CT_CFG.get(ad.get('status'), ('—', 'text_muted'))
        fc = ws.cell(row=row, column=5, value=cfg_label)
        fc.font = _font(size=10, bold=(cfg_ck != 'text_muted'),
                        color=getattr(C, cfg_ck, C.text_muted))
        if cfg_ck in ('ok', 'err'):
            fc.fill = _fill(C.ok_soft if cfg_ck == 'ok' else C.err_soft)
        fc.alignment = _align(horizontal='center')
        fc.border = _border(color=C.border_light)
        if ad.get('comment'):
            fc.comment = Comment(ad['comment'], 'Site Checker', height=90, width=280)

        # Подмена (браузер)
        if b is None:
            bl, bck = ('не проверяли', 'text_muted')
        else:
            bl, bck = _CT_BROW.get(b.get('status'), (b.get('status', ''), 'text_muted'))
        bc = ws.cell(row=row, column=6, value=bl)
        bc.font = _font(size=10, bold=(bck in ('ok', 'err')),
                        color=getattr(C, bck, C.text_muted))
        if bck in ('ok', 'err'):
            bc.fill = _fill(C.ok_soft if bck == 'ok' else C.err_soft)
        bc.alignment = _align(horizontal='center')
        bc.border = _border(color=C.border_light)

        shown = ', '.join(_fmt_num(n) for n in ((b or {}).get('shown') or [])) or '—'
        gc = ws.cell(row=row, column=7, value=shown if b is not None else '—')
        gc.font = _font(size=9, color=C.text_muted)
        gc.alignment = _align(horizontal='center')
        gc.border = _border(color=C.border_light)
        ws.row_dimensions[row].height = 20
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
    return _TLD_COUNTRY.get(tld, '-')


# ── Лист «Автокликер» ──────────────────────────────────────────────


# ── Лист «Фильтрация» (доп. чек-лист: фильтры товаров работают) ────

# Вердикт → (метка «работает/не работает» + причина, цвет, это баг?)
_FILTER_VERDICT = {
    'ok':            ('✅ работает',                                   'ok',  False),
    'empty':         ('❌ не работает — после фильтра пусто (ничего не найдено)', 'err', True),
    'not_narrowed':  ('❌ не работает — фильтр не применился (товары не изменились)', 'err', True),
    'http_error':    ('❌ не работает — ошибка загрузки страницы',     'err', True),
    'no_cards':      ('⚠ не проверено — карточки не распознаны (селектор card)', 'warn', False),
    'filter_absent': ('⚠ не проверено — фильтр не найден на странице (селектор filter)', 'warn', False),
    'config_error':  ('⚠ не проверено — ошибка конфига кейса',        'warn', False),
}


def _render_filters_section(ws, row, filters_test):
    """Секция «Фильтрация товаров» на листе «Вёрстка» (колонки B:E). Живой
    драйв фильтра в браузере по пер-проектным селекторам. Возвращает
    следующую свободную строку."""
    if not filters_test:
        return row
    cases = filters_test.get('cases') or []
    _bad = sum(1 for c in cases
               if _FILTER_VERDICT.get(c.get('verdict'), (None, None, False))[2])

    _ok = sum(1 for c in cases if c.get('verdict') == 'ok')
    _meta_section_title(
        ws, row, f'Фильтрация товаров (браузер)  ({len(cases)})',
        C.err if _bad else C.ok)
    row += 1
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = ('Проверено категорий прогона: '
               + (f'{len(cases)} · работают: {_ok} · не работают: {_bad}. '
                  if filters_test.get('available') and cases else '')
               + 'На каждой применяем фильтр и сравниваем набор товаров '
               '(ссылки карточек) на 1-й странице до/после - изменился = '
               'фильтр применился. Ниже - результат по КАЖДОЙ категории.')
    c.font = _font(size=9, italic=True, color=C.text_muted)
    c.alignment = _align(wrap=True, indent=1)
    ws.row_dimensions[row].height = 30
    row += 1

    # Тест не выполнялся / нет конфига
    if not filters_test.get('available') or not cases:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = filters_test.get('note') or (
            'Фильтр-тест не выполнялся: не заданы селекторы фильтра '
            '(catalogs/filters-<проект>.json).')
        c.font = _font(size=10, color=C.text_soft)
        c.alignment = _align(wrap=True, indent=1)
        ws.row_dimensions[row].height = 22
        return row + 2

    _CMAP = {'ok': C.ok, 'err': C.err, 'warn': C.warn}
    for cs in cases:
        label, ckey, _is_bad = _FILTER_VERDICT.get(
            cs.get('verdict'), (cs.get('verdict') or '?', 'warn', False))
        color = _CMAP.get(ckey, C.text_soft)
        _ff, _fg = cs.get('filter_fields'), cs.get('filter_groups')
        _ff_txt = ('' if _ff is None else
                   (f'полей {_ff} (групп {_fg})' if _fg else f'полей {_ff}'))
        # строка 1: имя + работает/не работает (+ сколько полей фильтра)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        c = ws.cell(row=row, column=2)
        c.value = (f'{cs.get("name", "")}: {label}'
                   + (f'   ({_ff_txt})' if _ff_txt else ''))
        c.font = _font(size=10, bold=_is_bad, color=color)
        c.fill = _fill(C.surface)
        c.alignment = _align(wrap=True, indent=1)
        c.border = _border()
        ws.row_dimensions[row].height = 20
        row += 1
        # строка 2: категория (ссылка)
        _cat = cs.get('category', '')
        if _cat:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = _cat
            c.hyperlink = _cat
            c.font = _font(size=9, color=C.accent, underline='single')
            c.alignment = _align(indent=2)
            ws.row_dimensions[row].height = 16
            row += 1
        # строка 3: причина (если не «ok») - подробность из движка
        if cs.get('verdict') != 'ok' and cs.get('detail'):
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
            c = ws.cell(row=row, column=2)
            c.value = 'причина: ' + cs['detail']
            c.font = _font(size=9, italic=True, color=C.text_muted)
            c.alignment = _align(wrap=True, indent=2)
            ws.row_dimensions[row].height = 16
            row += 1
    return row + 1


def _build_autoclick_sheet(wb, autoclick):
    """Итоги автокликера (перекликивание ошибок в Вебмастере/ГСК) - сводка
    по сайтам. Добавляется только если автокликер запускался."""
    if not autoclick:
        return
    ws = wb.create_sheet('Автокликер')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.accent

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 40   # Сайт
    ws.column_dimensions['C'].width = 16   # Сервис
    ws.column_dimensions['D'].width = 12   # Проблем
    ws.column_dimensions['E'].width = 14   # Прокликано
    ws.column_dimensions['F'].width = 16   # Проверяются
    ws.column_dimensions['G'].width = 14   # Без кнопки
    ws.column_dimensions['H'].width = 12   # Ошибки

    ws.merge_cells('B2:H2')
    c = ws['B2']
    c.value = 'Автокликер - перекликивание ошибок'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    # Недоступен (нет браузера / облако)
    if not autoclick.get('available'):
        ws.merge_cells('B4:H4')
        c = ws['B4']
        c.value = autoclick.get('note') or (
            'Автокликер не запускался: нужен локальный залогиненный Chrome '
            '(CDP 9222). На облаке недоступен.')
        c.font = _font(size=11, color=C.text_soft)
        c.alignment = _align(wrap=True, vertical='top')
        ws.row_dimensions[4].height = 44
        return

    sites = autoclick.get('sites') or []
    _t_prob = sum(s.get('problems', 0) for s in sites)
    _t_click = sum(s.get('clicked', 0) for s in sites)
    _t_check = sum(s.get('checking', 0) for s in sites)
    _t_skip = sum(s.get('no_button', 0) for s in sites)

    ws.merge_cells('B3:H3')
    c = ws['B3']
    c.value = (f'Сайтов обработано: {len(sites)}.  Проблем: {_t_prob}.  '
               f'Прокликано: {_t_click}.  Уже проверяются: {_t_check}.  '
               f'Без кнопки: {_t_skip}.')
    c.font = _font(size=11, color=C.text_soft)
    ws.row_dimensions[3].height = 22

    hdr_row = 5
    headers = ['Сайт', 'Сервис', 'Проблем', 'Прокликано',
               'Проверяются', 'Без кнопки', 'Ошибки']
    for ci, h in enumerate(headers, 2):
        cell = ws.cell(row=hdr_row, column=ci, value=h)
        cell.font = _font(size=9, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align(horizontal='center' if ci > 3 else 'left')
        cell.border = _border()
    ws.row_dimensions[hdr_row].height = 22
    ws.freeze_panes = f'B{hdr_row + 1}'

    row = hdr_row + 1
    for s in sorted(sites, key=lambda x: x.get('clicked', 0), reverse=True):
        ws.row_dimensions[row].height = 20
        vals = [
            (s.get('site', ''), 'left', C.text),
            (s.get('service', ''), 'center', C.text_soft),
            (s.get('problems', 0), 'center', C.text_soft),
            (s.get('clicked', 0), 'center', C.ok if s.get('clicked') else C.text_muted),
            (s.get('checking', 0), 'center', C.warn if s.get('checking') else C.text_muted),
            (s.get('no_button', 0), 'center', C.text_muted),
            (s.get('errors', 0), 'center', C.err if s.get('errors') else C.text_muted),
        ]
        for ci, (val, halign, color) in enumerate(vals, 2):
            cell = ws.cell(row=row, column=ci, value=val)
            cell.font = _font(size=10, color=color,
                              bold=(ci == 5 and bool(s.get('clicked'))))
            cell.alignment = _align(horizontal=halign, indent=1 if halign == 'left' else 0)
            cell.border = _border(color=C.border_light)
        row += 1


# ── Лист «Регион и СНГ» (п.1.8 верные переменные / п.1.9 СНГ-чистота) ──


def _region_issue_rows(pages, attr):
    """(result, issue) по каждой находке региональной проверки."""
    out = []
    for r in pages:
        data = getattr(r, attr, None) or {}
        for i in (data.get('issues') or []):
            out.append((r, i))
    return out


def _build_region_sheet(wb, results):
    """Лист региональных проверок: чужой город/телефон/почта на странице города
    (п.1.9) и упоминания РФ/СНГ/чужих стран на СНГ-доменах (п.1.10).
    Добавляется только если проверки выполнялись."""
    reg_checked = [r for r in results if getattr(r, 'region', None) is not None]
    cis_checked = [r for r in results if getattr(r, 'cis', None) is not None]
    if not reg_checked and not cis_checked:
        return

    reg_rows = _region_issue_rows(reg_checked, 'region')
    cis_rows = _region_issue_rows(cis_checked, 'cis')
    has_bugs = bool(reg_rows or cis_rows)

    ws = wb.create_sheet('Регион и СНГ')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 16   # Город
    ws.column_dimensions['C'].width = 13   # Тип страницы
    ws.column_dimensions['D'].width = 52   # URL
    ws.column_dimensions['E'].width = 30   # Что нашли
    ws.column_dimensions['F'].width = 44   # Пояснение
    ws.column_dimensions['G'].width = 52   # Контекст
    ws.column_dimensions['H'].width = 3

    ws.merge_cells('B2:G2')
    c = ws['B2']
    c.value = 'Региональные проверки (переменные города · чистота СНГ)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:G3')
    c = ws['B3']
    c.value = ('П.1.9: на странице города не должно быть подстановок другого города - '
               'чужой город в title/description/H1, телефон или почта другого города '
               '(сверка со справочником КП). '
               'П.1.10: на сайте страны СНГ в текстах, заголовках, метаданных и '
               'контактах не должно быть «РФ», «Россия», «СНГ» и названий других '
               'стран - только своя страна.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 42

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
    c = ws.cell(row=row, column=2)
    c.value = (f'Переменные города: страниц проверено {len(reg_checked)}, находок '
               f'{len(reg_rows)}  ·  СНГ-чистота: страниц проверено '
               f'{len(cis_checked)}, находок {len(cis_rows)}')
    c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 2

    def _section(title, rows, empty_text, color_ok=C.ok):
        nonlocal row
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
        c = ws.cell(row=row, column=2)
        c.value = f'{title}  ({len(rows)})'
        c.font = _font(size=13, bold=True, color=C.err if rows else color_ok)
        c.fill = _fill(C.accent_soft)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1
        if not rows:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
            c = ws.cell(row=row, column=2)
            c.value = empty_text
            c.font = _font(size=10, color=color_ok)
            c.alignment = _align(indent=1)
            ws.row_dimensions[row].height = 22
            row += 2
            return
        for ci, h in enumerate(['Город', 'Тип', 'URL', 'Что нашли',
                                'Пояснение', 'Контекст'], 2):
            cell = ws.cell(row=row, column=ci)
            cell.value = h
            cell.font = _font(size=9, bold=True, color=C.text_muted)
            cell.fill = _fill(C.surface)
            cell.alignment = _align()
            cell.border = _border()
        ws.row_dimensions[row].height = 20
        row += 1
        for r, i in rows:
            ws.row_dimensions[row].height = 34
            найдено = i.get('найдено', '')
            зона = i.get('зона', '')
            vals = [
                (r.city or '-', {'size': 10, 'color': C.text}),
                (r.type_label, {'size': 9, 'color': C.text_muted}),
                (r.url, {'size': 10, 'color': C.accent, 'underline': 'single'}),
                (f'«{найдено}» ({зона})', {'size': 10, 'color': C.err}),
                (i.get('пояснение', ''), {'size': 10, 'color': C.text}),
                (i.get('контекст', ''), {'size': 9, 'color': C.text_soft}),
            ]
            for ci, (val, kw) in enumerate(vals, 2):
                cell = ws.cell(row=row, column=ci)
                cell.value = val
                cell.font = _font(**kw)
                cell.alignment = _align(wrap=True, vertical='top')
                cell.border = _border(color=C.border_light)
                if ci == 4:
                    cell.hyperlink = r.url
            row += 1
        row += 1

    if reg_checked:
        _section('Чужой город / телефон / почта на странице (п.1.8)', reg_rows,
                 '✅ Подстановок другого города не найдено.')
    if cis_checked:
        _section('Упоминания РФ / СНГ / чужих стран на СНГ-доменах (п.1.9)', cis_rows,
                 '✅ На проверенных СНГ-страницах упоминаний РФ/СНГ/чужих стран нет.')
    elif reg_checked:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
        c = ws.cell(row=row, column=2)
        c.value = ('П.1.9 (СНГ-чистота): в этой выборке СНГ-доменов не было - '
                   'проверка выполняется только на доменах не-РФ.')
        c.font = _font(size=10, italic=True, color=C.text_soft)
        c.alignment = _align(indent=1)
        row += 2

    # ── Технический регион (гео-сигналы в коде) - по главным поддоменов ──
    _geo_rows = [r for r in results
                 if r.type_code == 'main'
                 and (getattr(r, 'region', None) or {}).get('geo')]
    if _geo_rows:
        _geo_warn = [r for r in _geo_rows if r.region['geo'].get('warnings')]
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
        c = ws.cell(row=row, column=2)
        c.value = (f'Технический регион поддоменов (гео-сигналы в коде)  '
                   f'({len(_geo_warn)} из {len(_geo_rows)} без сигналов)')
        c.font = _font(size=13, bold=True,
                       color=C.warn if _geo_warn else C.ok)
        c.fill = _fill(C.accent_soft)
        c.alignment = _align(indent=1)
        ws.row_dimensions[row].height = 24
        row += 1
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
        c = ws.cell(row=row, column=2)
        c.value = ('Снаружи видны только сигналы в коде: meta geo.* и '
                   'addressLocality из Schema.org. Настройку региона в '
                   'Яндекс.Вебмастере API не отдаёт - проверяется вручную.')
        c.font = _font(size=9, italic=True, color=C.text_soft)
        c.alignment = _align(indent=1, wrap=True)
        ws.row_dimensions[row].height = 24
        row += 1
        for r in _geo_rows:
            g = r.region['geo']
            ws.merge_cells(start_row=row, start_column=2,
                           end_row=row, end_column=7)
            c = ws.cell(row=row, column=2)
            if g.get('warnings'):
                c.value = f'⚠ {r.city} ({r.subdomain}): ' + '; '.join(g['warnings'])
                c.font = _font(size=10, color=C.warn)
            else:
                c.value = (f'✅ {r.city} ({r.subdomain}): '
                           + '; '.join(g.get('signals') or [])[:160])
                c.font = _font(size=10, color=C.ok)
            c.alignment = _align(indent=2, wrap=True)
            ws.row_dimensions[row].height = 18
            row += 1


# ── Лист «Заголовки и мета» (п.1.3.1: единственные H1/Title/Description) ──

_META_LABEL = {
    'title': 'Title', 'description': 'Meta description',
    'h1': 'H1', 'h2': 'H2', 'h3': 'H3', 'h4': 'H4', 'h5': 'H5', 'h6': 'H6',
}


def _build_meta_unique_sheet(wb, results):
    """Лист единственности ключевых SEO-тегов: несколько или отсутствие
    title/description/H1, дубли H2. Добавляется только если проверка выполнялась."""
    checked = [r for r in results if getattr(r, 'meta_unique', None) is not None]
    if not checked:
        return
    rows = []
    for r in checked:
        for i in (r.meta_unique.get('issues') or []):
            rows.append((r, i))

    ws = wb.create_sheet('Заголовки и мета')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if rows else C.ok

    ws.column_dimensions['A'].width = 3
    ws.column_dimensions['B'].width = 16   # Город
    ws.column_dimensions['C'].width = 13   # Тип страницы
    ws.column_dimensions['D'].width = 60   # URL
    ws.column_dimensions['E'].width = 16   # Тег
    ws.column_dimensions['F'].width = 66   # Что не так
    ws.column_dimensions['G'].width = 3

    ws.merge_cells('B2:F2')
    c = ws['B2']
    c.value = 'Заголовки и мета: единственность и «текстовость» (часть п.1.8)'
    c.font = _font(size=16, bold=True)
    ws.row_dimensions[2].height = 26

    ws.merge_cells('B3:F3')
    c = ws['B3']
    c.value = ('На странице должны быть в единственном экземпляре <title>, '
               '<meta name="description"> и <h1>: если их нет или больше одного - '
               'баг. Также ловим дубли H2 (два H2 с одинаковым текстом; '
               'несколько разных H2 - норма) и заголовки h2-h6 вне текста: '
               'в шапке, подвале, меню или сайдбаре им не место.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 40

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    c = ws.cell(row=row, column=2)
    pages_bad = len({id(r) for r, _ in rows})
    c.value = (f'Проверено страниц: {len(checked)}  ·  с проблемами: {pages_bad}  '
               f'·  всего замечаний: {len(rows)}')
    c.font = _font(size=10, bold=True, color=C.err if rows else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 24
    row += 2

    if not rows:
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        c = ws.cell(row=row, column=2)
        c.value = ('✅ На всех проверенных страницах title, description и H1 - '
                   'в единственном экземпляре, дублей H2 нет, заголовки '
                   'h2-h6 только в тексте.')
        c.font = _font(size=11, color=C.ok)
        c.alignment = _align(indent=1)
        return

    for ci, h in enumerate(['Город', 'Тип', 'URL', 'Тег', 'Что не так'], 2):
        cell = ws.cell(row=row, column=ci)
        cell.value = h
        cell.font = _font(size=9, bold=True, color=C.text_muted)
        cell.fill = _fill(C.surface)
        cell.alignment = _align()
        cell.border = _border()
    ws.row_dimensions[row].height = 20
    row += 1
    for r, i in rows:
        ws.row_dimensions[row].height = 30
        vals = [
            (r.city or '-', {'size': 10, 'color': C.text}),
            (r.type_label, {'size': 9, 'color': C.text_muted}),
            (r.url, {'size': 10, 'color': C.accent, 'underline': 'single'}),
            (_META_LABEL.get(i.get('тип'), i.get('тип', '')),
             {'size': 10, 'bold': True, 'color': C.err}),
            (i.get('пояснение', ''), {'size': 10, 'color': C.text}),
        ]
        for ci, (val, kw) in enumerate(vals, 2):
            cell = ws.cell(row=row, column=ci)
            cell.value = val
            cell.font = _font(**kw)
            cell.alignment = _align(wrap=True, vertical='top')
            cell.border = _border(color=C.border_light)
            if ci == 4:
                cell.hyperlink = r.url
        row += 1


# ── Пересборка листов в тематические группы ─────────────────────────
# Каждый детальный лист строится как раньше (временный), затем переносится
# СЕКЦИЕЙ в один из 7 групповых листов. Так весь рендер сохраняется без
# переписывания, а отчёт группируется по темам.

# Группа → упорядоченный список исходных листов (что в неё сливается).
_SHEET_GROUPS = [
    # «Структура страниц» - НЕ в группе: остаётся отдельным листом сразу
    # после «Обзора» (как было до пересборки).
    ('Техничка', [
        'Индексация', 'Метаданные', 'Заголовки и мета',
        'Разметка', 'Безопасность', 'Ошибки JavaScript',
        'Валидация и скорость', 'Страница 404', '404 в индексе',
        'Страницы в ГСК', 'Дубли главной', 'Индексация (Арсенкин)',
        'Фильтры ПС', 'Нагрузка и парсинг', 'Битые тексты',
    ]),
    ('Верстка', ['Вёрстка']),
    ('КП', ['Контакты по городам', 'Регион и СНГ']),
    ('Формы', []),                 # детальный отчёт форм - отдельный файл
    ('Админка', ['Настройки в админке']),
    ('Аналитика', [
        '404 из Метрики', 'Динамика трафика', 'Отзывы (докупка)',
        'Траст проекта', 'Уведомления', 'Ошибки сервисов', 'Автокликер',
        'Ссылочный профиль', 'Замена рекл. номера', 'Аномалии',
    ]),
    ('Контент', ['Изображения']),
]

# Групповые листы, которым добавляем поясняющую секцию, даже если исходных
# листов нет (чтобы структура из 7 листов существовала и была понятной).
_GROUP_NOTES = {
    'Формы': ('Детальная проверка форм — в отдельном отчёте форм-тестера '
              '(свой файл). Здесь, в основном отчёте, форма заявки/телефон '
              'проверяется как часть страниц (см. лист «Техничка» → блоки '
              'страниц и «Страница 404»).'),
    'Контент': ('Изображения (alt, современные форматы webp/avif, вес, '
                'lazy, уникальность картинок категорий, фото товаров не '
                'дублируются между категориями) — если проверка '
                'выполнялась, показаны ниже. SEO-текст частотных категорий '
                '(нейроответы) — на листе «Техничка» («Метаданные»); блоки '
                'товара (похожие/отзывы/сортировка/цены) — на листе '
                '«Структура страниц».'),
}

_GROUP_TAB_RANK = {C.err: 0, C.warn: 1}   # для агрегированного цвета вкладки


def _append_sheet_as_section(dst, src, start_row, title, gap=2):
    """Скопировать содержимое листа src в dst начиная со start_row (значения,
    стили, слияния, ширины колонок, высоты строк, гиперссылки, комментарии).
    Перед секцией — цветная полоса-разделитель с title. Возвращает следующую
    свободную строку."""
    # Полоса-разделитель секции (навигационный якорь).
    dst.merge_cells(start_row=start_row, start_column=2,
                    end_row=start_row, end_column=8)
    band = dst.cell(row=start_row, column=2, value='▸ ' + title)
    band.font = _font(size=12, bold=True, color='FFFFFF')
    band.fill = _fill(C.text_soft)
    band.alignment = _align(indent=1)
    dst.row_dimensions[start_row].height = 22
    row0 = start_row + 1
    offset = row0 - 1                          # src-строка r → dst-строка r+offset

    for col, dim in src.column_dimensions.items():
        if dim.width:
            cur = dst.column_dimensions[col].width or 0
            dst.column_dimensions[col].width = max(cur, dim.width)

    max_row, max_col = src.max_row, src.max_column
    for r in range(1, max_row + 1):
        for cc in range(1, max_col + 1):
            s = src.cell(row=r, column=cc)
            if s.value is None and not s.has_style:
                continue
            d = dst.cell(row=r + offset, column=cc)
            d.value = s.value
            if s.has_style:
                d.font = copy(s.font)
                d.fill = copy(s.fill)
                d.border = copy(s.border)
                d.alignment = copy(s.alignment)
                d.number_format = s.number_format
            if s.hyperlink:
                d.hyperlink = s.hyperlink.target
            if s.comment:
                d.comment = Comment(s.comment.text,
                                    s.comment.author or 'Site Checker')
        rd = src.row_dimensions.get(r)
        if rd is not None and rd.height:
            dst.row_dimensions[r + offset].height = rd.height

    for mr in list(src.merged_cells.ranges):
        c1, r1, c2, r2 = range_boundaries(str(mr))
        dst.merge_cells(start_row=r1 + offset, start_column=c1,
                        end_row=r2 + offset, end_column=c2)

    return max_row + offset + gap


def _regroup_into_groups(wb):
    """Собрать детальные листы в 7 тематических групповых листов.
    Обзор остаётся первым, «Все детали» - последним."""
    for group_name, members in _SHEET_GROUPS:
        present = [m for m in members if m in wb.sheetnames]
        note = _GROUP_NOTES.get(group_name)
        if not present and not note:
            continue
        grp = wb.create_sheet(group_name)
        grp.sheet_view.showGridLines = False
        grp.column_dimensions['A'].width = 3
        # Цвет вкладки - худший среди секций.
        _rank = 9
        for m in present:
            _tc = getattr(wb[m].sheet_properties, 'tabColor', None)
            _tcv = getattr(_tc, 'rgb', None) or _tc
            if isinstance(_tcv, str):
                _rank = min(_rank, _GROUP_TAB_RANK.get(_tcv[-6:].upper(), 9))
        grp.sheet_properties.tabColor = (
            C.err if _rank == 0 else C.warn if _rank == 1 else C.ok)

        row = 2
        if note:
            grp.merge_cells(start_row=row, start_column=2,
                            end_row=row, end_column=8)
            c = grp.cell(row=row, column=2, value=note)
            c.font = _font(size=10, italic=True, color=C.text_soft)
            c.alignment = _align(wrap=True, vertical='top', indent=1)
            grp.row_dimensions[row].height = 60
            row += 2
        for m in present:
            row = _append_sheet_as_section(grp, wb[m], row, m)
        # Удаляем исходные листы после переноса.
        for m in present:
            del wb[m]

    # Порядок: Обзор → Структура страниц → 7 групп → Я.Бизнес/GMB → Все детали.
    order = (['Обзор', 'Структура страниц']
             + [g for g, _ in _SHEET_GROUPS if g in wb.sheetnames]
             + ['Я.Бизнес и GMB', 'Все детали'])
    ordered = [wb[n] for n in order if n in wb.sheetnames]
    ordered += [ws for ws in wb.worksheets if ws not in ordered]
    wb._sheets = ordered


# ── Главная функция ────────────────────────────────────────────────


def build_report(
    *,
    project_name: str,
    started_at_ms: int,
    finished_at_ms: int,
    selected_subdomains: list,    # список Subdomain
    results: list,                 # список CheckResult
    output_path: Path | str,
    metrika_reports: list = None,  # список Report404 - добавит лист «404 из Метрики»
    metrika_data_date: str = None, # дата отчёта Метрики (YYYY-MM-DD)
    metrika_is_stale: bool = False,# True если данные не за вчера, а за более ранний день
    metrika_404_goal: dict = None, # has_404_goal() - строка на листе «404 из Метрики»
    notifications: list = None,    # список WebmasterNotification - добавит лист «Уведомления»
    service_issues: list = None,   # список ServiceIssue - добавит лист «Ошибки сервисов»
    autoclick: dict = None,        # итоги автокликера - добавит лист «Автокликер»
    indexing_summary: dict = None, # sitemap↔robots (п.1.7) - в лист «Индексация»
    meta_summary: dict = None,     # дубли мета/URL (п.1.8) - в лист «Метаданные»
    filters_test: dict = None,     # итоги фильтр-теста - секция на листе «Вёрстка»
    console_check: dict = None,    # ошибки JS в консоли (п.1.14) - лист «Ошибки JavaScript»
    calltracking_check: dict = None,  # браузерная проверка замены рекл. номера - лист «Замена рекл. номера»
    w3c_check: dict = None,        # валидация W3C + скорость (п.1.16) - лист «Валидация и скорость»
    p404_check: dict = None,       # страница 404 (п.1.18) - лист «Страница 404»
    ps_filters: dict = None,       # фильтры ПС (п.1.19) - лист «Фильтры ПС»
    search_check: dict = None,     # поиск находит категории - секция «Вёрстки»
    index_404_check: dict = None,  # 404 среди страниц в индексе - лист «404 в индексе»
    stress_check: dict = None,     # ошибки сервера: парсинг/нагрузка/дубли - лист «Нагрузка и парсинг»
    link_profile: dict = None,     # lite-профиль ссылок (Вебмастер) - лист «Ссылочный профиль»
    wm_metrics: dict = None,       # аномалии Вебмастера (Блок B) - секция «Аномалии» внизу «Аналитики»
    admin_settings: dict = None,   # функции настройки в админке (п.1.21) - лист «Настройки в админке»
    yabusiness: dict = None,       # Я.Бизнес/GMB (поддомен под свой регион) - лист «Я.Бизнес и GMB»
    gsc_pages: dict = None,        # количество страниц в ГСК (индекс/не-индекс/сумма) - лист «Страницы в ГСК»
    home_dupes: dict = None,       # дубли главной страницы - лист «Дубли главной»
    traffic: dict = None,          # сравнение трафика день/месяц/год - лист «Динамика трафика»
    arsenkin: dict = None,         # индексация URL через Арсенкин - лист «Индексация (Арсенкин)»
    review_priority: dict = None,  # приоритет докупки отзывов - лист «Отзывы (докупка)»
    anomalies: dict = None,        # аномалии ГСК/Метрика - лист «Аномалии»
    trust: dict = None,            # ИКС + DR - лист «Траст проекта»
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

    # Индексация (п.1.7): страницы выборки, закрытые от индексации
    indexing_bad_pages = [r for r in results if getattr(r, 'has_indexing_issues', False)]
    indexing_sitemap_conflicts = len((indexing_summary or {}).get('disallowed') or [])

    # Метаданные (п.1.8): проблемы title/description/H1 + дубли + единственность
    meta_bad_pages = [r for r in results
                      if getattr(r, 'has_meta_issues', False)
                      or getattr(r, 'has_meta_unique_issues', False)]

    # Вёрстка (п.1.11): нет viewport / битые CSS
    layout_bad_pages = [r for r in results if getattr(r, 'has_layout_issues', False)]

    # Разметка (п.1.12): OG/Schema.org
    markup_bad_pages = [r for r in results if getattr(r, 'has_markup_issues', False)]

    # Заголовки безопасности (доп. 1.8): битые значения HSTS/CSP/X-Frame
    security_bad_pages = [r for r in results
                          if getattr(r, 'has_security_issues', False)]

    # Изображения (п.1.15): картинки без alt
    images_bad_pages = [r for r in results
                        if getattr(r, 'has_image_issues', False)]
    _mdups = (meta_summary or {}).get('duplicates') or {}
    meta_dup_groups = (len(_mdups.get('same_city') or [])
                       + len(_mdups.get('cross_city') or [])
                       + sum(1 for d in ((meta_summary or {})
                                         .get('url_duplicates') or [])
                             if d.get('problem') != 'not_301'))

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
    _extra = ((1 if total_text_issues > 0 else 0)
              + (1 if total_content_bugs > 0 else 0)
              + (1 if (indexing_bad_pages or indexing_sitemap_conflicts) else 0)
              + (1 if (meta_bad_pages or meta_dup_groups) else 0))
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
            f'{total_text_issues} битых переменных в текстах - см. лист «Битые тексты».'
        )
    if total_content_bugs > 0:
        summary_text += (
            f'\nВ контенте {total_content_bugs} проблем на {len(pages_with_content_bugs)} страницах '
            f'(нет цены, кнопок заказа или заголовка) - см. лист «Структура страниц».'
        )
    _idx_blanket = (indexing_summary or {}).get('blanket_disallow') or []
    _idx_assets = (indexing_summary or {}).get('assets_closed') or []
    _idx_mc = (((indexing_summary or {}).get('sitemap_audit') or {})
               .get('missing_catalog') or {})
    _idx_missing = ((_idx_mc.get('categories') or [])
                    + (_idx_mc.get('filters') or [])
                    + (_idx_mc.get('services') or []))
    _idx_hm_junk = (((indexing_summary or {}).get('html_sitemap') or {})
                    .get('junk_links') or [])
    if (indexing_bad_pages or indexing_sitemap_conflicts
            or _idx_blanket or _idx_assets or _idx_missing or _idx_hm_junk):
        _idx_bits = []
        if indexing_bad_pages:
            _idx_bits.append(f'расхождения с robots.txt на {len(indexing_bad_pages)} '
                             f'{_plural_pages(len(indexing_bad_pages))}')
        if indexing_sitemap_conflicts:
            _idx_bits.append(f'{indexing_sitemap_conflicts} путей каталога под Disallow '
                             f'в robots.txt')
        if _idx_blanket:
            _idx_bits.append('в robots.txt есть «Disallow: /» - сайт закрыт целиком')
        if _idx_assets:
            _idx_bits.append(f'{len(_idx_assets)} файлов .css/.js закрыты в robots.txt')
        if _idx_missing:
            _idx_bits.append(f'{len(_idx_missing)} важных ссылок '
                             f'(категории/фильтры/услуги) нет в sitemap')
        if _idx_hm_junk:
            _idx_bits.append(f'{len(_idx_hm_junk)} служебных ссылок в HTML-карте')
        summary_text += ('\nИндексация: ' + ', '.join(_idx_bits)
                         + ' - см. лист «Индексация».')
    if meta_bad_pages or meta_dup_groups:
        _mb = []
        if meta_bad_pages:
            _mb.append(f'проблемы на {len(meta_bad_pages)} '
                       f'{_plural_pages(len(meta_bad_pages))}')
        if meta_dup_groups:
            _mb.append(f'{meta_dup_groups} групп дублей (title/описания/URL)')
        summary_text += ('\nМетаданные: ' + ', '.join(_mb)
                         + ' - см. лист «Метаданные».')
    if layout_bad_pages:
        summary_text += (f'\nВёрстка: проблемы (viewport/CSS) на '
                         f'{len(layout_bad_pages)} '
                         f'{_plural_pages(len(layout_bad_pages))} - '
                         f'см. лист «Вёрстка».')
    if markup_bad_pages:
        summary_text += (f'\nРазметка: проблемы (OG/Schema.org) на '
                         f'{len(markup_bad_pages)} '
                         f'{_plural_pages(len(markup_bad_pages))} - '
                         f'см. лист «Разметка».')
    if security_bad_pages:
        summary_text += (f'\nБезопасность: ошибки заголовков на '
                         f'{len(security_bad_pages)} '
                         f'{_plural_pages(len(security_bad_pages))} - '
                         f'см. лист «Безопасность».')
    if images_bad_pages:
        summary_text += (f'\nИзображения: картинки без alt на '
                         f'{len(images_bad_pages)} '
                         f'{_plural_pages(len(images_bad_pages))} - '
                         f'см. лист «Изображения».')
    _filters_cases = (filters_test or {}).get('cases') or []
    _filters_bad = sum(1 for c in _filters_cases
                       if _FILTER_VERDICT.get(c.get('verdict'),
                                              (None, None, False))[2])
    if _filters_bad:
        summary_text += (f'\nФильтрация: {_filters_bad} '
                         f'{"фильтр" if _filters_bad == 1 else "фильтров"} '
                         f'работают некорректно - см. лист «Вёрстка».')
    _console_bad = sum(1 for p in ((console_check or {}).get('pages') or [])
                       if p.get('errors'))
    if _console_bad:
        summary_text += (f'\nОшибки JavaScript: на {_console_bad} '
                         f'{_plural_pages(_console_bad)} есть ошибки в консоли '
                         f'- см. лист «Ошибки JavaScript».')
    if stress_check and stress_check.get('available'):
        _sp = stress_check.get('parsing') or {}
        _sl = stress_check.get('load') or {}
        _sd = stress_check.get('duplicates') or {}
        _s5 = (len(_sp.get('server_errors') or [])
               + sum(p.get('server_5xx', 0) for p in (_sl.get('pages') or []))
               + len(_sd.get('server_errors') or []))
        if _sp.get('banned'):
            summary_text += ('\nНагрузка и парсинг: сайт закрыл доступ '
                             '(принял бота за парсера) - см. лист «Нагрузка '
                             'и парсинг».')
        elif _s5:
            summary_text += (f'\nНагрузка и парсинг: ошибок сервера (5xx) '
                             f'{_s5} - см. лист «Нагрузка и парсинг».')
    if link_profile and link_profile.get('available'):
        _lp_w = sum(len(h.get('warnings') or [])
                    for h in (link_profile.get('hosts') or []))
        if _lp_w:
            summary_text += (f'\nСсылочный профиль: замечаний {_lp_w} '
                             f'(обвал/всплеск/спам) - см. лист «Ссылочный '
                             f'профиль».')
    if admin_settings and admin_settings.get('available'):
        _adm_bad = [c.get('title') for c in (admin_settings.get('checks') or [])
                    if not c.get('ok')]
        if _adm_bad:
            summary_text += ('\nНастройки в админке: не работают - '
                             + ', '.join(_adm_bad)
                             + ' (см. лист «Настройки в админке»).')
    summary_text += '\nПодробности - на листе «Все детали» (фильтр по колонке «Статус»).'
    # Ссылки на старые листы → на группу-лист (блок внутри группы), т.к.
    # детальные листы теперь секции в 7 тематических листах.
    _sheet_to_group = {m: g for g, ms in _SHEET_GROUPS for m in ms}
    for _old, _grp in _sheet_to_group.items():
        summary_text = summary_text.replace(
            f'лист «{_old}»', f'лист «{_grp}» (блок «{_old}»)')
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

    # Отчёт собран в 7 тематических листов (каждый - несколько блоков-секций).
    nav_items = [
        ('Обзор', 'эта страница: сколько проверено, сколько работает и сколько сломано.'),
        ('Структура страниц', 'что чинить в контенте по типам страниц (главная/каталог/листинг/разделы/карточки товаров/технические) - где нет цены, кнопок заказа, заголовка. Красное = баг.'),
        ('Техничка', 'SEO-техничка: индексация (robots/sitemap/canonical), метаданные и единственность заголовков, микроразметка (OG/Schema), безопасность и редиректы, ошибки JavaScript, валидность W3C и скорость, страница 404 и 404 в индексе, санкции ПС, нагрузка/парсинг, битые переменные.'),
        ('Верстка', 'вёрстка и адаптивность: viewport, CSS, сетка на пк/моб/планшет, переходы из меню, работа фильтров товаров, поиск по категориям.'),
        ('КП', 'сверка с картой присутствия: контакты по городам (телефон/почта/адрес), верные переменные города, чистота СНГ-доменов от РФ.'),
        ('Формы', 'формы: детальная проверка - в отдельном отчёте форм-тестера; здесь - точки формы на страницах.'),
        ('Админка', 'работа функций настройки в админке: поддомены/категории/товары/тех.страницы + CRUD (создание/правка/скрытие/удаление) с аудитом «было → стало».'),
        ('Аналитика', '404 из Метрики, письма Вебмастера/GSC, ошибки сервисов (сайтмапы/дубли/мусорные ссылки), прокликивание исправлений, lite-профиль беклинков.'),
        ('Контент', 'изображения (alt, webp/avif, вес, lazy, уникальность картинок категорий, фото товаров не дублируются между категориями); SEO-текст частотных категорий - в «Техничке».'),
        ('Я.Бизнес и GMB', 'если есть лист - каждый поддомен (город) зарегистрирован в Яндекс.Бизнесе под своим регионом; поддомены без организации.'),
        ('Все детали', 'каждая проверенная страница: адрес, код ответа, статус, скорость.'),
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

    # ─── Лист индексации (п.1.7) - если проверка выполнялась ────────
    _build_indexing_sheet(wb, results, indexing_summary)

    # ─── Лист единственности тегов (п.1.3.1) - если проверялась ─────
    _build_meta_unique_sheet(wb, results)
    _build_region_sheet(wb, results)

    # ─── Лист метаданных (п.1.8) - если проверка выполнялась ────────
    _build_meta_sheet(wb, results, meta_summary)

    # ─── Лист вёрстки (п.1.11) + секция фильтрации (браузер) ────────
    _build_layout_sheet(wb, results, filters_test, search_check)

    # ─── Лист разметки (п.1.12) - если проверка выполнялась ─────────
    _build_markup_sheet(wb, results)

    # ─── Лист заголовков безопасности (доп. 1.8) - если проверялась ──
    _build_security_sheet(wb, results)

    # ─── Лист изображений (п.1.15) - если проверка выполнялась ──────
    _build_images_sheet(wb, results)

    # ─── Лист ошибок JS в консоли (п.1.14) - если проверка выполнялась ──
    _build_console_sheet(wb, console_check)

    # ─── Лист валидации W3C + скорости (п.1.16) - если выполнялась ──────
    _build_w3c_sheet(wb, w3c_check)
    _build_gsc_pages_sheet(wb, gsc_pages)
    _build_home_dupes_sheet(wb, home_dupes)
    _build_arsenkin_sheet(wb, arsenkin)

    # ─── Лист «Страница 404» (п.1.18) - если проверка выполнялась ──────
    _build_404_sheet(wb, p404_check)

    # ─── Лист «404 в индексе» - если проверка выполнялась ──────────────
    _build_index_404_sheet(wb, index_404_check)

    # ─── Лист «Фильтры ПС» (п.1.19) - если проверка выполнялась ────────
    _build_ps_filters_sheet(wb, ps_filters)

    # ─── Лист «Нагрузка и парсинг» - если стресс-пробы выполнялись ─────
    _build_stress_sheet(wb, stress_check)

    # ─── Лист «Ссылочный профиль» - если lite-проверка выполнялась ─────
    _build_link_profile_sheet(wb, link_profile)
    _build_anomalies_sheet(wb, wm_metrics, link_profile, anomalies)
    _build_trust_sheet(wb, trust)

    # ─── Лист «Настройки в админке» - если проверка выполнялась ────────
    _build_admin_settings_sheet(wb, admin_settings)

    # ─── Лист «Я.Бизнес/GMB» - если проверка выполнялась ──────────────
    _build_yabusiness_sheet(wb, yabusiness)

    # ─── Лист «Динамика трафика» - если сравнение выполнялось ──────────
    _build_traffic_sheet(wb, traffic)

    # ─── Лист «Отзывы (докупка)» - приоритет докупки отзывов ───────────
    _build_review_priority_sheet(wb, review_priority)

    # ─── Лист сверки контактов с КП (если были главные с kp_result) ──
    _build_kp_sheet(wb, results)
    _build_calltracking_sheet(wb, results, calltracking_check)

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
            r.http_code if r.http_code else '-',  # 5 Код
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

        # URL - кликабельная гиперссылка
        url_cell = ws2.cell(row=row_idx, column=4)
        url_cell.hyperlink = r.url
        url_cell.font = _font(name='Consolas', size=10, color=C.accent, underline='single')

        # Откуда перешли - моноширинный для цепочек, курсивный для прямых
        path_cell = ws2.cell(row=row_idx, column=11)
        if r.redirect_chain:
            path_cell.font = _font(name='Consolas', size=9, color=C.text_soft)
        elif not r.is_ok:
            path_cell.font = _font(size=10, italic=True, color=C.text_muted)

        # Битые переменные - подсветка
        if r.has_text_issues:
            issue_cell = ws2.cell(row=row_idx, column=10)
            issue_cell.font = _font(size=10, bold=True, color=C.warn)
            issue_cell.fill = _fill(C.warn_soft)

        # Оценка скорости - цвет по уровню
        if r.speed_rating:
            speed_cell = ws2.cell(row=row_idx, column=8)
            color = SPEED_COLOR[r.speed_rating]
            bold = r.speed_rating in ('slow', 'very_slow')
            speed_cell.font = _font(size=10, bold=bold, color=color)

        # Статус - цвет по результату
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
            '({{city}}, %price%, #MIN_PRICE#, undefined и т.п.) остался виден пользователю в тексте страницы. '
            'Чтобы увидеть проблему - откройте URL и поищите по странице (Ctrl+F) то, '
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

                # URL - кликабельный
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
    # ЛИСТ 4: «404 из Метрики» - если есть данные (страницы ИЛИ хотя бы
    # проверка цели - иначе «404 не найдено, но и цель не проверялась»
    # молча теряется, когда отчёт Метрики просто пуст за период)
    # ═══════════════════════════════════════════════════════════════
    if metrika_reports or metrika_404_goal is not None:
        metrika_reports = metrika_reports or []
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
        ws4.freeze_panes = 'A7'  # шапка фиксируется

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
            date_display = metrika_data_date or '-'

        if metrika_is_stale:
            c.value = (
                f'⚠ Внимание: данные за {date_display}. '
                f'Свежий отчёт Метрики (за вчерашний день) ещё не пришёл - '
                f'используем последний доступный.'
            )
            c.font = _font(size=10, italic=True, bold=True, color=C.err)
            c.fill = _fill(C.err_soft)
        else:
            c.value = f'Данные за {date_display}'
            c.font = _font(size=10, color=C.text_soft)
        c.alignment = _align(wrap=True)
        ws4.row_dimensions[2].height = 30 if metrika_is_stale else 20

        # 3-я строка - пустая. Раньше тут была длинная пояснительная
        # строка про «🔴 Точно сломан / ⚠ Только в Метрике / Сортировка».
        # Убрана по требованию: цвета в колонке «Статус» интуитивно понятны,
        # а лишний текст загромождал шапку.
        ws4.row_dimensions[3].height = 8

        # ─── Цель на 404 в Метрике (регулярный мониторинг): есть/нет ───
        # Не влияет на сам сбор этого листа (он и так работает через
        # просмотры страниц с заголовком «не найдена») - это отдельная,
        # клиентская настройка: без неё 404 не видно в «Конверсиях»/
        # уведомлениях самой Метрики, только в этом отчёте.
        if metrika_404_goal is not None:
            ws4.merge_cells('A4:H4')
            c = ws4['A4']
            if metrika_404_goal.get('есть'):
                _names = sorted({v['название'] for v in
                                 metrika_404_goal.get('счётчики', {}).values()
                                 if v.get('есть') and v.get('название')})
                c.value = ('✅ Цель на 404 в Метрике: есть'
                           + (f' («{", ".join(_names)}»)' if _names else ''))
                c.font = _font(size=10, color=C.ok)
            else:
                c.value = ('❌ Цель на 404 в Метрике: не найдена - сбор на этом '
                           'листе всё равно работает (через просмотры страниц), '
                           'но в самой Метрике (вкладка «Конверсии», '
                           'уведомления) 404 без цели не отслеживаются - '
                           'стоит создать.')
                c.font = _font(size=10, bold=True, color=C.err)
            c.alignment = _align(wrap=True, indent=1)
            ws4.row_dimensions[4].height = 20
        else:
            ws4.row_dimensions[4].height = 4

        # ─── Шапка таблицы на 6-й строке ───────────────────────────
        # 5-я строка - пустая разделительная
        hdr_row = 6
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
                        # Также сравним по path - если в Метрике URL без поддомена, в SC с поддоменом
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
        # Если у всех или у большинства строк нет page_url - Метрика
        # отдала только заголовки страниц. Так бывает, если в шаблоне
        # рассылки не настроена группировка «Адрес страницы». Чинить
        # это в Метрике, не в коде. Помечаем это прямо в xlsx, чтобы
        # пользователь сразу понял что происходит.
        rows_with_url = sum(1 for fr in flat_rows if fr['url'])
        if flat_rows and rows_with_url == 0:
            warn_row = hdr_row - 1  # 5-я строка (пустая разделительная)
            ws4.merge_cells(f'A{warn_row}:H{warn_row}')
            wc = ws4.cell(row=warn_row, column=1)
            wc.value = (
                '⚠ Колонка «URL страницы» пустая: в текущем шаблоне рассылки '
                'Метрики нет «Адреса страницы» - приходят только заголовки. '
                'Чтобы получать URL: Метрика → Содержание → Страницы → 404 → '
                '«Группировки» → добавить «Адрес страницы» → сохранить шаблон '
                'рассылки. Со следующего письма URL начнут приходить.'
            )
            wc.font = _font(size=10, bold=True, color=C.warn)
            wc.fill = _fill(C.warn_soft)
            wc.alignment = _align(wrap=True, vertical='top')
            ws4.row_dimensions[warn_row].height = 48

        # ─── Если в почте есть отчёты но 404 не нашлось - короткое сообщение ──
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

                # Страна - определяем по доменной зоне URL
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

                # URL - кликабельный
                cell = ws4.cell(row=row_idx, column=4)
                cell.value = fr['url'] or '-'
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
                cell.value = fr['referer'] or '-'
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
    # ЛИСТ 5: Уведомления (Вебмастер + GSC) - если сбор включён
    # ═══════════════════════════════════════════════════════════════
    # notifications=None - сбор уведомлений был ВЫКЛЮЧЕН, листа нет.
    # notifications=[] - сбор включён, писем нет: лист с заглушкой
    # («проверено, писем нет» - это результат, а не отсутствие проверки).
    # Сюда же идут ошибки из Вебмастера по API (секция «Вебмастер»).
    if notifications is not None or service_issues:
        _build_notifications_sheet(wb, notifications, service_issues)

    # ЛИСТ: «Автокликер» - итоги перекликивания ошибок (если запускался).
    _build_autoclick_sheet(wb, autoclick)

    # Фильтрация товаров - теперь секцией на листе «Вёрстка» (см. выше).

    # ── Пересборка детальных листов в 7 тематических групп ──────────
    # (Техничка / Верстка / КП / Формы / Админка / Аналитика / Контент)
    _regroup_into_groups(wb)

    # ── Сохраняем ──────────────────────────────────────────────────
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


# ── Утилита для имени файла ─────────────────────────────────────────


def make_report_filename(project_id: str, started_at_ms: int, reports_dir: Path) -> str:
    """
    Имя файла: smu-21.05.2026.xlsx
    Если уже есть - smu-21.05.2026_2.xlsx, _3 и т.д.
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
