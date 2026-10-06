# SPDX-License-Identifier: GPL-3.0-or-later
"""`blender --command nodelizer_prepare <session folder>`: makes or updates the session's .blend.

The host runs this in the background before opening Blender. Without a .blend it starts an empty
scene; with one it opens it. Then it brings in each model session.json lists under `imports`,
each into its own collection, replacing the collection that model had before, and saves. The
host keeps a copy of an existing .blend before asking for an update.

Exit codes: 0 done, 2 bad session, 4 import failed, 5 save failed.
"""

from __future__ import annotations

import os
import sys

import bpy

from . import export
from . import session as sessions

COMMAND = "nodelizer_prepare"


def execute(argv) -> int:
    if len(argv) != 1:
        print("usage: blender --command nodelizer_prepare <session folder>", file=sys.stderr)
        return 2
    try:
        session = sessions.load(argv[0])
    except sessions.SessionError as error:
        print(f"nodelizer_prepare: {error}", file=sys.stderr)
        return 2
    existing = os.path.isfile(session.blend)
    try:
        if existing:
            bpy.ops.wm.open_mainfile(filepath=session.blend, load_ui=False)
        else:
            export.clear_default_scene()
        for item in session.imports:
            path = session.import_path(item)
            if not os.path.isfile(path):
                raise RuntimeError(f"{item.file} is missing")
            export.replace_input(path, item.input, item.name)
        export.finish_imports()
    except Exception as error:  # Importers raise all kinds of errors on broken files.
        print(f"nodelizer_prepare: import failed: {error}", file=sys.stderr)
        return 4
    try:
        os.makedirs(os.path.dirname(session.blend), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=session.blend, check_existing=False)
    except (RuntimeError, OSError) as error:
        print(f"nodelizer_prepare: save failed: {error}", file=sys.stderr)
        return 5
    return 0
