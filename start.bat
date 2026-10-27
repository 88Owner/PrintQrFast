@echo off
chcp 65001 >nul
cd /d "%~dp0"
title In tem QR
set "PY=python"
python --version >nul 2>&1
if errorlevel 1 set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo Khong tim thay Python. Cai Python 3.10+ va tick Add python.exe to PATH.
  pause
  exit /b 1
)
%PY% -c "import fastapi,uvicorn,PIL,qrcode,win32print,openpyxl,multipart" >nul 2>&1
if errorlevel 1 (
  echo Dang cai thu vien...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Cai dat that bai.
    pause
    exit /b 1
  )
)
%PY% app.py
if errorlevel 1 pause
