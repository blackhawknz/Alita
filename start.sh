#!/usr/bin/env bash
set -e
echo "========================================"
echo "  Alita - Autonomous AI Agent Launcher"
echo "========================================"

command -v python3 >/dev/null || { echo "[ERROR] Python 3.11+ is required."; exit 1; }
command -v node >/dev/null || { echo "[ERROR] Node.js 20+ is required."; exit 1; }

echo "[1/3] Setting up backend..."
cd backend
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -r requirements.txt -q
[ -f .env ] || { cp .env.example .env; echo "Created backend/.env - configure your model in Settings."; }

echo "[2/3] Starting backend..."
./venv/bin/python run.py &
BACKEND_PID=$!
cd ..

echo "[3/3] Starting UI..."
cd ui
[ -d node_modules ] || npm install
npm run dev -- -p 3001 &
UI_PID=$!
cd ..

echo ""
echo "  UI:       http://localhost:3001"
echo "  Backend:  http://localhost:8080  (docs: /docs)"
echo "  Press Ctrl+C to stop."
trap "kill $BACKEND_PID $UI_PID 2>/dev/null; exit" SIGINT SIGTERM
wait
