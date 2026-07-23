# Перенос авторизации OpenGAR → site-checker

Пакет собран из `auth/db.py`, `auth/security.py`, `auth/ui.py` OpenGAR
(https://github.com/... GAR-main), урезан под site-checker: без gated-вкладок
(`tab_access`), без статистики прогонов (`run_stats`), без автосейвов/секретов —
этого в site-checker нет и не нужно. БД — **своя**, новый Supabase-проект
(не общая с GAR).

Роли: `admin` / `manager` / `specialist`. Сотрудник регистрируется по
инвайт-коду от руководителя; руководитель — сам, заявка уходит админу на
одобрение. Проекты = ключи файлов `projects/*.json` (`id` внутри файла).

---

## Шаг 1 — новый Supabase-проект

1. https://supabase.com → **Sign Up** (тем же GitHub-аккаунтом, что и у
   site-checker, если хочешь единый вход в дашборд).
2. **New Project**: имя `site-checker`, придумать и **сохранить** пароль базы
   (понадобится в connection string), регион — любой ближайший, дефолт подойдёт.
3. Подождать ~2 минуты, пока проект поднимется.

## Шаг 2 — схема БД

Дашборд проекта → **SQL Editor → New query** → вставить содержимое
`schema.sql` из этой папки целиком → **Run**.

Проверка: слева **Table Editor** должны появиться `users`, `user_projects`,
`invite_codes`, `password_resets`.

## Шаг 3 — connection string

**Project Settings → Database → Connection string → Transaction pooler**
(именно Transaction, НЕ Session, НЕ Direct connection — порт должен быть
**6543**). Скопировать строку, подставить пароль из шага 1 вместо `[YOUR-PASSWORD]`.

## Шаг 4 — Fernet-ключ

В любом терминале с Python:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Сохранить вывод — это `app.fernet_key`.

## Шаг 5 — секреты

Скопировать `secrets.example.toml` → `.streamlit/secrets.toml` в репозитории
site-checker, заполнить реальными значениями:
- `supabase.db_url` — из шага 3
- `seed_admin.email` / `seed_admin.password` — твой первый вход как админ
- `app.fernet_key` — из шага 4
- `app.base_url` — адрес будущего деплоя (можно поставить временный, поправить
  после первого деплоя на Streamlit Cloud)
- `smtp.*` — опционально, только если нужен "Забыли пароль?" по email

Убедиться, что `.streamlit/secrets.toml` в `.gitignore` (реальные секреты в git
не идут).

## Шаг 6 — файлы кода

Скопировать из этой папки в репозиторий site-checker:
```
auth/__init__.py
auth/db.py
auth/security.py
auth/email_utils.py
auth/ui.py
```
(папки `auth/` в site-checker сейчас нет — создать.)

## Шаг 7 — зависимости

В `requirements.txt` site-checker добавить строки из `requirements-auth.txt`.

## Шаг 8 — интеграция в app.py

По образцу `app_integration_snippet.py`: `import auth`, `auth.require_login()`
+ `st.stop()` + `auth.render_account_ui()` вставить ПЕРЕД
`st.navigation(pages).run()` в их текущем `app.py`.

## Шаг 9 — проверка локально

```bash
streamlit run app.py
```
- Первый запуск молча создаёт seed-админа (без формы, просто по секретам).
- Логинишься `seed_admin.email` / `seed_admin.password`.
- В сайдбаре — «⚙️ Админ-панель» → вкладка «Создать пользователя» → создать
  тестового manager.
- Выйти → войти под manager → сайдбар → «🗂 Кабинет руководителя» →
  сгенерировать инвайт-код.
- Выйти → на вкладке «Зарегистрироваться» ввести код → создать specialist.
- Проверить, что после логина видно `projects` из назначенных руководителем.

## Шаг 10 — деплой

Streamlit Cloud → приложение site-checker → **Settings → Secrets** → вставить
финальный `secrets.toml` (с реальным `base_url` деплоя). Redeploy.

---

## Известные ограничения этой версии

- Доступ к вкладкам/страницам одинаковый для всех ролей — нет аналога
  GAR-шного `tab_access` (gated-вкладки). Если понадобится ограничивать
  какие-то `checklists/*.py` по ролям — это отдельная небольшая доработка
  поверх `auth.current_user()["role"]`.
- Проверка `user["projects"]` внутри конкретных страниц-чекеров НЕ подключена
  автоматически — это про то, какие URL/проекты видит юзер в интерфейсе
  чекера, а не про сам вход. Подключать точечно в каждом `checklists/*.py`,
  где это нужно (см. app_integration_snippet.py).
- Статистика прогонов (`run_stats` в GAR) не переносилась — в site-checker
  нет такого понятия «прогона». Если появится — заводить отдельно.
