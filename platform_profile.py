"""
platform_profile.py - определение движка сайта и применимость правил к нему.

Зачем. Правила чек-листа писались под классические серверные CMS (Bitrix,
Magento, OpenCart): один-два собранных бандла, имена файлов с «.min», разметка
целиком в HTML ответа. На SSR-фреймворках (Next.js и подобных) часть этих
правил меряет чужую архитектуру и даёт 100% ложных срабатываний:

  • «CSS/JS не объединены» - code-splitting по роутам там СПЕЦИАЛЬНО, и
    «объединить» означает ухудшить загрузку;
  • «JS без .min в имени» - чанки именуются хешем (webpack-c9bf0eab….js) и
    минифицированы всегда, проверка по имени файла бессмысленна;
  • «большие inline-<script>» - это RSC-пейлоад (self.__next_f.push), то есть
    ДАННЫЕ страницы, а не код; вынести во внешний файл физически нельзя;
  • «много инлайн-стилей» - next/image ставит style="color:transparent" на
    КАЖДУЮ картинку, рукописного инлайна там может не быть вовсе.

Поэтому правило спрашивает не «это проект mpi?», а «применимо ли оно к этому
движку». Тогда следующий проект на том же фреймворке заработает сразу, без
правок кода - и наоборот, Bitrix-проекты продолжают проверяться как раньше.

Движок определяется по HTML самой страницы (detect_platform), а не по конфигу
проекта: у http_checker нет конфига под рукой, зато HTML есть всегда, и на
разных поддоменах/разделах движок теоретически может отличаться. Если появится
сайт, который надёжно не определяется, сюда добавится явное переопределение из
projects/<pid>.json - пока такого случая нет, и лишнюю ручку не заводим.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Определение движка по HTML ───────────────────────────────────────

# Next.js: статика лежит в /_next/static, серверные компоненты стримятся
# через self.__next_f.push, старые версии кладут __NEXT_DATA__.
_RE_NEXTJS = re.compile(
    r'/_next/static|__NEXT_DATA__|self\.__next_f', re.I)
# Nuxt (Vue): аналогичная по духу сборка, те же правила неприменимы.
_RE_NUXT = re.compile(r'/_nuxt/|__NUXT__', re.I)
_RE_BITRIX = re.compile(r'/bitrix/|PAGEN_\d|bitrix_sessid', re.I)
_RE_MAGENTO = re.compile(r'/static/version\d|Mage\.Cookies|mage/requirejs', re.I)
_RE_OPENCART = re.compile(r'index\.php\?route=|catalog/view/theme', re.I)

# Движки со сборкой «чанками» - к ним не применяются правила про объединение,
# минификацию по имени файла и размер inline-скриптов.
BUNDLED_FRAMEWORKS = ('nextjs', 'nuxt')


def detect_platform(html: Optional[str]) -> str:
    """Движок сайта по HTML: 'nextjs' | 'nuxt' | 'bitrix' | 'magento' |
    'opencart' | 'unknown'.

    Смотрим только начало документа (256КБ): признаки движка всегда в <head>
    и первых блоках <body>, а гонять regex по мегабайтному листингу дорого.
    """
    if not html:
        return 'unknown'
    head = html[:256_000]
    if _RE_NEXTJS.search(head):
        return 'nextjs'
    if _RE_NUXT.search(head):
        return 'nuxt'
    if _RE_BITRIX.search(head):
        return 'bitrix'
    if _RE_MAGENTO.search(head):
        return 'magento'
    if _RE_OPENCART.search(head):
        return 'opencart'
    return 'unknown'


# ── Применимость правил ──────────────────────────────────────────────

# Правило → движки, на которых оно НЕ применяется. Пусто/нет ключа - правило
# работает везде (поведение по умолчанию, ничего не меняется для СМУ/ИМП/МПЭ).
_NOT_APPLICABLE = {
    # Объединение CSS/JS: на chunk-сборке файлов заведомо много - это норма.
    'assets_bundling': BUNDLED_FRAMEWORKS,
    # Минификация JS по имени файла: хеш-имена без «.min», но минифицированы.
    'js_min_by_name': BUNDLED_FRAMEWORKS,
    # Размер inline-<script>: основной объём - сериализованные данные страницы.
    'inline_script_size': BUNDLED_FRAMEWORKS,
    # Число атрибутов style="": их проставляет сам фреймворк на картинках.
    'inline_style_count': BUNDLED_FRAMEWORKS,
    # Проба пагинации ?PAGEN_1=… - параметр Bitrix, на других движках его нет.
    'pagen_probe': ('nextjs', 'nuxt', 'magento', 'opencart'),
}


def rule_applies(rule: str, platform: str) -> bool:
    """Применимо ли правило к движку. Неизвестное правило или движок -
    True (безопасный дефолт: лучше показать находку, чем молча скрыть)."""
    return (platform or 'unknown') not in _NOT_APPLICABLE.get(rule, ())
