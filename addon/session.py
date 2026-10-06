# SPDX-License-Identifier: GPL-3.0-or-later
"""The session folder the host app prepares, and the files the bridge writes back into it.

The host passes the folder in NODELIZER_BRIDGE_SESSION. Everything about it is checked before
use: its name, every field of session.json, and the names of the files read and written. The
bridge only ever writes inside the session folder, plus the .blend file session.json names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

SESSION_ENV = "NODELIZER_BRIDGE_SESSION"
PORT_ENV = "NODELIZER_BRIDGE_PORT"
TOKEN_ENV = "NODELIZER_BRIDGE_TOKEN"

SCHEMA = 1
_FOLDER = re.compile(r"^session-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SOURCE = re.compile(r"^source\.(glb|fbx|obj)$")
_MAX_JSON = 64 * 1024
FORMATS = ("glb", "fbx")


class SessionError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    folder: str
    session_id: str
    project_name: str
    asset_name: str
    format: str
    source: str
    blend: str
    # True when the .blend was just made from the model, so the view frames it once.
    frame: bool = False

    @property
    def source_path(self) -> str:
        return os.path.join(self.folder, self.source)

    def result_name(self, fmt: str | None = None) -> str:
        fmt = fmt or self.format
        if fmt not in FORMATS:
            raise SessionError("unknown format")
        return f"result.{fmt}"

    def result_path(self, fmt: str | None = None) -> str:
        return os.path.join(self.folder, self.result_name(fmt))

    @property
    def lock_path(self) -> str:
        return os.path.join(self.folder, "blender.lock")

    @property
    def result_json(self) -> str:
        return os.path.join(self.folder, "result.json")


def _name(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\x00" in value:
        raise SessionError("bad name")
    return value.strip()


def load(folder: str) -> Session:
    """Reads and checks session.json in `folder`. Raises SessionError for anything unexpected."""
    if not folder or not os.path.isabs(folder) or "\x00" in folder:
        raise SessionError("session folder must be an absolute path")
    folder = os.path.realpath(folder)
    match = _FOLDER.match(os.path.basename(folder))
    if not match or not os.path.isdir(folder):
        raise SessionError("not a session folder")
    file = os.path.join(folder, "session.json")
    if os.path.islink(file) or not os.path.isfile(file) or os.path.getsize(file) > _MAX_JSON:
        raise SessionError("session.json is missing")
    try:
        with open(file, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise SessionError("session.json can't be read") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise SessionError("unknown session schema")
    if data.get("sessionId") != match.group(1):
        raise SessionError("session id doesn't match its folder")
    fmt = data.get("format")
    if fmt not in FORMATS:
        raise SessionError("unknown format")
    source = data.get("source")
    if not isinstance(source, str) or not _SOURCE.match(source):
        raise SessionError("bad source name")
    blend = data.get("blend")
    if (
        not isinstance(blend, str)
        or "\x00" in blend
        or not os.path.isabs(blend)
        or not blend.endswith(".blend")
        or os.path.basename(os.path.dirname(blend)) != "blender"
    ):
        raise SessionError("bad .blend path")
    return Session(
        folder=folder,
        session_id=match.group(1),
        project_name=_name(data.get("projectName")),
        asset_name=_name(data.get("assetName")),
        format=fmt,
        source=source,
        blend=os.path.normpath(blend),
        frame=data.get("frame") is True,
    )


def same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _atomic_write(path: str, data: bytes):
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_result(session: Session, fmt: str, kind: str) -> str:
    """Records the result just exported, so the host finds it even if the socket is down."""
    if kind not in ("sync", "return"):
        raise SessionError("unknown result kind")
    name = session.result_name(fmt)
    record = {
        "schema": SCHEMA,
        "sessionId": session.session_id,
        "result": name,
        "format": fmt,
        "sha256": sha256(os.path.join(session.folder, name)),
        "kind": kind,
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write(session.result_json, json.dumps(record, indent=2).encode("utf-8"))
    return name


def heartbeat(session: Session):
    """Tells the host this session is still open in Blender, even across host restarts."""
    record = {"pid": os.getpid(), "at": int(time.time())}
    try:
        _atomic_write(session.lock_path, json.dumps(record).encode("utf-8"))
    except OSError:
        pass


def release(session: Session):
    try:
        os.remove(session.lock_path)
    except OSError:
        pass
