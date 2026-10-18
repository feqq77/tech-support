#!/usr/bin/env bash
# Remote Tech-Support - installer / bootstrap
#
# What it does:
#   1. Detects OS and architecture
#   2. Checks which dependencies are present (never reinstalls what exists)
#   3. Installs missing system packages via apt ONLY when genuinely needed
#      (one sudo prompt; skips gracefully when sudo is unavailable)
#   4. Creates a Python virtual environment and installs the app libraries
#   5. Generates config.env with a random technician token (600 permissions)
#   6. Creates launch scripts in the project directory
#
# It never modifies unrelated system configuration and never touches files
# outside this project directory (except optional ~/.local/bin symlinks).
#
# Usage:
#   ./install.sh              install / update, then start the app
#   ./install.sh --check      only report what is present or missing
#   ./install.sh --no-system  never call apt/sudo (fail if a piece is missing)
#   ./install.sh --no-launch  install but do not start the app
#   ./install.sh --appimage   download a published release AppImage instead
#                             (requires RELEASE_BASE_URL to be set)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
CONFIG_FILE="$PROJECT_DIR/config.env"
REQ_FILE="$PROJECT_DIR/requirements.txt"
STAMP="$VENV_DIR/.requirements.sha256"

MODE_INSTALL=1
USE_SYSTEM=1
DO_APPIMAGE=0
DO_LAUNCH=1

for arg in "$@"; do
  case "$arg" in
    --check) MODE_INSTALL=0 ;;
    --no-system) USE_SYSTEM=0 ;;
    --appimage) DO_APPIMAGE=1 ;;
    --no-launch) DO_LAUNCH=0 ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m      %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m    %s\n' "$*"; }
err()  { printf '  \033[31merror\033[0m   %s\n' "$*" >&2; }
info() { printf '        %s\n' "$*"; }

launch_app() {                      # launch_app <command...>
  if [ "$DO_LAUNCH" != "1" ]; then
    info "start it any time with: $*"
    return 0
  fi
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    warn "no graphical display was detected, so the app was not started"
    info "start it any time with: $*"
    return 0
  fi
  local log="${TMPDIR:-/tmp}/tech-support-launch.log"
  nohup "$@" >"$log" 2>&1 &
  local pid=$!
  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    bold "The app is starting now."
  else
    err "the app could not start (its output is below)"
    sed 's/^/        /' "$log" >&2 || true
    info "fix the problem above, then start it with: $*"
  fi
}

MISSING_REQUIRED=()
MISSING_OPTIONAL=()
SUDO_AVAILABLE=0

# ----------------------------------------------------------------- platform
bold "Remote Tech-Support installer"
OS_NAME="$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}" || echo unknown)"
OS_VERSION="$(. /etc/os-release 2>/dev/null && echo "${VERSION_ID:-?}" || echo ?)"
ARCH="$(uname -m)"
KERNEL="$(uname -r)"
info "OS:       $OS_NAME $OS_VERSION ($KERNEL)"
info "arch:     $ARCH"
info "project:  $PROJECT_DIR"

case "$ARCH" in
  x86_64|aarch64) : ;;
  *) err "unsupported architecture: $ARCH (supported: x86_64, aarch64)"; exit 1 ;;
esac

if [ "$OS_NAME" != "ubuntu" ] && [ "$OS_NAME" != "debian" ] && [ "$OS_NAME" != "linuxmint" ] && [ "$OS_NAME" != "pop" ]; then
  warn "this installer automates dependencies on Ubuntu/Debian; on '$OS_NAME' install the packages listed below manually"
fi

# -------------------------------------------------------------- dependency
bold "Checking dependencies"

have() { command -v "$1" >/dev/null 2>&1; }

check_cmd() {                       # check_cmd <binary> <package> <required?>
  local bin="$1" pkg="$2" required="$3"
  if have "$bin"; then
    ok "$bin ($(command -v "$bin"))"
  elif [ "$required" = "required" ]; then
    err "$bin missing (package: $pkg)"
    MISSING_REQUIRED+=("$pkg")
  else
    warn "$bin missing (optional package: $pkg)"
    MISSING_OPTIONAL+=("$pkg")
  fi
}

check_python_module() {             # check_python_module <module> <package> <required?>
  local mod="$1" pkg="$2" required="$3"
  if python3 -c "import $mod" >/dev/null 2>&1; then
    ok "python3 module $mod"
  elif [ "$required" = "required" ]; then
    err "python3 module $mod missing (package: $pkg)"
    MISSING_REQUIRED+=("$pkg")
  else
    warn "python3 module $mod missing (optional package: $pkg)"
    MISSING_OPTIONAL+=("$pkg")
  fi
}

if ! have python3; then
  err "python3 is not installed. Install it with: sudo apt-get install python3 python3-venv python3-tk"
  exit 1
fi
PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "python3 $PYTHON_VERSION"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  err "python3 >= 3.10 is required (found $PYTHON_VERSION)"
  exit 1
fi

check_python_module venv    python3-venv  required
check_python_module tkinter python3-tk    required
check_python_module ssl     python3       required
check_cmd xdpyinfo x11-utils  required
check_cmd ffmpeg   ffmpeg     optional
check_cmd Xvfb     xvfb       optional
check_cmd xdotool  xdotool    optional
check_cmd openbox  openbox    optional

if [ -e /usr/lib/x86_64-linux-gnu/libX11.so.6 ] || [ -e /usr/lib/aarch64-linux-gnu/libX11.so.6 ] || ldconfig -p 2>/dev/null | grep -q libX11.so.6; then
  ok "libX11 (X11 client library)"
else
  err "libX11 missing (package: libx11-6)"
  MISSING_REQUIRED+=("libx11-6")
fi

if sudo -n true 2>/dev/null; then
  SUDO_AVAILABLE=1
fi

if [ "${#MISSING_REQUIRED[@]}" -gt 0 ]; then
  echo
  if [ "$USE_SYSTEM" = "1" ] && have apt-get; then
    bold "Installing missing required packages (administrator privileges needed)"
    info "packages: ${MISSING_REQUIRED[*]}"
    if sudo -v; then
      sudo apt-get update -qq
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING_REQUIRED[@]}"
    else
      err "administrator privileges are required to install: ${MISSING_REQUIRED[*]}"
      info "run: sudo apt-get install ${MISSING_REQUIRED[*]}"
      info "then re-run ./install.sh"
      exit 1
    fi
  else
    err "missing required packages: ${MISSING_REQUIRED[*]}"
    info "run: sudo apt-get install ${MISSING_REQUIRED[*]}"
    info "(or re-run ./install.sh without --no-system to install them automatically)"
    exit 1
  fi
fi

if [ "${#MISSING_OPTIONAL[@]}" -gt 0 ] && [ "$MODE_INSTALL" = "1" ] && [ "$USE_SYSTEM" = "1" ] && have apt-get; then
  echo
  bold "Optional packages (installed automatically; Ctrl-C to skip)"
  info "packages: ${MISSING_OPTIONAL[*]}"
  if sudo -v 2>/dev/null; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING_OPTIONAL[@]}" || \
      warn "some optional packages could not be installed; the app will use built-in fallbacks"
  else
    warn "skipped (no administrator privileges). Optional features degrade gracefully."
  fi
fi

if [ "$MODE_INSTALL" = "0" ]; then
  echo
  bold "--check summary"
  info "required missing: ${MISSING_REQUIRED[*]:-none}"
  info "optional missing: ${MISSING_OPTIONAL[*]:-none}"
  exit 0
fi

# --------------------------------------------------------------- AppImage
if [ "$DO_APPIMAGE" = "1" ]; then
  bold "AppImage release install"
  BASE_URL="${RELEASE_BASE_URL:-}"
  if [ -z "$BASE_URL" ]; then
    err "no published release URL is configured."
    info "set RELEASE_BASE_URL to the real release location, e.g."
    info "  RELEASE_BASE_URL=https://github.com/YOUR/REPO/releases/download/v1.0 ./install.sh --appimage"
    info "no download URL is invented here; until a release is published, install from"
    info "this source tree with a plain './install.sh'."
    exit 1
  fi
  case "$ARCH" in
    x86_64) APPIMAGE_NAME="TechSupport-Customer-x86_64.AppImage" ;;
    aarch64) APPIMAGE_NAME="TechSupport-Customer-aarch64.AppImage" ;;
  esac
  DEST="$PROJECT_DIR/$APPIMAGE_NAME"
  info "downloading $BASE_URL/$APPIMAGE_NAME"
  curl -fL --proto '=https' -o "$DEST" "$BASE_URL/$APPIMAGE_NAME" || {
    err "download failed"; exit 1; }
  if curl -fL --proto '=https' -o "$DEST.sha256" "$BASE_URL/$APPIMAGE_NAME.sha256"; then
    (cd "$PROJECT_DIR" && sha256sum -c "$APPIMAGE_NAME.sha256") || {
      err "integrity check failed - deleting the download"; rm -f "$DEST" "$DEST.sha256"; exit 1; }
    ok "integrity verified"
  else
    err "no checksum file at $BASE_URL/$APPIMAGE_NAME.sha256 - refusing to run an unverified binary"
    rm -f "$DEST"
    exit 1
  fi
  chmod +x "$DEST"
  ok "installed: $DEST"
  echo
  launch_app "$DEST"
  exit 0
fi

# ------------------------------------------------------------ python setup
bold "Setting up the application"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  info "creating virtual environment: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
  ok "virtual environment created"
else
  ok "virtual environment exists"
fi

REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$REQ_HASH" ] \
   && "$VENV_DIR/bin/python" -c "import aiortc, aiohttp, mss, Xlib" >/dev/null 2>&1; then
  ok "python dependencies already installed (skipping)"
else
  info "installing python dependencies (aiortc, aiohttp, mss, python-xlib)"
  "$VENV_DIR/bin/pip" install --upgrade pip -q
  "$VENV_DIR/bin/pip" install -r "$REQ_FILE" || {
    err "python dependency installation failed (see pip output above)"
    info "common causes: no internet access, or pip is blocked by a proxy"
    exit 1
  }
  echo "$REQ_HASH" > "$STAMP"
  ok "python dependencies installed"
fi

# ------------------------------------------------------------------ config
if [ -f "$CONFIG_FILE" ]; then
  ok "config.env exists (keeping the existing technician token)"
else
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  cat > "$CONFIG_FILE" <<EOF
# Generated by install.sh - keep private.
# Technician authentication token (signaling server + technician console).
SUPPORT_TECH_TOKEN=$TOKEN
# Support code the customer types (fixed for this alpha; change here and in
# the signaling server's --code if you ever need a different one).
SUPPORT_CODE=1234
# Signaling server. Local default; for two computers across the internet set
# this to your deployed server, e.g. wss://support.example.com/ws
SIGNALING_URL=ws://127.0.0.1:8787/ws
# Optional ICE servers as JSON (add TURN for restrictive networks), e.g.
# ICE_SERVERS_JSON=[{"urls":"stun:stun.l.google.com:19302"},{"urls":"turn:turn.example.com:3478","username":"shop","credential":"secret"}]
# Quote the JSON: a shell sourcing this file would otherwise eat the quotes.
# ICE_SERVERS_JSON='[{"urls":"stun:stun.l.google.com:19302"}]'
# TURN relay for networks that block direct peer-to-peer traffic. Both sides
# pick this up live from the matchmaker; credentials are minted from the secret
# (coturn static-auth-secret scheme, valid 30 days per server start).
TECH_SUPPORT_TURN_URL=turns:staticauth.openrelay.metered.ca:443?transport=tcp
TECH_SUPPORT_TURN_SECRET=openrelayprojectsecret
EOF
  chmod 600 "$CONFIG_FILE"
  ok "config.env created (random technician token, permissions 600)"
fi

for script in run-customer.sh run-technician.sh run-signaling-server.sh uninstall.sh build_appimage.sh; do
  [ -f "$PROJECT_DIR/$script" ] && chmod +x "$PROJECT_DIR/$script"
done
ok "launch scripts ready"

# ------------------------------------------------------------ user commands
mkdir -p "$HOME/.local/bin"
LINKED=""
for pair in "tech-support-customer:run-customer.sh" \
            "tech-support-technician:run-technician.sh" \
            "tech-support-signaling:run-signaling-server.sh"; do
  name="${pair%%:*}"; script="${pair##*:}"
  if ln -sf "$PROJECT_DIR/$script" "$HOME/.local/bin/$name" 2>/dev/null; then
    LINKED="$LINKED $name"
  fi
done
# Short command.  Do not clobber a get.sh AppImage install.
if [ ! -e "$HOME/.local/bin/tech-support" ] || [ -L "$HOME/.local/bin/tech-support" ]; then
  if ln -sf "$PROJECT_DIR/run-customer.sh" "$HOME/.local/bin/tech-support" 2>/dev/null; then
    LINKED="$LINKED tech-support"
  fi
fi
[ -n "$LINKED" ] && ok "user commands:$LINKED (in ~/.local/bin)"

echo
bold "Installed."
info "technician console:  ./run-technician.sh   then open http://127.0.0.1:8765 in Chrome"
info "signaling server:    ./run-signaling-server.sh"
info "over the internet:   see docs/DEPLOY.md (signaling + TURN servers)"
info "uninstall:           ./uninstall.sh"
echo
launch_app "$PROJECT_DIR/run-customer.sh"
