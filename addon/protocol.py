# SPDX-License-Identifier: GPL-3.0-or-later
"""The messages the bridge and the host app exchange, one JSON object per line.

Every message has a "type" from a fixed list. Anything else is dropped, so the host can never
make Blender run code: it can only ask for one of the commands below. See docs/PROTOCOL.md.
"""

from __future__ import annotations

import json
import re

PROTOCOL = 1
# A message is a short status line. Models travel as files, never over the socket.
MAX_LINE_BYTES = 64 * 1024

_TEXT = 200
_FORMATS = ("GLB", "FBX")


def _text(value, limit=_TEXT):
    return isinstance(value, str) and 0 < len(value) <= limit


def encode(message: dict) -> bytes:
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(data) >= MAX_LINE_BYTES:
        raise ValueError("message too long")
    return data + b"\n"


def decode(line: bytes):
    """A message from the host, or None when it isn't one this bridge understands."""
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "WELCOME":
        if message.get("protocol") != PROTOCOL:
            return None
        return {"type": "WELCOME", "protocol": PROTOCOL}
    if kind == "REJECTED":
        reason = message.get("reason")
        return {"type": "REJECTED", "reason": reason if _text(reason, 40) else "unknown"}
    if kind == "EXPORT":
        if message.get("format") not in _FORMATS:
            return None
        return {"type": "EXPORT", "format": message["format"]}
    if kind == "PING":
        return {"type": "PING"}
    return None


_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def valid_token(value) -> bool:
    return isinstance(value, str) and bool(_TOKEN.match(value))


def hello(session_id: str, token: str, bridge_version: str, blender_version: str) -> dict:
    return {
        "type": "HELLO",
        "protocol": PROTOCOL,
        "sessionId": session_id,
        "token": token,
        "bridgeVersion": bridge_version,
        "blenderVersion": blender_version,
    }


def session_ready(session_id: str) -> dict:
    return {"type": "SESSION_READY", "sessionId": session_id}


def asset_changed(session_id: str, result: str) -> dict:
    return {"type": "ASSET_CHANGED", "sessionId": session_id, "result": result}


def return_to_app(session_id: str, result: str) -> dict:
    return {"type": "RETURN_TO_APP", "sessionId": session_id, "result": result}


def open_app(session_id: str) -> dict:
    return {"type": "OPEN_APP", "sessionId": session_id}


def failed(session_id: str, stage: str) -> dict:
    """Something the person asked for didn't work. Only the stage goes out, never a path."""
    if stage not in ("import", "export", "save"):
        stage = "export"
    return {"type": "FAILED", "sessionId": session_id, "stage": stage}
