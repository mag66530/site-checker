"""
wordstat_arsenkin.py - частотность фраз Яндекс.Вордстата через API Арсенкина.

Тем же токеном и тем же протоколом, что и проверка индексации
(arsenkin_checker): set → get, авторизация Bearer. Отличается только задача:

  {"tools_name":"wordstat",
   "data":{"type":1, "queries":[...фразы], "device":"",
           "regions":[0], "ws":["base","quoted","overal","exact"]}}

Типы частотности (имена сверены с живым ответом 21.08.2026 - в справке они
названы иначе, чем в API, поэтому проверялось перестановкой слов):
  base   - базовая (WS)        : все формы в любом порядке + доп. слова;
  quoted - фразовая ("WS")     : только эти слова, любые формы и порядок;
  overal - точная ("!WS")      : слова в конкретной форме, любой порядок;
  exact  - уточнённая ("[!WS]"): точные формы и СТРОГИЙ порядок слов.

Проверка на паре перестановок (регион «Весь мир»):
  труба бесшовная  base 48566  quoted 856  overal 575  exact 371
  бесшовная труба  base 48566  quoted 856  overal 575  exact 204
Порядок слов меняет только exact - значит именно он и есть «[!фраза]».

Фразы отправляем БЕЗ операторов: операторы Вордстата Арсенкин навешивает сам
по списку ws. Строка «[!труба бесшовная]» в queries даёт двойное обрамление и
нули в ответе (проверено там же).

Для проверки «ключ в title стоит в самой популярной последовательности слов»
нужна именно УТОЧНЁННАЯ: только она различает «труба бесшовная» и «бесшовная
труба». Названия полей в ответе в доке не формализованы, поэтому разбор
устойчивый: значение ищем по нескольким именам, а какой ключ отвечает за
«[!WS]», определяем по имени поля (см. _ЧАСТОТА_ПОЛЯ).

Регион: 0 = «Все регионы» (в задаче максимум 4 региона).
Одна задача принимает до 10 000 фраз, поэтому весь прогон укладывается в один
запрос - лимит API (30 запросов/мин) не расходуется впустую.
"""
from __future__ import annotations

import time

from arsenkin_checker import (API_GET, API_SET, _extract_task_id, _post, _walk)

TOOL_NAME = 'wordstat'
ВСЕ_РЕГИОНЫ = 0
ТИПЫ_ЧАСТОТЫ = ('base', 'quoted', 'overal', 'exact')
МАКС_ФРАЗ = 10000

# Как зовут частотность в ответе. Порядок внутри кортежа = приоритет.
_ЧАСТОТА_ПОЛЯ = {
    # «уточнённая» = [!фраза]: единственная, что различает порядок слов
    'уточнённая': ('exact', 'exact_order', 'ws_order', '[!ws]', 'utochnennaya'),
    'точная': ('overal', 'overall', 'clarified', '!ws', 'tochnaya'),
    'фразовая': ('quoted', 'phrase', '"ws"', 'frazovaya'),
    'базовая': ('base', 'basic', 'ws', 'bazovaya'),
}
# Где в строке ответа лежит сама фраза.
_ФРАЗА_ПОЛЯ = ('query', 'queries', 'phrase', 'keyword', 'key', 'word', 'text',
               'фраза', 'запрос')


def _к_числу(v):
    """«1 234» / «1234» / 1234 → int, иначе None. ЧИСТАЯ функция."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or '').strip().replace(' ', '').replace(' ', '')
    s = s.replace('&nbsp;', '')
    if not s or not s.lstrip('-').isdigit():
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _частоты_строки(row: dict) -> dict:
    """{'уточнённая': int|None, 'точная': …} из строки ответа. ЧИСТАЯ функция.

    Числа бывают вложены (регион → значение), поэтому если по имени лежит не
    число, а словарь - берём первое число внутри него."""
    низ = {str(k).lower(): v for k, v in (row or {}).items()}
    из_вложенных = {}
    for k, v in низ.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                из_вложенных.setdefault(str(kk).lower(), vv)
    out = {}
    for имя, ключи in _ЧАСТОТА_ПОЛЯ.items():
        знач = None
        for k in ключи:
            if k in низ:
                знач = _к_числу(низ[k])
                if знач is None and isinstance(низ[k], dict):
                    for vv in низ[k].values():
                        знач = _к_числу(vv)
                        if знач is not None:
                            break
            if знач is None and k in из_вложенных:
                знач = _к_числу(из_вложенных[k])
            if знач is not None:
                break
        out[имя] = знач
    return out


def _фраза_строки(row: dict) -> str:
    низ = {str(k).lower(): v for k, v in (row or {}).items()}
    for k in _ФРАЗА_ПОЛЯ:
        v = низ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0].strip()
    return ''


def parse_frequencies(result) -> dict:
    """Ответ get → {фраза (нижний регистр): {'уточнённая': int|None, …}}.
    ЧИСТАЯ функция (юнит-тест без сети).

    Живой ответ (сверено 21.08.2026) кладёт частотности в словарь «фраза →
    список из одной записи»:

        {"code": "TASK_RESULT", "result": {"data": {"regions": ["Весь мир"],
          "result": {"труба бесшовная": [{"base": 12275, "quoted": 380,
                                          "overal": 61, "exact": 74}], …}}}}

    То есть фразы тут - КЛЮЧИ, а не поле внутри записи. Разбираем оба вида
    (ключ-фраза и запись с полем query): дока формат не фиксирует, а ломаться
    от перестановки полей проверка не должна."""
    out = {}

    def _числа_из(значение):
        """Частотности из значения ключа-фразы: dict или список из одной
        записи. None - если чисел там нет (значит это не запись частотности)."""
        запись = значение
        if isinstance(запись, list):
            запись = next((x for x in запись if isinstance(x, dict)), None)
        if not isinstance(запись, dict):
            return None
        числа = _частоты_строки(запись)
        return числа if any(v is not None for v in числа.values()) else None

    def _собрать(node):
        if isinstance(node, dict):
            фраза = _фраза_строки(node)
            свои = _частоты_строки(node) if фраза else {}
            if фраза and any(v is not None for v in свои.values()):
                out.setdefault(фраза.lower(), свои)
            for k, v in node.items():
                if isinstance(k, str) and k.strip():
                    числа = _числа_из(v)
                    if числа is not None:
                        out.setdefault(k.strip().lower(), числа)
                _собрать(v)
        elif isinstance(node, list):
            for v in node:
                _собрать(v)

    _собрать(result)
    return out


def собрать_частотности(token, фразы, *, regions=(ВСЕ_РЕГИОНЫ,), device='',
                        log=None, proxy_url=None, poll_sec=5,
                        max_wait_sec=600) -> dict:
    """Частотности фраз через API Арсенкина.
    → {available, frequencies: {фраза: {тип: число}}, error, raw_sample}."""
    def _log(m):
        if log:
            log(m)

    фразы = [str(f).strip() for f in (фразы or []) if str(f or '').strip()]
    # Дубли фраз лишние: одна и та же комбинация встречается у разных страниц.
    фразы = list(dict.fromkeys(фразы))[:МАКС_ФРАЗ]
    if not (token or '').strip():
        return {'available': False, 'error': 'не указан API-токен Арсенкина'}
    if not фразы:
        return {'available': False, 'error': 'нет фраз для проверки частотности'}

    payload = {'tools_name': TOOL_NAME,
               'data': {'type': 1, 'queries': фразы, 'device': device or '',
                        'regions': list(regions)[:4],
                        'ws': list(ТИПЫ_ЧАСТОТЫ)}}
    try:
        r = _post(API_SET, token, payload, proxy_url)
    except Exception as e:  # noqa: BLE001
        return {'available': False, 'error': f'сеть недоступна: {e}'}
    if r.status_code in (401, 403):
        return {'available': False,
                'error': 'API-токен не принят (401/403) - проверь токен Арсенкина '
                         'и что тариф даёт доступ к API Вордстата'}
    if r.status_code == 429:
        return {'available': False,
                'error': 'превышен лимит запросов Арсенкина (429) - попробуй позже'}
    if r.status_code >= 400:
        return {'available': False,
                'error': f'set вернул {r.status_code}: {r.text[:200]}'}
    try:
        set_json = r.json()
    except Exception:  # noqa: BLE001
        return {'available': False, 'error': f'set вернул не JSON: {r.text[:200]}'}
    task_id = _extract_task_id(set_json)
    if task_id is None:
        return {'available': False,
                'error': f'не нашёл task_id в ответе set: {str(set_json)[:300]}'}
    _log(f'Частотность: задача поставлена (id={task_id}), фраз: {len(фразы)}…')

    id_body = {'task_id': task_id, 'id': task_id, 'report_id': task_id}
    дедлайн = time.time() + max_wait_sec
    частоты, gj, показан = {}, None, False
    опросов = 0
    while time.time() < дедлайн:
        try:
            rg = _post(API_GET, token, id_body, proxy_url)
            if rg.status_code == 429:
                time.sleep(8)
                continue
            gj = rg.json()
        except Exception:  # noqa: BLE001
            time.sleep(poll_sec)
            continue
        ошибка = (isinstance(gj, dict)
                  and str(gj.get('code', '')).upper() == 'RESULT_ERROR')
        if not ошибка:
            if not показан:
                показан = True
                _log(f'  [сырой ответ get] {str(gj)[:1200]}')
            найдено = parse_frequencies(gj)
            if найдено:
                частоты = найдено
                готово = isinstance(gj, dict) and gj.get('finished_at')
                if готово or len(частоты) >= len(фразы):
                    break
        опросов += 1
        if опросов == 1 or опросов % 6 == 0:
            _log(f'  считается… ({int(опросов * poll_sec)} c)')
        time.sleep(poll_sec)

    if not частоты:
        return {'available': False,
                'error': f'частотность не готова/не разобрана за {max_wait_sec} c',
                'raw_sample': str(gj)[:1500]}
    _log(f'Частотность собрана: фраз {len(частоты)} из {len(фразы)}.')
    return {'available': True, 'frequencies': частоты, 'task_id': task_id,
            'raw_sample': str(gj)[:1000]}
