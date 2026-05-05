@echo off
setlocal EnableDelayedExpansion

REM ====================================================================
REM  Build the LOTUS Agent Child MSI on Windows.
REM
REM  Prerequisites (one-time on the build machine):
REM    1. Python 3.10+ on PATH          https://www.python.org/downloads/
REM    2. WiX Toolset v3.11+            https://github.com/wixtoolset/wix3/releases
REM       - Installer puts candle.exe / light.exe / heat.exe on PATH.
REM    3. PyInstaller in Python:        pip install pyinstaller
REM
REM  Run this from the REPO ROOT:
REM    packaging\windows\build.bat
REM
REM  Output:
REM    dist\lotus-child-1.0.0.msi
REM ====================================================================

cd /d "%~dp0..\..\"

echo ============================================================
echo  LOTUS Child MSI build
echo ============================================================
echo  Repo: %CD%
echo.

REM -- 0. Verify toolchain --------------------------------------------
where python >nul 2>&1 || ( echo [FAIL] Python not on PATH. & pause & exit /b 2 )
where candle >nul 2>&1 || ( echo [FAIL] WiX candle not on PATH. Install WiX Toolset 3.x. & pause & exit /b 2 )
where light  >nul 2>&1 || ( echo [FAIL] WiX light not on PATH. Install WiX Toolset 3.x. & pause & exit /b 2 )
where heat   >nul 2>&1 || ( echo [FAIL] WiX heat not on PATH. Install WiX Toolset 3.x. & pause & exit /b 2 )
echo [OK] python, candle, light, heat all on PATH

REM -- 1. Install Python build deps -----------------------------------
echo.
echo --- Installing build dependencies ---
python -m pip install --upgrade pip wheel pyinstaller >nul
python -m pip install websockets requests pillow pytesseract mistune zeroconf >nul
if errorlevel 1 ( echo [FAIL] pip install failed. & pause & exit /b 2 )
echo [OK] deps installed

REM -- 2. PyInstaller bundle ------------------------------------------
echo.
echo --- PyInstaller: bundling lotus-child ---
if exist build rmdir /s /q build
if exist dist\lotus-child rmdir /s /q dist\lotus-child
pyinstaller packaging\lotus-child.spec --clean --noconfirm
if errorlevel 1 ( echo [FAIL] PyInstaller failed. & pause & exit /b 2 )
if not exist dist\lotus-child\lotus-child.exe (
  echo [FAIL] lotus-child.exe not found in dist. & pause & exit /b 2
)
echo [OK] PyInstaller bundle at dist\lotus-child\

REM -- 3. heat.exe harvests the bundle into a WiX component group -----
echo.
echo --- heat: harvesting files ---
if not exist build mkdir build
heat dir dist\lotus-child ^
     -gg -g1 -srd -sfrag -scom -sreg -ke ^
     -cg ProductComponents ^
     -dr INSTALLFOLDER ^
     -var var.LotusChildSource ^
     -out build\lotus-child-harvest.wxs
if errorlevel 1 ( echo [FAIL] heat failed. & pause & exit /b 2 )
echo [OK] harvest wxs at build\lotus-child-harvest.wxs

REM -- 4. candle: compile WiX source ----------------------------------
echo.
echo --- candle: compiling WiX sources ---
candle -ext WixUIExtension -ext WixUtilExtension ^
       -arch x64 ^
       -dLotusChildSource=dist\lotus-child ^
       packaging\windows\lotus-child.wxs ^
       build\lotus-child-harvest.wxs ^
       -out build\
if errorlevel 1 ( echo [FAIL] candle failed. & pause & exit /b 2 )
echo [OK] .wixobj files in build\

REM -- 5. light: link into the final MSI ------------------------------
echo.
echo --- light: linking MSI ---
light -ext WixUIExtension -ext WixUtilExtension ^
      -cultures:en-us ^
      -b dist\lotus-child ^
      build\lotus-child.wixobj build\lotus-child-harvest.wixobj ^
      -out dist\lotus-child-1.0.0.msi
if errorlevel 1 ( echo [FAIL] light failed. & pause & exit /b 2 )

echo.
echo ============================================================
echo  SUCCESS
echo  MSI: %CD%\dist\lotus-child-1.0.0.msi
echo ============================================================
echo.
pause
