# Remote Tech-Support

A lightweight remote tech-support application for a small computer repair
shop. The customer sees a single button; the technician gets a practical
console in Chrome to view and (with the customer's explicit approval) control
the customer's X11 desktop.

**Verified in this repository:** real WebRTC sessions between the customer
application and a real Chrome browser, streaming a real X11 desktop
(1280×800 Xvfb display at `127.0.0.1:106`), with real mouse/keyboard
injection and instant session teardown — see [docs/TESTING.md](docs/TESTING.md)
for the exact results and test evidence.

## Customer application

Exact screen flow (nothing else on screen):

1. one large button: **`tech check`**
2. **`confirm code`** + a simple input for the support code (the code is `1234`)
3. after the correct code: **`GETTING READY`** (connection setup runs in the
   background; the app never claims a technician is connected before one is)
4. when a technician actually requests access: a clear consent screen —
   approve *screen viewing* and *mouse/keyboard control* separately
5. session screen: visible sharing status and a large **`end`** button

The customer can end the session at any time; ending stops capture and input
immediately.

## Technician console

Open **http://127.0.0.1:8765** in Chrome (bound to `127.0.0.1` on purpose —
this local console is never exposed to the network). It shows:

* whether a customer is connected (live status pill)
* the customer's real selected X11 desktop, updating in real time
* what the customer currently allows (view / control badges)
* mouse, click, scroll and keyboard control of the remote desktop
* an **End session** button that cuts remote access immediately

## Quick start (everything on this machine)

```bash
./install.sh --no-launch            # deps check + venv + config (no root needed here)
./run-signaling-server.sh &         # local signaling for testing
./run-technician.sh &               # console on http://127.0.0.1:8765
./run-customer.sh --display :0      # or any display, e.g. --display 127.0.0.1:106
```

Then open http://127.0.0.1:8765, click **Connect** (code `1234`), switch to
the customer window, click `tech check` → enter `1234` → `GETTING READY` →
approve access. The screen appears in the console.

Demo without a physical desktop (virtual X11 display, exactly like the
customer environment with Xvfb/Openbox):

```bash
Xvfb :106 -screen 0 1280x800x24 -listen tcp &
./run-customer.sh --display 127.0.0.1:106
```

## Two separate computers (real internet use)

`127.0.0.1` addresses only work on the machine that runs them. For customer
and technician on different machines, both sides point at one shared
**matchmaker** (the signaling server). The published customer install command
above already contains this project's live matchmaker URL (baked into the
release), and the technician console reads the same URL from `config.env` —
see [docs/DEPLOY.md](docs/DEPLOY.md) for how it runs (Tailscale Funnel on the
operator's PC — no VPS needed) and for the always-on VPS + TURN alternative.

### A support session, step by step

1. **You (technician):** make sure the matchmaker is up on your PC
   (`systemctl --user status tech-support-signaling`), run
   `./run-technician.sh`, open http://127.0.0.1:8765, type `1234` and click
   **Connect** — the status pill shows you are waiting.
2. **Them (customer):** they run the install command; the app opens, they
   click `tech check`, type `1234`, click `GETTING READY`.
3. The matchmaker pairs you two automatically (first come, first served;
   extra customers queue). Their **consent screen** appears — they tick what
   they allow (viewing, and control if they want you to drive the mouse and
   keyboard).
4. Their desktop shows up live in your Chrome. **End session** on either
   side cuts it off immediately.

Screen video/audio and all input events then travel directly between the two
computers over end-to-end encrypted WebRTC (DTLS-SRTP); the matchmaker only
introduces the two peers, and TURN (when needed) relays ciphertext it
cannot read.

## Install

For customers there is one short command — it installs the app and starts it:

```bash
curl -fsSL https://raw.githubusercontent.com/feqq77/tech-support/main/get.sh | bash
```

* works on any Linux distribution and version (the release AppImage carries
  its own Python; a source fallback covers non-x86_64 machines)
* verifies the download against its published `.sha256` before running anything
* installs `tech-support` to `~/.local/bin` and launches the app automatically
  (`--no-launch` at the end of the command to skip that); run `tech-support`
  any time afterwards
* real error messages: nothing is invented — if the release does not exist
  yet, the command says exactly what is missing

The repository and its releases live at
[github.com/feqq77/tech-support](https://github.com/feqq77/tech-support) —
release assets must keep the exact name
`TechSupport-Customer-x86_64.AppImage` (+ `.sha256`) for the command above to
find them (exact steps in [docs/BUILD.md](docs/BUILD.md)).

Installing from this source tree (developer / technician machine):

```bash
./install.sh            # detects OS/arch, checks deps, installs what's missing, launches
./install.sh --check    # report only
./install.sh --no-launch
./uninstall.sh          # remove .venv, launchers (keeps config.env)
./uninstall.sh --purge-config
```

The installer:

* detects the operating system and architecture
* checks every dependency first and never reinstalls what exists
* uses a Python virtual environment inside the project directory
* asks for administrator privileges only when a required system package is
  genuinely missing (one `sudo` prompt, via `apt-get`)
* produces actionable error messages and stops on essential failures
* touches nothing outside the project directory (plus optional
  `~/.local/bin` command symlinks)
* starts the customer app when it finishes (`--no-launch` to skip)

## Architecture

```
customer computer                        technician computer
┌───────────────────────┐                ┌────────────────────────────┐
│ customer app (Tk)     │                │ Chrome                     │
│  consent UI           │                │  http://127.0.0.1:8765     │
│  X11 capture (XShm)   │◄══ WebRTC ════►│   video + input data chan. │
│  XTEST input inject   │  DTLS-SRTP     └─────────┬──────────────────┘
└──────────┬────────────┘                          │ localhost WS
           │ wss                                   ▼
           │            ┌──────────────┐   technician console server
           └───────────►│  signaling   │◄── (127.0.0.1 only, token auth)
                        │  server +    │
                        │  TURN relay  │
                        └──────────────┘
```

* **Screen capture**: real pixels of the *selected* X display (`:0`, `:106`,
  `127.0.0.1:106`, …) via XShm (mss), with an ffmpeg `x11grab` fallback. The
  display is verified before capture starts; failures are reported exactly.
* **Input control**: XTEST injection into that same display — mouse movement,
  buttons, wheel scrolling, keyboard (DOM key names mapped to X keysyms).
  Coordinates from the technician's (resized) video are sent normalized and
  mapped onto the exact desktop geometry, including root-window offsets.
* **Transport**: WebRTC VP8 video + a reliable data channel for input, with
  connection timeouts, disconnect detection and recovery on both sides.

## Security & customer trust

* Nothing is captured or controlled until the customer explicitly allows it;
  viewing and control are separate permissions, visible at all times.
* Either side can end the session instantly; ending stops capture and input
  immediately on the customer machine.
* Technician access requires a technician token (constant-time checked) —
  the browser never sees the token; the local console server injects it.
* All media and input traffic is end-to-end encrypted (WebRTC DTLS-SRTP).
* The technician console binds to `127.0.0.1` and refuses any other bind.
* No stealth features: no keylogging, no persistence, no hidden access, no
  credential collection. The visible Tk window is the application.
* The support code is fixed at **`1234`** for this alpha (a small, known
  community — a deliberate choice). Change it with the signaling server's
  `--code` option if needed; add generated, short-lived codes before any
  wider deployment. Session identifiers are random.

## Project layout

```
app/            customer application (UI, capture, input, WebRTC session)
technician/     technician console server + web UI (Chrome)
server/         signaling server + deployment templates (nginx, coturn, systemd)
tests/          51 automated tests incl. real-Chrome end-to-end (docs/TESTING.md)
docs/           deployment, build and testing documentation
get.sh          one-line customer installer (curl | bash, installs + auto-launches)
install.sh      dependency bootstrap installer (source tree)
uninstall.sh    clean removal
run-*.sh        launchers (customer / technician / signaling)
build_appimage.sh   customer AppImage build
```

## Requirements

Ubuntu 24.04 (X11) is the primary target. The customer app needs a working X
display (local, remote or Xvfb-based). Python ≥ 3.10; everything else is
installed by `install.sh`.
