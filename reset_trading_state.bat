@echo off
chcp 65001 >nul
setlocal

echo =========================================
echo  Forecast Trading Robot - Trading Reset
echo =========================================
echo.

REM Use existing .venv312 (Python 3.12)
if not exist "%~dp0.venv312\Scripts\python.exe" (
    echo Error: .venv312 not found.
    echo Run: py -3.12 -m venv .venv312
    echo Then: .venv312\Scripts\python.exe -m pip install -r requirements_server.txt
    pause
    exit /b 1
)

echo Running reset tool...
echo.
"%~dp0.venv312\Scripts\python.exe" "%~dp0scripts\tools\reset_trading_state.py" %*
set EXIT_CODE=%ERRORLEVEL%

echo.
if %EXIT_CODE% EQU 0 (
    echo Reset finished successfully.
) else (
    echo Reset finished with errors. Exit code: %EXIT_CODE%
)

pause
exit /b %EXIT_CODE%
