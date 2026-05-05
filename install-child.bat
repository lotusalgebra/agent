@echo off
setlocal EnableDelayedExpansion

REM ====================================================================
REM   LOTUS child installer — Windows
REM
REM Double-click this file to run. Bootstraps only what's needed for a
REM LOTUS child on this Windows PC:
REM   1. Verifies Python 3.10+         (prints installer URL if missing)
REM   2. Verifies Tesseract OCR        (optional — needed for ReviewAgent)
REM   3. Runs setup-child.py           (the cross-platform wizard)
REM
REM We DO NOT auto-install Python / Tesseract — that requires admin and
REM we don't silently elevate. The script detects missing deps and
REM prints the exact install path; you approve and run it.
REM ====================================================================

cd /d "%~dp0"

echo.
echo ====================================================================
echo    LOTUS Agent Child - Windows installer
echo ====================================================================
echo  Machine:   %COMPUTERNAME%
echo  Repo:      %CD%
echo ====================================================================
echo.

REM -- 1. Python 3.10+ ------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Python 3 not found on PATH.
  echo.
  echo   Install Python 3.10+ from:
  echo     https://www.python.org/downloads/windows/
  echo.
  echo   IMPORTANT: during install, check "Add python.exe to PATH".
  echo.
  pause
  exit /b 2
)

for /f "tokens=*" %%V in ('python -c "import sys;print('%%d.%%d.%%d'%%sys.version_info[:3])"') do set PY_VER=%%V
for /f "tokens=*" %%M in ('python -c "import sys;print(sys.version_info[0])"') do set PY_MAJOR=%%M
for /f "tokens=*" %%N in ('python -c "import sys;print(sys.version_info[1])"') do set PY_MINOR=%%N

if !PY_MAJOR! LSS 3 (
  echo [FAIL] Python !PY_VER! is too old. Need 3.10+.
  pause & exit /b 2
)
if !PY_MAJOR! EQU 3 (
  if !PY_MINOR! LSS 10 (
    echo [FAIL] Python !PY_VER! is too old. Need 3.10+.
    pause & exit /b 2
  )
)
for /f "tokens=*" %%P in ('where python') do set PY_PATH=%%P
echo [OK] Python !PY_VER!   ^(!PY_PATH!^)

REM -- 2. Tesseract ---------------------------------------------------
where tesseract >nul 2>&1
if errorlevel 1 (
  echo [WARN] Tesseract not found on PATH.
  echo        ReviewAgent's OCR pass will fail without it.
  echo.
  echo   Install from the UB-Mannheim build:
  echo     https://github.com/UB-Mannheim/tesseract/wiki
  echo.
  echo   During install, check "Add to system PATH".
  echo   ^(Skip if this child won't host ReviewAgent.^)
  echo.
  set /p ANS="  Continue without Tesseract? [y/N]: "
  if /i not "!ANS!"=="y" (
    echo Aborted. Install Tesseract then re-run.
    pause & exit /b 2
  )
) else (
  for /f "tokens=2" %%V in ('tesseract --version 2^>^&1 ^| findstr /C:"tesseract"') do set TS_VER=%%V
  echo [OK] Tesseract !TS_VER! — ReviewAgent OCR ready
)

REM -- 3. Chrome (optional) -------------------------------------------
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
  echo [OK] Google Chrome detected — RendererAgent can attach via CDP
) else if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
  echo [OK] Google Chrome detected — RendererAgent can attach via CDP
) else (
  echo [WARN] Google Chrome not found. Required ONLY if hosting RendererAgent.
  echo        Download: https://www.google.com/chrome/
)

REM -- 4. Run the wizard ----------------------------------------------
echo.
echo ====================================================================
echo   Running setup-child.py
echo ====================================================================
python setup-child.py

echo.
echo ====================================================================
echo   Setup finished. Press any key to close this window.
echo ====================================================================
pause >nul
