@echo off
chcp 65001 >nul
title WB Diskver - capture and diagnostics v2
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0wb_qr_test.ps1"
echo.
pause
