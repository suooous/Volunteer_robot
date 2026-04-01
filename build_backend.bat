@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "ACTIVATE_BAT=D:\Anaconda3\Scripts\activate.bat"
set "QQ_PYTHON=D:\Anaconda3\envs\qq\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%cd%\backend\browsers"

if exist "%QQ_PYTHON%" goto :python_ready
if exist "%ACTIVATE_BAT%" call "%ACTIVATE_BAT%" qq
set "QQ_PYTHON=python"

:python_ready

echo [1/4] install pyinstaller
"%QQ_PYTHON%" -m pip install pyinstaller
if errorlevel 1 goto :pip_fail

echo [2/4] clean old build
if exist ".\build" rmdir /s /q ".\build"
if exist ".\dist" rmdir /s /q ".\dist"

echo [3/4] build exe
"%QQ_PYTHON%" -m PyInstaller --noconfirm --clean --onefile --name auto_signup --collect-all playwright --collect-all pyee --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto ".\backend\app.py"
if errorlevel 1 goto :build_fail

echo [4/4] copy exe and install chromium
if not exist ".\backend\browsers" mkdir ".\backend\browsers"
copy /y ".\dist\auto_signup.exe" ".\backend\auto_signup.exe" >nul
"%QQ_PYTHON%" -m playwright install chromium
if errorlevel 1 goto :browser_warn

goto :success

:pip_fail
echo [ERROR] pip install pyinstaller failed
pause
exit /b 1

:build_fail
echo [ERROR] pyinstaller build failed
pause
exit /b 1

:browser_warn
echo [WARN] chromium install failed, you can retry later
echo.
echo output: backend\auto_signup.exe
echo config: backend\config.json
echo browsers: backend\browsers
pause
exit /b 0

:success
echo.
echo build finished
echo output: backend\auto_signup.exe
echo config: backend\config.json
echo browsers: backend\browsers
echo.
pause
