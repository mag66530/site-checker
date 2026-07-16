@echo off
cd /d "%~dp0"
title Show Browser - Google i Yandex

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
set "AUTOCLICK_MODE=visible"

echo Stavlyu brauzer Playwright (pervyy raz - minutku, potom bystro)...
%PY% -m playwright install chromium

echo.
echo ==================================================================
echo  SEYCHAS OTKROETSYA OKNO BRAUZERA i poydyot v Google Search Console.
echo  Esli poprosit voyti - voydi v Google PRYAMO V TOM OKNE
echo  (tot akkaunt, gde Search Console po stalmetural.ru).
echo  Zhdyot do 5 minut. Voshla - dalshe idyot sam.
echo  SMOTRI na okno brauzera.
echo ==================================================================
echo.
%PY% index_gsc_run.py --project smu --account 0

echo.
echo ==================================================================
echo  Teper OTKROETSYA OKNO i poydyot v Yandex Webmaster.
echo  Esli poprosit voyti - voydi v Yandex v tom okne.
echo ==================================================================
echo.
%PY% index404_run.py --project smu

echo.
echo ==================================================================
echo  GOTOVO. Chto poluchilos - napisano vyshe v etom okne.
echo  Esli gde-to oshibka ili brauzer NE otkrylsya -
echo  sfotkay eto chyornoe okno celikom i prishli mne.
echo ==================================================================
pause
