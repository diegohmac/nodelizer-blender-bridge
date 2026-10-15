# SPDX-License-Identifier: GPL-3.0-or-later
"""Edits of a mesh's UV layers: new UVs and pins for some face corners, new layers, and which layer
is active (the one Blender's UV editor shows) or rendered (the one textures use, and the glTF
exporter points them at).

UV layers belong to the mesh, by name, so every object using it changes with it. A layer's pins are
a boolean corner attribute named `.pn.<layer>`, which Blender makes only once something is pinned;
until then the layer's `pin` collection is empty."""

from __future__ import annotations

import numpy as np

from . import meshfile
from .worker_protocol import MAX_COORDINATE, MAX_UV_LAYERS, RequestError


def _read(op: dict, what: str) -> bytes:
    try:
        with open(op["file"], "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise RequestError(f"the {what} file is missing", "op_invalid") from error
    if meshfile.digest(data) != op["data"]:
        raise RequestError(f"the {what} file doesn't match its hash", "op_invalid")
    return data


def _corners(values: memoryview, mesh) -> np.ndarray:
    corners = np.frombuffer(values, dtype="<u4").astype(np.int64)
    if np.any(np.diff(corners) <= 0):
        raise RequestError("corners must go up, each once", "op_invalid")
    if corners[-1] >= len(mesh.loops):
        raise RequestError("a corner is past the mesh", "op_invalid")
    return corners


def check(op: dict, mesh, names: list[str]):
    """What a UV edit needs once it fits the mesh, given the mesh's layer names as the edits before
    it leave them (a layer added earlier in the same list counts)."""
    kind = op["op"]
    if kind == "add_uv_layer":
        if op["name"] in names:
            raise RequestError(f"there's a UV layer {op['name']!r} already", "op_invalid")
        if op["name"] in mesh.attributes:
            raise RequestError(f"the mesh has an attribute {op['name']!r} already", "op_invalid")
        if len(names) >= MAX_UV_LAYERS:
            raise RequestError(f"a mesh has at most {MAX_UV_LAYERS} UV layers", "op_invalid")
        if "copy" in op and op["copy"] not in names:
            raise RequestError(f"the mesh has no UV layer {op['copy']!r}", "op_invalid")
        names.append(op["name"])
        return None
    if op["layer"] not in names:
        raise RequestError(f"the mesh has no UV layer {op['layer']!r}", "op_invalid")
    if kind == "set_uv_layer":
        return None
    if kind == "set_uvs":
        try:
            count, corners, uvs = meshfile.read_uvs(_read(op, "UV"))
        except ValueError as error:
            raise RequestError(str(error), "op_invalid") from error
        if count != op["count"]:
            raise RequestError("the corner count doesn't match", "op_invalid")
        values = np.frombuffer(uvs, dtype="<f4").reshape(-1, 2)
        if not np.all(np.isfinite(values)) or np.any(np.abs(values) > MAX_COORDINATE):
            raise RequestError("a UV is out of range", "op_invalid")
        return _corners(corners, mesh), values
    try:
        _component, count, corners = meshfile.read_components(_read(op, "corner"), ("corners",))
    except ValueError as error:
        raise RequestError(str(error), "op_invalid") from error
    if count != op["count"]:
        raise RequestError("the corner count doesn't match", "op_invalid")
    return _corners(corners, mesh)


def _pins(mesh, name: str, make: bool):
    """The layer's pins, made when `make` and it has none yet; None when it has none."""
    layer = mesh.uv_layers[name]
    if len(layer.pin) == len(mesh.loops):
        return layer.pin
    if not make:
        return None
    mesh.attributes.new(f".pn.{name}", "BOOLEAN", "CORNER")
    # Looked up again: the layer's collections are read when it's fetched.
    return mesh.uv_layers[name].pin


def apply(mesh, op: dict, ready):
    kind = op["op"]
    if kind == "add_uv_layer":
        source = mesh.uv_layers[op["copy"]] if "copy" in op else None
        layer = mesh.uv_layers.new(name=op["name"], do_init=False)
        if layer is None or layer.name != op["name"]:
            raise RequestError("the UV layer couldn't be added", "op_invalid")
        if source is not None:
            data = np.empty(len(mesh.loops) * 2, dtype=np.float32)
            mesh.uv_layers[op["copy"]].data.foreach_get("uv", data)
            mesh.uv_layers[op["name"]].data.foreach_set("uv", data)
    elif kind == "set_uv_layer":
        layer = mesh.uv_layers[op["layer"]]
        if op.get("active"):
            mesh.uv_layers.active = layer
        if op.get("render"):
            layer.active_render = True
    elif kind == "set_uvs":
        corners, values = ready
        data = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        layer = mesh.uv_layers[op["layer"]]
        layer.data.foreach_get("uv", data)
        data = data.reshape(-1, 2)
        data[corners] = values
        layer.data.foreach_set("uv", data.ravel())
    else:
        pins = _pins(mesh, op["layer"], op["pinned"])
        if pins is not None:
            flags = np.zeros(len(mesh.loops), dtype=bool)
            pins.foreach_get("value", flags)
            flags[ready] = op["pinned"]
            pins.foreach_set("value", flags)
    mesh.update()
