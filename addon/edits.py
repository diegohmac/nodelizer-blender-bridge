# SPDX-License-Identifier: GPL-3.0-or-later
"""The edits the worker makes to its scene. Each has been checked by worker_protocol first; here
they're checked against the scene, all of them before any is made, so a batch is all or nothing."""

from __future__ import annotations

import bpy
from mathutils import Quaternion

from . import scene_data
from .worker_protocol import RequestError


def check(ops: list[dict]) -> dict:
    """The scene's objects by id, once every edit names one that's there."""
    objects = scene_data.by_id()
    for index, op in enumerate(ops):
        if op["object"] not in objects:
            raise RequestError("unknown object", "op_invalid", index)
    return objects


def _set_transform(obj, op: dict):
    if "location" in op:
        obj.location = op["location"]
    if "rotation" in op:
        turn = Quaternion(op["rotation"])
        mode = obj.rotation_mode
        if mode == "QUATERNION":
            obj.rotation_quaternion = turn
        elif mode == "AXIS_ANGLE":
            axis, angle = turn.to_axis_angle()
            obj.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
        else:
            # The Euler angles nearest the current ones, so a small turn doesn't flip the numbers.
            obj.rotation_euler = turn.to_euler(mode, obj.rotation_euler)
    if "scale" in op:
        obj.scale = op["scale"]


def apply(ops: list[dict]) -> list[str]:
    """Makes the edits and returns the ids of the objects they changed."""
    objects = check(ops)
    changed = []
    for op in ops:
        obj = objects[op["object"]]
        if op["op"] == "set_transform":
            _set_transform(obj, op)
        if op["object"] not in changed:
            changed.append(op["object"])
    # Matrices follow the new values only once the scene is evaluated.
    bpy.context.view_layer.update()
    return changed
