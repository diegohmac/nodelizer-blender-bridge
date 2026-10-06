# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's buttons: Send Back, Sync and Open Nodelizer."""

from __future__ import annotations

import time

import bpy

from . import export, protocol
from . import session as sessions
from .runtime import is_session_file, send, state


def deliver(kind: str) -> str | None:
    """Saves the .blend, exports the result and tells the host. Returns a problem, or None.

    The .blend is saved first, so the editable file is never behind what the host gets.
    """
    session = state.session
    if session is None:
        return "There's no Nodelizer session in this Blender."
    if not is_session_file():
        return "Open the session's own file to send it back. It's in File, Open Recent."
    try:
        bpy.ops.wm.save_mainfile()
    except RuntimeError:
        send(protocol.failed(session.session_id, "save"))
        return "Blender couldn't save the file. Check there's space on the disk and try again."
    try:
        export.export(session.result_path(), session.format)
        name = sessions.write_result(session, session.format, kind)
    except (RuntimeError, OSError, sessions.SessionError):
        send(protocol.failed(session.session_id, "export"))
        return "Blender couldn't export the model. Your .blend file is saved."
    message = (
        protocol.return_to_app(session.session_id, name)
        if kind == "return"
        else protocol.asset_changed(session.session_id, name)
    )
    state.last_sent = time.strftime("%H:%M")
    if not send(message):
        return (
            "Saved, but Nodelizer isn't connected. It picks up the model next time you open"
            " this project."
        )
    return None


class NODELIZER_OT_send_back(bpy.types.Operator):
    bl_idname = "nodelizer.send_back"
    bl_label = "Send Back to Nodelizer"
    bl_description = "Save, export the model and update its node in Nodelizer"

    @classmethod
    def poll(cls, context):
        return state.session is not None

    def execute(self, context):
        problem = deliver("return")
        if problem:
            self.report({"WARNING"}, problem)
            return {"CANCELLED"}
        self.report({"INFO"}, "Sent back to Nodelizer")
        return {"FINISHED"}


class NODELIZER_OT_sync(bpy.types.Operator):
    bl_idname = "nodelizer.sync"
    bl_label = "Sync"
    bl_description = "Update the node in Nodelizer and keep working here"

    @classmethod
    def poll(cls, context):
        return state.session is not None

    def execute(self, context):
        problem = deliver("sync")
        if problem:
            self.report({"WARNING"}, problem)
            return {"CANCELLED"}
        self.report({"INFO"}, "Nodelizer is up to date")
        return {"FINISHED"}


class NODELIZER_OT_open_app(bpy.types.Operator):
    bl_idname = "nodelizer.open_app"
    bl_label = "Open Nodelizer"
    bl_description = "Bring the Nodelizer window to the front"

    @classmethod
    def poll(cls, context):
        return state.client is not None and state.client.connected

    def execute(self, context):
        if not send(protocol.open_app(state.session.session_id)):
            self.report({"WARNING"}, "Nodelizer isn't connected.")
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (NODELIZER_OT_send_back, NODELIZER_OT_sync, NODELIZER_OT_open_app)
