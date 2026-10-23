@echo off
setlocal EnableExtensions
rem =========================================================
rem  Candidate English Panel - stop
rem  Kills the python/pythonw process running THIS folder's
rem  src\main.py (other Python programs are left alone).
rem  ASCII-only on purpose.
rem =========================================================

set "HERE=%~dp0"
set "TARGET=%HERE%src\main.py"

echo Stopping panel...
powershell -NoProfile -Command "$t=$env:TARGET; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($t.ToLower()) } | ForEach-Object { Write-Host ('  killed pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Done.
timeout /t 2 /nobreak >nul
