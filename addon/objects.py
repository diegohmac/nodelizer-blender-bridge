# SPDX-License-Identifier: GPL-3.0-or-later
"""Edits that change which objects the scene has, or make an object's mesh anew: joining mesh
objects into one, separating part of a mesh into objects of its own, and Blender's voxel remesh.

Each is checked against the scene before anything changes (`check_*`), then made (`join`,
`separate`, `remesh`). They number their meshes' components anew, so nothing naming components may
follow them on the same mesh in a list.

- `join` is Blender's Join: the objects named become part of the one joined into, which keeps its
  name, id, transform and modifiers. No vertices are merged. Materials, UV layers and vertex groups
  are matched by name. Children of the objects that go are parented to the one that stays, where
  they are.
- `separate` is Blender's Separate, by selection or by loose parts: each new object is a copy of
  the object (its transform, materials, vertex groups and modifiers) holding the geometry taken out
  of it. A new object's id is made from the edit's seed, so replaying the edit makes the same ids.
- `remesh` is Blender's voxel remesh: one closed surface at the voxel size, without the UV layers,
  materials, vertex groups and shape keys the old mesh had, shaded smooth.
"""

from __future__ import annotations

import hashlib

import bmesh
import bpy
import numpy as np
from mathutils import Matrix

from . import scene_data, topology
from .worker_protocol import MAX_REMESH_CELLS, MAX_SEPARATED, RequestError


def _fail(message: str, code: str = "op_invalid"):
    raise RequestError(message, code)


def _mesh(obj):
    if obj is None:
        _fail("unknown object")
    if obj.type != "MESH" or obj.data is None:
        _fail("not a mesh")
    return obj.data


def check_join(op: dict, objects: dict) -> list:
    """The objects a join takes in, once they're all meshes of the scene."""
    target = objects.get(op["object"])
    _mesh(target)
    named = topology.blocker([target])
    if named:
        raise RequestError(named, "op_unsupported")
    sources = [objects.get(each) for each in op["objects"]]
    for source in sources:
        _mesh(source)
    return sources


def _move_under(child, above):
    """Gives an object another parent, or none, leaving it exactly where it is. Its own location,
    rotation and scale stay as they are: what its old parents did to it goes into the parent
    inverse, which holds any transform, a skew from a parent scaled along one axis included.
    Without a parent there's nowhere to keep that, so it takes the nearest place it can hold."""
    world = child.matrix_world.copy()
    # Everything between the object's own values and the world: its parents and parent inverse.
    over = world @ child.matrix_basis.inverted_safe()
    child.parent = above
    if above is None:
        child.matrix_parent_inverse = Matrix.Identity(4)
        child.matrix_world = world
        return
    child.parent_type = "OBJECT"
    child.matrix_parent_inverse = above.matrix_world.inverted_safe() @ over


def delete(op: dict, obj) -> tuple[dict, list]:
    """Takes an object out of the scene. Its children stay where they are, under its own parent.
    A mesh no other object uses goes with it."""
    bpy.context.view_layer.update()
    above = obj.parent
    for child in list(obj.children):
        _move_under(child, above)
    data = obj.data
    removed = obj.get(scene_data.ID_PROP)
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        try:
            bpy.data.batch_remove([data])
        except Exception:  # Data Blender won't remove by hand goes with the next purge.
            pass
    nothing = {"vertices": 0, "edges": 0, "faces": 0}
    return {"object": op["object"], "counts": nothing, "added": nothing, "removed": [removed]}, []


def _all(mask: np.ndarray, start: np.ndarray, total: np.ndarray) -> np.ndarray:
    """For each face, whether every one of its corners is in `mask` (one flag per corner)."""
    if not len(start):
        return np.zeros(0, dtype=bool)
    return np.minimum.reduceat(mask.astype(np.uint8), start).astype(bool) & (total > 0)


def _flush(top: topology.Topology, component: str, indices: np.ndarray):
    """What a selection of one kind selects of the other two, as edit mode does: vertices take the
    edges and faces they wholly hold, edges their vertices and the faces they outline, faces their
    edges and vertices. Returns flags per vertex, edge and face."""
    verts = np.zeros(top.vertices, dtype=bool)
    edges = np.zeros(top.edges, dtype=bool)
    faces = np.zeros(top.faces, dtype=bool)
    if component == "vertices":
        verts[indices] = True
        if top.edges:
            edges = verts[top.edge_verts].all(axis=1)
        faces = _all(verts[top.loop_vert], top.face_start, top.face_total)
    elif component == "edges":
        edges[indices] = True
        verts[top.edge_verts[indices].ravel()] = True
        faces = _all(edges[top.loop_edge], top.face_start, top.face_total)
    else:
        faces[indices] = True
        corners = np.repeat(faces, top.face_total)
        verts[top.loop_vert[corners]] = True
        edges[top.loop_edge[corners]] = True
    return verts, edges, faces


def _islands(top: topology.Topology) -> np.ndarray:
    """Each vertex's island: the lowest numbered vertex it's joined to by edges."""
    label = np.arange(top.vertices, dtype=np.int64)
    a, b = top.edge_verts[:, 0], top.edge_verts[:, 1]
    while True:
        low = np.minimum(label[a], label[b])
        before = label.copy()
        np.minimum.at(label, a, low)
        np.minimum.at(label, b, low)
        # Follow each label to its own, so long chains settle in few rounds.
        label = label[label]
        if np.array_equal(before, label):
            return label


def check_separate(op: dict, obj, top: topology.Topology) -> list:
    """The parts a separation takes out, each as flags per vertex, edge and face, in the order
    their objects are made."""
    mesh = _mesh(obj)
    named = topology.blocker([o for o in scene_data.scene_objects() if o.data == mesh])
    if named:
        raise RequestError(named, "op_unsupported")
    if op["mode"] == "selection":
        indices = topology._components(op, {"vertices": top.vertices, "edges": top.edges, "faces": top.faces}[op["component"]])
        verts, edges, faces = _flush(top, op["component"], indices)
        if verts.all() and edges.all() and faces.all():
            _fail("that's the whole mesh")
        return [(verts, edges, faces)]
    label = _islands(top)
    roots = np.unique(label)
    if len(roots) < 2:
        _fail("the mesh is one piece")
    if len(roots) - 1 > MAX_SEPARATED:
        _fail(f"that would make {len(roots) - 1} objects")
    # The piece holding the first vertex stays; the others go, in the order of their first vertex.
    return [_flush(top, "vertices", np.nonzero(label == root)[0]) for root in roots[1:]]


def _new_id(seed: str, index: int, taken: set) -> str:
    value = hashlib.sha1(f"{seed}:{index}".encode("utf-8")).hexdigest()[:12]
    while value in taken:
        value = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    taken.add(value)
    return value


def _counts(mesh) -> dict:
    return {"vertices": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons)}


def _own_mesh(obj):
    """The object's mesh, its own: one shared with other objects is copied first, so they keep
    theirs."""
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    return obj.data


def join(op: dict, target, sources: list) -> tuple[dict, list]:
    """Joins `sources` into `target`. Returns the report and the meshes that changed."""
    before = _counts(target.data)
    removed = [source.get(scene_data.ID_PROP) for source in sources]
    bpy.context.view_layer.update()
    gone = set(sources)
    # A target under one of the objects that go moves up to the nearest parent that stays.
    above = target.parent
    while above is not None and above in gone:
        above = above.parent
    if above is not target.parent:
        _move_under(target, above)
        bpy.context.view_layer.update()
    for child in scene_data.scene_objects():
        if child.parent in gone and child not in gone and child is not target:
            _move_under(child, target)
    mesh = _own_mesh(target)
    # Sources can share a mesh, which goes once.
    old = list({source.data.name_full: source.data for source in sources}.values())
    for obj in scene_data.scene_objects():
        obj.select_set(False)
    chosen = [target, *sources]
    try:
        with bpy.context.temp_override(
            active_object=target,
            object=target,
            selected_objects=chosen,
            selected_editable_objects=chosen,
        ):
            result = bpy.ops.object.join()
    except Exception as error:
        raise RequestError(str(error), "op_failed") from error
    if "FINISHED" not in result:
        raise RequestError("Blender didn't join them", "op_failed")
    for data in old:
        if data.users == 0:
            bpy.data.meshes.remove(data)
    after = _counts(mesh)
    report = {
        "object": op["object"],
        "counts": after,
        "added": {key: after[key] - before[key] for key in after},
        "removed": removed,
    }
    return report, [mesh.name_full]


def _take(mesh, verts: np.ndarray, edges: np.ndarray, faces: np.ndarray, keep: bool):
    """Leaves a mesh with only the flagged geometry (`keep`), or without it. Without it, edges and
    vertices still used by what stays stay too, as Blender's Separate leaves them."""
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        for seq in (bm.verts, bm.edges, bm.faces):
            seq.ensure_lookup_table()
        if keep:
            # Looked up before the first delete, which outdates the tables.
            extra_edges = [bm.edges[i] for i in np.nonzero(~edges)[0].tolist()]
            extra_faces = [bm.faces[i] for i in np.nonzero(~faces)[0].tolist()]
            bmesh.ops.delete(
                bm, geom=[bm.verts[i] for i in np.nonzero(~verts)[0].tolist()], context="VERTS"
            )
            bmesh.ops.delete(bm, geom=[e for e in extra_edges if e.is_valid], context="EDGES")
            bmesh.ops.delete(bm, geom=[f for f in extra_faces if f.is_valid], context="FACES_ONLY")
        else:
            taken_edges = [bm.edges[i] for i in np.nonzero(edges)[0].tolist()]
            taken_verts = [bm.verts[i] for i in np.nonzero(verts)[0].tolist()]
            bmesh.ops.delete(
                bm, geom=[bm.faces[i] for i in np.nonzero(faces)[0].tolist()], context="FACES_ONLY"
            )
            bmesh.ops.delete(
                bm, geom=[e for e in taken_edges if not e.link_faces], context="EDGES"
            )
            bmesh.ops.delete(
                bm, geom=[v for v in taken_verts if v.is_valid and not v.link_edges], context="VERTS"
            )
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()


def separate(op: dict, obj, parts: list) -> tuple[dict, list]:
    """Takes each part out of `obj` into an object of its own. Returns the report and the meshes
    that changed."""
    source = _own_mesh(obj)
    before = _counts(source)
    taken = {o.get(scene_data.ID_PROP) for o in scene_data.scene_objects()}
    created = []
    meshes = [source.name_full]
    gone = (
        np.zeros(before["vertices"], dtype=bool),
        np.zeros(before["edges"], dtype=bool),
        np.zeros(before["faces"], dtype=bool),
    )
    for index, part in enumerate(parts):
        made = obj.copy()
        made.data = source.copy()
        for collection in obj.users_collection:
            collection.objects.link(made)
        made[scene_data.ID_PROP] = _new_id(op["seed"], index, taken)
        _take(made.data, *part, keep=True)
        created.append(
            {"id": made[scene_data.ID_PROP], "name": made.name, "counts": _counts(made.data)}
        )
        meshes.append(made.data.name_full)
        for flags, each in zip(gone, part):
            flags |= each
    _take(source, *gone, keep=False)
    after = _counts(source)
    report = {
        "object": op["object"],
        "counts": after,
        "added": {key: after[key] - before[key] for key in after},
        "created": created,
    }
    return report, meshes


def _longest(mesh) -> float:
    count = len(mesh.vertices)
    if not count:
        return 0.0
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    return float((co.max(axis=0) - co.min(axis=0)).max())


def check_remesh(op: dict, obj, known: bool):
    """`known` is whether the mesh is as the remesh will find it: not after a join in the same
    list."""
    mesh = _mesh(obj)
    named = topology.blocker([obj])
    if named:
        raise RequestError(named, "op_unsupported")
    if known:
        if not len(mesh.polygons):
            _fail("the mesh has no faces")
        if _longest(mesh) / op["voxel_size"] > MAX_REMESH_CELLS:
            _fail("the voxel size is too small for the mesh")


def remesh(op: dict, obj) -> tuple[dict, list]:
    """Blender's voxel remesh of one object. Returns the report and the meshes that changed."""
    mesh = _own_mesh(obj)
    before = _counts(mesh)
    if not before["faces"]:
        _fail("the mesh has no faces", "op_failed")
    if _longest(mesh) / op["voxel_size"] > MAX_REMESH_CELLS:
        _fail("the voxel size is too small for the mesh", "op_failed")
    if mesh.shape_keys is not None:
        obj.shape_key_clear()
    mesh.remesh_voxel_size = op["voxel_size"]
    mesh.remesh_voxel_adaptivity = 0.0
    mesh.use_remesh_fix_poles = False
    mesh.use_remesh_preserve_volume = True
    mesh.use_remesh_preserve_attributes = False
    for each in scene_data.scene_objects():
        each.select_set(False)
    try:
        with bpy.context.temp_override(active_object=obj, object=obj, selected_objects=[obj]):
            result = bpy.ops.object.voxel_remesh()
    except Exception as error:
        raise RequestError(str(error), "op_failed") from error
    mesh = obj.data
    if "FINISHED" not in result or not len(mesh.polygons):
        raise RequestError("Blender couldn't remesh it", "op_failed")
    # What the old surface carried means nothing on the new one.
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[0])
    mesh.materials.clear()
    obj.vertex_groups.clear()
    mesh.shade_smooth()
    mesh.update()
    after = _counts(mesh)
    report = {
        "object": op["object"],
        "counts": after,
        "added": {key: after[key] - before[key] for key in after},
    }
    return report, [mesh.name_full]
