@echo off
setlocal
echo ========================================
echo   Alita - Autonomous AI Agent Launcher
echo ========================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.11+ first.
    exit /b 1
)
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js not found. Install Node.js 20+ first.
    exit /b 1
)

echo [1/3] Setting up backend...
pushd backend
if not exist "venv" (
    python -m venv venv
    echo Created virtual environment.
)
venv\Scripts\python.exe -m pip install -r requirements.txt -q
if not exist ".env" (
    copy .env.example .env >nul
    echo Created backend\.env from .env.example - configure your model in Settings.
)

echo [2/3] Starting backend server...
start "Alita Backend" cmd /k "venv\Scripts\python.exe run.py"
popd

echo [3/3] Starting UI...
pushd ui
if not exist "node_modules" (
    call npm install
)
start "Alita UI" cmd /k "npm run dev -- -p 3001"
popd

echo.
echo ========================================
echo   Alita is starting up!
echo   UI:       http://localhost:3001
echo   Backend:  http://localhost:8080
echo   API docs: http://localhost:8080/docs
echo ========================================
echo.
timeout /t 6 >nul
start "" http://localhost:3001
endlocal
