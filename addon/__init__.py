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

from . import convert, operators, panel, prepare, runtime, worker

_classes = (*operators.classes, *panel.classes)
_command = None
_convert = None
_worker = None


def _quit():
    runtime.stop()


def register():
    global _command, _convert, _worker
    for cls in _classes:
        bpy.utils.register_class(cls)
    _command = bpy.utils.register_cli_command(prepare.COMMAND, prepare.execute)
    _convert = bpy.utils.register_cli_command(convert.COMMAND, convert.execute)
    _worker = bpy.utils.register_cli_command(worker.COMMAND, worker.execute)
    # In the background (the prepare, convert and worker commands) there's no window and nothing
    # to connect.
    # Persistent, since Blender drops other timers when it loads the file it was opened with.
    if not bpy.app.background:
        bpy.app.timers.register(runtime.start, first_interval=0.5, persistent=True)
        atexit.register(_quit)


def unregister():
    global _command, _convert, _worker
    atexit.unregister(_quit)
    if bpy.app.timers.is_registered(runtime.start):
        bpy.app.timers.unregister(runtime.start)
    runtime.stop()
    if _command is not None:
        bpy.utils.unregister_cli_command(_command)
        _command = None
    if _convert is not None:
        bpy.utils.unregister_cli_command(_convert)
        _convert = None
    if _worker is not None:
        bpy.utils.unregister_cli_command(_worker)
        _worker = None
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
