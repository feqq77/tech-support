/* Technician console: WebRTC receiver + remote input sender.
 *
 * All input events are forwarded over the WebRTC data channel "input" as
 * normalized coordinates (0..1 of the remote screen), so the customer side can
 * map them onto the exact desktop it captures regardless of how the video is
 * scaled in this window.  Input is only sent after the customer explicitly
 * allowed control.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const video = $("remote-video");
const wrap = $("screen-wrap");
const overlay = $("screen-overlay");
const statusEl = $("status");
const logEl = $("log");

let ws = null;
let pc = null;
let inputChannel = null;
let iceServers = [];
let state = "disconnected";
let controlAllowed = false;
let viewAllowed = false;
let currentCode = "";

function log(line) {
  const t = new Date().toLocaleTimeString();
  logEl.textContent = `[${t}] ${line}\n` + logEl.textContent.slice(0, 4000);
}

function setStatus(text, cls) {
  state = cls;
  statusEl.textContent = text;
  statusEl.className = "pill " + ({
    "pill-off": "pill-off", "pill-wait": "pill-wait",
    "pill-live": "pill-live", "pill-err": "pill-err"
  }[cls] || "pill-off");
  $("end-btn").disabled = !(cls === "pill-live" || cls === "pill-wait");
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

/* Input events travel over the WebRTC data channel ("input"), not signaling. */
function sendInput(msg) {
  if (inputChannel && inputChannel.readyState === "open") {
    inputChannel.send(JSON.stringify(msg));
  }
}

/* ------------------------------------------------------------- signaling */

function connectSignaling(code) {
  if (ws) try { ws.close(); } catch (_) {}
  setStatus("connecting…", "pill-wait");
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    log("connected to local console server");
    // The console server injects the technician token; the browser never sees it.
    send({ type: "hello", role: "technician", code: code });
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    handleSignal(msg);
  };
  ws.onclose = () => {
    if (state !== "pill-off") {
      setStatus("console link lost — retrying…", "pill-err");
      setTimeout(() => { if (currentCode) connectSignaling(currentCode); }, 3000);
    }
  };
  ws.onerror = () => log("console link error");
}

async function handleSignal(msg) {
  switch (msg.type) {
    case "welcome":
      iceServers = msg.ice_servers || [];
      ensurePeer();
      if (msg.peer === "customer") {
        setStatus("customer present — waiting for approval", "pill-wait");
        overlay.textContent = "waiting for customer approval…";
      } else {
        setStatus("waiting for customer…", "pill-wait");
        overlay.textContent = "waiting for a customer to join…";
      }
      break;

    case "peer-joined":
      setStatus("customer present — waiting for approval", "pill-wait");
      overlay.textContent = "waiting for customer approval…";
      log("customer joined");
      break;

    case "peer-left":
      log("customer disconnected");
      teardownPeer();
      setStatus("waiting for customer…", "pill-wait");
      overlay.textContent = "no customer connected";
      resetConsentBadges();
      break;

    case "signal":
      if (msg.kind === "offer") await onOffer(msg.payload);
      else if (msg.kind === "ice") await onRemoteIce(msg.payload);
      break;

    case "consent":
      viewAllowed = !!msg.view;
      controlAllowed = !!msg.control;
      updateConsentBadges();
      log(`customer permissions — view: ${viewAllowed}, control: ${controlAllowed}`);
      break;

    case "error":
      setStatus(msg.message || "error", "pill-err");
      log("server error: " + (msg.message || msg.code));
      break;
  }
}

function ensurePeer() {
  if (pc) return;
  pc = new RTCPeerConnection({ iceServers: iceServers });
  pc.onicecandidate = (ev) => {
    if (ev.candidate) {
      send({
        type: "signal", kind: "ice",
        payload: {
          candidate: ev.candidate.candidate,
          sdpMid: ev.candidate.sdpMid,
          sdpMLineIndex: ev.candidate.sdpMLineIndex,
        },
      });
    }
  };
  pc.ontrack = (ev) => {
    video.srcObject = ev.streams[0];
    overlay.style.display = "none";
    setStatus("live", "pill-live");
    log("receiving remote screen");
  };
  pc.ondatachannel = (ev) => {
    if (ev.channel.label === "input") {
      inputChannel = ev.channel;
      log("input channel ready");
    }
  };
  pc.onconnectionstatechange = () => {
    log("webrtc state: " + pc.connectionState);
    if (pc.connectionState === "failed") {
      setStatus("connection failed", "pill-err");
      overlay.style.display = "flex";
      overlay.textContent = "connection failed — check network / TURN relay";
    } else if (pc.connectionState === "disconnected") {
      setStatus("connection interrupted…", "pill-err");
    }
  };
}

async function onOffer(payload) {
  ensurePeer();
  await pc.setRemoteDescription({ type: "offer", sdp: payload.sdp });
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  send({ type: "signal", kind: "answer", payload: { type: "answer", sdp: pc.localDescription.sdp } });
}

async function onRemoteIce(payload) {
  if (!pc) return;
  try {
    if (payload.candidate) {
      await pc.addIceCandidate({
        candidate: payload.candidate,
        sdpMid: payload.sdpMid,
        sdpMLineIndex: payload.sdpMLineIndex,
      });
    } else {
      await pc.addIceCandidate(null);
    }
  } catch (e) {
    log("ice error: " + e.message);
  }
}

function teardownPeer() {
  if (pc) { try { pc.close(); } catch (_) {} pc = null; }
  inputChannel = null;
  video.srcObject = null;
  overlay.style.display = "flex";
}

function resetConsentBadges() {
  viewAllowed = controlAllowed = false;
  $("badge-view").hidden = true;
  $("badge-control").hidden = true;
}

function updateConsentBadges() {
  const bv = $("badge-view"), bc = $("badge-control");
  bv.hidden = false; bc.hidden = false;
  bv.textContent = viewAllowed ? "view allowed" : "view denied";
  bv.className = "badge" + (viewAllowed ? "" : " no");
  bc.textContent = controlAllowed ? "control allowed" : "control denied";
  bc.className = "badge" + (controlAllowed ? "" : " no");
}

/* ----------------------------------------------------- remote input events */

function normalizedCoords(ev) {
  // Map viewport coordinates to 0..1 within the actual video content box
  // (the video is letterboxed inside the wrapper because of object-fit:contain).
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return null;
  const rect = wrap.getBoundingClientRect();
  const scale = Math.min(rect.width / vw, rect.height / vh);
  const dispW = vw * scale, dispH = vh * scale;
  const offX = (rect.width - dispW) / 2, offY = (rect.height - dispH) / 2;
  const x = (ev.clientX - rect.left - offX) / dispW;
  const y = (ev.clientY - rect.top - offY) / dispH;
  return {
    x: Math.min(1, Math.max(0, x)),
    y: Math.min(1, Math.max(0, y)),
  };
}

let lastMove = 0, pendingMove = null;
function sendMove(ev) {
  const c = normalizedCoords(ev);
  if (!c) return;
  const now = performance.now();
  if (now - lastMove > 28) {
    lastMove = now;
    sendInput({ type: "move", x: c.x, y: c.y });
  } else {
    pendingMove = c;
    setTimeout(() => {
      if (pendingMove) {
        sendInput({ type: "move", x: pendingMove.x, y: pendingMove.y });
        pendingMove = null;
        lastMove = performance.now();
      }
    }, 30);
  }
}

function inputEnabled() {
  return controlAllowed && pc && pc.connectionState === "connected" &&
         inputChannel && inputChannel.readyState === "open";
}

wrap.addEventListener("mousemove", (ev) => { if (inputEnabled()) sendMove(ev); });

wrap.addEventListener("mousedown", (ev) => {
  ev.preventDefault();
  wrap.focus();
  if (!inputEnabled()) return;
  const c = normalizedCoords(ev);
  const button = ["left", "middle", "right"][ev.button] || "left";
  sendInput({ type: "button", button: button, down: true, x: c.x, y: c.y });
});

wrap.addEventListener("mouseup", (ev) => {
  ev.preventDefault();
  if (!inputEnabled()) return;
  const c = normalizedCoords(ev);
  const button = ["left", "middle", "right"][ev.button] || "left";
  sendInput({ type: "button", button: button, down: false, x: c.x, y: c.y });
});

wrap.addEventListener("contextmenu", (ev) => { if (inputEnabled()) ev.preventDefault(); });

wrap.addEventListener("wheel", (ev) => {
  if (!inputEnabled()) return;
  ev.preventDefault();
  const dy = ev.deltaY < 0 ? 1 : -1;
  const dx = ev.deltaX < 0 ? 1 : -1;
  sendInput({ type: "scroll", dx: dx, dy: dy });
}, { passive: false });

// Keyboard: only while the screen area has focus (so normal typing in the
// code field never reaches the customer).
const PASS_THROUGH = new Set(["F5", "F11", "F12"]);
wrap.addEventListener("keydown", (ev) => {
  if (!inputEnabled()) return;
  if (PASS_THROUGH.has(ev.key)) return;
  ev.preventDefault();
  if (!ev.repeat) sendInput({ type: "key", key: ev.key, down: true });
  else ev.preventDefault();
});
wrap.addEventListener("keyup", (ev) => {
  if (!inputEnabled()) return;
  if (PASS_THROUGH.has(ev.key)) return;
  ev.preventDefault();
  sendInput({ type: "key", key: ev.key, down: false });
});

/* ------------------------------------------------------------------ buttons */

$("connect-btn").addEventListener("click", () => {
  const code = $("code-input").value.trim();
  if (!code) { log("enter the support code first"); return; }
  currentCode = code;
  connectSignaling(code);
});
$("code-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("connect-btn").click();
});

$("end-btn").addEventListener("click", () => {
  log("ending session…");
  send({ type: "leave" });
  teardownPeer();
  resetConsentBadges();
  setStatus("session ended", "pill-off");
  overlay.textContent = "no customer connected";
  currentCode = "";
  if (ws) { try { ws.close(); } catch (_) {} ws = null; }
});

window.addEventListener("beforeunload", () => {
  send({ type: "leave" });
  if (pc) try { pc.close(); } catch (_) {}
});
