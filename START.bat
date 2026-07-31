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

rem -- keep localhost out of the system proxy (HTTP_PROXY is set globally) --
set "NO_PROXY=localhost,127.0.0.1,::1"
set "no_proxy=localhost,127.0.0.1,::1"

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
