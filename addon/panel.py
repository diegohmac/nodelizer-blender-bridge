# SPDX-License-Identifier: GPL-3.0-or-later
"""The Nodelizer tab in the 3D view's sidebar."""

from __future__ import annotations

import bpy

from .runtime import is_session_file, state


class NODELIZER_PT_bridge(bpy.types.Panel):
    bl_label = "Nodelizer Bridge"
    bl_idname = "NODELIZER_PT_bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nodelizer"

    def draw(self, context):
        layout = self.layout
        session = state.session
        if session is None:
            layout.label(text=state.problem or "Open a model from Nodelizer to edit it here.")
            return
        client = state.client
        connected = client is not None and client.connected
        column = layout.column(align=True)
        column.label(text=f"Connected: {'Yes' if connected else 'No'}")
        column.label(text=f"Project: {session.project_name}")
        column.label(text=f"Asset: {session.asset_name}")
        column.label(text=f"Format: {session.format.upper()}")
        if state.last_sent:
            column.label(text=f"Last sent at {state.last_sent}")
        if state.problem:
            layout.label(text=state.problem, icon="ERROR")
        if not is_session_file():
            layout.label(text="This isn't the session's file.", icon="INFO")
        layout.separator()
        send = layout.row()
        send.scale_y = 1.4
        send.enabled = is_session_file()
        send.operator("nodelizer.send_back", icon="EXPORT")
        row = layout.row(align=True)
        row.enabled = is_session_file()
        row.operator("nodelizer.sync", icon="FILE_REFRESH")
        layout.operator("nodelizer.open_app", icon="WINDOW")


classes = (NODELIZER_PT_bridge,)
