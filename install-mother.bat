@echo off
setlocal EnableDelayedExpansion

REM ====================================================================
REM   LOTUS MOTHER installer - Windows
REM
REM Double-click to run. Sets up the FULL mother on this Windows PC
REM (usually the bigger/beefier machine). Children use install-child.bat.
REM
REM This validates prereqs then runs setup-mother.py which does the
REM heavy lifting (venv, pip deps, Playwright, Ollama, Gemma pull,
REM launcher).
REM ====================================================================

cd /d "%~dp0"

echo.
echo ====================================================================
echo    LOTUS Agent Mother - Windows installer
echo ====================================================================
echo  Machine:   %COMPUTERNAME%
echo  Repo:      %CD%
echo ====================================================================
echo.

REM -- Python 3.10+ ---------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Python 3 not found on PATH.
  echo.
  echo   Install Python 3.10+:  https://www.python.org/downloads/windows/
  echo   IMPORTANT: check "Add python.exe to PATH" during install.
  echo.
  pause & exit /b 2
)
for /f "tokens=*" %%V in ('python -c "import sys;print('%%d.%%d.%%d'%%sys.version_info[:3])"') do set PY_VER=%%V
for /f "tokens=*" %%M in ('python -c "import sys;print(sys.version_info[0])"') do set PY_MAJOR=%%M
for /f "tokens=*" %%N in ('python -c "import sys;print(sys.version_info[1])"') do set PY_MINOR=%%N
if !PY_MAJOR! LSS 3 ( echo [FAIL] Python !PY_VER! too old. Need 3.10+. & pause & exit /b 2 )
if !PY_MAJOR! EQU 3 if !PY_MINOR! LSS 10 ( echo [FAIL] Python !PY_VER! too old. Need 3.10+. & pause & exit /b 2 )
echo [OK] Python !PY_VER!

REM -- Ollama ---------------------------------------------------------
where ollama >nul 2>&1
if errorlevel 1 (
  echo [WARN] Ollama not installed.
  echo    Download: https://ollama.com/download/windows
  echo    Install it, start it from Start Menu, then re-run this installer.
  echo.
  set /p ANS="  Continue anyway (you'll install Ollama later)? [y/N]: "
  if /i not "!ANS!"=="y" ( pause & exit /b 2 )
) else (
  echo [OK] Ollama detected
)

REM -- Tesseract ------------------------------------------------------
where tesseract >nul 2>&1
if errorlevel 1 (
  echo [WARN] Tesseract not on PATH - ReviewAgent OCR will fail.
  echo    Install: https://github.com/UB-Mannheim/tesseract/wiki
  echo    ^(Check "Add to system PATH" during install.^)
) else (
  echo [OK] Tesseract detected
)

REM -- Chrome ---------------------------------------------------------
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
  echo [OK] Google Chrome detected
) else if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
  echo [OK] Google Chrome detected
) else (
  echo [WARN] Chrome not found - RendererAgent needs it.
  echo    Download: https://www.google.com/chrome/
)

echo.
echo ====================================================================
echo   Running setup-mother.py
echo ====================================================================
python setup-mother.py

echo.
echo ====================================================================
echo   Setup finished. Press any key to close.
echo ====================================================================
pause >nul
