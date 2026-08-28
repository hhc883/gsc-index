@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title GSC 收录助手

where python >nul 2>&1
if errorlevel 1 goto NOPY

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   首次运行，正在准备运行环境，请稍候...
  echo.
  python -m venv .venv
  if errorlevel 1 goto NOVENV
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 goto NODEPS
  echo   环境准备完成。
)

rem 每次启动都verify一遍依赖装齐没有。
rem 以前只在首次建虚拟环境时装，之后 requirements.txt 加了新依赖也不会补装，
rem 老用户升级后会直接崩在 import 上，而且报错看起来跟版本更新毫无关系。
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,requests,google.auth,playwright,openpyxl" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   检测到有依赖缺失或版本更新，正在补装...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 goto NODEPS
  echo   补装完成。
  echo.
)

".venv\Scripts\python.exe" server.py
goto END

:NOPY
echo.
echo   没有检测到 Python，请先安装 Python 3.10 或更高版本：
echo   https://www.python.org/downloads/
echo   安装时务必勾选 "Add Python to PATH"。
goto END

:NOVENV
echo   创建虚拟环境失败，请检查 Python 安装是否完整。
goto END

:NODEPS
echo   依赖安装失败，请检查网络连接后重试。
echo   如果在国内，可以试试换源：
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
goto END

:END
echo.
pause