"""
Тест проверки админки (Уровень 1) — ОТДЕЛЬНО от основного прогона.

Как пользоваться:
1. Создай файл forms_tester/projects/<проект>/admin.local.json:
   {"login": "твой_логин", "password": "твой_пароль"}
   (этот файл в .gitignore — в GitHub не попадёт)
2. Запусти:  python check_admin.py --project smu
   (по умолчанию домен основного сайта из cities.csv, дата — сегодня)

Скрипт войдёт в админку, откроет «Уведомления с форм» за сегодня и напечатает
найденные заявки. Если всё ок — вошью проверку в основной прогон с записью в Excel.
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "forms_tester"))
import admin_check as ac  # noqa: E402


def основной_домен(проект: str) -> str:
    f = ROOT / "forms_tester" / "projects" / проект / "cities.csv"
    with open(f, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("url") or "").strip().rstrip("/")
            if url:
                return url
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--domain", default="")
    ap.add_argument("--show", action="store_true", help="показывать окно браузера")
    a = ap.parse_args()

    проект_дир = ROOT / "forms_tester" / "projects" / a.project
    креды = ac.загрузить_креды(проект_дир)
    if not креды:
        print(f"✗ Нет файла {проект_дир / 'admin.local.json'} с login/password.")
        print("  Создай его: {\"login\": \"...\", \"password\": \"...\"}")
        return 2
    домен = a.domain or основной_домен(a.project)
    if not домен:
        print("✗ Не удалось определить домен (нет cities.csv?). Укажи --domain")
        return 2

    print(f"→ Проект {a.project}, домен {домен}")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=not a.show,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="ru-RU")
        page = ctx.new_page()
        try:
            html = ac.войти_и_получить(page, домен, креды["login"], креды["password"],
                                       datetime.now())
        finally:
            b.close()

    if "USER_LOGIN" in html and "pixana" not in html.lower():
        print("✗ Похоже, вход НЕ выполнен (снова форма логина). Проверь login/password.")
        return 1
    заявки = ac.разобрать_заявки(html)
    print(f"✓ Открыт список заявок. Распознано строк: {len(заявки)}")
    for z in заявки[:20]:
        print(f"   #{z['id']} {z['дата_время']} | {z['тип_формы'][:30]:30} | "
              f"{z['город']:12} | {z['имя'][:16]:16} | {z['email']}")
    if not заявки:
        print("  (за сегодня заявок нет или изменилась вёрстка — пришли свежий HTML)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
