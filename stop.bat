@echo off
chcp 65001 >nul

echo 正在停止进程...
taskkill /f /im auto_signup.exe >nul 2>nul
taskkill /f /im NapCat.exe >nul 2>nul

echo 已执行停止命令。
exit
