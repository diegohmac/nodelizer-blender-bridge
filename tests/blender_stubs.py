# SPDX-License-Identifier: GPL-3.0-or-later
"""Stand-ins for Blender's modules, so the add-on's pure Python parts import outside Blender."""

import sys
import types

bpy = types.ModuleType("bpy")
bpy.types = types.SimpleNamespace(Operator=object, Panel=object)
bpy.app = types.SimpleNamespace(
    background=True,
    timers=None,
    version_string="test",
    handlers=types.SimpleNamespace(persistent=lambda function: function, save_post=[]),
)
sys.modules.setdefault("bpy", bpy)
mathutils = types.ModuleType("mathutils")
mathutils.Quaternion = object
sys.modules.setdefault("mathutils", mathutils)
sys.modules.setdefault("numpy", types.ModuleType("numpy"))
