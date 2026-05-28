"""
telegram_notify.py — отправка уведомлений и xlsx-отчётов в Telegram.

Архитектура:
  • Бот создаётся пользователем через @BotFather, токен лежит в Streamlit Secrets:
        telegram_bot_token = "8123456789:AAH-..."
  • Получатели хранятся по проектам:
        telegram_recipients_smu = ["1109083536", "987654321"]
        telegram_recipients_imp = [...]
        telegram_recipients_mpe = [...]
  • Поддержка прокси — если в Secrets есть proxy_url, идём через него.
    Это нужно потому что Streamlit Cloud (США) → api.telegram.org может быть медленным/недоступным.

Что отправляем:
  • Текст-сводка с метриками прогона
  • xlsx-отчёт как файл-вложение (Telegram API: sendDocument)
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Optional, Callable


TELEGRAM_API_BASE = 'https://api.telegram.org/bot'


# ── Структуры для сводки ────────────────────────────────────────────


def format_summary_message(
    project_name: str,
    started_at: str,             # "26.05.2026 19:43"
    duration_sec: int,
    total_checks: int,
    ok_count: int,
    warn_count: int,
    err_count: int,
    text_issues_count: int = 0,
    metrika_pages_count: int = 0,
    metrika_data_date: Optional[str] = None,
    top_problems: Optional[list] = None,  # список словарей {city, url, status}
) -> str:
    """
    Сформировать текст сообщения для Telegram.
    
    Использует HTML-разметку Telegram (Markdown работает капризно с URL).
    Особенности: <b>, <i>, <code>, <a href="...">.
    Скобки и спецсимволы можно использовать как есть.
    """
    has_problems = err_count > 0 or warn_count > 0 or text_issues_count > 0 or metrika_pages_count > 0

    # Короткое имя проекта: "СМУ — Сталметурал" → "СМУ"
    short_name = escape_html((project_name or '').split(' — ')[0].strip())
    # Только дата — без времени и длительности
    date_only = escape_html((started_at or '').split(' ')[0])

    lines = []
    # Заголовок: "Прогон СМУ – 28.05.2026" (без иконки, дата в той же строке)
    header = f'Прогон {short_name}'
    if date_only:
        header += f' – {date_only}'
    lines.append(f'<b>{header}</b>')
    lines.append('')

    # Метрики Site Checker — каждый статус с новой строки, без символов
    lines.append(f'<b>Site Checker</b> — проверено страниц: {total_checks}')
    if ok_count > 0:
        lines.append(f'Работает: <b>{ok_count}</b>')
    if warn_count > 0:
        lines.append(f'Предупреждения: <b>{warn_count}</b>')
    if err_count > 0:
        lines.append(f'Не работает: <b>{err_count}</b>')

    # Битые переменные
    if text_issues_count > 0:
        lines.append(f'Битых переменных: <b>{text_issues_count}</b>')

    # 404 из Метрики
    if metrika_pages_count > 0:
        date_str = ''
        if metrika_data_date:
            try:
                from datetime import datetime
                d = datetime.strptime(metrika_data_date, '%Y-%m-%d')
                date_str = f' (за {d.strftime("%d.%m.%Y")})'
            except ValueError:
                date_str = f' (за {metrika_data_date})'
        lines.append('')
        lines.append(f'<b>404 из Метрики</b>{escape_html(date_str)}: <b>{metrika_pages_count}</b> страниц')

    # Топ проблемных страниц (не более 5), сгруппированы по городу, ссылки кликабельны
    if top_problems:
        lines.append('')
        lines.append('<b>Самые срочные</b>')
        by_city: dict = {}
        for p in top_problems[:5]:
            by_city.setdefault(p.get('city') or '—', []).append(p)
        quote = []
        for city, items in by_city.items():
            quote.append(f'<b>{escape_html(city)}</b>')
            for p in items:
                url = p.get('url', '')
                status = escape_html(p.get('status', ''))
                label = escape_html(_link_label(url))
                quote.append(f'— <a href="{escape_html(url)}">{label}</a> — {status}')
        lines.append('<blockquote>' + '\n'.join(quote) + '</blockquote>')

    # Финальная строка
    lines.append('')
    if has_problems:
        lines.append('📎 Полный отчёт — в прикреплённом xlsx-файле')
    else:
        lines.append('Проблем не найдено')

    return '\n'.join(lines)


def escape_html(text: str) -> str:
    """Эскейпинг для Telegram HTML parse_mode."""
    if not text:
        return ''
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _link_label(url: str) -> str:
    """Читаемая подпись для ссылки из последнего сегмента URL.

    'https://stalmetural.ru/catalog/stal-hardoks-hardox/' → 'Stal hardoks hardox'
    Если разобрать не вышло — возвращаем сам URL.
    """
    try:
        from urllib.parse import urlparse, unquote
        path = urlparse(url).path.strip('/')
        slug = path.split('/')[-1] if path else ''
        label = unquote(slug).replace('-', ' ').replace('_', ' ').strip()
        if not label:
            return url
        return label[:1].upper() + label[1:]
    except Exception:
        return url


# ── Отправка ──────────────────────────────────────────────────────


def _build_proxy_handler(proxy_url: Optional[str]):
    """Создать urllib opener с поддержкой прокси (если задан)."""
    if not proxy_url:
        return urllib.request.build_opener()
    proxies = {'http': proxy_url, 'https': proxy_url}
    proxy_handler = urllib.request.ProxyHandler(proxies)
    return urllib.request.build_opener(proxy_handler)


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    proxy_url: Optional[str] = None,
    parse_mode: str = 'HTML',
    timeout: int = 30,
) -> dict:
    """
    Отправить текстовое сообщение через Telegram Bot API.
    
    Возвращает dict с результатом от Telegram API.
    Бросает Exception если что-то пошло не так.
    """
    url = f'{TELEGRAM_API_BASE}{bot_token}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': str(chat_id),
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    
    opener = _build_proxy_handler(proxy_url)
    req = urllib.request.Request(url, data=data)
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read().decode('utf-8')
            result = json.loads(body)
            if not result.get('ok'):
                raise RuntimeError(f'Telegram API: {result.get("description", "unknown error")}')
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            err_data = json.loads(body)
            desc = err_data.get('description', body)
        except Exception:
            desc = body
        raise RuntimeError(f'Telegram API HTTP {e.code}: {desc}')


def send_document(
    bot_token: str,
    chat_id: str,
    file_path: Path,
    *,
    caption: Optional[str] = None,
    proxy_url: Optional[str] = None,
    parse_mode: str = 'HTML',
    timeout: int = 120,
) -> dict:
    """
    Отправить файл-вложение через Telegram Bot API (sendDocument).
    Использует multipart/form-data.
    """
    url = f'{TELEGRAM_API_BASE}{bot_token}/sendDocument'
    file_path = Path(file_path)
    
    if not file_path.exists():
        raise FileNotFoundError(f'Файл не найден: {file_path}')
    
    # Готовим multipart/form-data вручную (без сторонних зависимостей)
    import secrets as _sec
    boundary = '----site-checker-' + _sec.token_hex(16)
    
    file_bytes = file_path.read_bytes()
    filename = file_path.name
    
    # MIME-тип xlsx
    if filename.lower().endswith('.xlsx'):
        mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif filename.lower().endswith('.pdf'):
        mime = 'application/pdf'
    else:
        mime = 'application/octet-stream'
    
    body = BytesIO()
    
    def add_text_field(name: str, value: str):
        body.write(f'--{boundary}\r\n'.encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(value.encode('utf-8'))
        body.write(b'\r\n')
    
    add_text_field('chat_id', str(chat_id))
    if caption:
        add_text_field('caption', caption)
        add_text_field('parse_mode', parse_mode)
    
    # Файл
    body.write(f'--{boundary}\r\n'.encode())
    body.write(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    )
    body.write(f'Content-Type: {mime}\r\n\r\n'.encode())
    body.write(file_bytes)
    body.write(b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode())
    
    body_bytes = body.getvalue()
    
    opener = _build_proxy_handler(proxy_url)
    req = urllib.request.Request(url, data=body_bytes)
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    req.add_header('Content-Length', str(len(body_bytes)))
    
    try:
        with opener.open(req, timeout=timeout) as response:
            resp_body = response.read().decode('utf-8')
            result = json.loads(resp_body)
            if not result.get('ok'):
                raise RuntimeError(f'Telegram API: {result.get("description", "unknown error")}')
            return result
    except urllib.error.HTTPError as e:
        body_str = e.read().decode('utf-8', errors='replace')
        try:
            err_data = json.loads(body_str)
            desc = err_data.get('description', body_str)
        except Exception:
            desc = body_str
        raise RuntimeError(f'Telegram API HTTP {e.code}: {desc}')


def check_bot_alive(bot_token: str, *, proxy_url: Optional[str] = None, timeout: int = 15) -> dict:
    """
    Проверить что бот работает — вызываем метод getMe.
    Возвращает dict с инфой о боте или бросает Exception.
    """
    url = f'{TELEGRAM_API_BASE}{bot_token}/getMe'
    opener = _build_proxy_handler(proxy_url)
    req = urllib.request.Request(url)
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read().decode('utf-8')
            result = json.loads(body)
            if not result.get('ok'):
                raise RuntimeError(f'Telegram API: {result.get("description", "unknown error")}')
            return result.get('result', {})
    except urllib.error.HTTPError as e:
        body_str = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Telegram API HTTP {e.code}: {body_str}')


# ── Высокоуровневая функция ─────────────────────────────────────────


def send_run_notification(
    bot_token: str,
    recipients: list,
    project_name: str,
    summary_text: str,
    report_file: Optional[Path] = None,
    *,
    proxy_url: Optional[str] = None,
    log: Optional[Callable] = None,
) -> dict:
    """
    Разослать уведомление всем получателям проекта.
    
    Возвращает словарь {'sent': N, 'failed': N, 'errors': [...]}
    """
    sent = 0
    failed = 0
    errors = []
    
    for chat_id in recipients:
        chat_id = str(chat_id).strip()
        if not chat_id:
            continue
        try:
            if report_file and report_file.exists():
                # Отправляем файл с подписью (caption)
                # Telegram caption ограничен 1024 символами.
                # Режем по границе строки, чтобы не порвать HTML-теги,
                # и закрываем blockquote, если он остался открытым.
                caption = summary_text
                if len(caption) > 1024:
                    caption = caption[:1000].rsplit('\n', 1)[0]
                    if caption.count('<blockquote') > caption.count('</blockquote>'):
                        caption += '</blockquote>'
                    caption += '\n…'
                send_document(
                    bot_token, chat_id, report_file,
                    caption=caption,
                    proxy_url=proxy_url,
                )
            else:
                send_message(bot_token, chat_id, summary_text, proxy_url=proxy_url)
            
            sent += 1
            if log:
                log('info', f'✓ Отправлено в chat_id={chat_id}')
        except Exception as e:
            failed += 1
            errors.append({'chat_id': chat_id, 'error': str(e)})
            if log:
                log('warn', f'⚠ Не доставлено в chat_id={chat_id}: {e}')
    
    return {'sent': sent, 'failed': failed, 'errors': errors}
