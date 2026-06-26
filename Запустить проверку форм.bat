@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Проверка форм

echo Запускаю «Проверка форм»...
echo Откроется в браузере. Чтобы остановить — закройте это окно.
echo.

python -m streamlit run app.py
if errorlevel 1 (
    echo.
    echo [!] Не удалось запустить. Сначала выполните "Установка (один раз).bat".
    pause
    exit /b 1
)
pause
