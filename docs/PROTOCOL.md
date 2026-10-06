# Bridge protocol, version 2

How the add-on and the host app (Nodelizer) talk. Large data never goes over the socket: models
travel as files in the session folder, and the socket only says when to look.

## Session folder

The host creates a folder named `session-<uuid>` and starts Blender with:

| Variable | Value |
| --- | --- |
| `NODELIZER_BRIDGE_SESSION` | Absolute path of the session folder |
| `NODELIZER_BRIDGE_PORT` | TCP port the host listens on, on `127.0.0.1` |
| `NODELIZER_BRIDGE_TOKEN` | Random token for this session, 32 to 128 URL-safe characters |

The add-on removes the token from its environment once read. It's never written to a file.

### session.json (written by the host)

```json
{
  "schema": 2,
  "sessionId": "<uuid, same as the folder name>",
  "projectName": "Knight",
  "nodeName": "Hero scene",
  "format": "glb",
  "blend": "/absolute/path/to/project/blender/hero-scene.blend",
  "imports": [
    { "input": "<node id>", "name": "Hero", "file": "input-1.glb" },
    { "input": "<node id>", "name": "Sword", "file": "input-2.fbx" }
  ],
  "frame": true
}
```

- `format` is `glb` or `fbx`: what the bridge exports back.
- `blend` must be absolute, end in `.blend` and sit in a folder named `blender`.
- `imports` are the models to bring in this time, at most 16. `input` is the host's id for the
  model (letters, digits, `-` and `_`), `name` names its collection, and `file` is
  `input-<n>.<glb|fbx|obj>` inside the session folder. Each model's collection carries a
  `nodelizer_input` property with its id, so a later session can replace just that one. An empty
  list opens the scene as it is.
- `frame` is true when the `.blend` was just made, so the view fits its models once.

The host may add fields the bridge ignores, such as `projectId`, `nodeId`, `status` and
timestamps. It never puts credentials in this file.

### Files the bridge writes

| File | When |
| --- | --- |
| `result.glb` or `result.fbx` | After each export. Written to a `.partial` name and moved into place. |
| `result.json` | After each export, atomically. |
| `blender.lock` | Every 15 seconds while Blender has the session open: `{"pid": 123, "at": <unix seconds>}`. Removed on exit. |

`result.json`:

```json
{
  "schema": 1,
  "sessionId": "<uuid>",
  "result": "result.glb",
  "format": "glb",
  "sha256": "<hex>",
  "kind": "return",
  "exportedAt": "2026-10-06T12:00:00Z"
}
```

`kind` is `return` for Send Back and `sync` for a save in Blender or a host `EXPORT`. The host checks the file
against `sha256` before using it, so a half-written result is never taken.

## Socket

One JSON object per line (`\n`), UTF-8, at most 64 KiB per line. The bridge connects to
`127.0.0.1:<port>` and speaks first.

### Bridge to host

| Message | Meaning |
| --- | --- |
| `{"type":"HELLO","protocol":2,"sessionId","token","bridgeVersion","blenderVersion"}` | First line on every connection. |
| `{"type":"SESSION_READY","sessionId"}` | The session's `.blend` is open. |
| `{"type":"ASSET_CHANGED","sessionId","result":"result.glb"}` | Saved in Blender: a new result is in the folder. |
| `{"type":"RETURN_TO_APP","sessionId","result":"result.glb"}` | Send Back: a new result, bring the host forward; Blender closes next. |
| `{"type":"OPEN_APP","sessionId"}` | Bring the host forward. |
| `{"type":"FAILED","sessionId","stage":"import" \| "export" \| "save"}` | Something the person asked for failed. No details or paths. |

`result` is only ever `result.glb` or `result.fbx`.

### Host to bridge

| Message | Meaning |
| --- | --- |
| `{"type":"WELCOME","protocol":2}` | The hello was accepted. |
| `{"type":"REJECTED","reason":"protocol"}` | The bridge speaks another protocol version and needs updating. |
| `{"type":"EXPORT","format":"GLB" \| "FBX"}` | Export now, as after a save. Ignored unless it's the session's format. |
| `{"type":"PING"}` | Nothing; checks the connection. |

A wrong token gets no answer: the host just closes the connection. Any other message, or any
other field, is dropped by both sides. There is no message that runs code.

## The prepare command

```sh
blender --background --addons bl_ext.user_default.nodelizer_bridge \
  --command nodelizer_prepare <session folder>
```

Opens `blend`, or starts an empty scene when there's none, then brings in each model under
`imports`: the collection tagged with its id is removed with its objects, and the model is
imported into a new collection named `name` and tagged. Then it drops data nothing uses any more,
packs textures into the file and saves `blend`. glTF files are imported with bones pointing at
their children and without bone shapes. The host keeps a copy of an existing `.blend` before
asking for an update.

| Exit code | Meaning |
| --- | --- |
| 0 | Done |
| 2 | The session folder or session.json is wrong |
| 4 | A model couldn't be imported |
| 5 | The `.blend` couldn't be saved |

## Versions

Version 2 (bridge 2.0.0) made a session a scene of several models (`imports`, `nodeName`) instead
of one (`source`, `assetName`), and saving in Blender now sends results.

`protocol` changes only when an older host or bridge couldn't work with the new one. The host
reads its release list from a URL that includes the protocol version, so each host version only
ever installs bridges it can talk to.
