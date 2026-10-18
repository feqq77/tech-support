#!/usr/bin/env bash
# Build an AppImage of the customer application.
#
# The AppImage bundles a standalone CPython and all Python dependencies so the
# customer machine needs nothing except a working X11 display and glibc.
#
# Network is required at build time (it downloads pinned tooling):
#   * appimagetool          (AppImage project, "continuous" release)
#   * python-build-standalone (Astral; exact release resolved via the GitHub
#     API and verified against that release's SHA256SUMS)
#
# Outputs:
#   dist/TechSupport-Customer-<arch>.AppImage
#   dist/TechSupport-Customer-<arch>.AppImage.sha256
#
# Usage:
#   ./build_appimage.sh [--self-test]

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
DIST_DIR="$PROJECT_DIR/dist"
APPDIR="$BUILD_DIR/AppDir"
SELF_TEST=0
[ "${1:-}" = "--self-test" ] && SELF_TEST=1

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m      %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }
err()  { printf '  \033[31merror\033[0m   %s\n' "$*" >&2; exit 1; }

case "$(uname -m)" in
  x86_64)  ARCH_TAG="x86_64";  PBS_TARGET="x86_64-unknown-linux-gnu" ;;
  aarch64) ARCH_TAG="aarch64"; PBS_TARGET="aarch64-unknown-linux-gnu" ;;
  *) err "unsupported architecture: $(uname -m)" ;;
esac

command -v curl >/dev/null || err "curl is required for the build"
command -v tar  >/dev/null || err "tar is required for the build"

mkdir -p "$BUILD_DIR" "$DIST_DIR"
rm -rf "$APPDIR"

bold "Building TechSupport-Customer AppImage ($ARCH_TAG)"

# ---------------------------------------------- python-build-standalone
PBS_CACHE="$BUILD_DIR/python-standalone.tar.gz"
if [ ! -f "$PBS_CACHE" ]; then
  info "resolving latest python-build-standalone release..."
  curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
       -o "$BUILD_DIR/pbs-release.json" \
    || err "cannot query GitHub for python-build-standalone (network problem?)"
  PBS_TAG="$(grep -m1 '"tag_name"' "$BUILD_DIR/pbs-release.json" | cut -d'"' -f4)" \
    || err "cannot determine the python-build-standalone release tag"
  info "release: $PBS_TAG"
  # Pick the exact asset URL from the release metadata (names include the
  # full CPython version, so they cannot be constructed by hand).
  PBS_URL="$(grep -o '"browser_download_url": *"[^"]*"' "$BUILD_DIR/pbs-release.json" \
             | cut -d'"' -f4 \
             | grep -E "cpython-3\.12\.[0-9]+(%2B|\\+)${PBS_TAG}-${PBS_TARGET}-install_only\.tar\.gz$" \
             | head -n1)" || true
  [ -n "$PBS_URL" ] || err "no cpython-3.12 install_only asset for ${PBS_TARGET} in release ${PBS_TAG}"
  # The URL is percent-encoded; checksums use the plain filename.
  PBS_ASSET="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "${PBS_URL##*/}")"
  info "downloading $PBS_ASSET"
  curl -fL --proto '=https' -o "$PBS_CACHE" "$PBS_URL" \
    || err "download failed: $PBS_URL"
  # Verify against the release's published checksums.
  if curl -fL --proto '=https' -o "$BUILD_DIR/SHA256SUMS" \
       "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/SHA256SUMS"; then
    (cd "$BUILD_DIR" && grep " ${PBS_ASSET}\$" SHA256SUMS | sed "s|${PBS_ASSET}|python-standalone.tar.gz|" | sha256sum -c -) \
      || err "python runtime checksum mismatch - aborting"
    ok "python runtime verified against release SHA256SUMS"
  else
    rm -f "$PBS_CACHE"
    err "could not fetch SHA256SUMS for the python runtime; refusing to bundle unverified binaries"
  fi
else
  ok "python runtime cached (build/python-standalone.tar.gz)"
fi

# ---------------------------------------------------------- appimagetool
TOOL="$BUILD_DIR/appimagetool"
if [ ! -x "$TOOL" ]; then
  info "downloading appimagetool..."
  curl -fL --proto '=https' -o "$BUILD_DIR/appimagetool.AppImage" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH_TAG}.AppImage" \
    || err "appimagetool download failed"
  # Extract instead of running the AppImage so FUSE is not required here.
  (cd "$BUILD_DIR" && chmod +x appimagetool.AppImage && ./appimagetool.AppImage --appimage-extract >/dev/null) \
    || err "could not extract appimagetool"
  mv "$BUILD_DIR/squashfs-root" "$BUILD_DIR/appimagetool-root"
  printf '#!/bin/sh\nexec "%s/AppRun" "$@"\n' "$BUILD_DIR/appimagetool-root" > "$TOOL"
  chmod +x "$TOOL"
  ok "appimagetool ready (extracted, no FUSE needed)"
else
  ok "appimagetool cached"
fi

# ------------------------------------------------------------- AppDir
info "assembling AppDir"
mkdir -p "$APPDIR/usr" "$APPDIR/app"

info "bundling python runtime"
tar -xzf "$PBS_CACHE" -C "$APPDIR/usr" --strip-components=1

info "bundling application code"
cp -r "$PROJECT_DIR/app" "$APPDIR/app/app"
cp "$PROJECT_DIR/requirements.txt" "$APPDIR/app/requirements.txt"

info "installing python dependencies into the bundle"
"$APPDIR/usr/bin/python3" -m pip install --quiet --disable-pip-version-check \
  --target "$APPDIR/usr/lib/app-packages" -r "$PROJECT_DIR/requirements.txt" \
  || err "pip install into the bundle failed"

info "generating icon"
"$APPDIR/usr/bin/python3" - "$APPDIR/org.techsupport.Customer.png" <<'PYGEN'
import struct, sys, zlib

W = H = 256
rows = []
for y in range(H):
    row = bytearray()
    row.append(0)  # PNG filter type 0
    for x in range(W):
        # Simple modern mark: blue rounded square with a white pulse bar.
        cx, cy = abs(x - W / 2), abs(y - H / 2)
        inside = max(cx, cy) < W / 2 - 8
        bar = abs(y - H / 2) < 14 and abs(x - W / 2) < W / 2 - 40
        if not inside:
            row += bytes((244, 246, 249))
        elif bar:
            row += bytes((255, 255, 255))
        else:
            row += bytes((47, 123, 229))
    rows.append(bytes(row))
raw = b"".join(rows)

def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(raw, 9))
       + chunk(b"IEND", b""))
with open(sys.argv[1], "wb") as fh:
    fh.write(png)
PYGEN

cat > "$APPDIR/org.techsupport.Customer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Tech Support (Customer)
Comment=Remote tech-support customer application
Exec=tech-support-customer
Icon=org.techsupport.Customer
Categories=Network;Utility;
Terminal=false
EOF

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONPATH="$HERE/usr/lib/app-packages:$HERE/app${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1
exec "$HERE/usr/bin/python3" -m app.customer_app "$@"
EOF
chmod +x "$APPDIR/AppRun"

# ------------------------------------------------------------ AppImage
info "packing AppImage"
ARCH="$ARCH_TAG" "$TOOL" "$APPDIR" "$DIST_DIR/TechSupport-Customer-${ARCH_TAG}.AppImage" >/dev/null \
  || err "appimagetool failed"
chmod +x "$DIST_DIR/TechSupport-Customer-${ARCH_TAG}.AppImage"
(cd "$DIST_DIR" && sha256sum "TechSupport-Customer-${ARCH_TAG}.AppImage" > "TechSupport-Customer-${ARCH_TAG}.AppImage.sha256")
ok "built: dist/TechSupport-Customer-${ARCH_TAG}.AppImage"
ok "checksum: dist/TechSupport-Customer-${ARCH_TAG}.AppImage.sha256"

# ---------------------------------------------------------- optional test
if [ "$SELF_TEST" = "1" ]; then
  if command -v Xvfb >/dev/null; then
    bold "Self-testing the bundle"
    Xvfb :107 -screen 0 1280x800x24 -listen tcp >/dev/null 2>&1 &
    XVFB_PID=$!
    sleep 1.5
    set +e
    DISPLAY=127.0.0.1:107 "$APPDIR/AppRun" --display 127.0.0.1:107 --self-test-ui
    RESULT=$?
    set -e
    kill $XVFB_PID 2>/dev/null
    [ $RESULT -eq 0 ] || err "bundle self-test failed"
    ok "bundle self-test passed"
  else
    info "Xvfb not available; skipped bundle self-test"
  fi
fi

bold "Done"
info "publish these two files to your release location and set RELEASE_BASE_URL"
info "in install.sh usage (see docs/BUILD.md)."
