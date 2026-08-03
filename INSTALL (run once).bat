@echo off
cd /d "%~dp0"
title INSTALL - Proverka form

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found.
    echo     Install Python 3.10+ from https://python.org
    echo     IMPORTANT: tick "Add Python to PATH", then run this file again.
    pause
    exit /b 1
)

echo [1/3] Upgrading pip...
%PY% -m pip install --upgrade pip

echo.
rem -- requirements.txt is the FULL app (incl. login: psycopg, bcrypt,
rem -- extra-streamlit-components, cryptography). requirements-local.txt alone
rem -- is not enough: START.bat would die with ModuleNotFoundError on import auth.
echo [2/3] Installing libraries (streamlit, playwright, login, ...)...
%PY% -m pip install -r requirements.txt -r requirements-local.txt
if errorlevel 1 (
    echo [!] Failed to install libraries. Check internet and try again.
    pause
    exit /b 1
)

echo.
echo [3/3] Installing Chromium browser for Playwright...
%PY% -m playwright install chromium

echo.
echo ============================================
echo   Done! Now run  START.bat
echo ============================================
pause
