"""
convert_kp.py - разовая конвертация «Карты присутствия» (xlsx) в компактный
catalogs/{proj}-kp.csv для сверки контактов.

Запуск:
    python convert_kp.py smu /путь/к/КП_СМУ.xlsx
    python convert_kp.py imp /путь/к/КП_ИМП.xlsx
    python convert_kp.py mpe /путь/к/КП_МПЭ.xlsx

В CSV кладём ТОЛЬКО контактные поля (домен, город, телефоны SEO/реклама/общий,
почта, адрес) - исходный xlsx с внутренними данными в репозиторий не идёт.
"""
import csv
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

from kp import KP_LAYOUT, CATALOGS_DIR, _norm_host
from kp import split_phones as _split_phones

# Домены, которые НЕ проверяем нигде (по просьбе заказчика) - даже если они есть
# в КП-таблице. Строку с таким доменом пропускаем при сборке CSV.
_EXCLUDE_HOSTS = {'steemet.uz'}


def _phone_columns(headers):
    """Индексы всех телефонных колонок (Общий/Реклама/SEO/Сотовый/основной/
    подменные/ватсап). Городскую колонку «Город» (название города) исключаем."""
    out = []
    for i, h in enumerate(headers):
        if h is None:
            continue
        ht = str(h).lower().replace('\n', ' ').strip()
        if ht == 'город':
            continue
        if any(k in ht for k in ('город', 'сотов', 'мобильн', 'основн',
                                 'подменн', 'ватсап', 'для ватсап')):
            out.append(i)
    return out


def _find_header_row(ws, max_scan=6):
    """Найти строку заголовков - где встречаются 'город' и 'адрес'."""
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan, values_only=True), 1):
        cells = [str(c).lower() if c else '' for c in row]
        joined = ' '.join(cells)
        if 'город' in joined and 'адрес' in joined:
            return i, row
    # запасной вариант - первая строка
    first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return 1, first


def _col(headers, *keywords, exact=None):
    """Индекс колонки, чей заголовок содержит все keywords (или равен exact)."""
    for i, h in enumerate(headers):
        if h is None:
            continue
        ht = str(h).lower().replace('\n', ' ').strip()
        if exact is not None and ht == exact:
            return i
        if keywords and all(k in ht for k in keywords):
            return i
    return None


def _sheet_has_kp_header(ws, max_scan=6):
    """Похож ли лист на КП: в первых строках есть и 'город', и 'адрес'."""
    for row in ws.iter_rows(min_row=1, max_row=max_scan, values_only=True):
        joined = ' '.join(str(c).lower() for c in row if c)
        if 'город' in joined and 'адрес' in joined:
            return True
    return False


def _pick_sheet(wb, preferred: str):
    """Выбрать лист КП. Приоритет:
      1) точное имя из KP_LAYOUT;
      2) то же имя без учёта регистра/пробелов (в таблице могли переименовать
         «КП » с пробелом, «кп» строчными и т.п.);
      3) любой лист, похожий на КП (в шапке есть «город» и «адрес»).
    Если ничего не подошло - понятная ошибка со списком листов таблицы,
    чтобы сразу было видно, как называется нужная вкладка."""
    names = wb.sheetnames
    if preferred in names:
        return wb[preferred]
    norm = lambda s: str(s).lower().replace('\n', ' ').strip()
    want = norm(preferred)
    for n in names:
        if norm(n) == want:
            return wb[n]
    for n in names:
        if _sheet_has_kp_header(wb[n]):
            return wb[n]
    raise RuntimeError(
        f'в таблице нет листа «{preferred}» (и ни один лист не похож на КП). '
        f'Листы таблицы: {", ".join(names)}. Переименуйте вкладку с КП '
        f'в «{preferred}» либо укажите её название.')


def _looks_tg(v: str) -> bool:
    """Похоже на Telegram-ник (буквы), а не число/пусто."""
    v = (v or '').strip().lower()
    if not v or re.fullmatch(r'[\d.,\s]+', v):
        return False
    return bool(re.search(r'[a-z][a-z0-9_]{2,}', v))


def _looks_wa(v: str) -> bool:
    """Похоже на телефон WhatsApp (>=7 цифр)."""
    return len(re.sub(r'\D', '', v or '')) >= 7


def _msgr_value(row, cands, is_valid):
    """Значение мессенджера из СТРОКИ: среди колонок-кандидатов (в КП бывают две
    одноимённые - 'Telegram' и 'Телеграм') берём первое РЕАЛЬНОЕ (проходит
    is_valid). Нет валидного - первое непустое (дальше отфильтрует clean_msgr).
    Так ник/номер читается из любой колонки, где он реально стоит."""
    vals = []
    for c in cands:
        v = row[c] if c is not None and c < len(row) else None
        vals.append('' if v is None else str(v).strip())
    for v in vals:
        if is_valid(v):
            return v
    return next((v for v in vals if v), '')


def convert(project_id: str, xlsx_path: str) -> Path:
    layout = KP_LAYOUT[project_id]
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = _pick_sheet(wb, layout['sheet'])

    hdr_row_idx, headers = _find_header_row(ws)

    ci_city = _col(headers, exact='город') or _col(headers, 'город')
    ci_addr = _col(headers, exact='адрес') or _col(headers, 'адрес')
    # Если у колонок «город»/«страна» ПУСТОЙ заголовок (как у АПС - первые два
    # столбца без шапки) - берём их по позиции из layout (city_col/country_col).
    if ci_city is None and layout.get('city_col') is not None:
        ci_city = layout['city_col']
    ci_email = _col(headers, 'e-mail') or _col(headers, 'почта') or _col(headers, 'email')
    # ВАЖНО: сначала ТОЧНАЯ колонка «url» - иначе _col(...,'ссылка') цеплял
    # «Ссылка для яндекс-карт» (iframe карты) вместо адреса сайта, и домены
    # городов (особенно поддомены СНГ) не читались - города выпадали из проверки.
    ci_url = (_col(headers, exact='url') or _col(headers, 'url', 'магазин')
              or _col(headers, 'домен') or _col(headers, 'ссылка')
              or _col(headers, 'url'))
    ci_seo = _col(headers, *layout['phone_seo'])
    ci_ad = _col(headers, *layout['phone_ad'])
    ci_common = _col(headers, *layout['phone_common'])
    phone_cols = _phone_columns(headers)
    # Доп. переменные (пункт 1.4): страна, Telegram, WhatsApp.
    ci_country = _col(headers, exact='страна') or _col(headers, 'страна')
    if ci_country is None and layout.get('country_col') is not None:
        ci_country = layout['country_col']
    # Telegram/WhatsApp: в КП бывают ДВЕ одноимённые колонки ('Telegram' и
    # 'Телеграм') - реальный ник/номер может лежать в любой. Собираем все
    # кандидаты, значение берём по строке (см. _msgr_value).
    _tg_cands = [i for i, h in enumerate(headers)
                 if h and ('telegram' in str(h).lower() or 'телеграм' in str(h).lower())]
    _wa_cands = [i for i, h in enumerate(headers)
                 if h and any(k in str(h).lower()
                              for k in ('whatsapp', 'ватсап', 'вацап', 'ватсапп'))]

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return ''
        v = row[idx]
        return '' if v is None else str(v).strip()

    def norm_country(v):
        """Приводим синонимы страны к единому виду (в КП АПС встречается и «РФ»,
        и «Россия» - это одна страна, иначе в отчёте две)."""
        s = (v or '').strip()
        return 'Россия' if s.lower() in ('рф', 'россия', 'russia') else s

    def clean_msgr(v):
        """Мусорные значения мессенджеров в КП (#N/A, «нет», «подтвердить») → пусто."""
        v = (v or '').strip()
        return '' if v.lower() in ('нет', '-', '#n/a', 'подтвердить',
                                   'подтвердить телефон') else v

    rows_out = []
    seen = set()
    for row in ws.iter_rows(min_row=hdr_row_idx + 1, values_only=True):
        if not row or not any(row):
            continue
        city = cell(row, ci_city)
        url = cell(row, ci_url)
        # домен: из url-колонки, либо ищем по строке любой домен сети
        # (.ru/.uz/.kz/.by - у МПЭ есть Узбекистан и Казахстан)
        host = _norm_host(url)
        if not host:
            joined = ' '.join(str(c) for c in row if c)
            m = re.search(r'([a-z0-9-]+\.)*(?:inmetprom|stalmetural|mepen|aviastal|smg)\.(?:ru|uz|kz|by|az|kg|am)', joined)
            host = _norm_host(m.group(0)) if m else ''
        if host in _EXCLUDE_HOSTS:          # исключённый домен - не проверяем нигде
            continue
        # Дедуп по (домен, ГОРОД), а не только по домену: у СНГ-стран все города
        # делят один сайт (stalmetural.kz/.by/.uz - поддоменов нет), но это РАЗНЫЕ
        # города КП - храним каждый (полный список городов в отчёте «Проверка КП»).
        _key = (host, (city or '').strip().lower())
        if not host or _key in seen:
            continue
        seen.add(_key)
        # Все телефоны города (нормализованные, 10 цифр) из всех тел. колонок -
        # сайт может статически показывать любой из них (Общий/SEO/Сотовый).
        all_norm = []
        for idx in phone_cols:
            for n in _split_phones(cell(row, idx)):
                if n not in all_norm:
                    all_norm.append(n)
        rows_out.append({
            'domain': host,
            'city': city,
            'phone_seo': cell(row, ci_seo),
            'phone_ad': cell(row, ci_ad),
            'phone_common': cell(row, ci_common),
            'all_phones': ';'.join(all_norm),
            'email': cell(row, ci_email),
            'address': cell(row, ci_addr),
            'country': norm_country(cell(row, ci_country)),
            'telegram': clean_msgr(_msgr_value(row, _tg_cands, _looks_tg)),
            'whatsapp': clean_msgr(_msgr_value(row, _wa_cands, _looks_wa)),
        })
    wb.close()

    CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = CATALOGS_DIR / f'{project_id}-kp.csv'
    with open(out, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['domain', 'city', 'phone_seo',
                                          'phone_ad', 'phone_common', 'all_phones',
                                          'email', 'address',
                                          'country', 'telegram', 'whatsapp'])
        w.writeheader()
        w.writerows(rows_out)
    print(f'{project_id}: {len(rows_out)} городов → {out}')
    # маленькая сводка качества
    no_phone = sum(1 for r in rows_out if not (r['phone_seo'] or r['phone_ad'] or r['phone_common']))
    with_addr = sum(1 for r in rows_out if r['address'])
    print(f'  без телефона в КП: {no_phone}, с адресом: {with_addr}')
    return out


def main():
    if len(sys.argv) != 3:
        print('Использование: python convert_kp.py <smu|imp|mpe> <путь_к_xlsx>')
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])


if __name__ == '__main__':
    main()
