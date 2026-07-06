"""
history.py - учёт ранее проверенных URL для ротации выборки.

Идея: помним какие URL проверялись за последние 7 дней. Когда формируем
случайную выборку - даём ИМ меньший вес (но НЕ исключаем совсем).

Хранилище: JSON-файл cache/history-{project_id}.json
  {
    "/catalog/balka/": 1716200000000,  # timestamp_ms последней проверки
    ...
  }

Не привязываемся к Streamlit, чтобы можно было использовать локально
и при будущей миграции.
"""
import json
import random
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
CACHE_DIR = PROJECT_ROOT / 'cache'

# Окно ротации по умолчанию (ежедневный чек-лист) - 7 дней
HISTORY_TTL_MS = 7 * 24 * 3600 * 1000

# Окно ротации еженедельного чек-листа - 30 дней: прогоны идут раз в неделю,
# и чтобы выборки соседних недель не пересекались, помнить надо дольше недели.
WEEKLY_TTL_MS = 30 * 24 * 3600 * 1000

# Сколько храним записи на диске. Должно покрывать самое длинное окно.
RETENTION_MS = WEEKLY_TTL_MS

# Вес "недавно проверенного" URL: 30% от обычного.
# То есть он не исключается полностью, но в 3 раза реже попадает в выборку.
RECENT_WEIGHT = 0.3


def _history_path(project_id: str) -> Path:
    return CACHE_DIR / f'history-{project_id}.json'


def load_history(project_id: str, ttl_ms: int = HISTORY_TTL_MS) -> dict:
    """
    Загрузить историю проверенных URL для проекта.
    ttl_ms - окно ротации: записи старше отсекаются. 7 дней для ежедневного
    чек-листа, WEEKLY_TTL_MS (30 дней) для еженедельного.
    """
    p = _history_path(project_id)
    if not p.exists():
        return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}

    # Чистим устаревшие записи (старше TTL)
    now_ms = time.time() * 1000
    return {url: ts for url, ts in data.items() if now_ms - ts < ttl_ms}


def save_history(project_id: str, urls_just_checked: list[str]) -> None:
    """
    Обновить историю: записать что эти URL только что проверены.
    На диске записи живут RETENTION_MS (30 дней), чтобы покрыть и недельное,
    и месячное окно ротации; каждое чтение фильтрует по своему ttl_ms.
    """
    history = load_history(project_id, ttl_ms=RETENTION_MS)
    now_ms = int(time.time() * 1000)
    for url in urls_just_checked:
        history[url] = now_ms

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_history_path(project_id), 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def weighted_sample(
    pool: list[str],
    n: int,
    recently_checked: set[str],
    rng: random.Random,
) -> list[str]:
    """
    Случайная выборка n штук из pool, где недавно проверенные URL
    имеют меньший вес (RECENT_WEIGHT).

    Без замены - каждый URL берётся максимум 1 раз.
    """
    if n >= len(pool):
        return list(pool)

    # Веса: 1.0 для свежих, RECENT_WEIGHT для недавно проверенных
    weights = [
        RECENT_WEIGHT if item in recently_checked else 1.0
        for item in pool
    ]

    # random.choices даёт замены - нам нужно без. Делаем через ручной алгоритм.
    # На pool < 30000 элементов простой O(n*k) подход норм.
    selected = []
    pool_copy = list(pool)
    weights_copy = list(weights)
    for _ in range(n):
        if not pool_copy:
            break
        total = sum(weights_copy)
        if total <= 0:
            # Все веса 0 - равновероятно
            idx = rng.randrange(len(pool_copy))
        else:
            r = rng.uniform(0, total)
            cum = 0.0
            idx = len(pool_copy) - 1
            for i, w in enumerate(weights_copy):
                cum += w
                if cum >= r:
                    idx = i
                    break
        selected.append(pool_copy.pop(idx))
        weights_copy.pop(idx)

    return selected
