@echo off
chcp 65001 >nul
title OCS 本地 AI 题库
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto no_python

echo 正在启动 OCS 本地 AI 题库...
echo 首次使用请先在 config.toml 里填写 api_key，配置文件不存在就先复制 config.example.toml。
echo.
python main.py %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" echo 服务异常退出，错误码 %CODE%，详情见上方输出。
echo.
pause
exit /b %CODE%

:no_python
echo [错误] 没有找到 python。
echo 请先安装 Python 3.11 或更高版本，安装时勾选 "Add Python to PATH"。
echo 下载地址：https://www.python.org/downloads/
echo.
pause
exit /b 1
