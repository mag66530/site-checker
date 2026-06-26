@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка (один раз) - Проверка форм

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден.
    echo     Установите Python 3.10+ с https://python.org
    echo     ВАЖНО: при установке отметьте "Add Python to PATH",
    echo     затем запустите этот файл снова.
    echo.
    pause
    exit /b 1
)

echo [1/3] Обновляю pip...
%PY% -m pip install --upgrade pip

echo.
echo [2/3] Ставлю библиотеки (streamlit, playwright, openpyxl и др.)...
%PY% -m pip install -r requirements-local.txt
if errorlevel 1 (
    echo [!] Не удалось поставить библиотеки. Проверьте интернет и повторите.
    pause
    exit /b 1
)

echo.
echo [3/3] Ставлю браузер Chromium для Playwright...
%PY% -m playwright install chromium

echo.
echo ============================================
echo   Готово! Теперь запускайте  START.bat
echo ============================================
pause
