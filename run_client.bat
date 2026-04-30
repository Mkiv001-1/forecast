@echo off
chcp 65001 >nul
echo =========================================
echo  Forecast Trading Robot - GUI Client
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
if not exist "%~dp0venv_client\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv "%~dp0venv_client"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM Activate venv
call "%~dp0venv_client\Scripts\activate.bat"

REM Install / upgrade dependencies
echo Checking dependencies...
pip install -q -r "%~dp0requirements_client.txt"
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

REM Create INI from example if missing
if not exist "%~dp0scripts\client\ini\client_config.ini" (
    echo Creating client_config.ini from example...
    copy "%~dp0scripts\client\ini\client_config.ini.example" "%~dp0scripts\client\ini\client_config.ini" >nul
    echo.
    echo IMPORTANT: Edit scripts\client\ini\client_config.ini and set server URL and API key!
    echo.
)

echo.
echo Starting GUI client...
echo.

python "%~dp0scripts\client\main.py" %*
