#!/usr/bin/env bash
# This launcher is executed by WSL directly from the Windows worktree.
set -euo pipefail

HOST="127.0.0.1"
PORT="8765"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${CLAUDE_CODEX_QUEUE_STATE:-}" ]; then
  STATE="$CLAUDE_CODEX_QUEUE_STATE"
elif [ -n "${CLAUDE_VSCODE_QUEUE_STATE:-}" ]; then
  STATE="$CLAUDE_VSCODE_QUEUE_STATE"
elif command -v powershell.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
  WIN_HOME_RAW="$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath("UserProfile")' 2>/dev/null | tr -d '\r' || true)"
  if [ -n "$WIN_HOME_RAW" ]; then
    WIN_HOME="$(wslpath -u "$WIN_HOME_RAW")"
    if [ -d "$WIN_HOME/.claude-codex-queue" ] || [ ! -d "$WIN_HOME/.claude-vscode-queue" ]; then
      STATE="$WIN_HOME/.claude-codex-queue"
    else
      STATE="$WIN_HOME/.claude-vscode-queue"
    fi
  else
    STATE="$HOME/.claude-codex-queue"
  fi
else
  STATE="$HOME/.claude-codex-queue"
fi
LOG="$STATE/visual-server.log"
PID_FILE="$STATE/visual-server.pid"

cd "$PROJECT"
mkdir -p "$STATE"

is_alive() {
  python3 - <<PY >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://$HOST:$PORT/api/queue", timeout=5).read()
PY
}

stop_stale() {
  if [ ! -f "$PID_FILE" ]; then
    return
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return
  fi
  local args
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ "$args" == *"claude_codex_queue.web --host $HOST --port $PORT"* ]] || [[ "$args" == *"claude_vscode_queue.web --host $HOST --port $PORT"* ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 1
  fi
}

if is_alive; then
  echo "http://$HOST:$PORT/"
  exit 0
fi

stop_stale
setsid -f python3 -m claude_codex_queue.web --host "$HOST" --port "$PORT" > "$LOG" 2>&1 < /dev/null

for _ in $(seq 1 30); do
  if is_alive; then
    pgrep -f "[c]laude_codex_queue.web --host $HOST --port $PORT" | tail -1 > "$PID_FILE" || true
    echo "http://$HOST:$PORT/"
    exit 0
  fi
  sleep 0.5
done

echo "Server non avviato. Log:" >&2
tail -80 "$LOG" >&2 || true
exit 1
