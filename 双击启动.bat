@echo off
chcp 936 >nul
cd /d "%~dp0"
title WorkBaby 账号及会话管理助手

echo ============================================================
echo   WorkBaby 账号及会话管理助手（跨账号迁移 / 复制会话）
echo ============================================================
echo.

set "PY1=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
set "PY2=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if exist "%PY1%" (
  "%PY1%" -c "import tkinter" >nul 2>&1
  if not errorlevel 1 (
    echo 正在用系统 Python 3.11 启动...
    start "" "%PY1%" "%~dp0账号互通工具.py"
    exit /b
  )
)

if exist "%PY2%" (
  "%PY2%" -c "import tkinter" >nul 2>&1
  if not errorlevel 1 (
    echo 正在用内置 Python 启动...
    start "" "%PY2%" "%~dp0账号互通工具.py"
    exit /b
  )
)

where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import tkinter" >nul 2>&1
  if not errorlevel 1 (
    echo 正在用 Python 启动器启动...
    start "" py -3 "%~dp0账号互通工具.py"
    exit /b
  )
)

echo.
echo [启动失败] 没找到带图形库(tkinter)的 Python。
echo 请手动运行： python "%~dp0账号互通工具.py"
echo.
pause
