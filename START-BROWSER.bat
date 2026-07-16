@echo off
cd /d "%~dp0"
title Site Checker

rem --- which Python to call ---
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

echo.
echo ==================================================
echo  SHAG 1. Otkryvayu Chrome dlya vhoda.
echo  Voydi v nyom: v Google i v Yandex.
echo  Yandex: otkroy tam zhe webmaster.yandex.ru i voydi.
echo  Chrome NE zakryvay - proverka budet hodit imenno v nyom.
echo ==================================================
echo.
%PY% open_browser.py

echo.
echo ==================================================
echo  SHAG 2. Otkryvayu sayt: http://localhost:8501
echo  Na sayte sleva: "Chek-list" - blok "Dopolnitelno" -
echo    galka "Proveryat 404 sredi stranic v indekse" - "Zapustit".
echo  SMOTRI na okno Chrome iz shaga 1 - v nyom poydyot proverka,
echo  i uvidish, na chyom ona spotykaetsya.
echo  ETO chyornoe okno NE zakryvay - zakroesh, vsyo vyklyuchitsya.
echo ==================================================
echo.

rem skip Streamlit email question on first start
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
>"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
>>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""

%PY% -m streamlit run app.py

echo.
echo ===== Esli sayt NE zapustilsya - sfotkay eto okno i prishli mne. =====
pause
