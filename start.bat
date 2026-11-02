@echo off
chcp 65001 >nul
cd /d "%~dp0"
title In tem QR

set "BASE=%~dp0"
set "VPY=%BASE%.venv\Scripts\python.exe"

if exist "%VPY%" goto havevenv

echo Dang tao moi truong Python rieng cho may nay...
set "BOOT=python"
python -c "import sys" >nul 2>&1
if not errorlevel 1 goto makevenv
set "BOOT=py -3"
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 goto makevenv
echo Khong tim thay Python. Cai Python 3.10+ va tick Add python.exe to PATH.
pause
exit /b 1

:makevenv
%BOOT% -m venv "%BASE%.venv"
if errorlevel 1 (
  echo Khong tao duoc moi truong Python.
  pause
  exit /b 1
)

:havevenv
echo Dang cai thu vien...
"%VPY%" -m pip install -r "%BASE%requirements.txt"
if errorlevel 1 (
  echo Cai dat that bai. May can co mang internet roi chay lai start.bat.
  pause
  exit /b 1
)
"%VPY%" -c "import fastapi,uvicorn,PIL,qrcode,win32print,openpyxl,multipart"
if not errorlevel 1 goto runapp
if "%REBUILT%"=="1" goto importdetail
echo Moi truong Python bi loi DLL. Dang tao lai...
set "REBUILT=1"
rmdir /s /q "%BASE%.venv"
set "BOOT=python"
python -c "import sys" >nul 2>&1
if not errorlevel 1 goto makevenv
set "BOOT=py -3"
goto makevenv

:importdetail
echo.
echo Thu vien van chua nap duoc. Chi tiet:
"%VPY%" -c "import fastapi,uvicorn,PIL,qrcode,win32print,openpyxl,multipart"
echo.
echo Cai lai Python 3.10+ ban 64-bit tu python.org, tick Add python.exe to PATH, roi chay lai start.bat.
pause
exit /b 1

:runapp
"%VPY%" "%BASE%app.py"
if errorlevel 1 pause
