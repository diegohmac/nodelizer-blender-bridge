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
  `input-<n>.<glb|fbx|obj>` inside the session folder. An OBJ that comes with its .mtl and
  textures is unpacked into a folder instead, and `file` names its OBJ, like `input-1/model.obj`,
  at most three folders further down (bridge 2.1.0 and later). Each model's collection carries a
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

## The convert command

```sh
blender --background --addons bl_ext.user_default.nodelizer_bridge \
  --command nodelizer_convert <job folder>
```

Saves one model in another format, with no window and no socket. The host makes a folder named
`convert-<uuid>` holding the model and `job.json`:

```json
{ "schema": 1, "input": "input.glb", "format": "obj" }
```

- `input` is `input.<glb|fbx|obj|stl|usdz>`, or the OBJ of a bundle unpacked into `input/`, like
  `input/model.obj`, at most three folders further down.
- `format` is `glb`, `fbx`, `obj`, `stl` or `usdz`.

The model comes into an empty scene and goes out as `result.<format>`. An OBJ goes out with its
.mtl and its textures, packed or not, zipped together into `result.zip`. Nothing is read or
written outside the job folder. Added in bridge 2.1.0; the protocol didn't change.

| Exit code | Meaning |
| --- | --- |
| 0 | Done |
| 2 | The job folder or job.json is wrong |
| 4 | The model couldn't be imported |
| 6 | The model couldn't be exported |

## The worker

```sh
blender --background --addons bl_ext.user_default.nodelizer_bridge --command nodelizer_worker
```

A Blender that stays open with no window, holding one scene at a time for the host's own 3D
editor (Nodelizer's Model Edit node). It talks over its own stdin and stdout, so there's no port
or token: only the process that started it can reach it. It stops when its stdin closes. Added in
bridge 2.2.0; the socket protocol didn't change.

### Lines

The host writes one JSON object per line on stdin, at most 4 MiB. The worker answers on stdout,
one JSON object per line starting with `@nlz ` (and preceded by a newline), since Blender prints
its own lines there too. Anything else on stdout is Blender's.

Its first line is `{"type":"ready","protocol":1,"bridgeVersion","blenderVersion"}`. Then:

| Host | Worker |
| --- | --- |
| `{"id","type":"run","steps":[...]}` | `{"id","ok":true,"results":[...],"key","seconds"}` or `{"id","ok":false,"step","code","message","key","index"?}` |
| `{"id","type":"ping"}` | `{"id","ok":true,"key"}` |
| `{"id","type":"quit"}` | `{"id","ok":true}`, then it exits |

`id` is 1 to 64 letters, digits, `-` and `_`. While a long step runs the worker may send
`{"type":"progress","id","stage":"import"|"load"}`. `key` is the host's name for the scene the
worker holds after the request, or null when it holds none.

### Steps

A `run` request is 1 to 16 steps, run in order until one fails. Every path must be absolute,
plain (no `..`) and inside a folder named `blender`.

| Step | Fields | What it does |
| --- | --- | --- |
| `load` | `key`, `blend` (a .blend, or null for an empty scene), `replay` (lists of edits) | Opens the .blend and applies each list of edits in turn. |
| `import` | `key`, `source` (`.glb`, `.fbx`, `.obj`, `.stl` or `.usdz`) | Brings a model into an empty scene, packs its textures and gives every object a new id. |
| `apply` | `from`, `to`, `ops` | Applies edits to the scene held as `from`; refused unless that's the one held. |
| `save` | `blend` | Saves a copy of the scene there, the file written under another name first, with no `.blend1` backup. |
| `snapshot` | `folder`, `name` (`r<n>`), `appearance` (`"export"`, a SHA-256 to keep, or null) | Writes `<name>.json`, the mesh files it names that aren't there yet, and with `"export"`, the appearance GLB. |
| `export` | `path`, `format` (`glb` or `fbx`) | Exports the scene as the bridge's own exports do. |

When a step fails after the scene changed, the worker drops the scene (`key` null), so a half made
edit is never saved by a later request. Edits refused before anything changed (`not_loaded`,
`op_invalid`, `op_unsupported`) leave it as it was.

`apply` answers `{"key", "changed", "reports"}`: the ids of the objects the edits changed, and for
each edit, null or, for a change of topology, what it did (see below).

| Code | Meaning |
| --- | --- |
| `bad_request` | The line or a step didn't check out. |
| `not_loaded` | `apply` named a scene the worker doesn't hold. |
| `op_invalid` | An edit is malformed or names an object the scene doesn't have; `index` says which. |
| `op_unsupported` | A change of topology on a mesh with a modifier that keeps data per vertex; `message` is the modifier's name as Blender shows it. |
| `op_failed` | A change of topology that checked out couldn't be made, like a knife cut crossing itself inside a face. The scene is dropped. |
| `load_failed`, `replay_failed` | The .blend couldn't be opened, or its edits replayed. |
| `import_failed` | The model couldn't be brought in. |
| `save_failed`, `snapshot_failed`, `export_failed` | Writing failed. |
| `internal` | Anything else. |

### Edits

There's no edit that runs code. Two move things:

```json
{"op": "set_transform", "object": "1b8208f6608e", "location": [0, 0, 1], "rotation": [1, 0, 0, 0], "scale": [1, 1, 1]}
```

`object` is the object's id. `location`, `rotation` (a quaternion, w first, normalized by the
worker) and `scale` are its own values, relative to its parent and without its delta transform;
each is optional, but one is needed. The rotation is written in the object's own rotation mode,
taking the Euler angles nearest the current ones. Numbers must be finite and within 10⁷.

```json
{"op": "set_vertices", "object": "2a520b2b8f0b", "count": 2, "data": "<sha256>", "file": "/…/blender/.edits/<scene>/ops/<sha256>.bin"}
```

Moves some of a mesh object's vertices to new places in its own space, in the mesh's rest state.
`file` is in the mesh file's layout (below) with header `{"kind": "vertices", "counts":
{"vertices": count}}` and two arrays: `indices` (u32, strictly increasing) and `positions` (f32, 3
per vertex). `data` is the file's SHA-256, which the worker checks, as it checks that every index is
a vertex of the mesh and every number is finite and within 10⁷. With shape keys, Blender evaluates
the basis key rather than the vertices, so the basis moves too and keys relative to it move by the
same amount, as leaving edit mode does in Blender. Every object using the mesh changes with it, and
`changed` lists them all. UVs, materials, vertex groups, seams and sharp edges stay as they were; a
moved mesh gets a new mesh file in the next snapshot.

The rest change a mesh's topology: which vertices, edges and faces it has. They work on the mesh
in its rest state with BMesh, as edit mode does, and write it back only once the edit worked. Each
has to be the last edit of its mesh in a list (anything after it would name components of a mesh
it can't know yet), and none is made on a mesh whose objects have a Multires, Hook, Surface
Deform, Mesh Deform or Laplacian Deform modifier, or a bound Corrective Smooth (`op_unsupported`).
Edits over a set of components name a file in the mesh file's layout with header `{"kind":
"components", "component": "vertices" | "edges" | "faces", "counts": {"items": count}}` and one
array, `indices` (u32, strictly increasing), checked like a vertex file: `count`, `data` (its
SHA-256) and `file`.

```json
{"op": "loop_cut", "object": "…", "edges": [12, 9, 4, 1], "cuts": 2, "factor": 0.25}
```

`edges` is a ring, in order: each two are opposite sides of the quad between them, and when the
last and the first are too, through a quad the ring hasn't used, it closes. The first edge runs from
its first vertex, each next one from the corner next to the previous one's start across their quad.
Cut k of `cuts` (1 to 64) sits at `(k + factor) / (cuts + 1)` along each edge, `factor` strictly
between -1 and 1; the cuts are joined across each quad. A ring that closes the other way round, like
a Möbius strip, joins its last cuts to the first in reverse.

```json
{"op": "knife", "object": "…", "points": [{"vertex": 3}, {"face": 7, "at": [0.1, 0.2, 0]}, {"edge": 9, "factor": 0.5}], "faces": [7, 7]}
```

A cut along `points`: a vertex, a place along an edge (from its first vertex, strictly inside), or a
place inside a face in the object's own space. `faces[i]` is the face the part from point i to i+1
crosses, which has to hold both. Parts from a vertex or edge to a vertex or edge, through any face
points between them, split their face; parts ending inside a face cut nothing.

```json
{"op": "subdivide", "object": "…", "count": 4, "data": "<sha256>", "file": "…", "cuts": 1}
{"op": "merge", "object": "…", "count": 3, "data": "<sha256>", "file": "…", "at": "vertex", "vertex": 7}
{"op": "merge_by_distance", "object": "…", "count": 8, "data": "<sha256>", "file": "…", "distance": 0.0001}
{"op": "delete", "object": "…", "component": "faces", "count": 2, "data": "<sha256>", "file": "…"}
{"op": "dissolve", "object": "…", "component": "edges", "count": 2, "data": "<sha256>", "file": "…"}
```

- `subdivide` splits the edges into `cuts + 1` equal parts and fills between them as Blender's
  Subdivide does (grid fill, straight cuts).
- `merge` welds the vertices into one: at their `center`, each connected group at its own center
  (`collapse`), or onto `vertex`, one of them. The vertex that stays is the lowest numbered, or
  `vertex`.
- `merge_by_distance` goes through the vertices in order: each one not merged yet takes every other
  one not merged yet within `distance` of it (in the object's space, at most that far) and stays
  where it is. Only vertices of the set merge.
- `delete` removes the components and what's built on them; `dissolve` removes them and joins what
  was around them (Blender's defaults: vertices without splitting faces, edges taking their
  vertices along, faces keeping theirs).

New data follows BMesh's interpolation, as in edit mode: a vertex made on an edge takes UVs, vertex
group weights and shape key places in proportion along it, and the halves of a split edge keep its
seam and sharp marks; a vertex made inside a face takes them from the face's corners; new edges
across faces are neither seams nor sharp. A merged vertex keeps its own weights, every face corner
keeps its UVs, and at a center it sits at the middle of the merged vertices in every shape key too;
an edge merged onto another is a seam, or sharp, when either was. Loop cuts, knife cuts and
subdivisions keep the numbers of what was there and add new components after; the others number
what's left again, in order.

Each change of topology reports:

```json
{"object": "…", "counts": {"vertices": 46, "edges": 85, "faces": 40}, "added": {"vertices": 10, "edges": 25, "faces": 15},
 "select": {"component": "edges", "indices": [60, 61]}, "merged": 2, "merged_into": [[1, 0], [7, 0]]}
```

`select` is what the editor selects afterwards: a loop cut's new loops, a knife cut's new edges,
subdivided edges with what's between them, merged vertices, a dissolve's joined faces, nothing after
a delete. It's left out past 2,000,000. Merges say how many vertices went and, for the first 1,000,
which vertex each became.

Every edit in a list is checked against the scene before any is made.

### Objects and snapshots

Each object carries a custom property `nodelizer_id`, 12 hex characters, which outlives renames.
An imported scene gets random ids. A copied object carries its original's id; the later of the
two by name gets one made from its own name, the same way on every computer.

`<name>.json` describes the scene in Blender's coordinates (metres, Z up, matrices column by
column):

```json
{
  "schema": 1,
  "units": {"system": "METRIC", "scaleLength": 1.0, "length": "METERS"},
  "up": "Z",
  "objects": [{
    "id": "2a520b2b8f0b", "name": "Body", "type": "MESH",
    "parent": "1b8208f6608e", "parentType": "OBJECT", "parentBone": null,
    "location": [0, 0, 0], "rotationMode": "QUATERNION", "rotation": [1, 0, 0, 0], "scale": [1, 1, 1],
    "matrixBasis": [16 numbers], "matrixParentInverse": [16 numbers], "matrixWorld": [16 numbers],
    "dimensions": [0.4, 0.4, 1.7], "visible": true, "modifiers": [{"name": "Armature", "type": "ARMATURE"}],
    "mesh": "<sha256>", "materials": ["Skin"], "vertexGroups": ["Hips", "Spine", "Head"]
  }],
  "meshes": {"<sha256>": {"vertices": 12, "edges": 30, "faces": 20, "loops": 60, "triangles": 20,
             "uvLayers": [], "shapeKeys": [], "bounds": {"min": [...], "max": [...]}, "bytes": 3216}},
  "appearance": "<sha256>"
}
```

`rotation` is always a quaternion; `euler` (radians) is added for Euler modes and `delta` when the
delta transform isn't nothing. Armatures list their `bones` with heads and tails in armature space.
`appearance/<sha256>.glb` holds every material the scene's objects use, one triangle each, so
materials and textures travel without the geometry.

### The mesh file

`mesh/<sha256>.bin`, named after its own SHA-256:

```text
"NLZM"            4 bytes
version           uint32 little-endian, 1
header length     uint32 little-endian
header            UTF-8 JSON: {"counts", "uvLayers", "arrays": [{"name", "type", "offset", "length"}]}
padding           zeros to a multiple of 16 bytes
data              the arrays, each starting on a multiple of 16 bytes
```

`offset` is in bytes from the start of the data, `length` in elements; types are `f32`, `u32`,
`u16` and `u8`, little-endian. The arrays:

| Name | Type | Length | What |
| --- | --- | --- | --- |
| `positions` | f32 | 3 × vertices | Each vertex, in the object's own space |
| `edges` | u32 | 2 × edges | Each edge's two vertices |
| `faceLoopStart` | u32 | faces + 1 | Where each face's corners start; the last is the corner count |
| `faceMaterial` | u16 | faces | Each face's material slot |
| `loopVertex`, `loopEdge` | u32 | loops | Each corner's vertex and the edge after it |
| `loopNormal` | f32 | 3 × loops | Each corner's shading normal |
| `uv0`, `uv1`, ... | f32 | 2 × loops | Each UV layer, per corner, named in `uvLayers` |
| `triangleLoops` | u32 | 3 × triangles | The corners of the triangles Blender draws |
| `triangleFace` | u32 | triangles | The face each triangle belongs to |
| `edgeSeam`, `edgeSharp` | u8 | edges | 1 for a UV seam, 1 for a sharp edge |
| `groupOffset` | u32 | vertices + 1 | Where each vertex's weights start |
| `groupIndex`, `groupWeight` | u16, f32 | weights | Each weight's vertex group (by the object's list) and value |

## Versions

Bridge 2.4.0 added changes of topology: `loop_cut`, `knife`, `subdivide`, `merge`,
`merge_by_distance`, `delete` and `dissolve`, `reports` in `apply`'s answer, and the codes
`op_unsupported` and `op_failed`. The host asks for 2.4.0 or later for Model Edit.

Bridge 2.3.1 turns off Blender's save versions in the worker, so `save` over an existing file
leaves no `.blend1` beside it. Only the worker's own process changes; preferences aren't saved.

Bridge 2.3.0 added the `set_vertices` edit. The host asks for 2.3.0 or later for Model Edit; the
worker protocol number didn't change.

Bridge 2.2.0 added the worker. The host asks for 2.2.0 or later only for Model Edit, so older
bridges keep working for everything else.

Bridge 2.1.0 added the convert command and OBJ files that bring their .mtl and textures. The
host asks for 2.1.0 or later only for those, so older bridges keep working for everything else.

Version 2 (bridge 2.0.0) made a session a scene of several models (`imports`, `nodeName`) instead
of one (`source`, `assetName`), and saving in Blender now sends results.

`protocol` changes only when an older host or bridge couldn't work with the new one. The host
reads its release list from a URL that includes the protocol version, so each host version only
ever installs bridges it can talk to.
