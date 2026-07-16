@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Site Checker - локально, с видимым браузером

rem ── Python ───────────────────────────────────────────────
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден.
    echo     Поставь Python 3.11+ с https://python.org
    echo     При установке ОБЯЗАТЕЛЬНО галочка "Add python.exe to PATH",
    echo     потом запусти этот файл ещё раз.
    pause
    exit /b 1
)

rem ── Библиотеки один раз ──────────────────────────────────
%PY% -c "import streamlit, playwright, aiohttp, openpyxl" 2>nul
if errorlevel 1 (
    echo Первый запуск - ставлю библиотеки, это пара минут, подожди...
    %PY% -m pip install --upgrade pip
    %PY% -m pip install -r requirements.txt
    %PY% -m playwright install chromium
)

echo.
echo =====================================================================
echo  ШАГ 1. Открываю отдельный Chrome для входа. В нём войди:
echo     - в Google (аккаунт, где Search Console по stalmetural.ru)
echo     - и в Яндекс: открой там же webmaster.yandex.ru и войди.
echo  Chrome НЕ закрывай - проверка будет ходить именно в нём.
echo =====================================================================
echo.
%PY% open_browser.py

echo.
echo =====================================================================
echo  ШАГ 2. Открываю сайт (в браузере на localhost:8501). На сайте:
echo    слева "Чек-лист" -^> блок "Дополнительно" -^>
echo    галочка "Проверять 404 среди страниц в индексе" -^> "Запустить проверку".
echo  СМОТРИ на окно Chrome из шага 1 - проверка будет им управлять,
echo  и ты увидишь, на чём (если что) спотыкается.
echo  Чтобы всё остановить - закрой это чёрное окно.
echo =====================================================================
echo.

rem убрать вопрос про email при первом старте Streamlit
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
>"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
>>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""

%PY% -m streamlit run app.py
pause
