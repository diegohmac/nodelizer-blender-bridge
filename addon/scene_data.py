# SPDX-License-Identifier: GPL-3.0-or-later
"""What the worker writes for the host about the scene it holds.

A snapshot is `<name>.json`, describing every object in the scene, beside `mesh/<sha256>.bin`, one
editable mesh file per mesh (see meshfile.py), and `appearance/<sha256>.glb`, the scene's
materials and textures. The host draws the scene from these without asking Blender again.
Coordinates are Blender's own: metres, Z up, -Y forward, matrices column by column.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import bpy
import numpy as np
from mathutils import Quaternion

from . import meshfile

SCHEMA = 1
# Each object's id, kept in the .blend so it outlives renames.
ID_PROP = "nodelizer_id"


def _matrix(m) -> list[float]:
    return [m[row][col] for col in range(4) for row in range(4)]


def _round(values) -> list[float]:
    return [float(v) for v in values]


def scene_objects():
    return list(bpy.context.scene.objects)


def ensure_ids():
    """Gives every object an id. A copy of an object carries its original's id, so the later of
    the two by name gets one made from its own name, the same way on every computer."""
    seen = set()
    for obj in sorted(scene_objects(), key=lambda o: o.name):
        value = obj.get(ID_PROP)
        if isinstance(value, str) and len(value) == 12 and value not in seen:
            seen.add(value)
            continue
        value = hashlib.sha1(obj.name.encode("utf-8")).hexdigest()[:12]
        while value in seen:
            value = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
        obj[ID_PROP] = value
        seen.add(value)


def new_ids():
    """Fresh ids for a scene just brought in."""
    for obj in scene_objects():
        obj[ID_PROP] = uuid.uuid4().hex[:12]


def by_id() -> dict:
    return {obj.get(ID_PROP): obj for obj in scene_objects() if obj.get(ID_PROP)}


def rotation(obj) -> Quaternion:
    """The object's own rotation, not counting its delta, whatever its rotation mode."""
    mode = obj.rotation_mode
    if mode == "QUATERNION":
        return obj.rotation_quaternion.normalized()
    if mode == "AXIS_ANGLE":
        angle, x, y, z = obj.rotation_axis_angle
        return Quaternion((x, y, z), angle)
    return obj.rotation_euler.to_quaternion()


def _delta(obj) -> dict | None:
    if obj.rotation_mode == "QUATERNION":
        turn = obj.delta_rotation_quaternion
    else:
        turn = obj.delta_rotation_euler.to_quaternion()
    location = _round(obj.delta_location)
    scale = _round(obj.delta_scale)
    if not any(location) and scale == [1.0, 1.0, 1.0] and abs(turn.w - 1) < 1e-9:
        return None
    return {"location": location, "rotation": _round(turn), "scale": scale}


def _bones(obj) -> list[dict]:
    names = {}
    bones = []
    for bone in obj.data.bones:
        names[bone.name] = len(bones)
        bones.append(
            {
                "name": bone.name,
                "parent": bone.parent.name if bone.parent else None,
                "head": _round(bone.head_local),
                "tail": _round(bone.tail_local),
                # Its rest place, turn (roll included) and length in armature space, which posing
                # works from, and whether it deforms meshes.
                "matrix": _matrix(bone.matrix_local),
                "deform": bool(bone.use_deform),
            }
        )
    return bones


def _modifier(modifier) -> dict:
    """A modifier's name and type, and the object it works with, like an Armature's armature."""
    entry = {"name": modifier.name, "type": modifier.type}
    target = getattr(modifier, "object", None)
    if isinstance(target, bpy.types.Object) and target.get(ID_PROP):
        entry["object"] = target[ID_PROP]
    return entry


def _get(collection, attribute: str, count: int, dtype, width: int = 1):
    data = np.empty(count * width, dtype=dtype)
    if count:
        collection.foreach_get(attribute, data)
    return data


def mesh_file(obj) -> tuple[bytes, dict]:
    """The editable mesh file for an object's mesh, and what the snapshot says about it."""
    mesh = obj.data
    vertices, edges, faces, loops = (
        len(mesh.vertices),
        len(mesh.edges),
        len(mesh.polygons),
        len(mesh.loops),
    )
    triangles = len(mesh.loop_triangles)
    positions = _get(mesh.vertices, "co", vertices, np.float32, 3)
    starts = np.empty(faces + 1, dtype=np.uint32)
    starts[:faces] = _get(mesh.polygons, "loop_start", faces, np.int32)
    starts[faces] = loops
    arrays = [
        ("positions", "f32", positions),
        ("edges", "u32", _get(mesh.edges, "vertices", edges, np.int32, 2).astype(np.uint32)),
        ("faceLoopStart", "u32", starts),
        (
            "faceMaterial",
            "u16",
            _get(mesh.polygons, "material_index", faces, np.int32).astype(np.uint16),
        ),
        ("loopVertex", "u32", _get(mesh.loops, "vertex_index", loops, np.int32).astype(np.uint32)),
        ("loopEdge", "u32", _get(mesh.loops, "edge_index", loops, np.int32).astype(np.uint32)),
        ("loopNormal", "f32", _get(mesh.corner_normals, "vector", loops, np.float32, 3)),
        (
            "triangleLoops",
            "u32",
            _get(mesh.loop_triangles, "loops", triangles, np.int32, 3).astype(np.uint32),
        ),
        (
            "triangleFace",
            "u32",
            _get(mesh.loop_triangles, "polygon_index", triangles, np.int32).astype(np.uint32),
        ),
        ("edgeSeam", "u8", _get(mesh.edges, "use_seam", edges, bool).astype(np.uint8)),
        ("edgeSharp", "u8", _get(mesh.edges, "use_edge_sharp", edges, bool).astype(np.uint8)),
    ]
    layers = []
    for index, layer in enumerate(mesh.uv_layers):
        arrays.append((f"uv{index}", "f32", _get(layer.data, "uv", loops, np.float32, 2)))
        entry = {"name": layer.name, "active": layer.active, "activeRender": layer.active_render}
        # Pins only once something on the layer was pinned: Blender keeps none before.
        if loops and len(layer.pin) == loops:
            pins = _get(layer.pin, "value", loops, bool).astype(np.uint8)
            arrays.append((f"uvPin{index}", "u8", pins))
            entry["pins"] = True
        layers.append(entry)
    # Vertex groups, vertex by vertex: where each vertex's weights start, then group and weight.
    offsets = np.zeros(vertices + 1, dtype=np.uint32)
    groups: list[int] = []
    weights: list[float] = []
    if obj.vertex_groups:
        for vertex in mesh.vertices:
            for element in vertex.groups:
                groups.append(element.group)
                weights.append(element.weight)
            offsets[vertex.index + 1] = len(groups)
    arrays.append(("groupOffset", "u32", offsets))
    arrays.append(("groupIndex", "u16", np.array(groups, dtype=np.uint16)))
    arrays.append(("groupWeight", "f32", np.array(weights, dtype=np.float32)))
    counts = {
        "vertices": vertices,
        "edges": edges,
        "faces": faces,
        "loops": loops,
        "triangles": triangles,
    }
    data = meshfile.pack(arrays, {"counts": counts, "uvLayers": layers})
    if vertices:
        points = positions.reshape(-1, 3)
        bounds = {"min": _round(points.min(axis=0)), "max": _round(points.max(axis=0))}
    else:
        bounds = None
    shape_keys = [key.name for key in mesh.shape_keys.key_blocks] if mesh.shape_keys else []
    info = {
        **counts,
        "uvLayers": [layer["name"] for layer in layers],
        "shapeKeys": shape_keys,
        "bounds": bounds,
        "bytes": len(data),
    }
    return data, info


def _write(path: str, data: bytes):
    """Written beside its final name first, so the host never reads half a file."""
    temporary = f"{path}.partial"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _materials() -> list:
    found = []
    for obj in scene_objects():
        for slot in getattr(obj, "material_slots", []):
            if slot.material is not None and slot.material not in found:
                found.append(slot.material)
    return found


def export_appearance(folder: str) -> str | None:
    """The scene's materials and their textures as a small GLB: one triangle per material, so the
    file holds no geometry worth the name. None when nothing has a material."""
    from . import export

    materials = _materials()
    if not materials:
        return None
    mesh = bpy.data.meshes.new("nodelizer appearance")
    count = len(materials)
    points = []
    for index in range(count):
        points += [(index, 0, 0), (index + 1, 0, 0), (index, 1, 0)]
    mesh.from_pydata(points, [], [(3 * i, 3 * i + 1, 3 * i + 2) for i in range(count)])
    mesh.uv_layers.new(name="UVMap")
    for material in materials:
        mesh.materials.append(material)
    for index, face in enumerate(mesh.polygons):
        face.material_index = index
    holder = bpy.data.objects.new("nodelizer appearance", mesh)
    bpy.context.scene.collection.objects.link(holder)
    selected = [obj for obj in bpy.context.view_layer.objects if obj.select_get()]
    active = bpy.context.view_layer.objects.active
    os.makedirs(os.path.join(folder, "appearance"), exist_ok=True)
    temporary = os.path.join(folder, "appearance", f"{uuid.uuid4().hex}.partial.glb")
    try:
        for obj in selected:
            obj.select_set(False)
        holder.select_set(True)
        with export.object_mode():
            bpy.ops.export_scene.gltf(
                filepath=temporary,
                export_format="GLB",
                **export._options(
                    bpy.ops.export_scene.gltf,
                    use_selection=True,
                    export_apply=False,
                    export_animations=False,
                    export_skins=False,
                    export_morph=False,
                    export_cameras=False,
                    export_lights=False,
                    export_yup=True,
                ),
            )
    finally:
        holder.select_set(False)
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        bpy.data.objects.remove(holder, do_unlink=True)
        bpy.data.meshes.remove(mesh)
    with open(temporary, "rb") as handle:
        data = handle.read()
    os.remove(temporary)
    digest = meshfile.digest(data)
    final = os.path.join(folder, "appearance", f"{digest}.glb")
    if not os.path.isfile(final):
        _write(final, data)
    return digest


class Snapshots:
    """Writes snapshots, remembering which mesh files are already written so an edit that only
    moves objects doesn't read every mesh again."""

    def __init__(self):
        # Mesh name -> (sha256, what the snapshot says about it), while the scene stays loaded.
        self.meshes: dict[str, tuple[str, dict]] = {}

    def forget(self, names=None):
        if names is None:
            self.meshes.clear()
        else:
            for name in names:
                self.meshes.pop(name, None)

    def _mesh(self, obj, folder: str) -> tuple[str, dict]:
        name = obj.data.name_full
        known = self.meshes.get(name)
        if known and os.path.isfile(os.path.join(folder, "mesh", f"{known[0]}.bin")):
            return known
        data, info = mesh_file(obj)
        digest = meshfile.digest(data)
        target = os.path.join(folder, "mesh", f"{digest}.bin")
        if not os.path.isfile(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _write(target, data)
        self.meshes[name] = (digest, info)
        return digest, info

    def write(self, folder: str, name: str, appearance) -> dict:
        ensure_ids()
        if appearance == "export":
            appearance = export_appearance(folder)
        objects = []
        meshes = {}
        for obj in sorted(scene_objects(), key=lambda o: o.name):
            entry = {
                "id": obj[ID_PROP],
                "name": obj.name,
                "type": obj.type,
                "parent": obj.parent.get(ID_PROP) if obj.parent else None,
                "parentType": obj.parent_type if obj.parent else None,
                "parentBone": (obj.parent_bone or None) if obj.parent else None,
                "location": _round(obj.location),
                "rotationMode": obj.rotation_mode,
                "rotation": _round(rotation(obj)),
                "scale": _round(obj.scale),
                "matrixBasis": _matrix(obj.matrix_basis),
                "matrixParentInverse": _matrix(obj.matrix_parent_inverse),
                "matrixWorld": _matrix(obj.matrix_world),
                "dimensions": _round(obj.dimensions),
                "visible": bool(obj.visible_get()),
                "modifiers": [_modifier(m) for m in obj.modifiers],
            }
            if obj.rotation_mode not in ("QUATERNION", "AXIS_ANGLE"):
                entry["euler"] = _round(obj.rotation_euler)
            delta = _delta(obj)
            if delta:
                entry["delta"] = delta
            if obj.type == "MESH" and obj.data is not None:
                digest, info = self._mesh(obj, folder)
                entry["mesh"] = digest
                meshes[digest] = info
                entry["materials"] = [
                    slot.material.name if slot.material else None for slot in obj.material_slots
                ]
                entry["vertexGroups"] = [group.name for group in obj.vertex_groups]
                entry["lockedVertexGroups"] = [
                    group.name for group in obj.vertex_groups if group.lock_weight
                ]
            if obj.type == "ARMATURE" and obj.data is not None:
                entry["bones"] = _bones(obj)
            objects.append(entry)
        units = bpy.context.scene.unit_settings
        snapshot = {
            "schema": SCHEMA,
            "units": {
                "system": units.system,
                "scaleLength": float(units.scale_length),
                "length": units.length_unit,
            },
            "up": "Z",
            "objects": objects,
            "meshes": meshes,
            "appearance": appearance,
        }
        os.makedirs(folder, exist_ok=True)
        _write(
            os.path.join(folder, f"{name}.json"),
            json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
        )
        return {"objects": len(objects), "meshes": sorted(meshes), "appearance": appearance}
