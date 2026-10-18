#!/usr/bin/env bash
# Remote Tech-Support - one-line installer for customers.
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/tech-support/main/get.sh | bash
#
# Downloads the released customer app, verifies its checksum, installs it to
# ~/.local/bin/tech-support and starts it automatically.  Works on any Linux
# distribution and version: the release AppImage carries its own Python.
#
# Options:
#   --no-launch    install but do not start the app
#   -h, --help     show this help
#
# After installing, start the app any time with:  tech-support
# Uninstall:  rm -rf ~/.local/share/tech-support ~/.local/bin/tech-support
#
# ---------------------------------------------------------------------------

set -euo pipefail

# >>> The GitHub repository this installer installs from (owner/repo).
REPO="feqq77/tech-support"

# Overridable for mirrors and testing; the defaults are the real GitHub
# endpoints and must stay as they are for the normal install.
REPO="${TECH_SUPPORT_REPO:-$REPO}"
DOWNLOAD_BASE="${TECH_SUPPORT_DOWNLOAD_BASE:-https://github.com}"
API_BASE="${TECH_SUPPORT_API_BASE:-https://api.github.com}"
VERSION="${TECH_SUPPORT_VERSION:-}"
ARCH="${TECH_SUPPORT_ARCH:-$(uname -m)}"

APPIMAGE_NAME="TechSupport-Customer-x86_64.AppImage"
BIN_DIR="$HOME/.local/bin"
SHARE_DIR="$HOME/.local/share/tech-support"

DO_LAUNCH=1
usage() {
  cat <<'EOF'
Remote Tech-Support - one-line installer for customers.

  curl -fsSL https://raw.githubusercontent.com/OWNER/tech-support/main/get.sh | bash

Installs the app to ~/.local/bin/tech-support and starts it automatically.
Works on any Linux distribution and version. Options:
  --no-launch    install but do not start the app
  -h, --help     show this help
EOF
}
for arg in "$@"; do
  case "$arg" in
    --no-launch) DO_LAUNCH=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m      %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }
err()  { printf '  \033[31merror\033[0m   %s\n' "$*" >&2; }
hint() { printf '        %s\n' "$*" >&2; }

bold "Remote Tech-Support installer"

if [ "$(uname -s)" != "Linux" ]; then
  err "this app runs on Linux (found: $(uname -s))"
  exit 1
fi

# ------------------------------------------------------------------ download
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1"; }
  download() { if [ "${1#https://}" != "$1" ]; then curl -fsSL --proto '=https' -o "$2" "$1"; else curl -fsSL -o "$2" "$1"; fi; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO- "$1"; }
  download() { wget -q -O "$2" "$1"; }
else
  err "curl or wget is required to download the app"
  hint "install one with: sudo apt-get install curl"
  exit 1
fi

# ------------------------------------------------------------- release lookup
if [ -z "$VERSION" ]; then
  info "looking up the latest release of $REPO ..."
  JSON="$(fetch "$API_BASE/repos/$REPO/releases/latest" 2>/dev/null || true)"
  VERSION="$(printf '%s' "$JSON" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
  if [ -z "$VERSION" ]; then
    err "no published release found for $REPO"
    if [ "$REPO" = "OWNER/tech-support" ]; then
      hint "the REPO line inside get.sh is still the placeholder 'OWNER/tech-support'."
      hint "whoever runs the support service must create the GitHub repository,"
      hint "set REPO= in get.sh to its owner/repo name and publish a release"
      hint "containing $APPIMAGE_NAME (see docs/BUILD.md)."
    else
      hint "check that https://github.com/$REPO exists and has a release with the"
      hint "$APPIMAGE_NAME asset attached (see docs/BUILD.md)."
    fi
    exit 1
  fi
fi
info "release:  $VERSION"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ------------------------------------------------------------ install AppImage
install_appimage() {
  local url="$DOWNLOAD_BASE/$REPO/releases/download/$VERSION/$APPIMAGE_NAME"
  info "downloading $url"
  if ! download "$url" "$WORK/$APPIMAGE_NAME"; then
    err "download failed: $url"
    hint "the release $VERSION exists but has no $APPIMAGE_NAME asset;"
    hint "publish it as a release asset (see docs/BUILD.md)."
    exit 1
  fi
  if ! download "$url.sha256" "$WORK/$APPIMAGE_NAME.sha256"; then
    err "no checksum file at $url.sha256 - refusing to run an unverified download"
    exit 1
  fi
  (cd "$WORK" && sha256sum -c "$APPIMAGE_NAME.sha256" >/dev/null 2>&1) || {
    err "integrity check failed - deleting the download"
    exit 1
  }
  ok "integrity verified"

  mkdir -p "$BIN_DIR" "$SHARE_DIR"
  mv "$WORK/$APPIMAGE_NAME" "$SHARE_DIR/$APPIMAGE_NAME"
  chmod +x "$SHARE_DIR/$APPIMAGE_NAME"

  # Launcher that also works on systems without FUSE.
  cat > "$BIN_DIR/tech-support" <<EOF
#!/bin/sh
# Remote Tech-Support launcher (created by get.sh).
APP="$SHARE_DIR/$APPIMAGE_NAME"
if [ -e /dev/fuse ] && { command -v fusermount >/dev/null 2>&1 || command -v fusermount3 >/dev/null 2>&1; }; then
  exec "\$APP" "\$@"
fi
exec "\$APP" --appimage-extract-and-run "\$@"
EOF
  chmod +x "$BIN_DIR/tech-support"
  printf '%s\n%s\n' "$BIN_DIR/tech-support" "$SHARE_DIR/$APPIMAGE_NAME" > "$SHARE_DIR/installed-files.txt"
  ok "installed: $BIN_DIR/tech-support"
}

# ----------------------------------------------------- install from source
# Fallback for non-x86_64 machines (no AppImage is published for them).
install_source() {
  local url="$DOWNLOAD_BASE/$REPO/archive/refs/tags/$VERSION.tar.gz"
  info "no AppImage is published for $ARCH - installing from source instead"
  info "downloading $url"
  mkdir -p "$SHARE_DIR"
  if ! download "$url" "$WORK/source.tar.gz"; then
    err "download failed: $url"
    exit 1
  fi
  tar xzf "$WORK/source.tar.gz" -C "$SHARE_DIR" --strip-components=1 || {
    err "could not extract the source archive"
    exit 1
  }
  if [ "$DO_LAUNCH" = "1" ]; then
    bash "$SHARE_DIR/install.sh" || { err "source install failed (see output above)"; exit 1; }
  else
    bash "$SHARE_DIR/install.sh" --no-launch || { err "source install failed (see output above)"; exit 1; }
  fi
  mkdir -p "$BIN_DIR"
  ln -sf "$SHARE_DIR/run-customer.sh" "$BIN_DIR/tech-support"
  printf '%s\n' "$BIN_DIR/tech-support" > "$SHARE_DIR/installed-files.txt"
  ok "installed: $BIN_DIR/tech-support"
  DO_LAUNCH=0   # install.sh already launched the app (unless --no-launch)
}

case "$ARCH" in
  x86_64|amd64) install_appimage ;;
  *) install_source ;;
esac

# ------------------------------------------------------------------ PATH note
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) info "note: add $BIN_DIR to your PATH to run 'tech-support' from anywhere" ;;
esac

# ------------------------------------------------------------------- launch
echo
if [ "$DO_LAUNCH" = "1" ]; then
  if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    LOG="${TMPDIR:-/tmp}/tech-support-launch.log"
    nohup "$BIN_DIR/tech-support" >"$LOG" 2>&1 &
    LAUNCH_PID=$!
    sleep 2
    if kill -0 "$LAUNCH_PID" 2>/dev/null; then
      bold "Installed. The app is starting now."
    else
      bold "Installed."
      err "the app could not start (its output is below)"
      sed 's/^/        /' "$LOG" >&2 || true
      hint "fix the problem above, then start it with: tech-support"
    fi
  else
    bold "Installed."
    info "no graphical display was detected, so the app was not started."
    info "start it any time with: tech-support"
  fi
else
  bold "Installed."
  info "start it any time with: tech-support"
fi
info "uninstall: rm -rf $SHARE_DIR $BIN_DIR/tech-support"
