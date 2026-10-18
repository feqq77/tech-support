"""Shared signaling protocol constants and helpers.

Message envelope: JSON objects with a mandatory "type" field.

Client -> Server
    hello    {"type":"hello","role":"customer|technician","code":str,"token":str?}
    signal   {"type":"signal","kind":"offer|answer|ice","payload":{...}}
    consent  {"type":"consent","view":bool,"control":bool}        (customer -> technician)
    leave    {"type":"leave"}
    ping     {"type":"ping"}

Server -> Client
    welcome       {"type":"welcome","session":str,"peer":role|null,"ice_servers":[...]}
    peer-joined   {"type":"peer-joined","role":str}
    peer-left     {"type":"peer-left","role":str}
    signal        {"type":"signal","kind":"offer|answer|ice","payload":{...}}
    consent       {"type":"consent","view":bool,"control":bool}
    error         {"type":"error","code":str,"message":str}
    pong          {"type":"pong"}

The signaling server only pairs sessions and relays messages; all media and
input traffic flows over end-to-end encrypted WebRTC (DTLS-SRTP) between the
customer application and the technician browser.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1

ROLE_CUSTOMER = "customer"
ROLE_TECHNICIAN = "technician"

# client -> server
MSG_HELLO = "hello"
MSG_SIGNAL = "signal"
MSG_CONSENT = "consent"
MSG_LEAVE = "leave"
MSG_PING = "ping"

# server -> client
MSG_WELCOME = "welcome"
MSG_PEER_JOINED = "peer-joined"
MSG_PEER_LEFT = "peer-left"
MSG_ERROR = "error"
MSG_PONG = "pong"

SIGNAL_OFFER = "offer"
SIGNAL_ANSWER = "answer"
SIGNAL_ICE = "ice"

ERR_BAD_TOKEN = "bad-token"
ERR_BAD_CODE = "bad-code"
ERR_BAD_MESSAGE = "bad-message"
ERR_SESSION_LIMIT = "session-limit"
ERR_INTERNAL = "internal"

# The support code for this alpha is fixed at 1234 (small community).
DEFAULT_SUPPORT_CODE = "1234"


class ProtocolError(ValueError):
    """Raised when a signaling message is malformed."""


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


def decode(raw: str | bytes) -> dict[str, Any]:
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("message must be a JSON object")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError("message missing 'type'")
    return msg


def hello(role: str, code: str, token: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": MSG_HELLO,
        "role": role,
        "code": code,
        "protocol": PROTOCOL_VERSION,
    }
    if token:
        msg["token"] = token
    return msg


def signal(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": MSG_SIGNAL, "kind": kind, "payload": payload}


def error(code: str, message: str) -> dict[str, Any]:
    return {"type": MSG_ERROR, "code": code, "message": message}


def validate_hello(msg: dict[str, Any]) -> tuple[str, str]:
    role = msg.get("role")
    code = msg.get("code")
    if role not in (ROLE_CUSTOMER, ROLE_TECHNICIAN):
        raise ProtocolError(f"invalid role: {role!r}")
    if not isinstance(code, str) or not code.strip():
        raise ProtocolError("missing support code")
    code = code.strip()
    if len(code) > 64:
        raise ProtocolError("support code too long")
    return role, code


def validate_signal(msg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = msg.get("kind")
    if kind not in (SIGNAL_OFFER, SIGNAL_ANSWER, SIGNAL_ICE):
        raise ProtocolError(f"invalid signal kind: {kind!r}")
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError("signal payload must be an object")
    return kind, payload
