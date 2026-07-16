@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Site Checker

rem ── какой Python звать ───────────────────────────────────
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

echo.
echo ==================================================
echo  ШАГ 1. Открываю Chrome для входа.
echo  Войди в нём: в Google и в Яндекс (webmaster.yandex.ru).
echo  Chrome НЕ закрывай - проверка будет ходить в нём.
echo ==================================================
echo.
%PY% open_browser.py

echo.
echo ==================================================
echo  ШАГ 2. Открываю сайт: http://localhost:8501
echo  На сайте: слева "Чек-лист" - блок "Дополнительно" -
echo    галочка "Проверять 404 среди страниц в индексе" - Запустить.
echo  СМОТРИ на окно Chrome из шага 1 - в нём пойдёт проверка.
echo  ЭТО чёрное окно НЕ закрывай (закроешь - всё выключится).
echo ==================================================
echo.

rem убрать вопрос про email при первом старте Streamlit
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
>"%USERPROFILE%\.streamlit\credentials.toml" echo [general]
>>"%USERPROFILE%\.streamlit\credentials.toml" echo email = ""

%PY% -m streamlit run app.py

echo.
echo ===== Если сайт НЕ запустился - сфоткай это окно и пришли мне. =====
pause
