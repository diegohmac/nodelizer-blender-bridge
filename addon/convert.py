# SPDX-License-Identifier: GPL-3.0-or-later
"""`blender --command nodelizer_convert <job folder>`: one model, saved in another format.

The host runs this in the background, with no window and nothing to connect to. The job folder
holds job.json and the model to convert: one file, or an OBJ with its materials and textures in a
folder of their own. The model comes into an empty scene and goes out as `result.<format>`; an OBJ
goes out with its .mtl and textures, packed into `result.zip`. Nothing is read or written outside
the job folder.

Exit codes: 0 done, 2 bad job, 4 import failed, 6 export failed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass

import bpy

from . import export

COMMAND = "nodelizer_convert"
SCHEMA = 1
_FOLDER = re.compile(r"^convert-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# One file, or the .obj of a bundle in the `input` folder, as the host wrote them.
_INPUT = re.compile(
    r"^(input\.(glb|fbx|obj|stl|usdz)|input/([A-Za-z0-9 _.()-]{1,120}/){0,3}[A-Za-z0-9 _.()-]{1,120}\.obj)$",
    re.I,
)
FORMATS = ("glb", "fbx", "obj", "stl", "usdz")
_MAX_JSON = 16 * 1024


class JobError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    folder: str
    input: str
    format: str

    @property
    def input_path(self) -> str:
        return os.path.join(self.folder, *self.input.split("/"))

    @property
    def result_name(self) -> str:
        return "result.zip" if self.format == "obj" else f"result.{self.format}"

    @property
    def result_path(self) -> str:
        return os.path.join(self.folder, self.result_name)


def load(folder: str) -> Job:
    """The job in `folder`, once every part of it checks out."""
    if not os.path.isabs(folder):
        raise JobError("the job folder must be absolute")
    folder = os.path.realpath(folder)
    if not _FOLDER.match(os.path.basename(folder)) or not os.path.isdir(folder):
        raise JobError("not a job folder")
    path = os.path.join(folder, "job.json")
    if not os.path.isfile(path) or os.path.getsize(path) > _MAX_JSON:
        raise JobError("job.json is missing or too large")
    with open(path, encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except ValueError as error:
            raise JobError("job.json isn't JSON") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise JobError("unknown job schema")
    source = data.get("input")
    fmt = data.get("format")
    if not isinstance(source, str) or not _INPUT.match(source) or ".." in source:
        raise JobError("bad input name")
    if fmt not in FORMATS:
        raise JobError("unknown format")
    job = Job(folder=folder, input=source, format=fmt)
    # A link pointing out of the folder isn't the host's file.
    if os.path.commonpath([os.path.realpath(job.input_path), folder]) != folder:
        raise JobError("the input leaves the job folder")
    return job


def _clear():
    """An empty scene: the start file's cube, camera and light would go out with the model."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)


def _import(path: str):
    extension = os.path.splitext(path)[1].lower()
    if extension == ".glb":
        options = export._options(
            bpy.ops.import_scene.gltf, bone_heuristic="TEMPERANCE", disable_bone_shape=True
        )
        result = bpy.ops.import_scene.gltf(filepath=path, **options)
    elif extension == ".fbx":
        result = bpy.ops.import_scene.fbx(filepath=path)
    elif extension == ".obj":
        result = bpy.ops.wm.obj_import(filepath=path)
    elif extension == ".stl":
        result = bpy.ops.wm.stl_import(filepath=path)
    elif extension == ".usdz":
        result = bpy.ops.wm.usd_import(filepath=path)
    else:
        raise ValueError("unknown model format")
    if "FINISHED" not in result or not bpy.data.objects:
        raise RuntimeError("the importer brought nothing in")


_IMAGE_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "BMP": ".bmp",
    "TARGA": ".tga",
    "TARGA_RAW": ".tga",
    "TIFF": ".tif",
    "WEBP": ".webp",
    "OPEN_EXR": ".exr",
}


def _write_textures(folder: str):
    """Puts every texture the model uses in `folder`, beside the OBJ, and points the materials at
    them there. A GLB or FBX keeps its textures inside, and the OBJ exporter only copies files."""
    taken = set()
    for image in bpy.data.images:
        if image.source != "FILE" or image.users == 0:
            continue
        base = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(image.name)[0])[:60] or "texture"
        extension = _IMAGE_EXTENSIONS.get(image.file_format, ".png")
        name = base + extension
        number = 1
        while name.lower() in taken:
            number += 1
            name = f"{base}_{number}{extension}"
        taken.add(name.lower())
        path = os.path.join(folder, name)
        if image.packed_file is not None:
            with open(path, "wb") as handle:
                handle.write(image.packed_file.data)
        else:
            source = bpy.path.abspath(image.filepath)
            if not os.path.isfile(source):
                continue
            shutil.copyfile(source, path)
        image.filepath = path


def _export_obj(job: Job):
    """An OBJ with its .mtl and its textures beside it, zipped into one file."""
    folder = os.path.join(job.folder, "result")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    _write_textures(folder)
    options = export._options(
        bpy.ops.wm.obj_export,
        export_materials=True,
        # The textures are already beside it: the .mtl names them by file name alone.
        path_mode="STRIP",
        export_uv=True,
        export_normals=True,
        export_selected_objects=False,
    )
    with export.object_mode():
        result = bpy.ops.wm.obj_export(filepath=os.path.join(folder, "model.obj"), **options)
    if "FINISHED" not in result:
        raise RuntimeError("the exporter didn't finish")
    temporary = job.result_path[: -len(".zip")] + ".partial.zip"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, _dirs, names in os.walk(folder):
            for name in sorted(names):
                full = os.path.join(root, name)
                archive.write(full, os.path.relpath(full, folder).replace(os.sep, "/"))
    shutil.rmtree(folder, ignore_errors=True)
    export._replace(temporary, job.result_path)


def _export(job: Job):
    if job.format in ("glb", "fbx"):
        export.export(job.result_path, job.format)
        return
    if job.format == "obj":
        _export_obj(job)
        return
    temporary = job.result_path[: -len(job.format) - 1] + f".partial.{job.format}"
    with export.object_mode():
        if job.format == "stl":
            result = bpy.ops.wm.stl_export(filepath=temporary)
        else:
            # USDZ packs its textures into the file.
            result = bpy.ops.wm.usd_export(
                filepath=temporary, **export._options(bpy.ops.wm.usd_export, export_textures=True)
            )
    if "FINISHED" not in result:
        raise RuntimeError("the exporter didn't finish")
    export._replace(temporary, job.result_path)


def execute(argv) -> int:
    if len(argv) != 1:
        print("usage: blender --command nodelizer_convert <job folder>", file=sys.stderr)
        return 2
    try:
        job = load(argv[0])
    except JobError as error:
        print(f"nodelizer_convert: {error}", file=sys.stderr)
        return 2
    try:
        _clear()
        if not os.path.isfile(job.input_path):
            raise RuntimeError(f"{job.input} is missing")
        _import(job.input_path)
    except Exception as error:  # Importers raise all kinds of errors on broken files.
        print(f"nodelizer_convert: import failed: {error}", file=sys.stderr)
        return 4
    try:
        _export(job)
    except Exception as error:
        print(f"nodelizer_convert: export failed: {error}", file=sys.stderr)
        return 6
    return 0
