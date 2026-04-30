@echo off
chcp 65001 >nul
echo =========================================
echo  Forecast Trading Robot - Server
echo =========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found in PATH.
    echo Install Python 3.10+ from https://python.org and add to PATH.
    pause
    exit /b 1
)

REM Create venv if missing
if not exist "%~dp0venv_server\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv "%~dp0venv_server"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM Activate venv
call "%~dp0venv_server\Scripts\activate.bat"

REM Install / upgrade dependencies
echo Checking dependencies...
pip install -q -r "%~dp0requirements_server.txt"
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

REM Create INI from example if missing
if not exist "%~dp0scripts\server\ini\server_config.ini" (
    echo Creating server_config.ini from example...
    copy "%~dp0scripts\server\ini\server_config.ini.example" "%~dp0scripts\server\ini\server_config.ini" >nul
    echo.
    echo IMPORTANT: Edit scripts\server\ini\server_config.ini and set your API key!
    echo.
)

echo.
echo Starting server...
echo Press Ctrl+C to stop.
echo.

python "%~dp0scripts\server\main.py" %*
pause
