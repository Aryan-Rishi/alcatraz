@echo off
REM Alcatraz Setup Wizard — Windows Launcher
REM Double-click this file from File Explorer to launch the wizard via WSL.

where wsl >nul 2>nul
if %errorlevel% neq 0 (
    echo WSL is not installed. Please install WSL first:
    echo   https://learn.microsoft.com/en-us/windows/wsl/install
    pause
    exit /b 1
)

REM If not already inside Windows Terminal, re-launch there if available
if not defined WT_SESSION (
    where wt >nul 2>nul
    if %errorlevel% equ 0 (
        wt -- "%~f0"
        exit /b 0
    )
)

REM Convert the batch file's directory to a WSL path
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:\=/%"
set "SCRIPT_DIR=%SCRIPT_DIR:C:=/mnt/c%"
set "SCRIPT_DIR=%SCRIPT_DIR:D:=/mnt/d%"
set "SCRIPT_DIR=%SCRIPT_DIR:E:=/mnt/e%"

wsl bash -c "cd '%SCRIPT_DIR%' && bash ./install.sh"
pause
