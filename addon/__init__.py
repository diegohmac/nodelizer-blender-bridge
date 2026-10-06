# SPDX-License-Identifier: GPL-3.0-or-later
#
# Nodelizer Bridge: edit Nodelizer models in Blender and send them back.
# Copyright (C) 2026 Diego Machado
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If
# not, see <https://www.gnu.org/licenses/>.

import atexit

import bpy

from . import operators, panel, prepare, runtime

_classes = (*operators.classes, *panel.classes)
_command = None


def _quit():
    runtime.stop()


def register():
    global _command
    for cls in _classes:
        bpy.utils.register_class(cls)
    _command = bpy.utils.register_cli_command(prepare.COMMAND, prepare.execute)
    # In the background (the prepare command) there's no window and nothing to connect.
    # Persistent, since Blender drops other timers when it loads the file it was opened with.
    if not bpy.app.background:
        bpy.app.timers.register(runtime.start, first_interval=0.5, persistent=True)
        atexit.register(_quit)


def unregister():
    global _command
    atexit.unregister(_quit)
    if bpy.app.timers.is_registered(runtime.start):
        bpy.app.timers.unregister(runtime.start)
    runtime.stop()
    if _command is not None:
        bpy.utils.unregister_cli_command(_command)
        _command = None
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
