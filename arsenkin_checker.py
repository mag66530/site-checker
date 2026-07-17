"""
arsenkin_checker.py - проверка индексации URL через API Арсенкина.

Инструмент «Проверка индексации URL» (arsenkin.ru/tools/indexation) массово
проверяет, есть ли страницы в индексе Яндекса и Google. Работает через API
(без браузера, без блокировок Google):

  • POST https://arsenkin.ru/api/tools/set    - поставить задачу
  • POST https://arsenkin.ru/api/tools/check  - узнать готовность
  • POST https://arsenkin.ru/api/tools/get    - забрать результат

Авторизация - заголовком Authorization: Bearer <токен> (токен вводится в поле на
странице, не в Secrets). Тело задачи:
  {"tools_name":"indexation","data":{"queries":[...url], "yandex":bool,
   "google":bool, "search_all":bool, "inurl":bool}}
Лимит API - 30 запросов/мин (429 при превышении). 1 URL × 1 ПС = 2 лимита.

Точный формат ответа get в доке не расписан, поэтому разбор сделан устойчивым
(ищем поля по нескольким именам) и в результат кладём «сырой» образец ответа -
на первом реальном прогоне сверим и при необходимости подгоним.
"""
from __future__ import annotations

import time

API_SET = 'https://arsenkin.ru/api/tools/set'
API_CHECK = 'https://arsenkin.ru/api/tools/check'
API_GET = 'https://arsenkin.ru/api/tools/get'
TOOL_NAME = 'indexation'

# Строки-маркеры статуса задачи.
DONE_MARKERS = {'done', 'ready', 'complete', 'completed', 'finished', 'success',
                'ok', 'готово', 'выполнено', 'завершено', 'завершён', 'готов'}
WORK_MARKERS = {'process', 'processing', 'progress', 'work', 'working', 'wait',
                'waiting', 'queue', 'queued', 'pending', 'running', 'new',
                'в работе', 'в очереди', 'ожидание', 'выполняется'}

# Значения «в индексе» / «нет в индексе».
YES_MARKERS = {'да', 'yes', 'y', 'true', '1', '+', 'in', 'index', 'indexed',
               'найдено', 'есть', 'проиндексировано', 'в индексе'}
NO_MARKERS = {'нет', 'no', 'n', 'false', '0', '-', 'not', 'notindex',
              'not_indexed', 'не найдено', 'отсутствует', 'не в индексе'}


def _headers(token: str) -> dict:
    return {'Authorization': f'Bearer {(token or "").strip()}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'}


def _post(url, token, payload, proxy_url=None, timeout=40):
    import requests
    proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
    r = requests.post(url, json=payload, headers=_headers(token),
                      proxies=proxies, timeout=timeout)
    return r


def _walk(obj):
    """Обойти вложенные dict/list, отдавая (key, value) для dict-узлов."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _extract_task_id(obj):
    """Найти идентификатор задачи в ответе set (имя поля заранее не известно)."""
    keys = ('task_id', 'report_id', 'reportid', 'taskid', 'id', 'hash', 'task',
            'report')
    for want in keys:
        for k, v in _walk(obj):
            if str(k).lower() == want and isinstance(v, (str, int)) and str(v):
                return v
    return None


def _extract_status(obj):
    """Строка статуса задачи из ответа check (по нескольким именам полей)."""
    for k, v in _walk(obj):
        if str(k).lower() in ('status', 'state', 'статус', 'stage') and \
                isinstance(v, (str, int)):
            return str(v).strip().lower()
    return None


def _to_bool(v):
    """Значение индексации → True/False/None (неизвестно)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, dict):
        for kk in ('index', 'indexed', 'status', 'result', 'value', 'in_index'):
            if kk in v:
                return _to_bool(v[kk])
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in YES_MARKERS:
            return True
        if s in NO_MARKERS:
            return False
    return None


def _row_field(row: dict, names):
    for n in names:
        for k, v in row.items():
            if str(k).lower() == n:
                return v
    return None


def _result_list(result):
    """Достать из ответа get список строк-результатов (форма заранее неизвестна)."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ('data', 'result', 'results', 'rows', 'items', 'queries',
                    'urls', 'report'):
            v = result.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                # dict вида {url: {...}} → список строк. ТОЛЬКО url-ключи, иначе
                # служебные ключи (y/g/resp/code) попадут в «строки».
                out = []
                for url, payload in v.items():
                    if 'http' not in str(url):
                        continue
                    if isinstance(payload, dict):
                        payload = dict(payload)
                        payload.setdefault('url', url)
                        out.append(payload)
                    else:
                        out.append({'url': url, 'value': payload})
                if out:
                    return out
        # сам dict вида {url: {...}}
        out = []
        for url, payload in result.items():
            if isinstance(payload, dict) and ('http' in str(url)):
                payload = dict(payload)
                payload.setdefault('url', url)
                out.append(payload)
        if out:
            return out
    return []


_ENGINE_Y = {'y', 'yandex', 'ya', 'yandex_index'}
_ENGINE_G = {'g', 'google', 'goo', 'google_index'}


_URL_KEYS = ('url', 'query', 'page', 'address', 'link', 'document', 'doc', 'q')
_IDX_KEYS = ('index', 'indexed', 'status', 'result', 'value', 'in_index',
             'indexation', 'is_index', 'exist', 'found')


def _engine_map(node):
    """Данные по одной ПС → {url: bool}. Узел бывает разной формы:
    {url: verdict}, {idx: {url, index}}, [{url, index}], ['url', ...]."""
    out = {}
    items = []
    if isinstance(node, dict):
        for k, v in node.items():
            if 'http' in str(k):
                out[str(k)] = _to_bool(v)     # {url: verdict}
            else:
                items.append(v)               # {idx: {...}} - соберём значения
    elif isinstance(node, list):
        items = node
    for item in items:
        if isinstance(item, dict):
            u = _row_field(item, _URL_KEYS)
            b = _to_bool(_row_field(item, _IDX_KEYS))
            if b is None:
                b = _to_bool(item)
            if u is not None:
                out[str(u)] = b
        elif isinstance(item, str) and 'http' in item:
            out[item] = True          # список = проиндексированные URL
    return out


def _find_engine_nodes(result):
    """Найти узлы с данными по ПС (ключи y/g) в ответе get."""
    nodes = [result]
    if isinstance(result, dict):
        for k in ('data', 'result', 'results', 'report', 'response'):
            if isinstance(result.get(k), dict):
                nodes.append(result[k])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        y = g = None
        for k, v in node.items():
            kl = str(k).lower()
            if kl in _ENGINE_Y:
                y = v
            elif kl in _ENGINE_G:
                g = v
        if y is not None or g is not None:
            return y, g
    return None, None


def parse_result(result, want_yandex=True, want_google=True) -> list:
    """Нормализовать ответ get в список {url, yandex, google}.

    Поддерживает два формата: (1) сгруппировано по ПС - {y:{url:...}, g:{...}};
    (2) построчно по URL - [{url, yandex, google}]."""
    # Формат 1: по поисковым системам (как реально отдаёт Арсенкин).
    y_node, g_node = _find_engine_nodes(result)
    if y_node is not None or g_node is not None:
        ymap = _engine_map(y_node) if (want_yandex and y_node is not None) else {}
        gmap = _engine_map(g_node) if (want_google and g_node is not None) else {}
        urls = list(dict.fromkeys(list(ymap.keys()) + list(gmap.keys())))
        rows = [{'url': u,
                 'yandex': ymap.get(u) if want_yandex else None,
                 'google': gmap.get(u) if want_google else None} for u in urls]
        if rows:
            return rows
    # Формат 2: построчно.
    rows = []
    for item in _result_list(result):
        if not isinstance(item, dict):
            continue
        url = _row_field(item, ('url', 'query', 'page', 'address', 'link'))
        if url is None:
            continue
        y = _to_bool(_row_field(
            item, ('yandex', 'y', 'yandex_index', 'yandex_indexed',
                   'index_yandex', 'ya'))) if want_yandex else None
        g = _to_bool(_row_field(
            item, ('google', 'g', 'google_index', 'google_indexed',
                   'index_google', 'goo'))) if want_google else None
        rows.append({'url': str(url), 'yandex': y, 'google': g})
    return rows


def run_indexation(token, urls, *, yandex=True, google=True, search_all=True,
                   inurl=False, log=None, proxy_url=None, poll_sec=3,
                   max_wait_sec=300) -> dict:
    """Поставить задачу индексации, дождаться и вернуть результат.

    urls - список URL (или строка, разделённая переводами строк).
    Возвращает dict для отчёта: {available, checked, not_indexed, rows, ...}."""
    def _log(m):
        if log:
            log(m)

    if isinstance(urls, str):
        urls = [u.strip() for u in urls.splitlines()]
    urls = [u for u in (urls or []) if u]
    if not (token or '').strip():
        return {'available': False, 'error': 'не указан API-токен Арсенкина'}
    if not urls:
        return {'available': False, 'error': 'не указаны URL для проверки'}
    if not (yandex or google):
        return {'available': False, 'error': 'не выбрана ни одна поисковая система'}

    payload = {'tools_name': TOOL_NAME,
               'data': {'queries': urls, 'yandex': bool(yandex),
                        'google': bool(google), 'search_all': bool(search_all),
                        'inurl': bool(inurl)}}
    try:
        import requests  # noqa: F401
    except Exception:
        return {'available': False, 'error': 'модуль requests недоступен'}

    # 1) поставить задачу
    try:
        r = _post(API_SET, token, payload, proxy_url)
    except Exception as e:  # noqa: BLE001
        return {'available': False, 'error': f'сеть недоступна: {e}'}
    if r.status_code in (401, 403):
        return {'available': False, 'error': 'API-токен не принят (401/403) - '
                                             'проверь токен Арсенкина'}
    if r.status_code == 429:
        return {'available': False, 'error': 'превышен лимит запросов Арсенкина '
                                             '(429) - попробуй позже'}
    if r.status_code >= 400:
        return {'available': False,
                'error': f'set вернул {r.status_code}: {r.text[:200]}'}
    try:
        set_json = r.json()
    except Exception:
        return {'available': False, 'error': f'set вернул не JSON: {r.text[:200]}'}
    task_id = _extract_task_id(set_json)
    if task_id is None:
        return {'available': False,
                'error': f'не нашёл task_id в ответе set: {str(set_json)[:300]}'}
    _log(f'Задача поставлена (id={task_id}), URL: {len(urls)}, жду результат…')

    # 2-3) Опрашиваем РЕЗУЛЬТАТ напрямую (get): статус check в доке не
    # формализован, а get отдаёт данные сразу, как задача готова - так быстрее.
    # Первый ответ пишем в лог (сверить формат). id шлём под несколькими именами
    # (лишние игнорируются).
    id_body = {'task_id': task_id, 'id': task_id, 'report_id': task_id}
    deadline = time.time() + max_wait_sec
    rows, result_json, gj = [], None, None
    logged_real = False
    polls = 0
    while time.time() < deadline:
        try:
            rg = _post(API_GET, token, id_body, proxy_url)
            if rg.status_code == 429:
                time.sleep(8)
                continue
            gj = rg.json()
        except Exception:
            time.sleep(poll_sec)
            continue
        # Пока задача считается, get отдаёт {"code":"RESULT_ERROR",...}. Логируем
        # ПЕРВЫЙ настоящий (не-ошибочный) ответ - это и есть формат результата.
        _is_err = (isinstance(gj, dict)
                   and str(gj.get('code', '')).upper() == 'RESULT_ERROR')
        if not logged_real and not _is_err:
            logged_real = True
            _log(f'  [сырой ответ get] {str(gj)[:1500]}')
        if not _is_err:
            rows = parse_result(gj, want_yandex=yandex, want_google=google)
            if rows:
                result_json = gj
                break
        polls += 1
        if polls == 1 or polls % 6 == 0:
            _log(f'  считается… ({int(polls * poll_sec)} c)')
        time.sleep(poll_sec)

    if not rows:
        return {'available': False,
                'error': f'результат не готов/не разобран за {max_wait_sec} c',
                'raw_sample': str(gj)[:1500]}

    ni_y = sum(1 for r in rows if yandex and r['yandex'] is False)
    ni_g = sum(1 for r in rows if google and r['google'] is False)
    not_indexed = sum(1 for r in rows
                      if (yandex and r['yandex'] is False)
                      or (google and r['google'] is False))
    _log(f'Готово: проверено {len(rows)}, не в индексе {not_indexed} '
         f'(Яндекс {ni_y}, Google {ni_g})')
    return {'available': True, 'checked': len(rows), 'rows': rows,
            'not_indexed': not_indexed, 'not_indexed_yandex': ni_y,
            'not_indexed_google': ni_g,
            'engines': {'yandex': bool(yandex), 'google': bool(google)},
            'task_id': task_id, 'raw_sample': str(result_json)[:1000]}
