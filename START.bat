@echo off
cd /d "%~dp0"
title Proverka form

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found. Run "INSTALL (run once).bat" first.
    pause
    exit /b 1
)

rem -- disable Streamlit first-run email prompt --
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
>"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
>>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""

echo Starting... A browser tab will open.
echo To stop - just close this window.
echo.

%PY% -m streamlit run app.py
if errorlevel 1 (
    echo.
    echo [!] Could not start. Run "INSTALL (run once).bat" first.
    pause
    exit /b 1
)
pause
