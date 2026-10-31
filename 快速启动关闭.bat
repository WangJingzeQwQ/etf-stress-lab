@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "APP_ROOT=%~dp0"
if "%APP_ROOT:~-1%"=="\" set "APP_ROOT=%APP_ROOT:~0,-1%"
set "APP_HELPER=%~dp0scripts\service_manager.ps1"

if not exist "%APP_HELPER%" (
  echo [错误] 找不到服务管理脚本：
  echo %APP_HELPER%
  pause
  exit /b 1
)

if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_HELPER%" -Root "%APP_ROOT%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_HELPER%" -Root "%APP_ROOT%" -Action "%~1"
)

set "APP_EXIT=%ERRORLEVEL%"
endlocal & exit /b %APP_EXIT%
