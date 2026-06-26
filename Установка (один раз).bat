@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка - Проверка форм

echo ============================================
echo   Установка зависимостей (нужно один раз)
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден. Установите Python 3.10+ с https://python.org
    echo     и при установке отметьте "Add Python to PATH".
    pause
    exit /b 1
)

echo [1/3] Обновляю pip...
python -m pip install --upgrade pip

echo.
echo [2/3] Ставлю библиотеки (streamlit, playwright, openpyxl и пр.)...
python -m pip install -r requirements-local.txt
if errorlevel 1 (
    echo [!] Не удалось поставить зависимости. Проверьте интернет и повторите.
    pause
    exit /b 1
)

echo.
echo [3/3] Ставлю браузер Chromium для Playwright...
python -m playwright install chromium

echo.
echo ============================================
echo   Готово! Теперь запускайте:
echo   "Запустить проверку форм.bat"
echo ============================================
pause
