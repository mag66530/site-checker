"""
gsc_login_helper.py - интерактивный вход в Google по скриншотам (для облака).

Проблема: числа «Количество страниц в ГСК» есть только в UI Search Console, а
автоматический вход бота Google блокирует (signin/rejected). Решение (как у
click-post): человек проходит вход РУКАМИ, но через скриншоты — тул на облаке
держит браузер, шлёт скрин страницы входа, человек вводит логин/пароль/код в
интерфейсе, тул печатает это в браузер. Google пускает, потому что проверку
проходит живой человек. На выходе - сохранённая сессия (storage_state).

Запускается страницей «Вход в Google» отдельным процессом:
    python gsc_login_helper.py --project smu

Общение со страницей - через файлы в cache/gsc_login/<pid>/:
    screen.png    - свежий скриншот (пишет helper);
    status.json   - {phase, prompt, url, msg} (пишет helper);
    input.txt     - что ввёл человек (пишет страница, helper печатает + Enter);
    action.txt    - команда без текста: enter / tab / refresh / back;
    session.b64   - готовая сессия base64 (пишет helper при успехе);
    stop.flag     - остановить (пишет страница).

Сессию потом использует та же проверка (runner подхватывает session.b64, если в
Secrets нет autoclick_session). Для устойчивости к перезапуску контейнера строку
можно скопировать в Secrets autoclick_session_<pid>.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

LOGIN_URL = 'https://accounts.google.com/signin/v2/identifier?hl=ru'
SESSION_MAX_SEC = 900          # держим сессию входа до 15 минут
STEP_WAIT_SEC = 180            # ждём ввод человека на каждом шаге до 3 минут


def _dir(pid: str) -> Path:
    d = ROOT / 'cache' / 'gsc_login' / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gsc_report_url(pid: str) -> str:
    try:
        from index_gsc_run import GSC_REPORT, _gsc_target
        res, acct = _gsc_target(pid)
        if res:
            return GSC_REPORT.format(acct=acct, res=res)
    except Exception:
        pass
    return 'https://search.google.com/search-console'


async def run(pid: str) -> int:
    d = _dir(pid)
    # чистим прошлые сигналы
    for f in ('input.txt', 'action.txt', 'session.b64', 'stop.flag', 'status.json'):
        try:
            (d / f).unlink(missing_ok=True)
        except Exception:
            pass

    report_url = _gsc_report_url(pid)

    def status(phase, prompt='', msg='', url=''):
        try:
            (d / 'status.json').write_text(json.dumps(
                {'phase': phase, 'prompt': prompt, 'msg': msg, 'url': url,
                 'ts': time.time()}, ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass

    status('start', msg='Запускаю браузер…')

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        # доустановка Chromium при необходимости (как в autoclick_browser)
        try:
            _path = p.chromium.executable_path
        except Exception:
            _path = None
        if not _path or not Path(_path).exists():
            status('start', msg='Ставлю браузер (~1 мин)…')
            import subprocess
            try:
                subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'],
                               check=True, capture_output=True, text=True, timeout=900)
            except Exception as e:
                status('error', msg=f'браузер не готов: {str(e)[:200]}')
                return 1

        try:
            browser = await p.chromium.launch(headless=True, args=[
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'])
        except Exception as e:
            status('error', msg=f'не запустился браузер: {str(e)[:200]}')
            return 1

        ctx = await browser.new_context(
            user_agent=UA, locale='ru-RU', timezone_id='Europe/Moscow',
            viewport={'width': 1280, 'height': 1000})
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()

        async def snap():
            try:
                await page.screenshot(path=str(d / 'screen.png'), full_page=False)
            except Exception:
                pass

        try:
            await page.goto(report_url, wait_until='domcontentloaded', timeout=45000)
        except Exception:
            try:
                await page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=45000)
            except Exception as e:
                status('error', msg=f'не открылась страница входа: {str(e)[:200]}')
                await browser.close()
                return 1

        deadline = time.time() + SESSION_MAX_SEC
        while time.time() < deadline:
            if (d / 'stop.flag').exists():
                status('stopped', msg='Остановлено')
                break

            await page.wait_for_timeout(1200)
            url = page.url
            await snap()

            # Уже в Search Console (не на страницах входа Google) → успех.
            logged_in = ('search.google.com' in url
                         and 'accounts.google.com' not in url
                         and 'signin' not in url)
            if logged_in:
                try:
                    state = await ctx.storage_state()
                    b64 = base64.b64encode(
                        json.dumps(state, ensure_ascii=False).encode('utf-8')).decode()
                    (d / 'session.b64').write_text(b64, encoding='utf-8')
                    status('done', msg='Вход выполнен, сессия сохранена', url=url)
                except Exception as e:
                    status('error', msg=f'сессия не сохранилась: {str(e)[:200]}')
                break

            # Иначе - страница входа. Просим человека ввести то, что видно на скрине.
            _rejected = 'rejected' in url
            _hint = ('Google отклонил вход - попробуй ещё раз/другой способ на скрине'
                     if _rejected else
                     'Посмотри скрин и введи, что просит Google (логин / пароль / код). '
                     'Кнопку «Далее» жми через «↵ Далее».')
            status('login', prompt=_hint, url=url)

            # ждём ввод/команду
            acted = False
            t_end = time.time() + STEP_WAIT_SEC
            while time.time() < t_end:
                if (d / 'stop.flag').exists():
                    acted = True
                    break
                inp = d / 'input.txt'
                act = d / 'action.txt'
                if inp.exists():
                    try:
                        val = inp.read_text(encoding='utf-8')
                        inp.unlink(missing_ok=True)
                        await page.keyboard.type(val.strip(), delay=45)
                        await page.wait_for_timeout(300)
                        await page.keyboard.press('Enter')
                    except Exception:
                        pass
                    acted = True
                    break
                if act.exists():
                    try:
                        a = act.read_text(encoding='utf-8').strip()
                        act.unlink(missing_ok=True)
                        if a == 'enter':
                            await page.keyboard.press('Enter')
                        elif a == 'tab':
                            await page.keyboard.press('Tab')
                        elif a == 'back':
                            await page.go_back()
                        # 'refresh' - просто пересъёмка ниже
                    except Exception:
                        pass
                    acted = True
                    break
                # периодически обновляем скрин, чтобы человек видел изменения
                await snap()
                await asyncio.sleep(1.5)

            if not acted:
                status('login', prompt='Долго нет ввода. Продолжай, когда готова.',
                       url=page.url)
            await page.wait_for_timeout(2500)   # дать странице перейти

        try:
            await browser.close()
        except Exception:
            pass
    return 0


def main():
    ap = argparse.ArgumentParser(description='Вход в Google по скриншотам (облако)')
    ap.add_argument('--project', required=True)
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(a.project)))
    except Exception as e:  # noqa: BLE001
        try:
            (_dir(a.project) / 'status.json').write_text(json.dumps(
                {'phase': 'error', 'msg': str(e)[:300]}, ensure_ascii=False),
                encoding='utf-8')
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
