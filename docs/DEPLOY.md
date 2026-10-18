# Deploying the support infrastructure (two computers over the internet)

The customer app and the technician console talk **directly to each other over
WebRTC** (end-to-end encrypted with DTLS-SRTP). They need two shared services
to find each other across the internet:

| Service      | Purpose                                              | Required? |
|--------------|------------------------------------------------------|-----------|
| signaling    | pairs customer + technician, relays SDP/ICE          | always    |
| TURN relay   | forwards the encrypted media when direct P2P fails   | strongly recommended |

Everything below runs on one small VPS (1 vCPU, 1 GB RAM is enough) with a
public DNS name, e.g. `support.example.com`. **Nothing here is invented:
replace the example names with your real host.**

## 0. Quick path: your own PC + Tailscale Funnel (no VPS, no domain)

For a small community the matchmaker can run on **your own PC** and be
published to the internet through
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) — free, real TLS,
no router or DNS setup. That is the deployment this project currently ships
with:

1. Install Tailscale on the PC and sign in: https://tailscale.com/download
2. Publish the matchmaker (the CLI prints a one-time approval URL the first
   time — open it while signed in to the **same Tailscale account as the
   PC**, then re-run the command):

   ```bash
   tailscale funnel --bg http://127.0.0.1:8787
   ```

   The matchmaker is now reachable at `https://<machine>.<tailnet>.ts.net`
   with valid TLS (its `/healthz` should answer from any network).
3. Run the signaling server as a **user** service — copy
   `server/deploy/tech-support-signaling.user.service` to
   `~/.config/systemd/user/tech-support-signaling.service`, adjust the two
   paths, then:

   ```bash
   systemctl --user enable --now tech-support-signaling
   curl -s http://127.0.0.1:8787/healthz
   ```

   It listens on `127.0.0.1:8787` only (Tailscale proxies to it) and reads
   `config.env` for the technician token and support code.
4. Bake the matchmaker URL into the customer app so customers configure
   nothing:

   ```bash
   TECH_SUPPORT_SIGNALING_URL=wss://<machine>.<tailnet>.ts.net/ws ./build_appimage.sh --self-test
   ```

Honest limitations of this quick path:

* **your PC must be on** (and logged in) while you do support sessions
* there is **no TURN relay**: direct WebRTC works for most home networks (the
  matchmaker hands out STUN), but customers behind symmetric NAT / CGNAT
  (some mobile hotspots, strict corporate networks) may see "no picture".
  The VPS + coturn path below removes this limitation.
* the technician console still runs only on your PC (`http://127.0.0.1:8765`)

The rest of this document describes the robust path (always-on VPS + TURN).

## 1. Signaling server (WebSocket, behind TLS)

### 1.1 Install

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv nginx certbot python3-certbot-nginx

sudo mkdir -p /opt/support-signaling
sudo chown $USER /opt/support-signaling
cd /opt/support-signaling
# copy this project's server/ + app/ directories to the VPS, then:
python3 -m venv .venv
.venv/bin/pip install aiohttp

# generate a strong technician token (store it in a password manager too)
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

### 1.2 Systemd service

`/etc/systemd/system/support-signaling.service` (see `signaling.service` in
this directory):

```ini
[Unit]
Description=Remote support signaling server
After=network-online.target

[Service]
User=www-data
WorkingDirectory=/opt/support-signaling
Environment=SUPPORT_TECH_TOKEN=PASTE-YOUR-RANDOM-TOKEN-HERE
Environment=ICE_SERVERS_JSON=[{"urls":"stun:support.example.com:3478"},{"urls":"turn:support.example.com:3478","username":"support","credential":"PASTE-TURN-SECRET"}]
ExecStart=/opt/support-signaling/.venv/bin/python -m server.signaling_server --host 127.0.0.1 --port 8787
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Notes:
* The support code is `1234` by default (fine for this alpha and its small
  community). Use `--code SOMETHING` to pick a different single code; move to
  generated one-time codes before any wider deployment.
* The signaling process binds **127.0.0.1 only**; nginx publishes it over TLS.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now support-signaling
curl -s http://127.0.0.1:8787/healthz   # {"ok": true, ...}
```

### 1.3 TLS with nginx + Let's Encrypt

Use `nginx.conf` from this directory (adapt the server name), then:

```bash
sudo certbot --nginx -d support.example.com
sudo systemctl reload nginx
```

The signaling WebSocket is then available as `wss://support.example.com/ws`.
Set this URL in `config.env` on the customer machine (`SIGNALING_URL`) and on
the technician machine (`--signaling-url wss://support.example.com/ws`).

## 2. TURN relay (coturn)

Many home routers and mobile networks block direct peer-to-peer traffic. A
TURN relay forwards the **already end-to-end encrypted** media; it cannot read
it. Without TURN, sessions behind strict NATs will fail with a clear error.

```bash
sudo apt-get install -y coturn
```

Edit `/etc/turnserver.conf` (see `coturn.conf` in this directory):

```
listening-port=3478
tls-listening-port=5349
fingerprint
use-auth-secret
static-auth-secret=PASTE-A-LONG-RANDOM-SECRET
realm=support.example.com
min-port=49152
max-port=65535
no-cli
no-tlsv1
no-tlsv1_1
```

Enable it in `/etc/default/coturn` (`TURNSERVER_ENABLED=1`) and:

```bash
sudo systemctl enable --now coturn
```

Then put the TURN server into `ICE_SERVERS_JSON` on the signaling server
(as shown in the unit above). TURN credentials are generated per session by
the standard `use-auth-secret` mechanism; the long-term secret lives only on
the server.

Firewall (both services): allow **3478/tcp+udp**, **5349/tcp**, **49152-65535/udp**
(TURN media), **443/tcp** (nginx). Nothing else needs to be reachable.

## 3. Technician machine

```bash
./run-technician.sh --signaling-url wss://support.example.com/ws
# then open http://127.0.0.1:8765 in Chrome
```

The technician token is read from `config.env` (`SUPPORT_TECH_TOKEN`); set it
to the same value as on the signaling server. The console binds **127.0.0.1
only** and refuses to bind anywhere else - it is a local operator panel.

## 4. Customer machine

```bash
./run-customer.sh --display :0 --signaling-url wss://support.example.com/ws
```

(or launch the AppImage: `TechSupport-Customer-x86_64.AppImage --signaling-url wss://support.example.com/ws`)

## 5. Production checklist

- [ ] support code agreed with your community (alpha default: `1234`);
      switch to generated one-time codes before wider use
- [ ] `SUPPORT_TECH_TOKEN` is a long random value, stored only in
      `config.env` (mode 600) and the systemd unit
- [ ] TLS certificates on `support.example.com` (certbot renews automatically)
- [ ] TURN `static-auth-secret` is long and random
- [ ] Firewall exposes only 443/tcp, 3478, 5349, 49152-65535/udp
- [ ] `journalctl -u support-signaling` is monitored
