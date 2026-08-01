@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NASCAR Modding App - install packages

rem You normally do not need this file: START_APP.bat installs the packages by
rem itself on first run. Use this when you want to install or repair them alone.

set "PYEXE="
for %%C in ("py -3" "python" "python3") do (
  if not defined PYEXE (
    for /f "delims=" %%V in ('%%~C -c "print('PYOK')" 2^>nul') do (
      if "%%V"=="PYOK" set "PYEXE=%%~C"
    )
  )
)

if not defined PYEXE (
  echo.
  echo   Python was not found on this PC.
  echo.
  echo   Download it from  https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" on the first setup screen.
  echo.
  echo   Avoid the Microsoft Store version - it cannot see this folder properly.
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%V in ('%PYEXE% -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))" 2^>nul') do set "PYVER=%%V"
echo Found Python %PYVER% via "%PYEXE%".
echo.

%PYEXE% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo   Python %PYVER% is too old. Python 3.10 or newer is needed.
  echo   Install a current version from  https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo Installing Flask, Pillow and NumPy...
echo.
%PYEXE% -m pip install --upgrade pip
%PYEXE% -m pip install -r requirements.txt
if errorlevel 1 goto failed

%PYEXE% -c "import flask, PIL, numpy" >nul 2>nul
if errorlevel 1 goto failed

echo.
echo   All three packages are installed and working.
echo   You can now run START_APP.bat.
echo.
pause
exit /b 0

:failed
echo.
echo   Installation did not finish.
echo.
echo   Most common causes:
echo     - No internet connection, or a network that blocks pip
echo     - Antivirus blocking the download
echo     - Python installed without "Add python.exe to PATH"
echo.
echo   See TROUBLESHOOTING.txt for step-by-step help.
echo.
pause
exit /b 1
