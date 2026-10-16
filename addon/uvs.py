# SPDX-License-Identifier: GPL-3.0-or-later
"""Edits of a mesh's UV layers: new UVs and pins for some face corners, new layers, which layer is
active (the one Blender's UV editor shows) or rendered (the one textures use, and the glTF exporter
points them at), seams, and layouts Blender works out by unwrapping and packing.

UV layers belong to the mesh, by name, so every object using it changes with it. A layer's pins are
a boolean corner attribute named `.pn.<layer>`, which Blender makes only once something is pinned;
until then the layer's `pin` collection is empty.

Unwrapping and packing are Blender's operators, which only run on a mesh in edit mode. They run on
a copy of the object and its mesh, made for the purpose and removed after, so the scene's own mesh
never goes through edit mode: only the UVs that came out different are set on it, and written to a
file the host keeps, so its history replays them as a set_uvs, exactly."""

from __future__ import annotations

import os

import bmesh
import bpy
import numpy as np

from . import meshfile, scene_data
from .worker_protocol import MAX_COORDINATE, MAX_UV_LAYERS, SOLVES, RequestError


def _read(op: dict, what: str) -> bytes:
    try:
        with open(op["file"], "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise RequestError(f"the {what} file is missing", "op_invalid") from error
    if meshfile.digest(data) != op["data"]:
        raise RequestError(f"the {what} file doesn't match its hash", "op_invalid")
    return data


def _indices(values: memoryview, limit: int, what: str) -> np.ndarray:
    indices = np.frombuffer(values, dtype="<u4").astype(np.int64)
    if np.any(np.diff(indices) <= 0):
        raise RequestError(f"{what} must go up, each once", "op_invalid")
    if indices[-1] >= limit:
        raise RequestError(f"{what} go past the mesh", "op_invalid")
    return indices


def _corners(values: memoryview, mesh) -> np.ndarray:
    return _indices(values, len(mesh.loops), "corners")


def _set_of(op: dict, component: str, limit: int) -> np.ndarray:
    """The components an edit's file names, checked against the mesh."""
    try:
        _component, count, indices = meshfile.read_components(_read(op, component), (component,))
    except ValueError as error:
        raise RequestError(str(error), "op_invalid") from error
    if count != op["count"]:
        raise RequestError(f"the {component} count doesn't match", "op_invalid")
    return _indices(indices, limit, component)


def check(op: dict, mesh, names: list[str]):
    """What a UV edit needs once it fits the mesh, given the mesh's layer names as the edits before
    it leave them (a layer added earlier in the same list counts)."""
    kind = op["op"]
    if kind == "mark_seams":
        return _set_of(op, "edges", len(mesh.edges))
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
    if kind in SOLVES:
        return _set_of(op, "faces", len(mesh.polygons))
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


def _uvs(mesh, name: str) -> np.ndarray:
    data = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    mesh.uv_layers[name].data.foreach_get("uv", data)
    return data.reshape(-1, 2)


def _face_corners(mesh, faces: np.ndarray) -> np.ndarray:
    """The corners of some faces, in order."""
    starts = np.empty(len(mesh.polygons), dtype=np.int64)
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_start", starts)
    mesh.polygons.foreach_get("loop_total", totals)
    mask = np.zeros(len(mesh.loops), dtype=bool)
    for start, total in zip(starts[faces].tolist(), totals[faces].tolist()):
        mask[start : start + total] = True
    return np.nonzero(mask)[0]


def _select_faces(mesh, faces: np.ndarray):
    """Selects only `faces`, with their edges and vertices, as face select mode has it."""
    chosen = np.zeros(len(mesh.polygons), dtype=bool)
    chosen[faces] = True
    corners = _face_corners(mesh, faces)
    loop_vertex = np.empty(len(mesh.loops), dtype=np.int64)
    loop_edge = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("vertex_index", loop_vertex)
    mesh.loops.foreach_get("edge_index", loop_edge)
    vertices = np.zeros(len(mesh.vertices), dtype=bool)
    vertices[loop_vertex[corners]] = True
    edges = np.zeros(len(mesh.edges), dtype=bool)
    edges[loop_edge[corners]] = True
    mesh.vertices.foreach_set("select", vertices)
    mesh.edges.foreach_set("select", edges)
    mesh.polygons.foreach_set("select", chosen)


def _run_in_edit_mode(obj, op: dict, faces: np.ndarray) -> np.ndarray:
    """Runs Blender's unwrap or pack on a copy of the object with only `faces` selected, and
    returns the layer's UVs as they came out of it."""
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    copy = obj.copy()
    copy.data = obj.data.copy()
    copy.modifiers.clear()
    copy.animation_data_clear()
    bpy.context.scene.collection.objects.link(copy)
    tool = bpy.context.scene.tool_settings
    sync = tool.use_uv_select_sync
    select_mode = tuple(tool.mesh_select_mode)
    try:
        for each in selected:
            each.select_set(False)
        copy.hide_viewport = False
        copy.hide_set(False)
        copy.select_set(True)
        view_layer.objects.active = copy
        data = copy.data
        data.uv_layers.active = data.uv_layers[op["layer"]]
        if data.shape_keys is not None:
            copy.active_shape_key_index = 0
        if op["op"] == "pack_uvs" and any(op["tile"]):
            # Blender packs into the first tile when no image editor says which: the selection
            # moves there first and back after, so pinned islands it keeps are where they were.
            shifted = _uvs(data, op["layer"])
            shifted[_face_corners(data, faces)] -= np.array(op["tile"], dtype=np.float32)
            data.uv_layers[op["layer"]].data.foreach_set("uv", shifted.ravel())
        _select_faces(data, faces)
        tool.mesh_select_mode = (False, False, True)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            tool.use_uv_select_sync = True
            if op["op"] == "unwrap_uvs":
                result = bpy.ops.uv.unwrap(
                    method=op["method"],
                    fill_holes=op["fill_holes"],
                    correct_aspect=op["correct_aspect"],
                    use_original_bounds=op.get("keep_bounds", False),
                    margin_method="FRACTION",
                    margin=op["margin"],
                )
            else:
                result = bpy.ops.uv.pack_islands(
                    udim_source="CLOSEST_UDIM",
                    rotate=op["rotate"],
                    rotate_method=op["rotate_method"],
                    scale=op["scale"],
                    merge_overlap=op["merge_overlap"],
                    margin_method="FRACTION",
                    margin=op["margin"],
                    pin=op["pin"],
                    pin_method=op["pin_method"],
                    shape_method=op["shape"],
                )
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
        if "FINISHED" not in result:
            raise RequestError("Blender didn't finish", "op_failed")
        out = _uvs(data, op["layer"]).copy()
    finally:
        mesh = copy.data
        bpy.data.objects.remove(copy)
        bpy.data.meshes.remove(mesh)
        for each in selected:
            each.select_set(True)
        view_layer.objects.active = active
        tool.use_uv_select_sync = sync
        tool.mesh_select_mode = select_mode
    return out


def _solve(obj, op: dict, faces: np.ndarray) -> dict:
    """Unwraps or packs, sets the UVs that changed on the object's mesh and writes them to a file
    in `out`, named by its SHA-256. Reports the layer, how many corners changed and the file."""
    mesh = obj.data
    before = _uvs(mesh, op["layer"])
    after = _run_in_edit_mode(obj, op, faces)
    if op["op"] == "pack_uvs" and any(op["tile"]):
        tile = np.array(op["tile"], dtype=np.float32)
        corners = _face_corners(mesh, faces)
        shifted = before[corners] - tile
        moved = np.any(after[corners] != shifted, axis=1)
        # What packing left alone keeps its UVs exactly, rather than shifted there and back.
        after[corners] = np.where(moved[:, None], after[corners] + tile, before[corners])
    if not np.all(np.isfinite(after)) or np.any(np.abs(after) > MAX_COORDINATE):
        raise RequestError("Blender made UVs out of range", "op_failed")
    changed = np.nonzero(np.any(after != before, axis=1))[0]
    report = {"layer": op["layer"], "count": int(len(changed))}
    if len(changed):
        values = after[changed]
        data = meshfile.pack(
            [("corners", "u32", changed.astype("<u4")), ("uvs", "f32", values.astype("<f4"))],
            {"kind": "uvs", "counts": {"corners": int(len(changed))}},
        )
        sha = meshfile.digest(data)
        os.makedirs(op["out"], exist_ok=True)
        target = os.path.join(op["out"], f"{sha}.bin")
        if not os.path.exists(target):
            partial = f"{target}.{os.getpid()}.part"
            with open(partial, "wb") as handle:
                handle.write(data)
            os.replace(partial, target)
        layer = mesh.uv_layers[op["layer"]]
        flat = before.copy()
        flat[changed] = values
        layer.data.foreach_set("uv", flat.ravel())
        mesh.update()
        report["data"] = sha
    return {"object": obj.get(scene_data.ID_PROP), "uvs": report}


def apply(obj, op: dict, ready):
    """Makes one edit. Returns what an unwrap or a pack reports, otherwise None."""
    mesh = obj.data
    kind = op["op"]
    if kind in SOLVES:
        return _solve(obj, op, ready)
    if kind == "mark_seams":
        seams = np.empty(len(mesh.edges), dtype=bool)
        mesh.edges.foreach_get("use_seam", seams)
        seams[ready] = op["seam"]
        mesh.edges.foreach_set("use_seam", seams)
        mesh.update()
        return None
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
    return None
