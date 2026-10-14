# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the parts that run without Blender: session checks, the protocol and the socket."""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import uuid

# The add-on's package imports bpy; these tests only need its pure Python modules.
sys.path.insert(0, os.path.dirname(__file__))
import blender_stubs  # noqa: E402, F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from addon import convert, ipc, protocol  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
