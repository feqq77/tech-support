"""TURN relay configuration: minted credentials and the ICE server list."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import time

from server.signaling_server import _load_ice_servers, _mint_turn_credentials


def test_minted_credentials_follow_coturn_static_auth_scheme():
    username, credential = _mint_turn_credentials("secret", lifetime_days=1)
    expiry_s, _, name = username.partition(":")
    assert name == "tech-support"
    expiry = int(expiry_s)
    assert abs(expiry - (time.time() + 86400)) < 30
    expected = base64.b64encode(
        hmac.new(b"secret", username.encode(), hashlib.sha1).digest()
    ).decode()
    assert credential == expected


def test_turn_relay_appended_from_environment(monkeypatch):
    monkeypatch.setenv("TECH_SUPPORT_TURN_URL", "turns:relay.example:443?transport=tcp")
    monkeypatch.setenv("TECH_SUPPORT_TURN_SECRET", "s3cret")
    monkeypatch.delenv("ICE_SERVERS_JSON", raising=False)
    monkeypatch.delenv("TECH_SUPPORT_TURN_USERNAME", raising=False)
    servers = _load_ice_servers(argparse.Namespace(ice_servers=None))
    assert servers[0]["urls"] == "stun:stun.l.google.com:19302"
    assert servers[-1]["urls"] == "turns:relay.example:443?transport=tcp"
    assert servers[-1]["username"].endswith(":tech-support")
    assert servers[-1]["credential"]


def test_turn_relay_fixed_credentials(monkeypatch):
    monkeypatch.setenv("TECH_SUPPORT_TURN_URL", "turn:relay.example:3478")
    monkeypatch.setenv("TECH_SUPPORT_TURN_USERNAME", "fixed-user")
    monkeypatch.setenv("TECH_SUPPORT_TURN_PASSWORD", "fixed-pass")
    monkeypatch.delenv("TECH_SUPPORT_TURN_SECRET", raising=False)
    monkeypatch.delenv("ICE_SERVERS_JSON", raising=False)
    servers = _load_ice_servers(argparse.Namespace(ice_servers=None))
    assert servers[-1] == {
        "urls": "turn:relay.example:3478",
        "username": "fixed-user",
        "credential": "fixed-pass",
    }


def test_without_turn_env_only_stun(monkeypatch):
    for name in (
        "TECH_SUPPORT_TURN_URL",
        "TECH_SUPPORT_TURN_SECRET",
        "TECH_SUPPORT_TURN_USERNAME",
        "TECH_SUPPORT_TURN_PASSWORD",
        "ICE_SERVERS_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    servers = _load_ice_servers(argparse.Namespace(ice_servers=None))
    assert servers == [{"urls": "stun:stun.l.google.com:19302"}]
