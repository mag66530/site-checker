@echo off
cd /d "%~dp0"

echo ============================================================
echo  Looking for the site-checker project folder...
echo ============================================================
echo.

set "PROJ="

REM 1) same folder as this bat
if exist "gsc_save_session.py" set "PROJ=%CD%"

REM 2) common spots
if not defined PROJ if exist "%USERPROFILE%\site-checker\gsc_save_session.py" set "PROJ=%USERPROFILE%\site-checker"
if not defined PROJ if exist "%USERPROFILE%\Desktop\site-checker\gsc_save_session.py" set "PROJ=%USERPROFILE%\Desktop\site-checker"
if not defined PROJ if exist "%USERPROFILE%\Downloads\site-checker\gsc_save_session.py" set "PROJ=%USERPROFILE%\Downloads\site-checker"
if not defined PROJ if exist "%USERPROFILE%\Documents\site-checker\gsc_save_session.py" set "PROJ=%USERPROFILE%\Documents\site-checker"

REM 3) deep search under the whole user profile
if not defined PROJ (
    echo Not in the usual spots - searching your computer,
    echo this can take up to a minute, please wait...
    echo.
    for /f "delims=" %%F in ('dir /s /b "%USERPROFILE%\gsc_save_session.py" 2^>nul') do (
        if not defined PROJ set "PROJ=%%~dpF"
    )
)

if not defined PROJ (
    echo.
    echo ============================================================
    echo  Could not find the project automatically.
    echo  Send this whole window to the chat and Claude will help.
    echo ============================================================
    pause
    exit /b 1
)

REM strip trailing backslash
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"

echo Found the project here:
echo   %PROJ%
echo.

REM make sure the checker script is inside the project
if exist "webmaster_404_export.py" copy /y "webmaster_404_export.py" "%PROJ%\webmaster_404_export.py" >nul

cd /d "%PROJ%"

REM pick python launcher
set "PY=python"
where python >nul 2>nul || set "PY=py"

echo ============================================================
echo  STEP 1 of 3: opening Chrome
echo ============================================================
echo  A SEPARATE Chrome window will open now.
echo  If it asks you to log into Google - log in there, then come
echo  back to THIS black window and press Enter.
echo ============================================================
echo.
%PY% gsc_save_session.py

echo.
echo ============================================================
echo  STEP 2 of 3: log into Yandex Webmaster
echo ============================================================
echo  In the SAME Chrome window: open a new tab, go to
echo  webmaster.yandex.ru and log in there, if you are not
echo  logged in yet.
echo.
echo  When ready - press any key HERE.
echo ============================================================
pause >nul

echo.
echo ============================================================
echo  STEP 3 of 3: safe test run
echo  (downloads nothing, does not call any pages)
echo ============================================================
echo.
%PY% webmaster_404_export.py --dry-run --project mpe --limit 1

echo.
echo ============================================================
echo  DONE. Copy ALL the text in this window and send it in chat.
echo ============================================================
pause
