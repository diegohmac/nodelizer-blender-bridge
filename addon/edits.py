# SPDX-License-Identifier: GPL-3.0-or-later
"""The edits the worker makes to its scene. Each has been checked by worker_protocol first; here
they're checked against the scene, all of them before any is made, so a batch is all or nothing."""

from __future__ import annotations

import bpy
import numpy as np
from mathutils import Quaternion

from . import meshfile, scene_data
from .worker_protocol import MAX_COORDINATE, RequestError


def _vertex_positions(op: dict, obj) -> tuple[np.ndarray, np.ndarray]:
    """A set_vertices edit's indices and places, read from its file and checked against the
    object's mesh."""
    if obj.type != "MESH" or obj.data is None:
        raise RequestError("not a mesh", "op_invalid")
    try:
        with open(op["file"], "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise RequestError("the vertex file is missing", "op_invalid") from error
    if meshfile.digest(data) != op["data"]:
        raise RequestError("the vertex file doesn't match its hash", "op_invalid")
    try:
        count, indices, positions = meshfile.read_vertex_positions(data)
    except ValueError as error:
        raise RequestError(str(error), "op_invalid") from error
    if count != op["count"]:
        raise RequestError("the vertex count doesn't match", "op_invalid")
    indices = np.frombuffer(indices, dtype="<u4").astype(np.int64)
    positions = np.frombuffer(positions, dtype="<f4").reshape(-1, 3)
    if np.any(np.diff(indices) <= 0):
        raise RequestError("vertex indices must go up, each once", "op_invalid")
    if indices[-1] >= len(obj.data.vertices):
        raise RequestError("a vertex index is past the mesh", "op_invalid")
    if not np.all(np.isfinite(positions)) or np.any(np.abs(positions) > MAX_COORDINATE):
        raise RequestError("a vertex is out of range", "op_invalid")
    return indices, positions


def check(ops: list[dict]) -> tuple[dict, list]:
    """The scene's objects by id and what each edit needs, once every edit fits the scene."""
    objects = scene_data.by_id()
    prepared = []
    for index, op in enumerate(ops):
        obj = objects.get(op["object"])
        try:
            if obj is None:
                raise RequestError("unknown object", "op_invalid")
            prepared.append(_vertex_positions(op, obj) if op["op"] == "set_vertices" else None)
        except RequestError as error:
            error.index = index
            raise
    return objects, prepared


def _set_transform(obj, op: dict):
    if "location" in op:
        obj.location = op["location"]
    if "rotation" in op:
        turn = Quaternion(op["rotation"])
        mode = obj.rotation_mode
        if mode == "QUATERNION":
            obj.rotation_quaternion = turn
        elif mode == "AXIS_ANGLE":
            axis, angle = turn.to_axis_angle()
            obj.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
        else:
            # The Euler angles nearest the current ones, so a small turn doesn't flip the numbers.
            obj.rotation_euler = turn.to_euler(mode, obj.rotation_euler)
    if "scale" in op:
        obj.scale = op["scale"]


def _coordinates(collection, count: int) -> np.ndarray:
    data = np.empty(count * 3, dtype=np.float32)
    if count:
        collection.foreach_get("co", data)
    return data.reshape(-1, 3)


def _set_vertices(mesh, indices: np.ndarray, positions: np.ndarray):
    """Moves vertices in the mesh's rest state. With shape keys, Blender evaluates the basis key
    rather than the vertices, so the basis moves too, and keys relative to it move by the same
    amount, as leaving edit mode does in Blender."""
    count = len(mesh.vertices)
    keys = mesh.shape_keys
    if keys is not None and keys.use_relative:
        basis = keys.reference_key
        rest = _coordinates(basis.data, count)
        delta = positions - rest[indices]
        for block in keys.key_blocks:
            if block == basis or block.relative_key != basis:
                continue
            shape = _coordinates(block.data, count)
            shape[indices] += delta
            block.data.foreach_set("co", shape.ravel())
        rest[indices] = positions
        basis.data.foreach_set("co", rest.ravel())
    elif keys is not None:
        rest = _coordinates(keys.reference_key.data, count)
        rest[indices] = positions
        keys.reference_key.data.foreach_set("co", rest.ravel())
    co = _coordinates(mesh.vertices, count)
    co[indices] = positions
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.update()


def apply(ops: list[dict]) -> tuple[list[str], list[str]]:
    """Makes the edits. Returns the ids of the objects they changed, and the names of the meshes
    whose vertices moved, whose files have to be written again."""
    objects, prepared = check(ops)
    changed = []
    meshes = []
    for op, ready in zip(ops, prepared):
        obj = objects[op["object"]]
        if op["op"] == "set_transform":
            touched = [obj]
            _set_transform(obj, op)
        else:
            mesh = obj.data
            _set_vertices(mesh, *ready)
            if mesh.name_full not in meshes:
                meshes.append(mesh.name_full)
            # Every object using the mesh changes with it, as in Blender.
            touched = [other for other in scene_data.scene_objects() if other.data == mesh]
        for each in touched:
            each_id = each.get(scene_data.ID_PROP)
            if each_id and each_id not in changed:
                changed.append(each_id)
    # Matrices follow the new values only once the scene is evaluated.
    bpy.context.view_layer.update()
    return changed, meshes
