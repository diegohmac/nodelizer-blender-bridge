# SPDX-License-Identifier: GPL-3.0-or-later
"""`blender --background --command nodelizer_worker`: a Blender that stays open, with no window,
holding one scene at a time for the host's 3D editor.

The host writes requests on stdin and reads answers on stdout (see worker_protocol.py and
docs/PROTOCOL.md). A request is a list of steps run in order: load a .blend, bring a model into an
empty scene, apply edits, save, write a snapshot, export. When a step fails after the scene was
changed, the scene is dropped, so a half-made edit is never saved later; the host loads it again.
The worker stops when its stdin closes, so it never outlives the host.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import bpy

from . import assemble, convert, edits, export, runtime, scene_data
from .worker_protocol import (
    MAX_LINE_BYTES,
    PROTOCOL,
    RequestError,
    decode,
    encode,
    request_id,
)

COMMAND = "nodelizer_worker"
_MUTATING = ("load", "import", "add", "assemble", "assemble_parts", "apply")
# Failures that happen before a step changes anything.
_UNTOUCHED = ("not_loaded", "op_invalid", "op_unsupported")


def send(message: dict):
    data = encode(message)
    while data:
        written = os.write(1, data)
        data = data[written:]


def _clear():
    """An empty scene with Blender's default units, keeping the add-on and preferences loaded."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    units = bpy.context.scene.unit_settings
    units.system = "METRIC"
    units.scale_length = 1.0
    units.length_unit = "METERS"


class Failure(Exception):
    def __init__(self, code: str, message: str, index: int | None = None):
        super().__init__(message)
        self.code = code
        self.index = index


class Worker:
    def __init__(self):
        # The host's name for the scene loaded now, such as `<node>:12`; None when there's none.
        self.key: str | None = None
        self.snapshots = scene_data.Snapshots()

    def reset(self):
        self.key = None
        self.snapshots.forget()
        _clear()

    def _load(self, step: dict) -> dict:
        try:
            if step["blend"] is None:
                _clear()
            else:
                if not os.path.isfile(step["blend"]):
                    raise Failure("load_failed", "the .blend is missing")
                bpy.ops.wm.open_mainfile(filepath=step["blend"], load_ui=False)
        except Failure:
            raise
        except Exception as error:  # A damaged file raises whatever it likes.
            raise Failure("load_failed", str(error)) from error
        self.snapshots.forget()
        for ops in step["replay"]:
            try:
                edits.apply(ops)
            except RequestError as error:
                raise Failure("replay_failed", str(error), error.index) from error
        self.key = step["key"]
        return {"key": self.key}

    def _import(self, step: dict) -> dict:
        _clear()
        self.snapshots.forget()
        try:
            convert._import(step["source"], step["root"])
            export.pack_files()
        except Exception as error:  # Importers raise all kinds of errors on broken files.
            raise Failure("import_failed", str(error)) from error
        scene_data.new_ids()
        bpy.context.view_layer.update()
        self.key = step["key"]
        return {"key": self.key, "objects": len(scene_data.scene_objects())}

    def _add(self, step: dict) -> dict:
        """Brings a model into the scene held, beside what's there: its objects come in where its
        file puts them, each with a new id. Nothing already in the scene changes."""
        if self.key is None or self.key != step["from"]:
            raise Failure("not_loaded", "another scene is loaded")
        before = set(scene_data.scene_objects())
        try:
            convert._import(step["source"], step["root"])
            export.pack_files()
        except Exception as error:  # Importers raise all kinds of errors on broken files.
            raise Failure("import_failed", str(error)) from error
        taken = {obj.get(scene_data.ID_PROP) for obj in before}
        made = []
        for obj in sorted(scene_data.scene_objects(), key=lambda o: o.name):
            if obj in before:
                continue
            value = uuid.uuid4().hex[:12]
            while value in taken:
                value = uuid.uuid4().hex[:12]
            taken.add(value)
            obj[scene_data.ID_PROP] = value
            made.append(value)
        if not made:
            raise Failure("import_failed", "the model holds no objects")
        bpy.context.view_layer.update()
        # A mesh brought in can take the name of one that went, whose file is remembered.
        self.snapshots.forget()
        self.key = step["to"]
        return {"key": self.key, "objects": made}

    def _build(self, step: dict) -> list[str]:
        try:
            return assemble.build(step["parts"], step["copies"])
        except assemble.PartError as error:
            raise Failure("import_failed", str(error), error.index) from error

    def _assemble(self, step: dict) -> dict:
        _clear()
        self.snapshots.forget()
        self.key = None
        changed = self._build(step)
        self.key = step["key"]
        return {"key": self.key, "objects": changed}

    def _assemble_parts(self, step: dict) -> dict:
        if self.key is None or self.key != step["from"]:
            raise Failure("not_loaded", "another scene is loaded")
        changed = self._build(step)
        # Replaced meshes are new mesh data, so their files are written again; the others keep
        # theirs.
        self.snapshots.forget()
        self.key = step["to"]
        return {"key": self.key, "objects": changed}

    def _apply(self, step: dict) -> dict:
        if self.key is None or self.key != step["from"]:
            raise Failure("not_loaded", "another scene is loaded")
        try:
            changed, meshes, reports = edits.apply(step["ops"])
        except RequestError as error:
            raise Failure(error.code, str(error), error.index) from error
        # Changed meshes make a new mesh file; the others keep theirs.
        self.snapshots.forget(meshes)
        self.key = step["to"]
        return {"key": self.key, "changed": changed, "reports": reports}

    def _save(self, step: dict) -> dict:
        try:
            os.makedirs(os.path.dirname(step["blend"]), exist_ok=True)
            with export.object_mode():
                bpy.ops.wm.save_as_mainfile(
                    filepath=step["blend"], copy=True, check_existing=False
                )
        except Exception as error:
            raise Failure("save_failed", str(error)) from error
        return {"bytes": os.path.getsize(step["blend"])}

    def _snapshot(self, step: dict) -> dict:
        try:
            return self.snapshots.write(step["folder"], step["name"], step["appearance"])
        except Exception as error:
            raise Failure("snapshot_failed", str(error)) from error

    def _export(self, step: dict) -> dict:
        try:
            os.makedirs(os.path.dirname(step["path"]), exist_ok=True)
            objects = None
            if "objects" in step:
                known = scene_data.by_id()
                missing = [each for each in step["objects"] if each not in known]
                if missing:
                    raise Failure("export_failed", "the scene doesn't have an object named")
                objects = [known[each] for each in step["objects"]]
            export.export(step["path"], step["format"], objects)
        except Failure:
            raise
        except Exception as error:
            raise Failure("export_failed", str(error)) from error
        return {"bytes": os.path.getsize(step["path"])}

    def run(self, request: dict) -> dict:
        results = []
        mutated = False
        started = time.monotonic()
        for index, step in enumerate(request["steps"]):
            kind = step["type"]
            if kind in ("import", "load", "add", "assemble", "assemble_parts"):
                stage = "load" if kind == "load" else "import"
                send({"type": "progress", "id": request["id"], "stage": stage})
            try:
                results.append(getattr(self, f"_{kind}")(step))
                mutated = mutated or kind in _MUTATING
            except Exception as error:
                failure = (
                    error if isinstance(error, Failure) else Failure("internal", repr(error))
                )
                # The scene may be half changed: drop it rather than save it later. Edits that
                # were refused before anything changed leave it as it was.
                if mutated or (kind in _MUTATING and failure.code not in _UNTOUCHED):
                    self.reset()
                answer = {
                    "id": request["id"],
                    "ok": False,
                    "step": index,
                    "code": failure.code,
                    "message": str(failure)[:500],
                    "key": self.key,
                }
                if failure.index is not None:
                    answer["index"] = failure.index
                return answer
        return {
            "id": request["id"],
            "ok": True,
            "results": results,
            "key": self.key,
            "seconds": round(time.monotonic() - started, 3),
        }


def _lines(stream):
    """Request lines, with an over-long one cut down to a size `decode` refuses."""
    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
            while True:
                rest = stream.readline(MAX_LINE_BYTES)
                if not rest or rest.endswith(b"\n"):
                    break
        yield line


def execute(argv) -> int:
    if argv:
        print("usage: blender --background --command nodelizer_worker", file=sys.stderr)
        return 2
    # Saving over the node's .blend mustn't leave a .blend1 beside it each time: the host keeps
    # the scene's history itself. Only this background process changes; it never saves preferences,
    # so Blender opened by a person keeps its backups.
    bpy.context.preferences.filepaths.save_version = 0
    worker = Worker()
    worker.reset()
    send(
        {
            "type": "ready",
            "protocol": PROTOCOL,
            "bridgeVersion": runtime.bridge_version(),
            "blenderVersion": bpy.app.version_string,
        }
    )
    for line in _lines(sys.stdin.buffer):
        if not line.strip():
            continue
        try:
            request = decode(line)
        except RequestError as error:
            send({"id": request_id(line), "ok": False, "code": error.code, "message": str(error)})
            continue
        if request["type"] == "quit":
            send({"id": request["id"], "ok": True})
            return 0
        if request["type"] == "ping":
            send({"id": request["id"], "ok": True, "key": worker.key})
            continue
        send(worker.run(request))
    return 0
