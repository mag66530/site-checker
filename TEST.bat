@echo off
echo ------------------------------------------
echo  Eto okno otkrylos - znachit .bat rabotaet.
echo ------------------------------------------
echo.
echo Python (py):
where py
py --version
echo.
echo Python (python):
where python
python --version
echo.
cd /d "%~dp0"
echo Papka etogo fayla:
cd
echo.
if exist app.py echo app.py NAYDEN - horosho, fayl v nuzhnoy papke
if not exist app.py echo app.py NE NAYDEN - polozhi etot .bat ryadom s app.py
echo.
echo ===== Nazhmi lyubuyu klavishu chtoby zakryt =====
pause
