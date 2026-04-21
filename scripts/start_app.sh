#!/bin/bash
# Gideon — clean start script (Unix/macOS bash)
# Kills any process using port 5001 before starting Flask.
# Usage: ./scripts/start_app.sh
#        PORT=5000 ./scripts/start_app.sh   (if you need a different port)

PORT="${PORT:-5001}"

echo "Checking for processes on port $PORT..."

PIDS=$(lsof -ti :"$PORT" 2>/dev/null)

if [ -n "$PIDS" ]; then
    echo "Found stale process(es): $PIDS"
    echo "$PIDS" | xargs kill -9 2>/dev/null
    echo "Killed stale processes"
    sleep 1
else
    echo "No stale processes found on port $PORT"
fi

echo "Starting Flask app on port $PORT..."
cd "$(dirname "$0")/.." || exit 1
python web/app.py
