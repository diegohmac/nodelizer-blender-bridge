# Bridge protocol, version 1

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
  "schema": 1,
  "sessionId": "<uuid, same as the folder name>",
  "projectName": "Knight",
  "assetName": "Hero",
  "format": "glb",
  "source": "source.glb",
  "blend": "/absolute/path/to/project/blender/<node id>.blend",
  "frame": true
}
```

- `format` is `glb` or `fbx`: what the bridge exports back.
- `source` is `source.glb`, `source.fbx` or `source.obj`, inside the session folder.
- `blend` must be absolute, end in `.blend` and sit in a folder named `blender`.
- `frame` is true when the `.blend` was just made, so the view fits the model once.

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

`kind` is `return` for Send Back and `sync` for Sync or a host `EXPORT`. The host checks the file
against `sha256` before using it, so a half-written result is never taken.

## Socket

One JSON object per line (`\n`), UTF-8, at most 64 KiB per line. The bridge connects to
`127.0.0.1:<port>` and speaks first.

### Bridge to host

| Message | Meaning |
| --- | --- |
| `{"type":"HELLO","protocol":1,"sessionId","token","bridgeVersion","blenderVersion"}` | First line on every connection. |
| `{"type":"SESSION_READY","sessionId"}` | The session's `.blend` is open. |
| `{"type":"ASSET_CHANGED","sessionId","result":"result.glb"}` | Sync: a new result is in the folder. |
| `{"type":"RETURN_TO_APP","sessionId","result":"result.glb"}` | Send Back: a new result, and bring the host forward. |
| `{"type":"OPEN_APP","sessionId"}` | Bring the host forward. |
| `{"type":"FAILED","sessionId","stage":"import" \| "export" \| "save"}` | Something the person asked for failed. No details or paths. |

`result` is only ever `result.glb` or `result.fbx`.

### Host to bridge

| Message | Meaning |
| --- | --- |
| `{"type":"WELCOME","protocol":1}` | The hello was accepted. |
| `{"type":"REJECTED","reason":"protocol"}` | The bridge speaks another protocol version and needs updating. |
| `{"type":"EXPORT","format":"GLB" \| "FBX"}` | Export now, as a Sync. Ignored unless it's the session's format. |
| `{"type":"PING"}` | Nothing; checks the connection. |

A wrong token gets no answer: the host just closes the connection. Any other message, or any
other field, is dropped by both sides. There is no message that runs code.

## The prepare command

```sh
blender --background --addons bl_ext.user_default.nodelizer_bridge \
  --command nodelizer_prepare <session folder>
```

Imports `source.*` into an empty scene, in a collection named after `assetName`, packs its
textures into the file and saves it as `blend`. It never writes over an existing `.blend`.

| Exit code | Meaning |
| --- | --- |
| 0 | Done |
| 2 | The session folder or session.json is wrong |
| 3 | The `.blend` already exists; nothing was changed |
| 4 | The model couldn't be imported |
| 5 | The `.blend` couldn't be saved |

## Versions

`protocol` changes only when an older host or bridge couldn't work with the new one. The host
reads its release list from a URL that includes the protocol version, so each host version only
ever installs bridges it can talk to.
