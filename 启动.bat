@echo off
cd /d "%~dp0"
title GSC Index Assistant

where python >nul 2>&1
if errorlevel 1 goto NOPY

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   First run. Setting up the Python environment...
  echo.
  python -m venv .venv
  if errorlevel 1 goto NOVENV
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 goto NODEPS
  echo   Setup complete.
)

rem Check for missing dependencies on every launch.
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,requests,google.auth,playwright,openpyxl" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Installing missing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 goto NODEPS
  echo   Installation complete.
  echo.
)

".venv\Scripts\python.exe" server.py
goto END

:NOPY
echo.
echo   Python was not found. Install Python 3.10 or later.
echo   https://www.python.org/downloads/
echo   Select "Add Python to PATH" during installation.
goto END

:NOVENV
echo   Could not create the virtual environment. Check your Python installation.
goto END

:NODEPS
echo   Could not install dependencies. Check your network connection and retry.
echo   In mainland China, you can try this package mirror:
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
goto END

:END
echo.
pause
