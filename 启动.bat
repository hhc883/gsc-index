@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title GSC 索引提交器

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

".venv\Scripts\python.exe" server.py
goto END

:NOPY
echo.
echo   没有检测到 Python。请先安装 Python 3.10 或更高版本：
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
