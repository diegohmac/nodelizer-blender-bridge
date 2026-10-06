# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's buttons: Send Back and Open Nodelizer. Saving with Ctrl+S sends too."""

from __future__ import annotations

import time

import bpy

from . import export, protocol
from . import session as sessions
from .runtime import is_session_file, send, state


def deliver(kind: str, save: bool = True) -> str | None:
    """Saves the .blend, exports the result and tells the host. Returns a problem, or None.

    The .blend is saved first, so the editable file is never behind what the host gets. After
    Ctrl+S it's already saved, and `save` is False.
    """
    session = state.session
    if session is None:
        return "There's no Nodelizer session in this Blender."
    if not is_session_file():
        return "Open the session's own file to send it back. It's in File, Open Recent."
    if save:
        state.saving = True
        try:
            bpy.ops.wm.save_mainfile()
        except RuntimeError:
            send(protocol.failed(session.session_id, "save"))
            return "Blender couldn't save the file. Check there's space on the disk and try again."
        finally:
            state.saving = False
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
    state.problem = None
    if not send(message):
        return (
            "Saved, but Nodelizer isn't connected. It picks up the model next time you open"
            " this project."
        )
    return None


def _quit():
    """Closes Blender once Send Back is done; the file is saved, so Blender doesn't ask."""
    for window in bpy.context.window_manager.windows:
        with bpy.context.temp_override(window=window):
            bpy.ops.wm.quit_blender()
        break
    return None


class NODELIZER_OT_send_back(bpy.types.Operator):
    bl_idname = "nodelizer.send_back"
    bl_label = "Send Back to Nodelizer"
    bl_description = "Save, put the model on its node in Nodelizer and close Blender"

    @classmethod
    def poll(cls, context):
        return state.session is not None

    def execute(self, context):
        problem = deliver("return")
        if problem:
            self.report({"WARNING"}, problem)
            return {"CANCELLED"}
        self.report({"INFO"}, "Sent back to Nodelizer")
        # A moment for the message to leave before Blender closes.
        bpy.app.timers.register(_quit, first_interval=0.4)
        return {"FINISHED"}


class NODELIZER_OT_open_app(bpy.types.Operator):
    bl_idname = "nodelizer.open_app"
    bl_label = "Open Nodelizer"
    bl_description = "Bring the Nodelizer window to the front and keep working here"

    @classmethod
    def poll(cls, context):
        return state.client is not None and state.client.connected

    def execute(self, context):
        if not send(protocol.open_app(state.session.session_id)):
            self.report({"WARNING"}, "Nodelizer isn't connected.")
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (NODELIZER_OT_send_back, NODELIZER_OT_open_app)
