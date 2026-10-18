# Build & release (AppImage)

The customer application is packaged as a self-contained AppImage: it bundles
a standalone CPython 3.12 runtime and all Python libraries (aiortc, aiohttp,
mss, python-xlib). The customer machine needs **no** Python, pip packages or
build tools — only a glibc-based Linux with an X11 display.

## Build

```bash
./build_appimage.sh            # build only
./build_appimage.sh --self-test  # build, then run the UI self-test from the bundle
```

Output:

```
dist/TechSupport-Customer-x86_64.AppImage
dist/TechSupport-Customer-x86_64.AppImage.sha256
```

What the build does:

1. Downloads the latest `cpython-3.12` **python-build-standalone** runtime
   (release resolved through the GitHub API) and verifies it against that
   release's published `SHA256SUMS`. A mismatch aborts the build.
2. Downloads `appimagetool` (AppImage project, continuous release) and runs
   it extracted, so FUSE is not required on the build machine.
3. Installs the application code and `requirements.txt` into the bundle.
4. Packs the AppImage and writes its `.sha256` checksum.

Build-time requirements: `curl`, `tar`, internet access, ~600 MB free disk.

To bake the project's matchmaker URL into the bundle (so customers never
configure anything), set `TECH_SUPPORT_SIGNALING_URL` at build time:

```bash
TECH_SUPPORT_SIGNALING_URL=wss://matchmaker.example/ws ./build_appimage.sh --self-test
```

The value becomes the app's default `SIGNALING_URL`; a `SIGNALING_URL` in the
customer's environment still overrides it. This project's releases are built
with the live matchmaker URL described in [docs/DEPLOY.md](DEPLOY.md) § 0.

## Publish a release

1. The GitHub repository is `feqq77/tech-support`, and `get.sh` already
   contains that `REPO=` value.
2. Create a release in the repository (e.g. tag `v1.0`) — `get.sh` resolves
   the **latest** release automatically.
3. Upload both `dist/TechSupport-Customer-<arch>.AppImage` and
   `dist/TechSupport-Customer-<arch>.AppImage.sha256` as release assets,
   **keeping those exact file names** — `get.sh` downloads
   `TechSupport-Customer-x86_64.AppImage` (+ `.sha256`) from
   `https://github.com/feqq77/tech-support/releases/download/<tag>/`.
4. Note the asset base URL, e.g.
   `https://github.com/feqq77/tech-support/releases/download/v1.0`.

> No download URL is hard-coded anywhere: until a release is actually
> published, install from the source tree. The installers refuse to proceed
> without a real release location and refuse to run a download whose checksum
> cannot be verified.

## Customer install (one command)

After the first release is published, customers install and start the app
with one short command (works on any Linux distribution and version):

```bash
curl -fsSL https://raw.githubusercontent.com/feqq77/tech-support/main/get.sh | bash
```

This downloads the released AppImage, verifies its `.sha256`, installs
`tech-support` to `~/.local/bin` and launches the app. Customers run
`tech-support` to start it later.

The `install.sh --appimage` route still exists for installing from a source
checkout:

```bash
RELEASE_BASE_URL=https://github.com/feqq77/tech-support/releases/download/v1.0 ./install.sh --appimage
```

The installer downloads the AppImage, verifies `sha256sum -c` against the
published `.sha256`, marks it executable and starts the app (`--no-launch` to
skip):

```bash
./TechSupport-Customer-x86_64.AppImage --signaling-url wss://support.example.com/ws
```

If the target system has no FUSE (e.g. some containers), run the AppImage
with `--appimage-extract` once and use the generated `squashfs-root/AppRun`.

## Local smoke test of a bundle

```bash
Xvfb :107 -screen 0 1280x800x24 -listen tcp &
./dist/TechSupport-Customer-x86_64.AppImage --display 127.0.0.1:107 --self-test-ui
```

This runs the exact screen-flow self-test (`tech check` → `confirm code` →
`GETTING READY` → consent → session) from inside the AppImage.
