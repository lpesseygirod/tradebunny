#!/bin/zsh
# Double-click to open TradeBunny's investment council in your browser.
# Starts the interface if it is not already running. Close this window to stop it.
cd "$(dirname "$0")"
export TRADEBUNNY_HOME="$PWD" PYTHONPATH="$PWD/src"
PORT=8090
if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT/"
  exit 0
fi
.venv/bin/python -m council.cli web --port $PORT &
SERVER=$!
for i in {1..20}; do
  sleep 0.5
  lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && break
done
open "http://127.0.0.1:$PORT/"
echo "TradeBunny is running at http://127.0.0.1:$PORT/  (close this window to stop it)"
wait $SERVER
