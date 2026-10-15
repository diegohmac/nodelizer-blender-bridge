# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's requests and answers, checked without Blender.

The host writes one JSON request per line on the worker's stdin. The worker answers on stdout,
one JSON object per line after a marker, since Blender prints its own lines there too. Every
request is checked against a fixed list before anything runs; there is no request that runs code.
"""

from __future__ import annotations

import json
import math
import os
import re

PROTOCOL = 1
# Answers start a line with this, so Blender's own output on stdout is told apart.
MARK = "@nlz "
# A batch of edits can be long; models never travel this way, only file paths.
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STEPS = 16
MAX_OPS = 10_000
MAX_REPLAY = 2_000
# Coordinates past this are a mistake, not a model.
MAX_COORDINATE = 1e7

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KEY = re.compile(r"^[A-Za-z0-9:_.-]{1,200}$")
_SNAPSHOT_NAME = re.compile(r"^r[0-9]{1,9}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")

STEPS = ("load", "import", "apply", "save", "snapshot", "export")
OPS = ("set_transform", "set_vertices")
# The most vertices one edit moves.
MAX_VERTICES = 16_000_000
SOURCE_EXTENSIONS = (".glb", ".fbx", ".obj", ".stl", ".usdz")
OUTPUT_FORMATS = ("glb", "fbx")


class RequestError(Exception):
    """A request the worker won't run. `code` is for the host, the message for its log."""

    def __init__(self, message: str, code: str = "bad_request", index: int | None = None):
        super().__init__(message)
        self.code = code
        self.index = index


def encode(message: dict) -> bytes:
    """One answer line. The leading newline ends whatever Blender left half-written."""
    return ("\n" + MARK + json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def _path(value, extensions: tuple[str, ...], what: str) -> str:
    """An absolute path inside a project's `blender` folder, with one of `extensions`."""
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise RequestError(f"{what} isn't a path")
    if not os.path.isabs(value):
        raise RequestError(f"{what} must be absolute")
    normal = os.path.normpath(value)
    if normal != value:
        raise RequestError(f"{what} isn't a plain path")
    parts = re.split(r"[\\/]+", normal)
    if ".." in parts or "blender" not in parts[:-1]:
        raise RequestError(f"{what} must be in a project's blender folder")
    if extensions and not normal.lower().endswith(extensions):
        raise RequestError(f"{what} has the wrong extension")
    return normal


def _folder(value, what: str) -> str:
    return _path(value, (), what)


def _vector(value, size: int, what: str) -> list[float]:
    if not isinstance(value, list) or len(value) != size:
        raise RequestError(f"{what} needs {size} numbers", "op_invalid")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise RequestError(f"{what} needs numbers", "op_invalid")
        number = float(item)
        if not math.isfinite(number) or abs(number) > MAX_COORDINATE:
            raise RequestError(f"{what} is out of range", "op_invalid")
        out.append(number)
    return out


def check_op(op, index: int) -> dict:
    """One edit, with only the fields it may have."""
    if not isinstance(op, dict) or op.get("op") not in OPS:
        raise RequestError("unknown operation", "op_invalid", index)
    try:
        if op["op"] == "set_transform":
            allowed = {"op", "object", "location", "rotation", "scale"}
            if set(op) - allowed:
                raise RequestError("unknown field", "op_invalid")
            if not isinstance(op.get("object"), str) or not _ID.match(op["object"]):
                raise RequestError("bad object id", "op_invalid")
            out = {"op": "set_transform", "object": op["object"]}
            if "location" in op:
                out["location"] = _vector(op["location"], 3, "location")
            if "rotation" in op:
                rotation = _vector(op["rotation"], 4, "rotation")
                length = math.sqrt(sum(c * c for c in rotation))
                if length < 1e-6:
                    raise RequestError("rotation isn't a rotation", "op_invalid")
                out["rotation"] = [c / length for c in rotation]
            if "scale" in op:
                out["scale"] = _vector(op["scale"], 3, "scale")
            if len(out) == 2:
                raise RequestError("nothing to change", "op_invalid")
            return out
        if op["op"] == "set_vertices":
            if set(op) != {"op", "object", "count", "data", "file"}:
                raise RequestError("set_vertices takes object, count, data and file", "op_invalid")
            if not isinstance(op["object"], str) or not _ID.match(op["object"]):
                raise RequestError("bad object id", "op_invalid")
            count = op["count"]
            whole = isinstance(count, int) and not isinstance(count, bool)
            if not whole or not 1 <= count <= MAX_VERTICES:
                raise RequestError("bad vertex count", "op_invalid")
            if not isinstance(op["data"], str) or not _SHA.match(op["data"]):
                raise RequestError("bad vertex file hash", "op_invalid")
            try:
                file = _path(op["file"], (".bin",), "file")
            except RequestError as error:
                raise RequestError(str(error), "op_invalid") from error
            return {
                "op": "set_vertices",
                "object": op["object"],
                "count": count,
                "data": op["data"],
                "file": file,
            }
    except RequestError as error:
        error.index = index
        raise
    raise RequestError("unknown operation", "op_invalid", index)


def check_ops(ops) -> list[dict]:
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise RequestError("ops must be a list", "op_invalid")
    return [check_op(op, index) for index, op in enumerate(ops)]


def _key(value, what: str) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise RequestError(f"{what} isn't a key")
    return value


def check_step(step) -> dict:
    """One step of a request, with its paths and edits checked."""
    if not isinstance(step, dict) or step.get("type") not in STEPS:
        raise RequestError("unknown step")
    kind = step["type"]
    if kind == "load":
        replay = step.get("replay", [])
        if not isinstance(replay, list) or len(replay) > MAX_REPLAY:
            raise RequestError("replay must be a list")
        blend = step.get("blend")
        return {
            "type": "load",
            "key": _key(step.get("key"), "key"),
            "blend": None if blend is None else _path(blend, (".blend",), "blend"),
            "replay": [check_ops(ops) for ops in replay],
        }
    if kind == "import":
        return {
            "type": "import",
            "key": _key(step.get("key"), "key"),
            "source": _path(step.get("source"), SOURCE_EXTENSIONS, "source"),
        }
    if kind == "apply":
        return {
            "type": "apply",
            "from": _key(step.get("from"), "from"),
            "to": _key(step.get("to"), "to"),
            "ops": check_ops(step.get("ops")),
        }
    if kind == "save":
        return {"type": "save", "blend": _path(step.get("blend"), (".blend",), "blend")}
    if kind == "snapshot":
        name = step.get("name")
        if not isinstance(name, str) or not _SNAPSHOT_NAME.match(name):
            raise RequestError("bad snapshot name")
        appearance = step.get("appearance")
        if appearance not in (None, "export") and not (
            isinstance(appearance, str) and _SHA.match(appearance)
        ):
            raise RequestError("bad appearance")
        return {
            "type": "snapshot",
            "folder": _folder(step.get("folder"), "folder"),
            "name": name,
            "appearance": appearance,
        }
    fmt = step.get("format")
    if fmt not in OUTPUT_FORMATS:
        raise RequestError("unknown format")
    return {
        "type": "export",
        "path": _path(step.get("path"), (f".{fmt}",), "path"),
        "format": fmt,
    }


def decode(line: bytes) -> dict:
    """A request line, checked: `{"id", "type": "run", "steps": [...]}`, `ping` or `quit`."""
    if len(line) > MAX_LINE_BYTES:
        raise RequestError("line too long")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RequestError("not JSON") from error
    if not isinstance(data, dict):
        raise RequestError("not an object")
    request_id = data.get("id")
    if not isinstance(request_id, str) or not _ID.match(request_id):
        raise RequestError("bad id")
    kind = data.get("type")
    if kind in ("ping", "quit"):
        return {"id": request_id, "type": kind}
    if kind != "run":
        raise RequestError("unknown request")
    steps = data.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise RequestError("steps must be a list")
    return {"id": request_id, "type": "run", "steps": [check_step(step) for step in steps]}


def request_id(line: bytes) -> str | None:
    """The id of a request that didn't check out, so its answer still reaches the host."""
    try:
        value = json.loads(line.decode("utf-8")).get("id")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and _ID.match(value) else None
