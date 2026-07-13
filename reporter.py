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
            _u = getattr(i, 'url', '')
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
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'indexing', 'warnings'), C.warn)

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


def _build_layout_sheet(wb, results, filters_test=None):
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
               'в HTML = предупреждение). Полный визуальный рендер это не '
               'заменяет - выборочный ручной просмотр остаётся.')
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
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = (f'Проверено страниц: {len(checked)} · без viewport: {_no_vp} · '
               f'с битыми CSS: {_css_broken_pages} · '
               f'ссылок меню прозвонено: {_menu_checked}, битых: {_menu_broken} · '
               f'предупреждений: {len(warned)}')
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

    # Секция 5: фильтрация товаров (браузерный тест) - если запускался
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
               'предупреждение.')
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
            ws, row, _issue_groups(bad, 'markup', 'issues'), C.err)

    if warned:
        _meta_section_title(ws, row, f'Предупреждения  ({len(warned)})', C.warn)
        row += 1
        row = _render_issue_groups(
            ws, row, _issue_groups(warned, 'markup', 'warnings'), C.warn)


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
               'X-Content-Type-Options: nosniff или защиты от кликджекинга '
               '(X-Frame-Options / CSP frame-ancestors) - предупреждение. '
               'Битое значение (HSTS max-age=0, устаревший ALLOW-FROM, '
               'X-Content-Type-Options не nosniff, конфликт дублей) - баг: '
               'заголовок есть, но работает во вред или впустую. Отсутствие '
               'CSP не считаем ошибкой - для сайта-визитки это норма. '
               'Полную оценку даёт securityheaders.com.')
    c.font = _font(size=10, italic=True, color=C.text_soft)
    c.alignment = _align(wrap=True, vertical='top')
    ws.row_dimensions[3].height = 60

    row = 5
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    c = ws.cell(row=row, column=2)
    c.value = (f'Проверено страниц: {len(checked)} · с багами: {len(bad)} · '
               f'с предупреждениями: {len(warned)}')
    c.font = _font(size=10, bold=True, color=C.err if has_bugs else C.ok)
    c.fill = _fill(C.surface)
    c.alignment = _align(wrap=True)
    ws.row_dimensions[row].height = 26
    row += 2

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
               'предупреждение). (3) Оптимизация - свои картинки не тяжелее '
               f'{_IMG_HEAVY_KB} КБ (тяжелее = вероятно не оптимизированы, '
               'предупреждение). (4) Lazy loading - у картинок/видео есть '
               'ленивая загрузка (loading="lazy"/data-src/preload="none"). '
               'Вес берётся по Content-Length.')
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

    def _img_extra(r):
        im = getattr(r, 'images', None) or {}
        bits = []
        if im.get('no_alt'):
            bits.append('без alt: ' + ', '.join(im['no_alt'][:3])
                        + (f' … +{len(im["no_alt"]) - 3}'
                           if len(im['no_alt']) > 3 else ''))
        if im.get('legacy'):
            bits.append(f'устаревших: {len(im["legacy"])}')
        if im.get('heavy'):
            bits.append('тяжёлые: ' + ', '.join(
                f'{h["url"].rsplit("/", 1)[-1]} {h["kb"]}КБ'
                for h in im['heavy'][:3]))
        if im.get('img_total') and not im.get('lazy_imgs'):
            bits.append(f'без lazy: {im["img_total"]} картинок')
        if im.get('media_total') and not im.get('lazy_media'):
            bits.append(f'видео/iframe: {im["media_total"]}')
        return ' · '.join(bits)

    _meta_section_title(ws, row, f'Проблемы (нет alt)  ({len(bad)})',
                        C.err if bad else C.ok)
    row += 1
    if not bad:
        _meta_ok_line(ws, row, '✅ У всех картинок на проверенных страницах '
                               'есть атрибут alt.')
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


# ── Лист «Валидация и скорость» (п.1.16: W3C HTML/CSS + время ресурсов) ─


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
               '(4) есть ссылки на основные разделы и форма заявки/телефон. '
               'Шаблон 404 сквозной - проверяются главный домен и один '
               'поддомен.')
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

    ws = wb.create_sheet('Ошибки JavaScript')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if bad else C.ok

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
               'самого сайта. Страница с ошибками = баг.')
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
               f'с ошибками JS: {len(bad)}')
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
        return

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
    if not checked and not meta_summary:
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
    has_bugs = bool(bad or same_city or cross_city or url_dups)

    ws = wb.create_sheet('Метаданные')
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = C.err if has_bugs else C.ok

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

    # ── Секции 3-4: дубли метаданных ──
    for title_text, groups, note in (
        (f'Дубли внутри города  ({len(same_city)})', same_city,
         'Одинаковое значение у разных страниц одного поддомена.'),
        (f'Межгородские дубли (город не подставился)  ({len(cross_city)})', cross_city,
         'Полное совпадение между разными городами - шаблон не подставил город.'),
    ):
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
    notifications: list = None,    # список WebmasterNotification - добавит лист «Уведомления»
    service_issues: list = None,   # список ServiceIssue - добавит лист «Ошибки сервисов»
    autoclick: dict = None,        # итоги автокликера - добавит лист «Автокликер»
    indexing_summary: dict = None, # sitemap↔robots (п.1.7) - в лист «Индексация»
    meta_summary: dict = None,     # дубли мета/URL (п.1.8) - в лист «Метаданные»
    filters_test: dict = None,     # итоги фильтр-теста - секция на листе «Вёрстка»
    console_check: dict = None,    # ошибки JS в консоли (п.1.14) - лист «Ошибки JavaScript»
    w3c_check: dict = None,        # валидация W3C + скорость (п.1.16) - лист «Валидация и скорость»
    p404_check: dict = None,       # страница 404 (п.1.18) - лист «Страница 404»
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
    summary_text += '\nПодробности - на листе «Все детали» (фильтр по колонке «Статус»).'
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
        ('Структура страниц', 'что чинить в контенте - где нет цены, кнопок заказа, заголовка. Красное = баг.'),
        ('Индексация', 'если есть лист - расхождения сигналов страниц с robots.txt (noindex, canonical) и sitemap↔robots.'),
        ('Метаданные', 'если есть лист - title/description/H1: наличие, город, длины и дубли (в т.ч. дубли адресов).'),
        ('Заголовки и мета', 'если есть лист - единственность title/description/H1, дубли H2 и заголовки вне текста.'),
        ('Вёрстка', 'если есть лист - тег viewport, загрузка CSS, адаптивность (@media), переходы из меню шапки и работа фильтров товаров (браузерный тест).'),
        ('Разметка', 'если есть лист - OpenGraph-теги и Schema.org (крошки, компания, товар, цены, фото).'),
        ('Изображения', 'если есть лист - alt у картинок, современные форматы (webp/avif) и вес (п.1.15).'),
        ('Регион и СНГ', 'если есть лист - чужой город/телефон/почта на странице города и чистота СНГ-доменов.'),
        ('Ошибки JavaScript', 'если есть лист - страницы, где в консоли браузера есть ошибки JS (п.1.14).'),
        ('Валидация и скорость', 'если есть лист - валидность HTML/CSS (W3C) и время загрузки ресурсов по выборке (п.1.16).'),
        ('Страница 404', 'если есть лист - несуществующий адрес отдаёт 404, дизайн/тексты/навигация 404-страницы (п.1.18).'),
        ('Все детали', 'каждая проверенная страница: адрес, код ответа, статус, скорость.'),
        ('Битые тексты', 'если есть лист - страницы с незаменёнными переменными ({{city}} и т.п.).'),
        ('404 из Метрики', 'если есть лист - страницы, куда заходили люди и упёрлись в 404.'),
        ('Уведомления', 'если есть лист - письма от Яндекс.Вебмастера и GSC за выбранный период.'),
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
    _build_layout_sheet(wb, results, filters_test)

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

    # ─── Лист «Страница 404» (п.1.18) - если проверка выполнялась ──────
    _build_404_sheet(wb, p404_check)

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
            '({{city}}, %price%, undefined и т.п.) остался виден пользователю в тексте страницы. '
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
    # ЛИСТ 4: «404 из Метрики» - если есть данные
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

        # ─── Шапка таблицы на 5-й строке ───────────────────────────
        # 4-я строка - пустая разделительная
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
            warn_row = hdr_row - 1  # 4-я строка (там сейчас пусто)
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
