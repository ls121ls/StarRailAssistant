@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_sra.ps1"
if errorlevel 1 (
    echo.
    echo ============================================
    echo  启动失败，请把上面的错误信息发给开发者。
    echo ============================================
    pause
)
endlocal
