#!/bin/bash
# Convenience script to run the TRA field warning server with auto-restart.
# Run this in your own Terminal window and leave it open.
# Then open http://127.0.0.1:8000/ in browser and hard-refresh (Cmd+Shift+R).

cd "$(dirname "$0")"
echo "=== TRA Position App ==="
echo "Dir: $(pwd)"
echo "Activating venv..."
source .venv/bin/activate || { echo "Failed to activate .venv/bin/activate. Make sure venv exists."; exit 1; }

echo "Starting uvicorn (auto-restart on exit)..."
echo "Press Ctrl+C to stop permanently."
echo ""

# For 本機 + RTDB test: in another terminal run:
#   cd /Users/weidilin/tra-position-app
#   source .venv/bin/activate
#   python rtdb_worker.py
# (after placing the service account json and it will use default DB_URL)
while true; do
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  echo ""
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Server exited. Restarting in 1s..."
  sleep 1
done
