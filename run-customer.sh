#!/bin/sh
# Remote Tech-Support launcher (created by get.sh).
APP="/home/fequla/.local/share/tech-support/TechSupport-Customer-x86_64.AppImage"
if [ -e /dev/fuse ] && { command -v fusermount >/dev/null 2>&1 || command -v fusermount3 >/dev/null 2>&1; }; then
  exec "$APP" "$@"
fi
exec "$APP" --appimage-extract-and-run "$@"
