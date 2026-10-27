@echo off
chcp 936 >nul
cd /d "%~dp0"
title WorkBaby 账号及会话管理助手 - 打包 exe

echo ============================================================
echo   打包成独立 exe（自动带版本号命名）
echo ============================================================
echo.

set "PY1=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
set "PY2=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if exist "%PY1%" (
  "%PY1%" -c "import PyInstaller" >nul 2>&1
  if not errorlevel 1 (
    "%PY1%" "%~dp0打包exe.py" %*
    goto done
  )
)

if exist "%PY2%" (
  "%PY2%" -c "import PyInstaller" >nul 2>&1
  if not errorlevel 1 (
    "%PY2%" "%~dp0打包exe.py" %*
    goto done
  )
)

echo [失败] 没找到装了 PyInstaller 的 Python。
echo 装一个： pip install pyinstaller
echo.

:done
echo.
pause
