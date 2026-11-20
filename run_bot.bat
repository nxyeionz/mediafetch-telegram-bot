@echo off
title MediaFetch Telegram Bot (Zero-Cookie Permanent Runner)
cd /d "%~dp0"
echo =======================================================
echo   MediaFetch Telegram Bot - Permanent Local Runner
echo   Zero Cookies Needed - No Bot Checks - 2GB MTProto Upload
echo =======================================================
echo.
python -m mediafetch.main
pause
