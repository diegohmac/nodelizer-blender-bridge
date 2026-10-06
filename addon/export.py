# SPDX-License-Identifier: GPL-3.0-or-later
"""Importing the host's model into a new .blend, and exporting the scene back as GLB or FBX.

Blender's own importers and exporters do the work. Exports go to a temporary name first and are
moved into place once complete, so the host never reads a half-written file.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import bpy


@contextmanager
def object_mode():
    """Exports read mesh data, which edit and sculpt modes only write back when left."""
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active if view_layer else None
    mode = bpy.context.mode
    switched = False
    if active is not None and mode != "OBJECT" and bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")
        switched = True
    try:
        yield
    finally:
        if switched and view_layer.objects.active == active:
            # context.mode says EDIT_MESH, SCULPT and so on; mode_set wants the object's own names.
            back = {
                "EDIT_MESH": "EDIT",
                "EDIT_ARMATURE": "EDIT",
                "EDIT_CURVE": "EDIT",
                "EDIT_SURFACE": "EDIT",
                "EDIT_TEXT": "EDIT",
                "EDIT_LATTICE": "EDIT",
                "EDIT_METABALL": "EDIT",
                "EDIT_CURVES": "EDIT",
                "EDIT_GREASE_PENCIL": "EDIT",
                "PAINT_WEIGHT": "WEIGHT_PAINT",
                "PAINT_VERTEX": "VERTEX_PAINT",
                "PAINT_TEXTURE": "TEXTURE_PAINT",
                "PARTICLE": "PARTICLE_EDIT",
            }.get(mode, mode)
            try:
                bpy.ops.object.mode_set(mode=back)
            except (RuntimeError, TypeError):
                pass


def _replace(temporary: str, final: str):
    if not os.path.isfile(temporary) or os.path.getsize(temporary) == 0:
        raise RuntimeError("the exporter wrote nothing")
    os.replace(temporary, final)


def export_glb(path: str):
    temporary = path[: -len(".glb")] + ".partial.glb"
    with object_mode():
        bpy.ops.export_scene.gltf(
            filepath=temporary,
            export_format="GLB",
            use_selection=False,
            export_apply=True,
            export_yup=True,
            export_animations=True,
            export_cameras=False,
            export_lights=False,
        )
    _replace(temporary, path)


def export_fbx(path: str):
    temporary = path[: -len(".fbx")] + ".partial.fbx"
    with object_mode():
        bpy.ops.export_scene.fbx(
            filepath=temporary,
            use_selection=False,
            object_types={"ARMATURE", "EMPTY", "MESH", "OTHER"},
            use_mesh_modifiers=True,
            add_leaf_bones=False,
            bake_anim=True,
            path_mode="COPY",
            embed_textures=True,
            axis_forward="-Z",
            axis_up="Y",
        )
    _replace(temporary, path)


def export(path: str, fmt: str):
    if fmt == "glb":
        export_glb(path)
    elif fmt == "fbx":
        export_fbx(path)
    else:
        raise ValueError("unknown format")


def _clear_default_scene():
    """A fresh file starts with Blender's cube. The camera and light stay for rendering."""
    cube = bpy.data.objects.get("Cube")
    if cube is not None and cube.type == "MESH":
        mesh = cube.data
        bpy.data.objects.remove(cube, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def import_model(path: str, name: str):
    """Brings the model into its own collection, named after the node."""
    _clear_default_scene()
    collection = bpy.data.collections.new(name[:63])
    bpy.context.scene.collection.children.link(collection)
    layer = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer
    extension = os.path.splitext(path)[1].lower()
    if extension == ".glb":
        result = bpy.ops.import_scene.gltf(filepath=path)
    elif extension == ".fbx":
        result = bpy.ops.import_scene.fbx(filepath=path)
    elif extension == ".obj":
        result = bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError("unknown model format")
    if "FINISHED" not in result or not collection.all_objects:
        raise RuntimeError("the importer brought nothing in")
    # The source file is temporary; textures must live inside the .blend.
    bpy.ops.file.pack_all()
