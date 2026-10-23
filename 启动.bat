@echo off
setlocal EnableExtensions
rem =========================================================
rem  Candidate English Panel - launcher
rem
rem  ASCII-only on purpose: .bat is parsed with the console code
rem  page, so non-ASCII text can break the parser.
rem  NOTE: no %VAR% inside if( ... ) blocks - a path containing
rem  ( or ) would break the cmd.exe parser. All checks use goto.
rem =========================================================

set "HERE=%~dp0"
set "SCRIPT=%HERE%src\main.py"
set "LOGDIR=%HERE%logs"
set "LOG=%LOGDIR%\panel.log"

if not exist "%SCRIPT%" goto no_script

rem ---- 1) locate python.exe (console, used for the running check) ----
if exist "%HERE%python\python.exe" goto py_bundled
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" goto py_local
if exist "C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\python.exe" goto py_hard
python --version >nul 2>nul
if not errorlevel 1 goto py_path
goto find_pyw

:py_bundled
set "PYEXE=%HERE%python\python.exe"
set "PYW=%HERE%python\pythonw.exe"
goto check_run

:py_local
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
goto check_run

:py_hard
set "PYEXE=C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\python.exe"
set "PYW=C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\pythonw.exe"
goto check_run

:py_path
set "PYEXE=python"
pythonw --version >nul 2>nul
if errorlevel 1 goto find_pyw
set "PYW=pythonw"
goto check_run

rem ---- 2) no console python found: try pythonw alone ----
:find_pyw
if exist "%HERE%python\pythonw.exe" goto pyw_bundled
if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" goto pyw_local
if exist "C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\pythonw.exe" goto pyw_hard
pyw --version >nul 2>nul
if not errorlevel 1 goto pyw_launcher
goto no_python

:pyw_bundled
set "PYW=%HERE%python\pythonw.exe"
goto start
:pyw_local
set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
goto start
:pyw_hard
set "PYW=C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\pythonw.exe"
goto start
:pyw_launcher
set "PYW=pyw"
goto start

rem ---- 3) is the panel already running? ----
:check_run
"%PYEXE%" -c "import ctypes,sys; sys.exit(0 if ctypes.windll.user32.FindWindowW('WLXDWSSPanelWnd', None) else 1)"
if not errorlevel 1 goto already

rem ---- 4) launch (pythonw = no console window) ----
:start
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul
start "" "%PYW%" "%SCRIPT%" >> "%LOG%" 2>&1
if not defined PYEXE exit /b 0
call :wait_window
if errorlevel 1 goto start_failed
exit /b 0

:wait_window
rem wait up to 8 seconds for the panel window to appear
set /a _n=0
:wait_loop
if %_n% GEQ 8 exit /b 1
"%PYEXE%" -c "import ctypes,sys; sys.exit(0 if ctypes.windll.user32.FindWindowW('WLXDWSSPanelWnd', None) else 1)"
if not errorlevel 1 exit /b 0
timeout /t 1 /nobreak >nul
set /a _n+=1
goto wait_loop

rem ---- messages ----
:already
echo.
echo  [INFO] The panel is already running.
echo         To restart: run  stop.bat  first, then this script again.
echo.
pause
exit /b 0

:start_failed
echo.
echo  [WARN] Launch attempted, but no panel window appeared.
echo         Log file: %LOG%
echo         Run it with a console to see the real error:
echo             "%PYEXE%" "%SCRIPT%"
echo.
pause
exit /b 1

:no_python
echo.
echo  [ERROR] No Python runtime found.
echo          Looked for a bundled python, Python311 in LOCALAPPDATA,
echo          the hard-coded Python311 path, and pythonw on PATH.
echo.
pause
exit /b 1

:no_script
echo.
echo  [ERROR] src\main.py not found.
echo          expected: %SCRIPT%
echo.
pause
exit /b 1
