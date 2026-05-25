"""
reporter.py — формирование xlsx-отчёта.

Структура (как в Node.js версии):
  • Лист «Обзор» — метрики, сводка, параметры прогона
  • Лист «Все детали» — каждая проверка отдельной строкой
  • Лист «Битые тексты» — добавляется ТОЛЬКО если есть находки

Колонки в «Все детали»:
  Город | Поддомен | Тип | URL | Код | Статус |
  Скорость, с | Оценка скорости | Битые переменные | Откуда перешли
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter


# ── Стили (цвета как в Node.js версии) ──────────────────────────────


class C:
    text = '09090B'
    text_soft = '3F3F46'
    text_muted = '71717A'
    border = 'D4D4D8'
    border_light = 'E4E4E7'
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


# ── Главная функция ────────────────────────────────────────────────


def build_report(
    *,
    project_name: str,
    started_at_ms: int,
    finished_at_ms: int,
    selected_subdomains: list,    # список Subdomain
    results: list,                 # список CheckResult
    output_path: Path | str,
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
        ('D', 'ПРЕДУПРЕЖДЕНИЯ', warn_count, C.warn),
        ('E', 'НЕ РАБОТАЕТ', err_count, C.err),
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
    ws1.row_dimensions[sum_body_row].height = 64 if total_text_issues > 0 else 50
    ws1.merge_cells(f'B{sum_body_row}:E{sum_body_row}')
    c = ws1[f'B{sum_body_row}']
    summary_text = (
        f'Из {total} проверенных страниц: '
        f'{ok_count} работают, {warn_count} с перенаправлениями, {err_count} не открываются.'
    )
    if total_text_issues > 0:
        summary_text += (
            f'\nДополнительно: на {len(pages_with_issues)} страницах найдено '
            f'{total_text_issues} битых переменных в текстах — см. лист «Битые тексты».'
        )
    summary_text += '\nПодробности — на листе «Все детали» (фильтр по колонке «Статус»).'
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

    # ═══════════════════════════════════════════════════════════════
    # ЛИСТ 2: Все детали
    # ═══════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet('Все детали')
    ws2.sheet_view.showGridLines = False
    ws2.freeze_panes = 'A2'

    headers = [
        ('Город', 18), ('Поддомен', 28), ('Тип', 12), ('URL', 55),
        ('Код', 8), ('Статус', 22), ('Скорость, с', 12),
        ('Оценка скорости', 18), ('Битые переменные', 18),
        ('Откуда перешли', 50),
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
            r.city,                            # A
            r.subdomain,                       # B
            r.type_label,                      # C
            r.url,                             # D
            r.http_code if r.http_code else '–',  # E
            STATUS_LABEL.get(r.status, r.status),  # F
            speed_sec,                         # G
            speed_label,                       # H
            text_issue_text,                   # I
            _build_path_description(r),        # J
        ]

        for col_idx, value in enumerate(values, 1):
            cell = ws2.cell(row=row_idx, column=col_idx)
            cell.value = value
            cell.font = _font(size=10)
            cell.alignment = _align(wrap=True)
            cell.border = _border(color=C.border_light)

        # Спец-шрифты для отдельных колонок
        ws2.cell(row=row_idx, column=2).font = _font(name='Consolas', size=10, color=C.text_muted)

        # URL — кликабельная гиперссылка
        url_cell = ws2.cell(row=row_idx, column=4)
        url_cell.hyperlink = r.url
        url_cell.font = _font(name='Consolas', size=10, color=C.accent, underline='single')

        # Откуда перешли — моноширинный для цепочек, курсивный для прямых
        path_cell = ws2.cell(row=row_idx, column=10)
        if r.redirect_chain:
            path_cell.font = _font(name='Consolas', size=9, color=C.text_soft)
        elif not r.is_ok:
            path_cell.font = _font(size=10, italic=True, color=C.text_muted)

        # Битые переменные — подсветка
        if r.has_text_issues:
            issue_cell = ws2.cell(row=row_idx, column=9)
            issue_cell.font = _font(size=10, bold=True, color=C.warn)
            issue_cell.fill = _fill(C.warn_soft)

        # Оценка скорости — цвет по уровню
        if r.speed_rating:
            speed_cell = ws2.cell(row=row_idx, column=8)
            color = SPEED_COLOR[r.speed_rating]
            bold = r.speed_rating in ('slow', 'very_slow')
            speed_cell.font = _font(size=10, bold=bold, color=color)

        # Статус — цвет по результату
        status_color = C.ok if r.is_ok else C.warn if r.is_warning else C.err
        ws2.cell(row=row_idx, column=6).font = _font(size=10, bold=True, color=status_color)

    ws2.auto_filter.ref = f'A1:J{len(sorted_results) + 1}'

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
            'Чтобы увидеть проблему — откройте URL и поищите по странице (Ctrl+F) то, '
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

                # URL — кликабельный
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

    # ── Сохраняем ──────────────────────────────────────────────────
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


# ── Утилита для имени файла ─────────────────────────────────────────


def make_report_filename(project_id: str, started_at_ms: int, reports_dir: Path) -> str:
    """
    Имя файла: smu-21.05.2026.xlsx
    Если уже есть — smu-21.05.2026_2.xlsx, _3 и т.д.
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
