@echo off
cd /d "%~dp0"
title Site Checker

rem --- which Python to call ---
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

rem --- VIDIMYY brauzer: proverka sama otkroet okno i budet v nyom hodit ---
set "AUTOCLICK_MODE=visible"

echo Proveryayu brauzer Playwright (pervyy raz - minutku, potom bystro)...
%PY% -m playwright install chromium

rem skip Streamlit email question on first start
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
>"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
>>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""

echo.
echo ==================================================================
echo  Otkryvayu sayt: http://localhost:8501
echo  Na sayte sleva: "Chek-list" - blok "Dopolnitelno" -
echo    galka "Proveryat 404 sredi stranic v indekse" - "Zapustit".
echo.
echo  Kak nazhmesh - SAM OTKROETSYA BRAUZER, i ty uvidish, kak on
echo  hodit v Google (Search Console) i Yandex (Webmaster).
echo  Esli poprosit voyti - voydi PRYAMO V TOM OKNE (do 5 minut zhdyot).
echo  Voshla odin raz - dalshe pomnit.
echo.
echo  ETO chyornoe okno NE zakryvay - zakroesh, vsyo vyklyuchitsya.
echo ==================================================================
echo.

%PY% -m streamlit run app.py

echo.
echo ===== Esli sayt NE zapustilsya - sfotkay eto okno i prishli mne. =====
pause
