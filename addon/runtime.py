# SPDX-License-Identifier: GPL-3.0-or-later
"""The bridge's state in this Blender process: the session, the connection and what was sent."""

from __future__ import annotations

import os
import re

import bpy

from . import ipc, protocol
from . import session as sessions

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.2
# Blender's sidebar only knows a tab once it has drawn it, so the first polls keep asking.
TAB_TRIES = 25


class State:
    session: sessions.Session | None = None
    client: ipc.Client | None = None
    # A line for the panel when something about the session isn't right.
    problem: str | None = None
    last_sent: str | None = None
    ready_sent = False
    tab_tries = TAB_TRIES


state = State()


def bridge_version() -> str:
    """This extension's version, from the manifest it was installed with."""
    manifest = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
    try:
        with open(manifest, encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r'^version\s*=\s*"([0-9][0-9A-Za-z.+-]{0,31})"\s*$', line)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return "0.0.0"


def is_session_file() -> bool:
    return state.session is not None and sessions.same_file(bpy.data.filepath, state.session.blend)


def start():
    """Picks up the session the host launched Blender for, if any. Runs once after startup."""
    folder = os.environ.get(sessions.SESSION_ENV)
    if not folder:
        return None
    try:
        state.session = sessions.load(folder)
    except sessions.SessionError:
        state.problem = "This Blender was opened with a session that can't be read."
        return None
    port = os.environ.get(sessions.PORT_ENV, "")
    token = os.environ.get(sessions.TOKEN_ENV, "")
    # The token is only for this connection; nothing else in Blender needs to see it.
    os.environ.pop(sessions.TOKEN_ENV, None)
    try:
        hello = protocol.hello(
            state.session.session_id,
            token,
            bridge_version(),
            bpy.app.version_string,
        )
        state.client = ipc.Client(int(port), token, hello)
        state.client.start()
    except ValueError:
        state.problem = "Nodelizer didn't pass a connection, so results are saved to files only."
    sessions.heartbeat(state.session)
    bpy.app.timers.register(_poll, first_interval=POLL_SECONDS, persistent=True)
    bpy.app.timers.register(_beat, first_interval=HEARTBEAT_SECONDS, persistent=True)
    _show_sidebar()
    if state.session.frame and is_session_file():
        _frame_model()
    return None


def stop():
    for timer in (_poll, _beat):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    if state.client is not None:
        state.client.stop()
    if state.session is not None:
        sessions.release(state.session)
    state.client = None
    state.session = None


def send(message: dict) -> bool:
    return state.client is not None and state.client.send(message)


def _beat():
    if state.session is not None:
        sessions.heartbeat(state.session)
    return HEARTBEAT_SECONDS


def _poll():
    if state.tab_tries > 0:
        state.tab_tries -= 1
        _show_tab()
    client = state.client
    if client is None:
        return POLL_SECONDS if state.tab_tries > 0 else None
    if client.connected and not state.ready_sent and is_session_file():
        state.ready_sent = send(protocol.session_ready(state.session.session_id))
    while True:
        try:
            message = client.incoming.get_nowait()
        except Exception:
            break
        _handle(message)
    _redraw()
    return POLL_SECONDS


def _handle(message: dict):
    kind = message["type"]
    if kind == "REJECTED":
        state.problem = (
            "This bridge doesn't match your Nodelizer version. Update Blender from Nodelizer's Settings."
            if message.get("reason") == "protocol"
            else "Nodelizer didn't accept this session. Open the model from Nodelizer again."
        )
    elif kind == "EXPORT":
        fmt = message["format"].lower()
        if fmt == state.session.format:
            from .operators import deliver

            deliver("sync")


_last_state = None


def _redraw():
    """Repaints the panel when the connection changes, without redrawing every poll."""
    global _last_state
    current = (state.client.state if state.client else None, state.problem, state.last_sent)
    if current == _last_state:
        return
    _last_state = current
    for window in getattr(bpy.context.window_manager, "windows", []):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _views():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                yield window, area


def _show_sidebar():
    """Opens the 3D view's sidebar, so the bridge panel is in sight."""
    try:
        for _window, area in _views():
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.show_region_ui = True
            area.tag_redraw()
    except Exception:
        pass


def _show_tab():
    """Turns the sidebar to the Nodelizer tab, once the sidebar has drawn it."""
    try:
        for _window, area in _views():
            for region in area.regions:
                if region.type == "UI" and region.active_panel_category != "Nodelizer":
                    region.active_panel_category = "Nodelizer"
                    area.tag_redraw()
                elif region.type == "UI":
                    state.tab_tries = 0
    except Exception:
        pass


def _frame_model():
    """Fits a just-made file's model in view. A file opened again keeps its own view."""
    try:
        collection = bpy.data.collections.get(state.session.asset_name[:63])
        objects = list(collection.all_objects) if collection else []
        if not objects:
            return
        for obj in bpy.context.view_layer.objects:
            obj.select_set(obj in objects)
        bpy.context.view_layer.objects.active = objects[0]
        for window, area in _views():
            region = next(r for r in area.regions if r.type == "WINDOW")
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.view3d.view_selected()
    except Exception:
        pass
