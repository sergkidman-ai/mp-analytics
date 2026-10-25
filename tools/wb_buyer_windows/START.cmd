@echo off
chcp 65001 >nul
title WB Diskver - QR test
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0wb_qr_test.ps1" -Normal
echo.
pause
