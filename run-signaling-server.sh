#!/usr/bin/env bash
# Signaling server launcher (run this on a reachable host for two-computer use).
# Usage: ./run-signaling-server.sh [--host 127.0.0.1] [--port 8787] [--tech-token T] [...]
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "The application is not installed yet. Run: $PROJECT_DIR/install.sh" >&2
  exit 1
fi

exec "$PROJECT_DIR/.venv/bin/python" -m server.signaling_server "$@"
