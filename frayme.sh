#!/usr/bin/env bash
# Start, stop, and check on the Frayme server.
#
#   ./frayme.sh start [port]   run in the background and open the browser
#   ./frayme.sh stop           shut it down
#   ./frayme.sh restart [port]
#   ./frayme.sh status
#   ./frayme.sh logs           follow the server log

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$APP_DIR/.frayme.pid"
PORT_FILE="$APP_DIR/.frayme.port"
# Log file in the OS standard location (see docs/logging.md). The app itself
# writes it with rotation. Startup output (anything printed before logging
# is set up) goes to a separate file next to it, truncated on each start.
if [[ "$(uname -s)" == "Darwin" ]]; then
  LOG_FILE="$HOME/Library/Logs/Frayme/app.log"
else
  LOG_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/Frayme/app.log"
fi
STARTUP_LOG="$(dirname "$LOG_FILE")/startup.log"
DEFAULT_PORT=5901

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
    return 0
  fi
  # Stale PID file from a crash or reboot
  rm -f "$PID_FILE" "$PORT_FILE"
  return 1
}

port_in_use() {
  # Python is already a requirement, so use it instead of lsof (not always installed on Linux)
  python3 -c 'import socket, sys; sys.exit(socket.socket().connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0)' "$1"
}

open_browser() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1 || true
  else
    open "$1" 2>/dev/null || true
  fi
}

start() {
  local port="${1:-$DEFAULT_PORT}"
  local pid
  if pid="$(running_pid)"; then
    echo "Frayme is already running (PID $pid) at http://localhost:$(cat "$PORT_FILE")"
    open_browser "http://localhost:$(cat "$PORT_FILE")"
    return 0
  fi
  if port_in_use "$port"; then
    echo "Port $port is already in use by another program. Try: $0 start <port>" >&2
    return 1
  fi
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Warning: ffmpeg not found on PATH; recovery will not work (brew install ffmpeg)." >&2
  fi

  # The app writes its own rotating log file, so stdout and stderr go to
  # the startup log instead, truncated on each start. That keeps a single
  # writer on app.log while startup failures still leave a traceback.
  mkdir -p "$(dirname "$LOG_FILE")"
  nohup python3 -u "$APP_DIR/app.py" "$port" >"$STARTUP_LOG" 2>&1 &
  echo $! >"$PID_FILE"
  echo "$port" >"$PORT_FILE"

  # Wait for the server to start listening
  for _ in $(seq 1 20); do
    if port_in_use "$port"; then
      echo "Frayme started (PID $(cat "$PID_FILE")) at http://localhost:$port"
      return 0
    fi
    if ! running_pid >/dev/null; then
      echo "Frayme failed to start. Last log lines:" >&2
      tail -n 20 "$LOG_FILE" >&2 2>/dev/null || echo "(no log file yet)" >&2
      echo "Startup output:" >&2
      tail -n 20 "$STARTUP_LOG" >&2 2>/dev/null || echo "(no startup log yet)" >&2
      return 1
    fi
    sleep 0.25
  done
  echo "Frayme is running but not yet listening on port $port; check: $0 logs" >&2
}

stop() {
  local pid
  if ! pid="$(running_pid)"; then
    echo "Frayme is not running."
    return 0
  fi
  kill "$pid"
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "Server did not exit cleanly; forcing it."
    kill -9 "$pid"
  fi
  rm -f "$PID_FILE" "$PORT_FILE"
  echo "Frayme stopped."
}

status() {
  local pid
  if pid="$(running_pid)"; then
    echo "Frayme is running (PID $pid) at http://localhost:$(cat "$PORT_FILE")"
  else
    echo "Frayme is not running."
    return 1
  fi
}

case "${1:-}" in
  start)   start "${2:-}" ;;
  stop)    stop ;;
  restart) stop; start "${2:-}" ;;
  status)  status ;;
  logs)    mkdir -p "$(dirname "$LOG_FILE")"; touch "$LOG_FILE"; tail -F "$LOG_FILE" ;;
  *)
    echo "Usage: $0 {start [port]|stop|restart [port]|status|logs}" >&2
    exit 1
    ;;
esac
