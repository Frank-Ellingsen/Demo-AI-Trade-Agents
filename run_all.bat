@echo off
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo ===============================================================================
echo OBX AI TRADING DESK - FULL OPERATIONAL SEQUENCE
echo ===============================================================================
echo.
echo [1/4] Running all 20 unit tests across all modules...
python -m pytest -v
if errorlevel 1 (
    echo [ERROR] Unit tests failed! Aborting sequence.
    if not "%~1"=="--no-server" pause
    exit /b 1
)
echo.
echo [2/4] Running daily EOD scan [Agent 1 to 4]...
python run_daily_scan.py
if errorlevel 1 (
    echo [ERROR] EOD scan failed!
    if not "%~1"=="--no-server" pause
    exit /b 1
)
echo.
echo [3/4] Running portfolio health check and updating drift views [Agent 5]...
python portfolio_monitor.py
if errorlevel 1 (
    echo [ERROR] Portfolio monitor failed!
    if not "%~1"=="--no-server" pause
    exit /b 1
)
python setup_drift_views.py
if errorlevel 1 (
    echo [ERROR] Drift views setup failed!
    if not "%~1"=="--no-server" pause
    exit /b 1
)
echo.
if "%~1"=="--no-server" (
    echo [4/4] Skipped starting Streamlit server [--no-server]. All 3 batch steps completed!
    exit /b 0
)
echo [4/4] Starting Streamlit Control Tower on port 8501...
python -m streamlit run app.py --server.port=8501
pause
