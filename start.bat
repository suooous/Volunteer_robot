@echo off
setlocal
cd /d "%~dp0"

echo [1/2] Start NapCat
if exist ".\NapCat\napcat.bat" (
    start "NapCat" cmd /k "cd /d .\NapCat && call napcat.bat"
) else (
    echo [WARN] Missing .\NapCat\napcat.bat
)

ping 127.0.0.1 -n 6 >nul

echo [2/2] Start backend
if exist ".\backend\auto_signup.exe" (
    start "Backend" /d ".\backend" auto_signup.exe
) else (
    echo [ERROR] Missing .\backend\auto_signup.exe
    pause
    exit /b 1
)

echo Open: http://127.0.0.1:8000/health
pause
