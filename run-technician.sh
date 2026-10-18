#!/usr/bin/env bash
# Technician console launcher. Serves http://127.0.0.1:8765 (localhost only).
# Usage: ./run-technician.sh [--signaling-url URL] [--open] [...]
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "The application is not installed yet. Run: $PROJECT_DIR/install.sh" >&2
  exit 1
fi

exec "$PROJECT_DIR/.venv/bin/python" -m technician.technician_server "$@"
