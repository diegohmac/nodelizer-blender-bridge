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
    if len(data) < 12:
        raise ValueError("not a mesh file")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != MAGIC or version != VERSION or 12 + length > len(data):
        raise ValueError("not a mesh file")
    try:
        header = json.loads(data[12 : 12 + length].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("the header is damaged") from error
    if not isinstance(header, dict) or not isinstance(header.get("arrays"), list):
        raise ValueError("the header is damaged")
    return header


def read(data: bytes) -> tuple[dict, dict[str, tuple[str, memoryview]]]:
    """A file's header and its arrays' raw little-endian bytes, by name, each checked to fit."""
    header = read_header(data)
    _magic, _version, length = struct.unpack_from("<4sII", data, 0)
    start = 12 + length + _pad(12 + length)
    view = memoryview(data)
    arrays = {}
    for entry in header["arrays"]:
        if not isinstance(entry, dict):
            raise ValueError("a damaged array")
        name, kind = entry.get("name"), entry.get("type")
        offset, count = entry.get("offset"), entry.get("length")
        if (
            not isinstance(name, str)
            or kind not in SIZES
            or not isinstance(offset, int)
            or not isinstance(count, int)
            or offset < 0
            or count < 0
            or offset % ALIGN
        ):
            raise ValueError("a damaged array")
        end = start + offset + count * SIZES[kind]
        if end > len(data):
            raise ValueError("the file is cut short")
        arrays[name] = (kind, view[start + offset : end])
    return header, arrays


def read_vertex_positions(data: bytes) -> tuple[int, memoryview, memoryview]:
    """A vertex edit's file, as the host writes it: how many vertices, their indices (u32) and
    their new places (three f32 each), as raw little-endian bytes."""
    header, arrays = read(data)
    count = (header.get("counts") or {}).get("vertices")
    indices = arrays.get("indices")
    positions = arrays.get("positions")
    if (
        header.get("kind") != "vertices"
        or not isinstance(count, int)
        or isinstance(count, bool)
        or indices is None
        or positions is None
        or indices[0] != "u32"
        or positions[0] != "f32"
        or len(indices[1]) != count * 4
        or len(positions[1]) != count * 12
    ):
        raise ValueError("not a file of vertex positions")
    return count, indices[1], positions[1]


def read_components(
    data: bytes, kinds: tuple[str, ...] = ("vertices", "edges", "faces")
) -> tuple[str, int, memoryview]:
    """A file of components an edit works on, as the host writes it: which kind (one of `kinds`:
    `vertices`, `edges` or `faces`, or `corners` for a UV edit), how many and their indices (u32,
    increasing), as raw little-endian bytes."""
    header, arrays = read(data)
    component = header.get("component")
    count = (header.get("counts") or {}).get("items")
    indices = arrays.get("indices")
    if (
        header.get("kind") != "components"
        or component not in kinds
        or not isinstance(count, int)
        or isinstance(count, bool)
        or indices is None
        or indices[0] != "u32"
        or len(indices[1]) != count * 4
    ):
        raise ValueError("not a file of components")
    return component, count, indices[1]


def read_weights(data: bytes) -> tuple[int, memoryview, memoryview, memoryview]:
    """A weight edit's file, as the host writes it: how many weights, and for each its vertex (u32),
    its group by place in the edit's list of names (u16) and its value (f32), as raw little-endian
    bytes, ordered by vertex and then group."""
    header, arrays = read(data)
    count = (header.get("counts") or {}).get("weights")
    vertices = arrays.get("vertices")
    groups = arrays.get("groups")
    weights = arrays.get("weights")
    if (
        header.get("kind") != "weights"
        or not isinstance(count, int)
        or isinstance(count, bool)
        or vertices is None
        or groups is None
        or weights is None
        or vertices[0] != "u32"
        or groups[0] != "u16"
        or weights[0] != "f32"
        or len(vertices[1]) != count * 4
        or len(groups[1]) != count * 2
        or len(weights[1]) != count * 4
    ):
        raise ValueError("not a file of weights")
    return count, vertices[1], groups[1], weights[1]


def read_uvs(data: bytes) -> tuple[int, memoryview, memoryview]:
    """A UV edit's file, as the host writes it: how many face corners, their indices (u32) and their
    new UVs (two f32 each), as raw little-endian bytes."""
    header, arrays = read(data)
    count = (header.get("counts") or {}).get("corners")
    corners = arrays.get("corners")
    uvs = arrays.get("uvs")
    if (
        header.get("kind") != "uvs"
        or not isinstance(count, int)
        or isinstance(count, bool)
        or corners is None
        or uvs is None
        or corners[0] != "u32"
        or uvs[0] != "f32"
        or len(corners[1]) != count * 4
        or len(uvs[1]) != count * 8
    ):
        raise ValueError("not a file of UVs")
    return count, corners[1], uvs[1]
