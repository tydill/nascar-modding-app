@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NASCAR Modding App

rem ---------------------------------------------------------------- find Python
rem Try the py launcher first, then python, then python3. Each candidate must
rem actually print PYOK: the Microsoft Store ships a "python.exe" stub that
rem silently opens the Store instead of running anything.
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
  echo   1. Download Python from  https://www.python.org/downloads/
  echo   2. IMPORTANT: on the first setup screen, tick
  echo        "Add python.exe to PATH"
  echo   3. Finish the installer, then run this file again.
  echo.
  echo   If you installed Python from the Microsoft Store, install it from
  echo   python.org instead - the Store version cannot see this folder properly.
  echo.
  pause
  exit /b 1
)

rem ------------------------------------------------------- check the version
%PYEXE% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   The selected Python version is too old for this app.
  %PYEXE% --version
  echo   Python 3.10 or newer is needed.
  echo.
  echo   Install a current version from  https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

rem --------------------------------------------- make sure the packages exist
%PYEXE% -c "import flask, PIL, numpy" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   First run: installing the three Python packages this app needs
  echo   ^(Flask, Pillow, NumPy^). This needs an internet connection and
  echo   usually takes under a minute.
  echo.
  %PYEXE% -m pip install --upgrade pip >nul 2>nul
  %PYEXE% -m pip install -r requirements.txt
  echo.
  %PYEXE% -c "import flask, PIL, numpy" >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   The packages still are not available.
    echo.
    echo   Most common causes:
    echo     - No internet connection, or a company/school network blocking pip
    echo     - Antivirus blocking the download
    echo     - Python installed without the "Add python.exe to PATH" option
    echo.
    echo   See TROUBLESHOOTING.txt. To retry by hand, run:
    echo     %PYEXE% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
  echo   Packages installed.
  echo.
)

rem ------------------------------------------------------------------ run it
echo Starting the app. A browser tab should open by itself.
echo Leave this window open while you use the app.
echo.
%PYEXE% app.py
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo   The app stopped with an error ^(code %RC%^).
  echo   The lines above this message say why. See TROUBLESHOOTING.txt.
) else (
  echo   The app has stopped.
)
echo.
pause
exit /b %RC%
