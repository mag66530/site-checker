"""
goals_run.py - фоновый прогон «Проверки целей» (страница панели запускает его
как отдельный процесс, вывод пишется в лог-файл).

Запуск:
    python goals_run.py --projects smu,smu-uz [--with-forms] [--show-browser]
    python goals_run.py --project smu           # обратная совместимость (один)

Результат: cache/goals/<project>/goals_report.xlsx (листы «Сводка» и «Цели Метрики»)
для КАЖДОЙ выбранной страны.
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent


def _forms_uses_proxy(base: str) -> bool:
    """У проекта форм включён прокси? Такие сайты (напр. ИМП/inmetprom.ru,
    МПК/metpromko.ru) режут прямое подключение из дата-центра и требуют
    российский IP. Источник - ЕДИНЫЙ use_proxy из projects/<id>.json (тот же
    флаг, что у чек-листа/форм/«Проверки КП»), а не отдельная константа
    ИСПОЛЬЗОВАТЬ_ПРОКСИ из forms_tester/projects/<id>/config.py - два флага на
    один проект расходились. base - id форм-тестера (напр. 'metpromko') -
    отображаем на канонический id через proxy_config.canonical_project_id."""
    try:
        from proxy_config import canonical_project_id, project_use_proxy
        return project_use_proxy(canonical_project_id(base))
    except Exception:  # noqa: BLE001
        return False


def _stamp(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _прогнать_формы(base: str, show: bool, only_orders: bool = False) -> None:
    """Синхронно прогоняет «Проверку форм» для базового проекта (Москва) - чтобы
    цели отправки форм реально сработали и подтянулись в отчёт целей.

    only_orders=True - гоним ТОЛЬКО сквозной заказ (корзина → оформление): так
    «Проверка целей» сама подтверждает заказ-цели, не отправляя лишние формы
    (по умолчанию для проверки целей). only_orders=False - полный прогон форм.

    ВСЕ сработавшие при формах цели (в т.ч. те, что форм-движок пишет только в
    лог, а не в лист «Цели») вылавливаем прямо из вывода и сохраняем в
    cache/forms/<base>/fired_goals.json - отчёт целей их подхватит."""
    import re
    import json
    import goals_tester as gt
    # У части проектов id в «Проверке целей» и в форм-тестере РАЗНЫЕ: цели знают
    # проект как «sm», а конфиг форм лежит под «shopmet». Без перевода запуск
    # падал на разборе аргументов («--project sm» форм-тестер не знает), и цели
    # отправки форм оставались непроверенными.
    forms_pid = gt._форм_проект(base)
    _что = 'заказ (корзина → оформление)' if only_orders else 'все формы'
    _stamp(f'ФОРМЫ: запускаю прогон ({_что}) для «{forms_pid}» (Москва) - поймать цели')
    args = [sys.executable, 'forms_run.py', '--project', forms_pid, '--no-admin',
            '--check-goals']       # цели ловим ЗДЕСЬ (в «Проверке целей»)
    if only_orders:
        args.append('--only-orders')
    if show:
        args.append('--show-browser')
    _pat1 = re.compile(r'зафиксирована цель [«"]([\w\-.]+)[»"]')
    _pat2 = re.compile(r'Сработала цель:\s*([\w\-.]+)')
    # События, которые Метрика фиксирует САМА при отправке формы (page-url=
    # form://). На них держатся цели-конструкторы («Отправка формы») - в коде
    # сайта у них ничего нет. Форм-движок печатает строку с именем формы.
    _patf = re.compile(r'Метрика зафиксировала форма(?: на форме [«"]([^»"]+)[»"])?')
    # URL, до которых дошёл прогон форм (переходы + итоговый URL сценария): по ним
    # «Проверка целей» подтверждает url-цели (оформленный заказ / «спасибо»).
    _patu = re.compile(r'(?:URL сценария:|переход →)\s*(https?://\S+)')
    fired: set = set()
    urls: set = set()
    формы_метрики: set = set()      # формы, чью отправку Метрика зафиксировала
    # Прокси форм-подпроцессу: у «Проверки целей» есть свой прокси прогона
    # (GOALS_PROXY, ставит goals_check из блока «Доступ к сайту»). Форм-движок
    # читает FORMS_PROXY, а не GOALS_PROXY - поэтому для проектов, где формам
    # нужен прокси (ИМП режет дата-центр), пробрасываем его подпроцессу. Иначе
    # заказ-сценарий 403-ится, не доходит до /cart/ и корзинные url-цели красные.
    _env = os.environ.copy()
    _gp = (os.environ.get('GOALS_PROXY') or '').strip()
    if _gp and not _env.get('FORMS_PROXY') and _forms_uses_proxy(forms_pid):
        _env['FORMS_PROXY'] = _gp
        _stamp(f'ФОРМЫ: прокси прогона проброшен форм-движку ({_gp.split("@")[-1]})')
    try:
        proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env=_env)
        for line in proc.stdout:            # стримим в общий лог И ловим цели/URL
            print(line, end='', flush=True)
            for m in _pat1.finditer(line):
                fired.add(m.group(1))
            for m in _pat2.finditer(line):
                fired.add(m.group(1))
            for m in _patu.finditer(line):
                urls.add(m.group(1).rstrip('.,;'))
            for m in _patf.finditer(line):
                формы_метрики.add((m.group(1) or '').strip() or 'без имени')
        proc.wait(timeout=1800)
        # Складываем туда же, откуда отчёт целей их читает (_форм_проект).
        d = ROOT / 'cache' / 'forms' / forms_pid
        d.mkdir(parents=True, exist_ok=True)
        (d / 'fired_goals.json').write_text(
            json.dumps(sorted(fired), ensure_ascii=False), encoding='utf-8')
        (d / 'fired_urls.json').write_text(
            json.dumps(sorted(urls), ensure_ascii=False), encoding='utf-8')
        (d / 'metrika_forms.json').write_text(
            json.dumps(sorted(формы_метрики), ensure_ascii=False), encoding='utf-8')
        _stamp(f'ФОРМЫ: готово (код {proc.returncode}); поймано целей форм: '
               f'{len(fired)}, URL прогона: {len(urls)}, '
               f'отправок, зафиксированных Метрикой: {len(формы_метрики)}')
    except Exception as e:  # noqa: BLE001
        _stamp(f'ФОРМЫ: не удалось прогнать ({e}) - продолжаю без них')


_МЕТКИ = {'': 'РФ', 'uz': 'УЗ', 'az': 'АЗ', 'az2': 'АЗ-перевод', 'am': 'АМ',
          'kg': 'КГ', 'kz': 'КЗ', 'rb': 'РБ'}

_ИМЕНА = {'smu': 'СМУ - Стальметурал', 'imp': 'ИМП - Инметпром',
          'mpe': 'МПЭ - Мепэн', 'sm': 'SM - SHOPMET'}

_СТРАНЫ = {'': 'Россия', 'uz': 'Узбекистан', 'az': 'Азербайджан',
           'az2': 'Азербайджан (перевод)', 'am': 'Армения', 'kg': 'Кыргызстан',
           'kz': 'Казахстан', 'rb': 'Беларусь'}


def _метка(pid: str) -> str:
    suf = pid.split('-', 1)[1] if '-' in pid else ''
    return _МЕТКИ.get(suf, suf.upper() or 'РФ')


def _страна(pid: str) -> str:
    """Полное название страны сайта (для подписи «Проверено: …» в Telegram)."""
    suf = pid.split('-', 1)[1] if '-' in pid else ''
    return _СТРАНЫ.get(suf, suf.upper() or 'Россия')


def _сводка_для_telegram(base: str, результаты: list) -> str:
    """Короткая подпись к отчёту целей для Telegram: бренд + список проверенных
    стран (детали по целям - в самом xlsx)."""
    from telegram_notify import escape_html
    бренд = _ИМЕНА.get(base, base.upper()).split(' - ')[0].strip()
    страны = [_страна(pid) for pid, _к, _п, _м in результаты]
    части = [f'<b>Проверка целей {escape_html(бренд)}</b>']
    if страны:
        части.append(f'Проверено: {escape_html(", ".join(страны))}')
    части.append('📎 Полный отчёт - в прикреплённом xlsx-файле')
    return '\n\n'.join(части)


def main() -> int:
    ap = argparse.ArgumentParser(description='Проверка целей Яндекс.Метрики.')
    # project(s) = коды каталогов: базовый (smu/imp/mpe) или страны (smu-uz…).
    ap.add_argument('--projects', help='несколько через запятую: smu,smu-uz,smu-az')
    ap.add_argument('--project', help='один проект (обратная совместимость)')
    # По умолчанию цели форм ищем ПО КОДУ (reachGoal в HTML/JS/DOM, включая
    # открытие модалок - без отправки), а из форм гоним только сквозной заказ,
    # чтобы подтвердить url-цели («заказ оформлен»). Быстро и без ручных шагов.
    ap.add_argument('--with-forms', action='store_true',
                    help='прогнать ВСЕ формы (медленнее), чтобы поймать даже чисто-GTM цели')
    ap.add_argument('--only-orders', action='store_true',
                    help='(по умолчанию) только сквозной заказ, цели форм - по коду')
    ap.add_argument('--no-orders', action='store_true',
                    help='вообще не прогонять формы; цели форм брать из последнего прогона «Проверки форм»')
    ap.add_argument('--show-browser', action='store_true')
    a = ap.parse_args()

    projects = []
    for src in (a.projects or ''), (a.project or ''):
        for p in src.split(','):
            p = p.strip()
            if p and p not in projects:
                projects.append(p)
    if not projects:
        _stamp('✗ Не заданы проекты (--projects smu,smu-uz)')
        return 2

    import goals_tester as gt

    _stamp(f'ПРОВЕРКА ЦЕЛЕЙ СТАРТ - сайтов: {len(projects)} '
           f'({", ".join(projects)})')

    # Внешний IP прогона: некоторые сайты (напр. inmetprom.ru) отдают 403
    # запросам из дата-центра. Чтобы добавить нас в белый список, админу сайта
    # нужен именно этот IP - выводим его в лог, чтобы можно было прочитать и
    # передать. Мягко: короткий таймаут, любая ошибка не мешает прогону.
    try:
        import urllib.request as _u
        _ip = _u.urlopen('https://api.ipify.org', timeout=8).read().decode().strip()
        _stamp(f'Внешний IP прогона (для белого списка сайта): {_ip}')
    except Exception:
        _stamp('Внешний IP прогона: определить не удалось')

    try:
        sys.path.insert(0, str(ROOT / 'forms_tester'))
        from form_tester.stop_signal import make_stop_check
        stop = make_stop_check()
    except Exception:
        stop = None

    # Формы прогоняем один раз на базовый проект (цели общие для всех стран).
    # По умолчанию - ТОЛЬКО сквозной заказ (корзина → оформление): он подтверждает
    # url-цели, а цели отправки форм «Проверка целей» находит по коду (reachGoal
    # в HTML/JS/DOM) без отправки. --with-forms - полный прогон всех форм (ловит
    # даже чисто-GTM цели); --no-orders - вообще не трогать формы.
    if not a.no_orders:
        bases = []
        for p in projects:
            b = gt._базовый(p)
            if b not in bases:
                bases.append(b)
        for b in bases:
            if stop and stop():
                break
            _прогнать_формы(b, a.show_browser, only_orders=not a.with_forms)

    результаты = []
    поймано = 0
    for i, pid in enumerate(projects, 1):
        if stop and stop():
            _stamp('⛔ Остановлено')
            break
        каталог = gt.загрузить_каталог(pid)
        if not каталог:
            _stamp(f'✗ Нет каталога целей catalogs/goals-{pid}.json')
            continue
        _stamp(f'СТРАНА {i}/{len(projects)}: {каталог.get("проект")} '
               f'(счётчик {каталог.get("счётчик")}, целей: {len(каталог.get("цели", []))})')
        прогон = gt.выполнить_прогон(pid, headless=not a.show_browser,
                                     log=_stamp, stop=stop)
        _stamp(f'  сработавших идентификаторов: {len(прогон["fired"])}')
        поймано += len(прогон['fired'])
        результаты.append((pid, каталог, прогон, _метка(pid)))

    # Один сводный отчёт: лист «Сводка» + по листу целей на каждый сайт.
    base = gt._базовый(projects[0])
    out = ROOT / 'cache' / 'goals' / base / 'goals_report.xlsx'
    if результаты:
        gt.построить_сводный_отчёт(результаты, out)
        _stamp(f'Отчёт (сводный, {len(результаты)} лист(ов) целей): {out}')

        # Telegram: шлём сводный отчёт получателям проекта (креды - в окружении,
        # их проставляет страница из секретов). Без настроенного TG - тихо пропуск.
        try:
            import telegram_notify as tn
            import datetime as _dt
            текст = _сводка_для_telegram(base, результаты)
            # Диск: <Год>/Сайт чекер/<Месяц>/<Дата>/Проверка целей/<файл>.
            if out.is_file():
                try:
                    import drive_reports
                    _d = drive_reports.upload_from_env(
                        str(out), 'Проверка целей', log=_stamp)
                    if _d.get('link'):
                        текст += (f'\n\n📁 <a href="{_d["link"]}">Отчёт на '
                                  f'Google Диске</a>')
                except Exception as _e:  # noqa: BLE001
                    _stamp(f'⚠ Google Диск: {_e}')
            _дата = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5))).strftime('%d.%m.%Y')
            res = tn.send_report_from_env(
                project_name=_ИМЕНА.get(base, base.upper()),
                summary_text=текст, report_file=out if out.is_file() else None,
                report_filename=f'Goals-{base}-{_дата}.xlsx',
                log=lambda lvl, msg: _stamp(msg))
            if not res.get('skipped'):
                _stamp(f'✓ Telegram: отправлено {res.get("sent", 0)}, '
                       f'не доставлено {res.get("failed", 0)}')
        except Exception as e:  # noqa: BLE001
            _stamp(f'⚠ Telegram-отправка не удалась ({e}) - отчёт всё равно готов.')

    _stamp(f'Всего сработавших идентификаторов по сайтам: {поймано}')
    # УНИКАЛЬНЫЙ финал именно для целей: форм-прогон (--with-forms) пишет своё
    # «✅ ВСЁ ГОТОВО» в тот же лог, и страница не должна принять его за конец целей.
    _stamp('🏁 ПРОВЕРКА ЦЕЛЕЙ ЗАВЕРШЕНА')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
