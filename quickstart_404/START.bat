@echo off
cd /d "%~dp0"

if not exist "autoclick_browser.py" (
    echo ERROR: autoclick_browser.py not found next to this file.
    echo.
    echo This folder must be placed INSIDE the site-checker project
    echo folder - next to app.py and gsc_save_session.py.
    echo.
    echo Move webmaster_404_export.py and START.bat there, then run
    echo START.bat again from that location.
    pause
    exit /b 1
)

echo ============================================================
echo  STEP 1 of 3: opening Chrome
echo ============================================================
echo  A SEPARATE Chrome window will open now.
echo  If it asks you to log into Google - log in there, then come
echo  back to THIS black window and press Enter.
echo ============================================================
echo.
python gsc_save_session.py

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
python webmaster_404_export.py --dry-run --project mpe --limit 1

echo.
echo ============================================================
echo  DONE. Copy ALL the text above (from "STEP 3" down to here)
echo  and send it in the chat.
echo ============================================================
pause
