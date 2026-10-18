#!/usr/bin/env bash
# Uninstall the Remote Tech-Support application.
#
# Removes ONLY what this project created:
#   * .venv/  (python virtual environment)
#   * config.env (with --purge-config; kept by default so re-install resumes)
#   * ~/.local/bin symlinks created by install.sh
#   * build outputs and caches inside this project directory
#
# It never touches files outside this project except those symlinks.

set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PURGE_CONFIG=0

for arg in "$@"; do
  case "$arg" in
    --purge-config) PURGE_CONFIG=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "Uninstalling Remote Tech-Support from: $PROJECT_DIR"

if [ -d "$PROJECT_DIR/.venv" ]; then
  rm -rf "$PROJECT_DIR/.venv"
  echo "  removed .venv/"
fi

if [ "$PURGE_CONFIG" = "1" ] && [ -f "$PROJECT_DIR/config.env" ]; then
  rm -f "$PROJECT_DIR/config.env"
  echo "  removed config.env (technician token deleted)"
else
  [ -f "$PROJECT_DIR/config.env" ] && echo "  kept config.env (use --purge-config to delete)"
fi

for name in tech-support tech-support-customer tech-support-technician tech-support-signaling; do
  target="$HOME/.local/bin/$name"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$PROJECT_DIR/run-"* ] 2>/dev/null; then
    rm -f "$target"
    echo "  removed $target"
  elif [ -L "$target" ]; then
    case "$(readlink "$target")" in
      "$PROJECT_DIR"/*) rm -f "$target"; echo "  removed $target" ;;
    esac
  fi
done

# Installs made by get.sh (AppImage release or source fallback).
SHARE="$HOME/.local/share/tech-support"
if [ -f "$SHARE/installed-files.txt" ]; then
  while IFS= read -r path; do
    if [ -n "$path" ] && [ -e "$path" ]; then
      rm -f "$path"
      echo "  removed $path"
    fi
  done < "$SHARE/installed-files.txt"
fi
if [ -d "$SHARE" ]; then
  rm -rf "$SHARE"
  echo "  removed $SHARE/"
fi

find "$PROJECT_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$PROJECT_DIR/build" "$PROJECT_DIR/dist" "$PROJECT_DIR/AppDir" 2>/dev/null || true

echo "Done. The project source files remain in: $PROJECT_DIR"
echo "To delete everything, remove that directory manually."
