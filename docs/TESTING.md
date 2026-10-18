# Testing & Verification Report

This document records what was **actually run and verified** on the development
machine (Ubuntu, x86_64, X11) versus what still requires a real two-computer
deployment. Nothing below is aspirational — every claim maps to a command that
was executed and its observed result.

## 1. Automated test suite

Run from the project root (an X server on `127.0.0.1:106` is started and stopped
by the fixtures themselves):

```bash
.venv/bin/python -m pytest --no-header -q
```

**Result: 51/51 passing** (verified with three consecutive full-suite runs plus
repeated capture+input runs after a race fix, see §1.2).

| File | Tests | What it proves |
|---|---|---|
| `tests/test_display.py` | 7 | Display-string parsing, live `xdpyinfo`/Xlib verification of `127.0.0.1:106`, unix-socket fallback, rejection of wrong/missing displays |
| `tests/test_coords.py` | 5 | Normalized (0..1) → root-window pixel mapping incl. window offsets and video-resize scaling |
| `tests/test_capture.py` | 5 | Real pixel capture from the selected display (XShm path); captured frames contain the drawn pattern, not a black frame |
| `tests/test_input.py` | 7 | XTEST pointer motion, button clicks, scroll (wheel 4/5), key presses (incl. Shift) verified against **real X events** delivered to a mapped window |
| `tests/test_signaling.py` | 7 | Pairing by code, signal relay, technician-token auth (constant-time compare), fixed support code `1234` accepted, wrong code rejected with a real message, `--code` override, no leak between concurrent sessions |
| `tests/test_e2e.py` | 4 | Full aiortc WebRTC session: consent flow, real video frames, control messages injected as X input, deny-paths (view denied → no video, control denied → no input), unreachable signaling → real error text |
| `tests/test_technician_server.py` | 6 | Console server binds `127.0.0.1` only, WS proxy relays both directions, injects the tech token server-side, reports unreachable signaling |
| `tests/test_install_scripts.py` | 7 | `get.sh` (the one-line installer) run for real against a local fake release server: download + checksum verify, auto-launch (verified by the app actually starting), `--no-launch`, launch-failure reporting, latest-release lookup, bad-checksum rejection, missing-release error text, non-x86_64 source fallback |
| `tests/test_ui.py` | 2 | Customer UI shows exactly the required labels on the required screens |
| `tests/test_browser_e2e.py` | 1 | End-to-end in **real Google Chrome** via CDP: console loads, pairs, receives live WebRTC video of the customer's X display, End session tears everything down |

### 1.1 Real-Chrome browser test

`tests/test_browser_e2e.py` drives an installed Google Chrome (not a mock)
through a minimal CDP client (`tests/cdp.py`) — no Playwright/Selenium
dependency. Screenshots of every stage are saved to `tests/artifacts/`:

- `console-initial.png` — console before connecting
- `console-live.png` — console showing the **live remote desktop** over WebRTC
- `console-ended.png` — console after End session

### 1.2 Flaky-test fix

An intermittent failure was observed in `test_mouse_button_events_reach_window`:
the injected clicks were valid (pointer motion to exact coordinates was proven
by the neighbouring test in the same failing runs), but they never reached the
test window — the signature of some other top-level window briefly stacked
above it, which routes button events elsewhere. The harness now, before
clicking, raises the test window and **verifies via `XQueryPointer` that it is
actually the top-level window under the pointer**; the assertion messages
include the offending window id and any event-pump error. After the fix: 5×
capture+input runs and 3× consecutive full suites with no failures.

## 2. Customer UI verification

`app/customer_app.py --self-test-ui` walks every screen and screenshots were
reviewed side by side with the spec (`tests/artifacts/customer-*.png`):

- `customer-1-home.png` — exactly `tech check`
- `customer-2-confirm-code.png` — exactly `confirm code`
- `customer-2b-wrong-code.png` — wrong-code feedback path
- `customer-3-getting-ready.png` — exactly `GETTING READY`
- `customer-4-consent.png` — separate consent for **view** and for **control**
- `customer-5-session.png` — visible sharing status + `end` button

Nothing else appears on the `tech check`, `confirm code`, or `GETTING READY`
screens.

## 3. AppImage build

```bash
./build_appimage.sh
```

- Produced `dist/TechSupport-Customer-x86_64.AppImage` (~106 MB) + `.sha256`.
- The bundle embeds python-build-standalone CPython 3.12 (checksum-verified
  against the upstream release `SHA256SUMS`) and runs without FUSE
  (appimagetool is extracted, not executed via mount).
- **Bundle self-test passed**: the AppImage launched on a clean Xvfb
  (`:107`), showed the UI, and completed its self-check.

## 4. LAN end-to-end demo (single machine, real network stack)

A demo (`tests/artifacts/demo-*.png`) ran the **real Tkinter customer app** and
**real Chrome technician console** on the machine's actual LAN IP
(`10.0.0.155`), pairing through the signaling server bound on that interface —
i.e. the whole stack over a genuine TCP network path, not loopback shortcuts:

1. `demo-01..03` — customer home → `confirm code` (`1234`) → `GETTING READY`
2. `demo-04` — console connected, waiting for customer approval
3. `demo-05` — consent screen; the real consent buttons were clicked
4. `demo-06` — customer session screen: `sharing screen · control allowed`
5. `demo-07` — console showing the **live remote desktop** (WebRTC video of the
   customer's X session, verified visually frame-by-frame)
6. `demo-08..09` — End session: customer returned to home, console showed
   `session ended`

Mouse control was verified in this demo by sending a mousemove from Chrome and
observing the X pointer land at the mapped coordinate (959, 199).

## 5. Manual/scripted behavior checks

- **Console binding**: technician server refuses to bind anything but
  `127.0.0.1` (tested; also covered by `tests/test_technician_server.py`).
- **Both-end teardown**: clicking End in the console and End in the customer app
  were each verified to immediately stop screen capture (`session._capturer` is
  `None`) and input injection (`session._input` is `None`) — no lingering
  capture or control after either side ends the session.
- **Display selection**: the app captures the display the customer (or
  launcher) selects — including Xvfb `127.0.0.1:106` at 1280×800 — and verifies
  it before capture, reporting the transport actually used (TCP or unix
  socket). No `:0` assumption. No X access-control (`xhost`/xauth) changes are
  made anywhere.
- **Installer** (`install.sh`): dependency detection, idempotent re-runs,
  `--check`, `--no-system`, and `--appimage` (with `RELEASE_BASE_URL` +
  sha256 verification) were exercised locally. Auto-launch was verified both
  ways on this machine: with a working display the customer app process was
  observed running ("The app is starting now."), and with an unusable display
  the installer printed the app's real `xdpyinfo` error instead of claiming
  success. Install/uninstall round-trip leaves no stray processes or files.
- **One-line installer** (`get.sh`): tested at unit level against a local fake
  release server (`tests/test_install_scripts.py`), and then **verified for
  real on 2026-10-17 against live GitHub** (repository `feqq77/tech-support`,
  release `v0.1.0`): the exact customer command
  `curl -fsSL https://raw.githubusercontent.com/feqq77/tech-support/main/get.sh | bash`
  was run from a clean home directory — it resolved the latest release through
  the GitHub API, downloaded the 106 MB AppImage, verified its published
  `.sha256`, installed `~/.local/bin/tech-support`, and the customer app
  process was observed running on a test display.

## 6. Known limitations (stated honestly)

1. **Two physical computers**: this development machine is a single host. The
   LAN demo (§4) runs the real stack over a real network interface, but a
   genuine two-machine, cross-internet session has **not** been performed. For
   that, deploy the signaling server and a TURN relay as described in
   [`DEPLOY.md`](DEPLOY.md) and test from a second computer on another network.
2. **Cross-internet NAT traversal** relies on the TURN configuration in
   `docs/DEPLOY.md` + `server/deploy/coturn.conf`; it has not been validated
   against real carrier-grade NAT from outside this network.
3. **Code `1234` is the fixed alpha code by design** — chosen for a small,
   known community. Change it with the signaling server's `--code` option if
   needed and move to generated one-time codes before any wider deployment.
4. Pytest reports a handful of **deprecation warnings** from aiohttp/aiohttp-ws
   (`ws_connect(timeout=...)` API) and python-xlib usage; they are cosmetic and
   do not affect behavior.

## 7. How to reproduce

```bash
pkill -x Xvfb 2>/dev/null        # tests start their own Xvfb :106
.venv/bin/python -m pytest --no-header -q          # 51 tests
.venv/bin/python -m app.customer_app --self-test-ui
./build_appimage.sh                                  # builds dist/*.AppImage
```

Screenshots and demo evidence land in `tests/artifacts/`.
