# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the parts that run without Blender: session checks, the protocol and the socket."""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

# The add-on's package imports bpy; these tests only need its pure Python modules.
sys.path.insert(0, os.path.dirname(__file__))
import blender_stubs  # noqa: E402, F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from addon import convert, export, ipc, protocol  # noqa: E402
from addon import session as sessions  # noqa: E402

TOKEN = "t" * 43


def make_session(root, **overrides):
    session_id = str(uuid.uuid4())
    folder = os.path.join(root, f"session-{session_id}")
    os.makedirs(folder)
    blend = os.path.join(root, "project", "blender", "node.blend")
    data = {
        "schema": 2,
        "sessionId": session_id,
        "projectName": "Knight",
        "nodeName": "Hero scene",
        "format": "glb",
        "blend": blend,
        "imports": [
            {"input": "node-1", "name": "Hero", "file": "input-1.glb"},
            {"input": "node-2", "name": "Sword", "file": "input-2.fbx"},
        ],
    }
    data.update(overrides)
    with open(os.path.join(folder, "session.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return folder, session_id


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())

    def test_loads_a_valid_session(self):
        folder, session_id = make_session(self.root)
        session = sessions.load(folder)
        self.assertEqual(session.session_id, session_id)
        self.assertEqual(session.result_path(), os.path.join(folder, "result.glb"))
        self.assertEqual(session.node_name, "Hero scene")
        self.assertEqual([i.input for i in session.imports], ["node-1", "node-2"])
        self.assertEqual(
            session.import_path(session.imports[1]), os.path.join(folder, "input-2.fbx")
        )

    def test_takes_a_scene_with_nothing_to_import(self):
        folder, _ = make_session(self.root, imports=[])
        self.assertEqual(sessions.load(folder).imports, ())

    def test_refuses_an_older_schema(self):
        folder, _ = make_session(self.root, schema=1)
        with self.assertRaises(sessions.SessionError):
            sessions.load(folder)

    def test_refuses_relative_and_odd_folders(self):
        with self.assertRaises(sessions.SessionError):
            sessions.load("session-x")
        odd = os.path.join(self.root, "not-a-session")
        os.makedirs(odd)
        with self.assertRaises(sessions.SessionError):
            sessions.load(odd)

    def test_refuses_a_mismatched_id(self):
        folder, _ = make_session(self.root, sessionId=str(uuid.uuid4()))
        with self.assertRaises(sessions.SessionError):
            sessions.load(folder)

    def test_refuses_import_files_that_leave_the_folder(self):
        for file in ("../input-1.glb", "/etc/passwd", "input-1.blend", "input-1.glb/../x", "x.glb"):
            imports = [{"input": "node-1", "name": "Hero", "file": file}]
            folder, _ = make_session(self.root, imports=imports)
            with self.assertRaises(sessions.SessionError, msg=file):
                sessions.load(folder)

    def test_refuses_an_import_that_links_out_of_the_folder(self):
        outside = os.path.join(self.root, "elsewhere.glb")
        open(outside, "wb").close()
        folder, _ = make_session(
            self.root, imports=[{"input": "node-1", "name": "Hero", "file": "input-1.glb"}]
        )
        os.symlink(outside, os.path.join(folder, "input-1.glb"))
        with self.assertRaises(sessions.SessionError):
            sessions.load(folder)

    def test_refuses_odd_input_ids(self):
        for input_id in ("", "../x", "a b", "x" * 200):
            imports = [{"input": input_id, "name": "Hero", "file": "input-1.glb"}]
            folder, _ = make_session(self.root, imports=imports)
            with self.assertRaises(sessions.SessionError, msg=input_id):
                sessions.load(folder)

    def test_refuses_a_blend_outside_a_blender_folder(self):
        for blend in ("relative.blend", os.path.join(self.root, "x.blend"), "/tmp/blender/x.txt"):
            folder, _ = make_session(self.root, blend=blend)
            with self.assertRaises(sessions.SessionError, msg=blend):
                sessions.load(folder)

    def test_refuses_unknown_formats(self):
        folder, _ = make_session(self.root, format="obj")
        with self.assertRaises(sessions.SessionError):
            sessions.load(folder)

    def test_writes_a_result_record(self):
        folder, session_id = make_session(self.root)
        session = sessions.load(folder)
        with open(session.result_path(), "wb") as handle:
            handle.write(b"glTF")
        name = sessions.write_result(session, "glb", "return")
        self.assertEqual(name, "result.glb")
        with open(session.result_json, encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(record["sessionId"], session_id)
        self.assertEqual(record["kind"], "return")
        self.assertEqual(len(record["sha256"]), 64)


class ProtocolTests(unittest.TestCase):
    def test_accepts_only_known_commands(self):
        self.assertEqual(protocol.decode(b'{"type":"PING"}'), {"type": "PING"})
        self.assertEqual(
            protocol.decode(b'{"type":"EXPORT","format":"FBX"}'), {"type": "EXPORT", "format": "FBX"}
        )
        self.assertIsNone(protocol.decode(b'{"type":"EXPORT","format":"OBJ"}'))
        self.assertIsNone(protocol.decode(b'{"python":"import os"}'))
        self.assertIsNone(protocol.decode(b'{"type":"RUN","code":"x"}'))
        self.assertIsNone(protocol.decode(b"not json"))
        self.assertIsNone(protocol.decode(b"[1,2]"))
        self.assertIsNone(protocol.decode(b'{"type":"WELCOME","protocol":9}'))

    def test_drops_extra_fields(self):
        message = protocol.decode(b'{"type":"EXPORT","format":"GLB","path":"/etc/passwd"}')
        self.assertEqual(message, {"type": "EXPORT", "format": "GLB"})

    def test_failures_never_carry_details(self):
        self.assertEqual(protocol.failed("id", "anything")["stage"], "export")
        self.assertNotIn("message", protocol.failed("id", "import"))


class ClientTests(unittest.TestCase):
    def test_says_hello_and_takes_commands_after_welcome(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = []

        def host():
            connection, _ = server.accept()
            reader = connection.makefile("rb")
            received.append(json.loads(reader.readline()))
            connection.sendall(b'{"type":"PING"}\n')  # Before WELCOME: ignored.
            connection.sendall(b'{"type":"WELCOME","protocol":2}\n{"type":"EXPORT","format":"GLB"}\n')
            reader.readline()
            connection.close()

        thread = threading.Thread(target=host, daemon=True)
        thread.start()
        hello = protocol.hello("id", TOKEN, "1.0.0", "5.2.2")
        client = ipc.Client(port, TOKEN, hello)
        client.start()
        try:
            self.assertTrue(ipc.wait_for(lambda: client.connected, 5))
            self.assertTrue(ipc.wait_for(lambda: client.incoming.qsize() >= 2, 5))
            self.assertEqual(client.incoming.get_nowait()["type"], "WELCOME")
            self.assertEqual(client.incoming.get_nowait(), {"type": "EXPORT", "format": "GLB"})
            self.assertEqual(received[0]["token"], TOKEN)
            self.assertTrue(client.send(protocol.open_app("id")))
        finally:
            client.stop()
            server.close()

    def test_refuses_bad_tokens_and_ports(self):
        with self.assertRaises(ValueError):
            ipc.Client(0, TOKEN, {})
        with self.assertRaises(ValueError):
            ipc.Client(1234, "short", {})



def make_job(root, **overrides):
    folder = os.path.join(root, f"convert-{uuid.uuid4()}")
    os.makedirs(folder)
    data = {"schema": 1, "input": "input.glb", "format": "obj", **overrides}
    with open(os.path.join(folder, "job.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return folder


class ConvertJobTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_loads_a_job_and_names_its_result(self):
        job = convert.load(make_job(self.root))
        self.assertEqual(job.format, "obj")
        self.assertEqual(job.result_name, "result.zip")
        self.assertTrue(job.input_path.endswith("input.glb"))
        stl = convert.load(make_job(self.root, input="input.usdz", format="stl"))
        self.assertEqual(stl.result_name, "result.stl")

    def test_takes_an_obj_with_its_files_in_a_folder(self):
        job = convert.load(make_job(self.root, input="input/Model (1).obj", format="glb"))
        self.assertTrue(job.input_path.endswith(os.path.join("input", "Model (1).obj")))

    def test_refuses_what_leaves_the_folder_or_isnt_known(self):
        for overrides in (
            {"input": "../input.glb"},
            {"input": "input/../../x.obj"},
            {"input": "/etc/input.glb"},
            {"input": "input.blend"},
            {"input": "input/model.mtl"},
            {"format": "blend"},
            {"schema": 2},
        ):
            with self.assertRaises(convert.JobError, msg=str(overrides)):
                convert.load(make_job(self.root, **overrides))

    def test_refuses_a_linked_job_file(self):
        folder = make_job(self.root)
        elsewhere = os.path.join(self.root, "job.json")
        os.replace(os.path.join(folder, "job.json"), elsewhere)
        os.symlink(elsewhere, os.path.join(folder, "job.json"))
        with self.assertRaises(convert.JobError):
            convert.load(folder)

    def test_refuses_relative_and_odd_folders(self):
        with self.assertRaises(convert.JobError):
            convert.load("convert-x")
        odd = os.path.join(self.root, "somewhere")
        os.makedirs(odd)
        with self.assertRaises(convert.JobError):
            convert.load(odd)


class BundleImportTests(unittest.TestCase):
    def test_a_session_takes_an_unpacked_obj_but_nothing_outside_its_folder(self):
        root = tempfile.mkdtemp()
        folder, _ = make_session(
            root, imports=[{"input": "a", "name": "Knight", "file": "input-0/knight.obj"}]
        )
        good = sessions.load(folder)
        self.assertTrue(good.import_path(good.imports[0]).endswith(os.path.join("input-0", "knight.obj")))
        nested, _ = make_session(
            root, imports=[{"input": "a", "name": "Knight", "file": "input-0/model/knight.obj"}]
        )
        self.assertEqual(sessions.load(nested).imports[0].file, "input-0/model/knight.obj")
        for file in ("input-0/../x.obj", "input-0/knight.mtl", "input-0/a/b/c/d/e.obj", "input-0//x.obj"):
            folder, _ = make_session(root, imports=[{"input": "a", "name": "Knight", "file": file}])
            with self.assertRaises(sessions.SessionError, msg=file):
                sessions.load(folder)


class Data(list):
    """A stand-in for one of bpy.data's collections."""

    def remove(self, item):
        super().remove(item)


def datablock(path, packed=False, name=None):
    item = types.SimpleNamespace(
        name=name or os.path.basename(path) or "image",
        filepath=path,
        library=None,
        packed_file=object() if packed else None,
        packed_files=[],
        reloaded=False,
    )
    item.as_pointer = lambda: id(item)
    item.reload = lambda: setattr(item, "reloaded", True)
    return item


def abspath(path, library=None):
    return path


class ModelFileTests(unittest.TestCase):
    """What an import may bring in: only files inside the folder the host put the model in."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.folder = os.path.join(self.root, f"session-{uuid.uuid4()}")
        os.makedirs(os.path.join(self.folder, "input-1", "textures"))
        self.secret = os.path.join(self.root, "id_ed25519")
        open(self.secret, "w").close()

    def keep(self, images, before=(), model=None, **others):
        data = types.SimpleNamespace(images=Data(images), **{k: Data(v) for k, v in others.items()})
        with mock.patch.object(export.bpy, "data", data, create=True), mock.patch.object(
            export.bpy, "path", types.SimpleNamespace(abspath=abspath), create=True
        ), mock.patch("sys.stderr", io.StringIO()):
            export.keep_inside(
                self.folder,
                {item.as_pointer() for item in before},
                model or os.path.join(self.folder, "input-1", "model.obj"),
            )
        return data

    def test_drops_textures_a_model_names_outside_its_folder(self):
        inside = datablock(os.path.join(self.folder, "input-1", "textures", "wood.png"))
        climbing = datablock(os.path.join(self.folder, "input-1", "..", "..", "id_ed25519"))
        absolute = datablock(self.secret)
        link = os.path.join(self.folder, "input-1", "textures", "link.png")
        os.symlink(self.secret, link)
        linked = datablock(link)
        data = self.keep([inside, climbing, absolute, linked])
        self.assertEqual(list(data.images), [inside])

    def test_keeps_what_the_model_carries_and_what_was_there(self):
        # A GLB's or FBX's own textures are packed from its bytes, whatever path they name.
        embedded = datablock(os.path.join(self.folder, "..", "textures", "wood.png"), packed=True)
        generated = datablock("")
        earlier = datablock(os.path.join(self.root, "my texture.png"))
        font = datablock("<builtin>")
        data = self.keep([embedded, generated, earlier], before=[earlier], fonts=[font])
        self.assertEqual(list(data.images), [embedded, generated, earlier])
        self.assertEqual(list(data.fonts), [font])

    def test_drops_other_files_a_model_names_outside_its_folder(self):
        volume = datablock(os.path.join(self.root, "cloud.vdb"))
        sound = datablock(self.secret)
        data = self.keep([], volumes=[volume], sounds=[sound])
        self.assertEqual((list(data.volumes), list(data.sounds)), ([], []))

    def test_a_texture_named_from_another_computer_comes_from_the_models_own_files(self):
        # Exporters write paths from the computer they ran on; the bundle carries the file.
        own = os.path.join(self.folder, "input-1", "textures", "Wood.PNG")
        open(own, "w").close()
        original = os.path.join(self.root, "wood.png")
        open(original, "w").close()
        here = datablock(original)
        windows = datablock("C:\\Users\\me\\Wood.png")
        data = self.keep([here, windows])
        self.assertEqual(list(data.images), [here, windows])
        self.assertEqual((here.filepath, windows.filepath), (own, own))
        self.assertTrue(here.reloaded and windows.reloaded)

    def test_a_texture_of_the_same_name_must_be_the_models_own(self):
        # Another model's files, and a link out of the folder, aren't this model's.
        os.makedirs(os.path.join(self.folder, "input-2"))
        open(os.path.join(self.folder, "input-2", "wood.png"), "w").close()
        os.symlink(self.secret, os.path.join(self.folder, "input-1", "textures", "id_ed25519"))
        single = os.path.join(self.folder, "input-3.glb")
        open(single, "w").close()
        wood = datablock(os.path.join(self.root, "wood.png"))
        key = datablock(self.secret)
        self.assertEqual(list(self.keep([wood, key]).images), [])
        self.assertEqual(list(self.keep([datablock("/elsewhere/wood.png")], model=single).images), [])

    def test_a_missing_texture_doesnt_fail_the_import(self):
        def pack_all():
            raise RuntimeError("Unable to pack file, source path 'wood.png' not found")

        ops = types.SimpleNamespace(file=types.SimpleNamespace(pack_all=pack_all))
        with mock.patch.object(export.bpy, "ops", ops, create=True), mock.patch(
            "sys.stderr", io.StringIO()
        ):
            export.pack_files()

    def test_gltf_leaves_its_images_for_the_check(self):
        calls = []

        def gltf(**options):
            calls.append(options)
            return {"FINISHED"}

        known = ("filepath", "bone_heuristic", "disable_bone_shape", "import_pack_images")
        rna = types.SimpleNamespace(properties=[types.SimpleNamespace(identifier=k) for k in known])
        gltf.get_rna_type = lambda: rna
        ops = types.SimpleNamespace(import_scene=types.SimpleNamespace(gltf=gltf))
        with mock.patch.object(export.bpy, "ops", ops, create=True):
            export.import_gltf("/tmp/input-1.glb")
        self.assertIs(calls[0]["import_pack_images"], False)


if __name__ == "__main__":
    unittest.main()
