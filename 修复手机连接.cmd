@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.12 launcher.py --background
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0repair-phone-access.ps1" -RequestElevation
if errorlevel 1 goto failed
echo 已修复听答的局域网访问规则，请在手机刷新页面或重新扫码。
pause
exit /b 0
:failed
echo 修复未完成。请确认管理员弹窗已允许，并查看 .localirewall-result.json。
pause
exit /b 1
