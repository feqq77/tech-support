#!/usr/bin/env bash
# Customer application launcher.
# Usage: ./run-customer.sh [--display :0|:106|127.0.0.1:106] [--signaling-url URL] [...]
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "The application is not installed yet. Run: $PROJECT_DIR/install.sh" >&2
  exit 1
fi

exec "$PROJECT_DIR/.venv/bin/python" -m app.customer_app "$@"
