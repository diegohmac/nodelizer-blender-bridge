# Nodelizer Bridge

A Blender add-on that lets [Nodelizer](https://nodelizer.com) open a 3D model in Blender and take
the edited model back. You edit in Blender as you normally would, with every tool Blender has,
then press **Send Back to Nodelizer**.

It's free software under the GNU General Public License, version 3 or later. See
[LICENSE](LICENSE).

## How it works

Nodelizer installs its own copy of Blender and this add-on. It doesn't touch any Blender you
already have. When you choose **Edit in Blender** on a 3D model:

1. Nodelizer writes a session folder with `session.json` and a copy of the model.
2. If the model has no `.blend` yet, Nodelizer runs
   `blender --background --command nodelizer_prepare <session folder>`, which imports the model
   into an empty scene and saves the `.blend`.
3. Nodelizer opens Blender on that `.blend`. The add-on finds the session from the
   `NODELIZER_BRIDGE_SESSION` environment variable and connects to Nodelizer on `127.0.0.1`.
4. In the 3D view's sidebar, the **Nodelizer** tab shows the session and three buttons:
   - **Send Back to Nodelizer** saves the `.blend`, exports the model (GLB, or FBX for quad
     meshes), writes `result.json` and tells Nodelizer, which updates the model's node.
   - **Sync** does the same without switching to Nodelizer.
   - **Open Nodelizer** brings Nodelizer to the front.

The `.blend` stays with the Nodelizer project, so editing the model in Blender again later picks up
where you left off, with modifiers, rigs, materials and everything else Blender keeps.

## What it will and won't do

- It only talks to `127.0.0.1`, and only after presenting the session's token, which Nodelizer
  passes in the environment and never writes to disk.
- It understands a fixed list of messages (see [docs/PROTOCOL.md](docs/PROTOCOL.md)). It never
  runs code it receives.
- It reads and writes only inside the session folder, plus the one `.blend` file `session.json`
  names, after checking every path.
- It holds no accounts, keys or Nodelizer business logic. It's a small, generic file and socket
  bridge.

## Layout

```text
addon/
  __init__.py            registers everything
  blender_manifest.toml  the extension manifest (Blender 4.2 and later)
  operators.py           Send Back, Sync, Open Nodelizer
  panel.py               the sidebar panel
  session.py             session.json, result.json and the heartbeat file
  ipc.py                 the socket to Nodelizer
  protocol.py            the messages, and the check that drops anything else
  export.py              import into a new .blend, export GLB and FBX
  prepare.py             the nodelizer_prepare command
  runtime.py             the state of the bridge in this Blender
scripts/build.py         builds the extension zip
tests/                   tests that run without Blender
```

## Developing

Tests run on any Python 3.9 or later, without Blender:

```sh
python3 -m unittest discover -s tests -v
```

Build the zip Blender installs. The same sources always give the same bytes:

```sh
python3 scripts/build.py   # dist/nodelizer_bridge-<version>.zip and .sha256
```

To try it inside Nodelizer without publishing anything, start a development build of Nodelizer
with `NODELIZER_BLENDER_BRIDGE_SOURCE` set to this repository's `addon` folder. Nodelizer links it
into its own Blender instead of installing a release, so edits show the next time Blender opens.

You can also install the zip into any Blender 4.2 or later from Edit, Preferences, Get
Extensions, Install from Disk, but without Nodelizer starting a session the panel only says to
open a model from Nodelizer.

## Releasing

1. Bump `version` in `addon/blender_manifest.toml`. Change `PROTOCOL` in `addon/protocol.py` only
   for a change older Nodelizer versions can't understand.
2. Tag the commit `v<version>` and push the tag. The release workflow runs the tests, builds the
   zip and attaches it with its `.sha256` to a GitHub release.
3. Nodelizer's maintainers mirror the zip and list it in the signed release manifest Nodelizer
   reads. Nodelizer installs it on the next edit or update.

## License

Copyright (C) 2026 Diego Machado

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY. See
[LICENSE](LICENSE) for details.

Blender is a trademark of the Blender Foundation. This add-on isn't made or endorsed by the
Blender Foundation.
