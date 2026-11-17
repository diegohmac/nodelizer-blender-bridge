# SPDX-License-Identifier: GPL-3.0-or-later
"""The edits the worker makes to its scene. Each has been checked by worker_protocol first; here
they're checked against the scene, all of them before any is made, so a batch is all or nothing.
Edits that change a mesh's topology are in topology.py.

Vertex groups belong to the object, by name; their weights belong to the mesh, by the object's
group index. A weight edit sets each weight it lists, putting the vertex in the group when it wasn't,
and leaves every other weight as it was. UV layers and seams belong to the mesh; their edits,
unwrapping and packing among them, are in uvs.py."""

from __future__ import annotations

import bpy
import numpy as np
from mathutils import Quaternion

from . import meshfile, objects as scene_objects, scene_data, topology, uvs
from .worker_protocol import (
    MAX_COORDINATE,
    OBJECTS,
    TOPOLOGY,
    UNNUMBERED,
    UVS,
    WEIGHTS,
    RequestError,
)


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


def _weights(op: dict, obj, names: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A set_weights edit's vertices, groups (by place in its list) and values, read from its file
    and checked against the object's mesh."""
    if obj.type != "MESH" or obj.data is None:
        raise RequestError("not a mesh", "op_invalid")
    missing = [name for name in op["groups"] if name not in names]
    if missing:
        raise RequestError(f"the object has no vertex group {missing[0]!r}", "op_invalid")
    try:
        with open(op["file"], "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise RequestError("the weight file is missing", "op_invalid") from error
    if meshfile.digest(data) != op["data"]:
        raise RequestError("the weight file doesn't match its hash", "op_invalid")
    try:
        count, vertices, groups, weights = meshfile.read_weights(data)
    except ValueError as error:
        raise RequestError(str(error), "op_invalid") from error
    if count != op["count"]:
        raise RequestError("the weight count doesn't match", "op_invalid")
    vertices = np.frombuffer(vertices, dtype="<u4").astype(np.int64)
    groups = np.frombuffer(groups, dtype="<u2").astype(np.int64)
    weights = np.frombuffer(weights, dtype="<f4")
    if vertices[-1] >= len(obj.data.vertices) or np.any(np.diff(vertices) < 0):
        raise RequestError("weights go by vertex, each a vertex of the mesh", "op_invalid")
    if np.any(groups >= len(op["groups"])):
        raise RequestError("a weight names a group past the edit's list", "op_invalid")
    key = vertices * len(op["groups"]) + groups
    if np.any(np.diff(key) <= 0):
        raise RequestError("each vertex lists a group once, in order", "op_invalid")
    if not np.all(np.isfinite(weights)) or np.any(weights < 0) or np.any(weights > 1):
        raise RequestError("a weight is outside 0 to 1", "op_invalid")
    return vertices, groups, weights


def _users(mesh) -> list:
    return [obj for obj in scene_data.scene_objects() if obj.data == mesh]


def check(ops: list[dict]) -> tuple[dict, list]:
    """The scene's objects by id and what each edit needs, once every edit fits the scene.

    A change of topology numbers a mesh's components anew, so it has to be the last edit naming
    components of its mesh in a batch: anything after it would name components of a mesh it can't
    know yet. Joining, separating and remeshing number them anew too, and a join takes objects out
    of the scene, which nothing after it may name."""
    objects = scene_data.by_id()
    prepared = []
    topologies: dict = {}
    last = {}
    # Each object's vertex group names, and each mesh's UV layer names, as the edits before each one
    # leave them.
    groups: dict = {}
    layers: dict = {}
    # Objects a join earlier in the batch took in, and meshes numbered anew by an edit that isn't
    # their last to name components: a join, a remesh or a separation of loose pieces.
    gone: set = set()
    anew: set = set()
    # Meshes a change of topology earlier in the batch leaves other than they're read now.
    reshaped: set = set()
    for index, op in enumerate(ops):
        obj = objects.get(op["object"])
        names_components = op["op"] not in UNNUMBERED and op.get("mode") != "loose"
        if obj is not None and names_components and obj.type == "MESH" and obj.data:
            last[obj.data.name_full] = index

    def is_mesh(obj):
        if obj.type != "MESH" or obj.data is None:
            raise RequestError("not a mesh", "op_invalid")
        return obj.data

    def read(mesh):
        """The mesh's topology as the edit will find it."""
        if mesh.name_full in anew or mesh.name_full in reshaped:
            raise RequestError("the mesh is made anew earlier in the list", "op_invalid")
        if mesh.name_full not in topologies:
            topologies[mesh.name_full] = topology.Topology(mesh)
        return topologies[mesh.name_full]

    for index, op in enumerate(ops):
        obj = objects.get(op["object"])
        try:
            if obj is None or op["object"] in gone:
                raise RequestError("unknown object", "op_invalid")
            if op["op"] in TOPOLOGY:
                mesh = is_mesh(obj)
                if last[mesh.name_full] != index:
                    raise RequestError("a change of topology has to be its mesh's last edit", "op_invalid")
                prepared.append(topology.check(op, mesh, _users(mesh), read(mesh)))
                reshaped.add(mesh.name_full)
            elif op["op"] == "join":
                if any(each in gone for each in op["objects"]):
                    raise RequestError("unknown object", "op_invalid")
                sources = scene_objects.check_join(op, objects)
                for each in (obj, *sources):
                    if last.get(each.data.name_full, -1) > index:
                        raise RequestError("a join has to come after its meshes' edits", "op_invalid")
                    anew.add(each.data.name_full)
                gone.update(op["objects"])
                prepared.append(sources)
            elif op["op"] == "delete_object":
                gone.add(op["object"])
                prepared.append(None)
            elif op["op"] == "separate":
                mesh = is_mesh(obj)
                if last.get(mesh.name_full, -1) > index:
                    raise RequestError("a separation has to be its mesh's last edit", "op_invalid")
                prepared.append(scene_objects.check_separate(op, obj, read(mesh)))
                anew.add(mesh.name_full)
            elif op["op"] == "remesh":
                mesh = is_mesh(obj)
                if last.get(mesh.name_full, -1) > index:
                    raise RequestError("a remesh has to come after its mesh's edits", "op_invalid")
                known = mesh.name_full not in anew and mesh.name_full not in reshaped
                scene_objects.check_remesh(op, obj, known)
                anew.add(mesh.name_full)
                prepared.append(None)
            elif op["op"] in UVS:
                mesh = is_mesh(obj)
                if mesh.name_full in anew:
                    raise RequestError("the mesh is made anew earlier in the list", "op_invalid")
                names = layers.setdefault(mesh.name_full, [layer.name for layer in mesh.uv_layers])
                prepared.append(uvs.check(op, mesh, names))
            elif op["op"] in WEIGHTS:
                mesh = is_mesh(obj)
                if mesh.name_full in anew:
                    raise RequestError("the mesh is made anew earlier in the list", "op_invalid")
                names = groups.setdefault(op["object"], [g.name for g in obj.vertex_groups])
                if op["op"] == "set_weights":
                    prepared.append(_weights(op, obj, names))
                elif op["op"] == "add_vertex_group":
                    if op["name"] in names:
                        raise RequestError(f"there's a vertex group {op['name']!r} already", "op_invalid")
                    names.append(op["name"])
                    prepared.append(None)
                else:
                    missing = [name for name in op["groups"] if name not in names]
                    if missing:
                        raise RequestError(f"the object has no vertex group {missing[0]!r}", "op_invalid")
                    prepared.append(None)
            elif op["op"] == "set_vertices":
                if obj.type == "MESH" and obj.data and obj.data.name_full in anew:
                    raise RequestError("the mesh is made anew earlier in the list", "op_invalid")
                prepared.append(_vertex_positions(op, obj))
            else:
                prepared.append(None)
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


def _set_weights(obj, op: dict, vertices: np.ndarray, groups: np.ndarray, weights: np.ndarray):
    """Sets each weight, putting the vertex in its group when it wasn't. One call per weight is
    quick enough: 200,000 different weights take about 0.13 s."""
    targets = [obj.vertex_groups[name] for name in op["groups"]]
    for vertex, group, weight in zip(vertices.tolist(), groups.tolist(), weights.tolist()):
        targets[group].add((vertex,), weight, "REPLACE")
    obj.data.update()


def apply(ops: list[dict]) -> tuple[list[str], list[str], list]:
    """Makes the edits. Returns the ids of the objects they changed, the names of the meshes that
    changed, whose files have to be written again, and for each edit what it reports: what to
    select after a change of topology, the UVs an unwrap or a pack made, or None."""
    objects, prepared = check(ops)
    changed = []
    meshes = []
    reports = []
    for index, (op, ready) in enumerate(zip(ops, prepared)):
        obj = objects[op["object"]]
        report = None
        if op["op"] in OBJECTS:
            try:
                if op["op"] == "delete_object":
                    report, made = scene_objects.delete(op, obj)
                    objects.pop(op["object"], None)
                    obj = None
                elif op["op"] == "join":
                    report, made = scene_objects.join(op, obj, ready)
                elif op["op"] == "separate":
                    report, made = scene_objects.separate(op, obj, ready)
                else:
                    report, made = scene_objects.remesh(op, obj)
            except RequestError as error:
                error.index = index
                raise
            for name in made:
                if name not in meshes:
                    meshes.append(name)
            created = scene_data.by_id()
            touched = [
                *([] if obj is None else [obj]),
                *(created[each["id"]] for each in report.get("created", [])),
            ]
        elif op["op"] == "set_transform":
            touched = [obj]
            _set_transform(obj, op)
        elif op["op"] == "set_visible":
            touched = [obj]
            scene_data.set_visible(obj, op["visible"])
        elif op["op"] == "add_vertex_group":
            touched = [obj]
            obj.vertex_groups.new(name=op["name"])
        elif op["op"] == "lock_vertex_groups":
            touched = [obj]
            for name in op["groups"]:
                obj.vertex_groups[name].lock_weight = op["locked"]
        else:
            mesh = obj.data
            if op["op"] == "set_vertices":
                _set_vertices(mesh, *ready)
            elif op["op"] == "set_weights":
                _set_weights(obj, op, *ready)
            elif op["op"] in UVS:
                try:
                    report = uvs.apply(obj, op, ready)
                except RequestError as error:
                    error.index = index
                    raise
            else:
                try:
                    report = topology.apply(mesh, op, ready)
                except RequestError as error:
                    error.index = index
                    raise
            if mesh.name_full not in meshes:
                meshes.append(mesh.name_full)
            # Every object using the mesh changes with it, as in Blender.
            touched = _users(mesh)
        reports.append(report)
        for each in touched:
            each_id = each.get(scene_data.ID_PROP)
            if each_id and each_id not in changed:
                changed.append(each_id)
    # Matrices follow the new values only once the scene is evaluated.
    bpy.context.view_layer.update()
    return changed, meshes, reports
