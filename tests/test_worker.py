# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the worker's request checks and its mesh file, which run without Blender."""

import array
import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import blender_stubs  # noqa: E402, F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from addon import meshfile  # noqa: E402
from addon import worker_protocol as wp  # noqa: E402

ROOT = os.path.realpath(os.sep + os.path.join("tmp", "project", "blender"))
EDITS = os.path.join(ROOT, ".edits", "node-1")


def line(message) -> bytes:
    return (json.dumps(message) + "\n").encode("utf-8")


def run(*steps, request_id="r1"):
    return line({"id": request_id, "type": "run", "steps": list(steps)})


class RequestTests(unittest.TestCase):
    def test_takes_a_commit(self):
        request = wp.decode(
            run(
                {
                    "type": "apply",
                    "from": "n:1",
                    "to": "n:2",
                    "ops": [
                        {
                            "op": "set_transform",
                            "object": "a1b2c3d4e5f6",
                            "location": [1, 2, 3],
                            "rotation": [2, 0, 0, 0],
                        }
                    ],
                },
                {"type": "save", "blend": os.path.join(ROOT, "hero.blend")},
                {"type": "snapshot", "folder": EDITS, "name": "r2", "appearance": None},
            )
        )
        self.assertEqual([step["type"] for step in request["steps"]], ["apply", "save", "snapshot"])
        op = request["steps"][0]["ops"][0]
        # Rotations come back as unit quaternions.
        self.assertEqual(op["rotation"], [1.0, 0.0, 0.0, 0.0])
        self.assertNotIn("scale", op)

    def test_ping_and_quit_need_nothing_else(self):
        self.assertEqual(wp.decode(line({"id": "p", "type": "ping"})), {"id": "p", "type": "ping"})
        self.assertEqual(wp.decode(line({"id": "q", "type": "quit"}))["type"], "quit")

    def test_refuses_paths_outside_a_blender_folder(self):
        for blend in (
            "relative/blender/x.blend",
            os.path.join(os.sep, "tmp", "x.blend"),
            os.path.join(ROOT, "..", "blender", "x.blend"),
            os.path.join(ROOT, "x.txt"),
        ):
            with self.assertRaises(wp.RequestError, msg=blend):
                wp.decode(run({"type": "save", "blend": blend}))

    def test_refuses_unknown_steps_ops_and_fields(self):
        cases = [
            {"type": "python", "code": "print(1)"},
            {"type": "apply", "from": "a", "to": "b", "ops": [{"op": "exec"}]},
            {
                "type": "apply",
                "from": "a",
                "to": "b",
                "ops": [{"op": "set_transform", "object": "x", "location": [0, 0, 0], "code": 1}],
            },
            {
                "type": "apply",
                "from": "a",
                "to": "b",
                "ops": [{"op": "set_transform", "object": "x"}],
            },
            {"type": "export", "path": os.path.join(ROOT, "out.glb"), "format": "blend"},
            {"type": "snapshot", "folder": EDITS, "name": "../r1", "appearance": None},
        ]
        for step in cases:
            with self.assertRaises(wp.RequestError, msg=json.dumps(step)):
                wp.decode(run(step))

    def test_names_the_edit_that_is_wrong(self):
        ops = [
            {"op": "set_transform", "object": "a", "location": [0, 0, 0]},
            {"op": "set_transform", "object": "b", "location": [0, float("nan"), 0]},
        ]
        with self.assertRaises(wp.RequestError) as caught:
            wp.check_ops(json.loads(json.dumps(ops)))
        self.assertEqual(caught.exception.index, 1)
        self.assertEqual(caught.exception.code, "op_invalid")
        with self.assertRaises(wp.RequestError):
            wp.check_ops([{"op": "set_transform", "object": "a", "scale": [1, True, 1]}])
        with self.assertRaises(wp.RequestError):
            wp.check_ops([{"op": "set_transform", "object": "a", "rotation": [0, 0, 0, 0]}])

    def test_bad_lines_keep_their_id_when_they_have_one(self):
        with self.assertRaises(wp.RequestError):
            wp.decode(b"not json\n")
        self.assertEqual(wp.request_id(run({"type": "nope"}, request_id="abc")), "abc")
        self.assertIsNone(wp.request_id(b"[1, 2]"))
        with self.assertRaises(wp.RequestError):
            wp.decode(b"x" * (wp.MAX_LINE_BYTES + 1))

    def test_answers_start_their_own_line(self):
        data = wp.encode({"id": "a", "ok": True})
        self.assertTrue(data.startswith(b"\n" + wp.MARK.encode()))
        self.assertTrue(data.endswith(b"\n"))


class MeshFileTests(unittest.TestCase):
    def test_packs_aligned_arrays_with_a_header(self):
        positions = array.array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0])
        loops = array.array("I", [0, 1, 2])
        flags = array.array("B", [1, 0, 1])
        data = meshfile.pack(
            [("positions", "f32", positions), ("loopVertex", "u32", loops), ("seam", "u8", flags)],
            {"counts": {"vertices": 3}},
        )
        header = meshfile.read_header(data)
        self.assertEqual(header["counts"], {"vertices": 3})
        _magic, _version, length = struct.unpack_from("<4sII", data, 0)
        start = 12 + length + (16 - (12 + length) % 16) % 16
        for entry in header["arrays"]:
            self.assertEqual((start + entry["offset"]) % 16, 0)
        listed = {entry["name"]: entry for entry in header["arrays"]}
        self.assertEqual(listed["positions"]["length"], 9)
        offset = start + listed["loopVertex"]["offset"]
        self.assertEqual(list(struct.unpack_from("<3I", data, offset)), [0, 1, 2])
        offset = start + listed["seam"]["offset"]
        self.assertEqual(list(data[offset : offset + 3]), [1, 0, 1])

    def test_the_same_mesh_gives_the_same_bytes(self):
        arrays = [("positions", "f32", array.array("f", [1, 2, 3]))]
        first = meshfile.pack(arrays, {"counts": {"vertices": 1}})
        second = meshfile.pack(arrays, {"counts": {"vertices": 1}})
        self.assertEqual(meshfile.digest(first), meshfile.digest(second))

    def test_refuses_unknown_types(self):
        with self.assertRaises(ValueError):
            meshfile.pack([("x", "f64", array.array("d", [1.0]))])


if __name__ == "__main__":
    unittest.main()
