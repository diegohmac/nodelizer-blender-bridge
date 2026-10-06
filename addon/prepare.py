# SPDX-License-Identifier: GPL-3.0-or-later
"""`blender --command nodelizer_prepare <session folder>`: makes the session's first .blend.

The host runs this once, in the background, before opening Blender for a model that has no .blend
yet. It imports the model into an empty scene and saves it where session.json says. It never
writes over an existing .blend.

Exit codes: 0 done, 2 bad session, 3 the .blend already exists, 4 import failed, 5 save failed.
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
    if os.path.exists(session.blend):
        print("nodelizer_prepare: the .blend already exists", file=sys.stderr)
        return 3
    if not os.path.isfile(session.source_path):
        print("nodelizer_prepare: the source model is missing", file=sys.stderr)
        return 4
    try:
        export.import_model(session.source_path, session.asset_name)
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
