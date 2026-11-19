@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || goto fail
)
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto fail
.venv\Scripts\python.exe collect.py login oz_acc1 || goto fail
.venv\Scripts\python.exe collect.py login oz_acc2 || goto fail
.venv\Scripts\python.exe collect.py poll all || goto fail
.venv\Scripts\python.exe collect.py poll all || goto fail
powershell -NoProfile -Command "$action=New-ScheduledTaskAction -Execute (Resolve-Path '.venv\Scripts\pythonw.exe').Path -Argument (([string][char]34)+(Resolve-Path collect.py).Path+[char]34+' poll all') -WorkingDirectory (Get-Location).Path; $trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15); Register-ScheduledTask -TaskName 'Rev Ozon Reviews' -Action $action -Trigger $trigger -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -Force -ErrorAction Stop" || goto fail
echo Ready. Task: Rev Ozon Reviews
pause
exit /b 0
:fail
echo Setup incomplete. Check the status shown above.
pause
exit /b 1
