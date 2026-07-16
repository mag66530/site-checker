@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Посмотреть GSC в браузере

rem ── Python ───────────────────────────────────────────────
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден.
    echo     Установи Python 3.11+ с https://python.org
    echo     При установке ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH",
    echo     потом запусти этот файл ещё раз.
    pause
    exit /b 1
)

rem ── Библиотеки: ставим один раз (если ещё не стоят) ──────
%PY% -c "import playwright, aiohttp, openpyxl" 2>nul
if errorlevel 1 (
    echo Первый запуск - ставлю библиотеки, это пара минут, подожди...
    %PY% -m pip install --upgrade pip
    %PY% -m pip install -r requirements.txt
    %PY% -m playwright install chromium
)

echo.
echo =====================================================================
echo  ШАГ 1. Сейчас откроется отдельный Chrome.
echo    - Войди в тот Google-аккаунт, где Search Console по stalmetural.ru
echo      (входи ТОЛЬКО в него - тогда номер аккаунта будет 0).
echo    - Дождись, пока откроется Search Console.
echo    - Вернись в это чёрное окно и нажми Enter.
echo  Chrome НЕ закрывай.
echo =====================================================================
echo.
pause
%PY% gsc_save_session.py

echo.
echo =====================================================================
echo  ШАГ 2. Теперь смотрю GSC прямо в этом Chrome.
echo  СЛЕДИ ЗА ОКНОМ БРАУЗЕРА - он сам откроет "Индексирование - Страницы",
echo  найдёт "Не найдено (404)" и попробует экспорт.
echo =====================================================================
echo.
%PY% index_gsc_run.py --project smu --account 0

echo.
echo =====================================================================
echo  Готово. Что произошло - в тексте выше в этом окне.
echo  Если написало "нет доступа" - значит номер аккаунта другой:
echo  напиши мне, попробуем --account 1 или 2.
echo =====================================================================
pause
