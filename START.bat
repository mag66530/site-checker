@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Проверка форм

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден. Сначала запустите  "INSTALL (run once).bat".
    pause
    exit /b 1
)

rem -- отключаем приветственный вопрос Streamlit про email --
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    >"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

echo Запускаю «Проверку форм»... Откроется в браузере.
echo Чтобы остановить — закройте это окно.
echo.

%PY% -m streamlit run app.py
if errorlevel 1 (
    echo.
    echo [!] Не удалось запустить. Сначала выполните  "INSTALL (run once).bat".
    pause
    exit /b 1
)
pause
