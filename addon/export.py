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


# Custom property on each collection holding one of the host's models, naming which one.
INPUT_TAG = "nodelizer_input"


def clear_default_scene():
    """A new file starts with Blender's cube. The camera and light stay for rendering."""
    cube = bpy.data.objects.get("Cube")
    if cube is not None and cube.type == "MESH":
        mesh = cube.data
        bpy.data.objects.remove(cube, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _options(operator, **wanted):
    """The options this Blender's importer has, so older versions still import."""
    known = {prop.identifier for prop in operator.get_rna_type().properties}
    return {key: value for key, value in wanted.items() if key in known}


def tagged_collections(input_id=None):
    """The collections holding the host's models, or the one holding `input_id`."""
    return [
        c
        for c in bpy.data.collections
        if c.get(INPUT_TAG) and (input_id is None or c.get(INPUT_TAG) == input_id)
    ]


def _remove_collection(collection):
    """Removes a model's collection with its objects. Data nothing else uses is purged later."""
    for obj in list(collection.all_objects):
        if all(c == collection or c in collection.children_recursive for c in obj.users_collection):
            bpy.data.objects.remove(obj, do_unlink=True)
    for child in list(collection.children_recursive):
        bpy.data.collections.remove(child)
    bpy.data.collections.remove(collection)


def replace_input(path: str, input_id: str, name: str):
    """Brings one of the host's models into its own collection, replacing the one it had before.

    Only that collection changes: everything else in the scene, including edits to other models,
    stays as it is.
    """
    for old in tagged_collections(input_id):
        _remove_collection(old)
    collection = bpy.data.collections.new(name[:63])
    collection[INPUT_TAG] = input_id
    bpy.context.scene.collection.children.link(collection)
    layer = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer
    extension = os.path.splitext(path)[1].lower()
    if extension == ".glb":
        # Bones point at their children (TEMPERANCE) instead of Blender's default, which ignores
        # an armature's own scale and makes metre-long bones on centimetre rigs. Bone shapes are
        # off: the importer would draw every bone as a sphere it adds to the scene.
        options = _options(
            bpy.ops.import_scene.gltf, bone_heuristic="TEMPERANCE", disable_bone_shape=True
        )
        result = bpy.ops.import_scene.gltf(filepath=path, **options)
    elif extension == ".fbx":
        result = bpy.ops.import_scene.fbx(filepath=path)
    elif extension == ".obj":
        result = bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError("unknown model format")
    if "FINISHED" not in result or not collection.all_objects:
        raise RuntimeError("the importer brought nothing in")
    # Return to the scene's own collection, so whatever the person adds next goes there.
    bpy.context.view_layer.active_layer_collection = bpy.context.view_layer.layer_collection


def finish_imports():
    """Drops data the replaced models left unused and packs textures into the .blend, since the
    model files the host passed are temporary."""
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)
    bpy.ops.file.pack_all()
