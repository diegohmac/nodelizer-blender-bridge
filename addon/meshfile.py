# SPDX-License-Identifier: GPL-3.0-or-later
"""The editable mesh file: one mesh's real vertices, edges, faces and face corners.

    "NLZM"                      4 bytes
    version                     uint32, little-endian (1)
    header length               uint32, little-endian
    header                      UTF-8 JSON
    zero padding                to a multiple of 16 bytes
    data                        the arrays, each starting on a multiple of 16 bytes

The header lists each array as `{"name", "type", "offset", "length"}`, where `offset` is in bytes
from the start of the data and `length` counts elements. Types are f32, u32, u16 and u8, all
little-endian. See docs/PROTOCOL.md for the arrays a mesh has.
"""

from __future__ import annotations

import hashlib
import json
import struct

MAGIC = b"NLZM"
VERSION = 1
ALIGN = 16
SIZES = {"f32": 4, "u32": 4, "u16": 2, "u8": 1}


def _pad(length: int) -> int:
    return (ALIGN - length % ALIGN) % ALIGN


def pack(arrays, header: dict | None = None) -> bytes:
    """The file for `arrays`, a list of `(name, type, data)` where data has `tobytes()`."""
    listed = []
    blobs = []
    offset = 0
    for name, kind, data in arrays:
        if kind not in SIZES:
            raise ValueError(f"unknown type {kind}")
        raw = data.tobytes()
        if len(raw) % SIZES[kind]:
            raise ValueError(f"{name} isn't whole {kind} values")
        listed.append(
            {"name": name, "type": kind, "offset": offset, "length": len(raw) // SIZES[kind]}
        )
        blobs.append(raw)
        blobs.append(b"\0" * _pad(len(raw)))
        offset += len(raw) + _pad(len(raw))
    meta = dict(header or {})
    meta["arrays"] = listed
    text = json.dumps(meta, separators=(",", ":"), sort_keys=True).encode("utf-8")
    start = struct.pack("<4sII", MAGIC, VERSION, len(text)) + text
    return b"".join([start, b"\0" * _pad(len(start)), *blobs])


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_header(data: bytes) -> dict:
    """The header of a mesh file, for tests and checks."""
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != MAGIC or version != VERSION:
        raise ValueError("not a mesh file")
    return json.loads(data[12 : 12 + length].decode("utf-8"))
