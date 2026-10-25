@echo off
chcp 65001 >nul
title WB Diskver - Edge normal test v2
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0wb_qr_test.ps1" -Normal -Browser Edge
echo.
pause
