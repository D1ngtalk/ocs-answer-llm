@echo off
chcp 65001 >nul
title OCS 本地 AI 题库
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.11+ 并勾选 "Add to PATH"。
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] 需要 Python 3.11 或更高版本。
    python --version
    pause
    exit /b 1
)

echo 正在启动 OCS 本地 AI 题库...
python main.py %*
echo.
echo 服务已退出。
pause
