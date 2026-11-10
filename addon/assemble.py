# SPDX-License-Identifier: GPL-3.0-or-later
"""Building a scene out of parts, for the host's Assemble node.

Each part is a model the host placed. It comes in with the same importers and texture rules as
`import`, and becomes one mesh object: its meshes joined, their transforms applied into the mesh,
its origin moved to the middle of its bounds, and the object left at the origin with no rotation
and a scale of 1, under the name and id the host chose. A copy is a linked duplicate of its
source: another object sharing the same mesh. The host works out where each one goes and moves it
with `set_transform`.
"""

from __future__ import annotations

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import convert, export, scene_data


class PartError(Exception):
    """A part that couldn't be brought in. `index` says which, counting parts then copies."""

    def __init__(self, message: str, index: int):
        super().__init__(message)
        self.index = index


def _remove(objects):
    for obj in objects:
        if obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)


def _centre(mesh):
    """Moves a mesh so the middle of its bounds is its origin."""
    points = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", points)
    points = points.reshape(-1, 3)
    middle = Vector(((points.min(axis=0) + points.max(axis=0)) / 2).tolist())
    mesh.transform(Matrix.Translation(-middle), shape_keys=True)
    mesh.update()


def _joined(new: list, name: str):
    """One mesh object made of every mesh an import brought in, in world space, centred on its
    bounds. Everything else the import made (empties, armatures, cameras, lights) goes."""
    meshes = [obj for obj in new if obj.type == "MESH" and obj.data is not None and obj.data.vertices]
    others = [obj for obj in new if obj not in meshes]
    if not meshes:
        _remove(new)
        raise RuntimeError("the model has no mesh")
    # Each mesh takes its world place into its vertices, so the join has nothing left to apply.
    for obj in meshes:
        world = obj.matrix_world.copy()
        obj.parent = None
        # A mesh a model shares between objects becomes each one's own, so the join can take it.
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        obj.data.transform(world, shape_keys=True)
        # A node that mirrors (a scale of -1 along an axis) turns the faces inside out once baked,
        # with nothing left on the object to say so: they're turned back to face out.
        if world.determinant() < 0:
            obj.data.flip_normals()
        obj.data.update()
        obj.matrix_world = Matrix.Identity(4)
        # An armature or a hook would bend the joined mesh by objects that go.
        for modifier in list(obj.modifiers):
            if getattr(modifier, "object", None) is not None:
                obj.modifiers.remove(modifier)
    target = meshes[0]
    if len(meshes) > 1:
        with bpy.context.temp_override(
            active_object=target,
            object=target,
            selected_objects=meshes,
            selected_editable_objects=meshes,
        ):
            bpy.ops.object.join()
    _remove(others)
    mesh = target.data
    _centre(mesh)
    target.rotation_mode = "QUATERNION"
    target.location = (0.0, 0.0, 0.0)
    target.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    target.scale = (1.0, 1.0, 1.0)
    target.name = name
    mesh.name = name
    return target


def _bring_in(part: dict):
    """A part's model as one mesh object at rest, with the host's name and id."""
    before = set(bpy.data.objects)
    try:
        convert._import(part["source"], part["root"])
    except Exception:
        _remove([obj for obj in bpy.data.objects if obj not in before])
        raise
    new = [obj for obj in bpy.data.objects if obj not in before]
    obj = _joined(new, part["name"])
    obj[scene_data.ID_PROP] = part["id"]
    return obj


def _copy(source, copy: dict):
    """A linked duplicate: its own object, the source's mesh."""
    obj = source.copy()
    obj.data = source.data
    for collection in source.users_collection:
        collection.objects.link(obj)
    obj.rotation_mode = "QUATERNION"
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj.name = copy["name"]
    obj[scene_data.ID_PROP] = copy["id"]
    return obj


def _parts(parts: list[dict], copies: list[dict]) -> list[str]:
    """Brings each part in, replacing an object with its id, then makes each copy. Returns the ids
    of the objects made or changed."""
    changed = []
    for index, part in enumerate(parts):
        existing = scene_data.by_id().get(part["id"])
        try:
            obj = _bring_in(part)
        except Exception as error:  # Importers raise all kinds of errors on broken files.
            raise PartError(str(error), index) from error
        if existing is not None:
            # The object stays, with its transform; it and every copy of it take the new mesh.
            old = existing.data
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            old.user_remap(mesh)
            existing.name = part["name"]
            mesh.name = part["name"]
        changed.append(part["id"])
    for offset, copy in enumerate(copies):
        objects = scene_data.by_id()
        source = objects.get(copy["of"])
        if source is None or source.type != "MESH":
            raise PartError("a copy names an object the scene doesn't have", len(parts) + offset)
        existing = objects.get(copy["id"])
        if existing is not None:
            existing.data = source.data
            existing.name = copy["name"]
        else:
            _copy(source, copy)
        changed.append(copy["id"])
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)
    export.pack_files()
    bpy.context.view_layer.update()
    return changed


def build(parts: list[dict], copies: list[dict]) -> list[str]:
    """Adds or replaces the parts and copies named; every other object stays as it is. `assemble`
    empties the scene first, so it holds the parts and copies and nothing else."""
    return _parts(parts, copies)
