# SPDX-License-Identifier: GPL-3.0-or-later
"""Importing the host's model into a new .blend, and exporting the scene back as GLB or FBX.

Blender's own importers and exporters do the work. Exports go to a temporary name first and are
moved into place once complete, so the host never reads a half-written file.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager

import bpy

from . import session as sessions


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


def import_gltf(path: str):
    """Bones point at their children (TEMPERANCE) instead of Blender's default, which ignores an
    armature's own scale and makes metre-long bones on centimetre rigs. Bone shapes are off: the
    importer would draw every bone as a sphere it adds to the scene. Images stay unpacked, so
    `keep_inside` sees the files they name; textures inside the GLB are packed either way."""
    options = _options(
        bpy.ops.import_scene.gltf,
        bone_heuristic="TEMPERANCE",
        disable_bone_shape=True,
        import_pack_images=False,
    )
    return bpy.ops.import_scene.gltf(filepath=path, **options)


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


def replace_input(path: str, input_id: str, name: str, folder: str):
    """Brings one of the host's models into its own collection, replacing the one it had before.

    Only that collection changes: everything else in the scene, including edits to other models,
    stays as it is. The model's textures may only come from `folder`, where the host put it.
    """
    for old in tagged_collections(input_id):
        _remove_collection(old)
    collection = bpy.data.collections.new(name[:63])
    collection[INPUT_TAG] = input_id
    bpy.context.scene.collection.children.link(collection)
    layer = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer
    extension = os.path.splitext(path)[1].lower()
    before = file_data()
    if extension == ".glb":
        result = import_gltf(path)
    elif extension == ".fbx":
        result = bpy.ops.import_scene.fbx(filepath=path)
    elif extension == ".obj":
        result = bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError("unknown model format")
    keep_inside(folder, before, path)
    if "FINISHED" not in result or not collection.all_objects:
        raise RuntimeError("the importer brought nothing in")
    # Return to the scene's own collection, so whatever the person adds next goes there.
    bpy.context.view_layer.active_layer_collection = bpy.context.view_layer.layer_collection


# Data that names a file on disk, and that Blender reads, packs into the .blend or copies into an
# export.
_FILE_DATA = ("images", "volumes", "sounds", "fonts", "movieclips", "cache_files")


def file_data() -> set:
    """Every datablock that names a file now, so `keep_inside` can tell what an import added."""
    return {item.as_pointer() for kind in _FILE_DATA for item in getattr(bpy.data, kind, ())}


def _packed(item) -> bool:
    return getattr(item, "packed_file", None) is not None or bool(getattr(item, "packed_files", ()))


def _same_name(name: str, folder: str, model: str):
    """A file called `name`, in any case, among the model's own files: the folder an OBJ was
    unpacked into, beside its .mtl and textures. A model in a file of its own has none."""
    top = os.path.relpath(model, folder).split(os.sep)[0]
    own = os.path.join(folder, top)
    if top in ("", os.curdir, os.pardir) or not os.path.isdir(own):
        return None
    wanted = name.lower()
    for parent, folders, files in os.walk(own):
        folders.sort()
        for file in sorted(files):
            path = os.path.join(parent, file)
            if file.lower() == wanted and sessions.inside(path, folder):
                return path
    return None


def keep_inside(folder: str, before: set, model: str):
    """Drops what an import of `model` brought in from files outside `folder`.

    A model names its textures by path, and that path can be any file the person can read, such as
    `/Users/me/.ssh/id_ed25519` or `../../Documents/scan.png`. Left in, Blender would pack it into
    the .blend and copy it into exports. What the model carries inside itself is packed from its
    own bytes, so it stays: no importer here packs from a path, once `import_gltf` asks glTF not to.

    Exporters often write paths from the computer they ran on, and Blender reads such a file when
    it's still there. A texture of the same name among the model's own files is used instead, as
    Nodelizer's viewer does.
    """
    for kind in _FILE_DATA:
        collection = getattr(bpy.data, kind, None)
        if collection is None:
            continue
        for item in list(collection):
            if item.as_pointer() in before or _packed(item):
                continue
            path = item.filepath
            if not path or path == "<builtin>":
                continue
            if sessions.inside(bpy.path.abspath(path, library=item.library), folder):
                continue
            own = None
            if kind == "images":
                own = _same_name(re.split(r"[\\/]", path)[-1], folder, model)
            if own is not None:
                item.filepath = own
                item.reload()
                continue
            print(f"nodelizer: left out {item.name!r}, a file outside the model's folder", file=sys.stderr)
            collection.remove(item)


def pack_files():
    """Packs the files the scene uses into the .blend, since the model files the host passed are
    temporary. A texture a model names but came without stays missing, as Blender shows it, instead
    of failing the whole import."""
    try:
        bpy.ops.file.pack_all()
    except RuntimeError as error:
        print(f"nodelizer: {error}", file=sys.stderr)


def finish_imports():
    """Drops data the replaced models left unused and packs textures into the .blend."""
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)
    pack_files()
