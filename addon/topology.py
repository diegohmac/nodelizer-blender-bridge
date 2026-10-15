# SPDX-License-Identifier: GPL-3.0-or-later
"""Edits that change a mesh's vertices, edges and faces rather than only where its vertices are:
loop cuts, subdividing, knife cuts, merging, deleting and dissolving.

Each is checked against the mesh before anything changes (`check`), then made with BMesh on a copy
of the mesh's rest state, written back only once it worked (`apply`). The answer says what the
editor should select afterwards, so a selection never points at components that moved on.

New data follows BMesh's own interpolation, which edit mode uses too:

- A vertex made on an edge takes its UVs, vertex group weights and shape key places in proportion
  along the edge, and the two halves of a split edge keep its seam and sharp marks.
- A vertex made inside a face (a knife cut) takes them from the face's corners.
- Edges made across a face are neither seams nor sharp.
- Merging keeps the vertex that stays, with its weights; every face corner keeps its UVs. Merged at
  the center or collapsed, it sits at the middle of the merged vertices in every shape key too. An
  edge merged onto another is a seam, or sharp, when either of them was.

Added, cut and subdivided meshes keep the indices of what they had; new components come after.
Merging, deleting and dissolving number what's left again, in the same order.
"""

from __future__ import annotations

import bmesh
import numpy as np
from mathutils import Vector, kdtree

from . import meshfile
from .worker_protocol import RequestError

# Modifiers that keep data for each vertex by its index, which a change of topology breaks.
BOUND_MODIFIERS = {
    "MULTIRES": "Multires",
    "HOOK": "Hook",
    "SURFACE_DEFORM": "Surface Deform",
    "MESH_DEFORM": "Mesh Deform",
    "LAPLACIANDEFORM": "Laplacian Deform",
}
# Past this many, an answer doesn't list what to select; the editor selects nothing instead.
MAX_REPORTED = 2_000_000
# How close to an end of its edge a knife point may be before it should have been that vertex.
EDGE_END = 1e-6


def _get(collection, attribute: str, count: int, width: int = 1) -> np.ndarray:
    data = np.empty(count * width, dtype=np.int32)
    if count:
        collection.foreach_get(attribute, data)
    return data


class Topology:
    """A mesh's connectivity as Blender numbers it, read once for the checks."""

    def __init__(self, mesh):
        self.vertices = len(mesh.vertices)
        self.edges = len(mesh.edges)
        self.faces = len(mesh.polygons)
        loops = len(mesh.loops)
        self.edge_verts = _get(mesh.edges, "vertices", self.edges, 2).reshape(-1, 2)
        self.face_start = _get(mesh.polygons, "loop_start", self.faces)
        self.face_total = _get(mesh.polygons, "loop_total", self.faces)
        self.loop_vert = _get(mesh.loops, "vertex_index", loops)
        self.loop_edge = _get(mesh.loops, "edge_index", loops)
        self._edge_faces = None

    def corners(self, face: int) -> tuple[np.ndarray, np.ndarray]:
        start = self.face_start[face]
        end = start + self.face_total[face]
        return self.loop_vert[start:end], self.loop_edge[start:end]

    def faces_of_edge(self, edge: int) -> np.ndarray:
        if self._edge_faces is None:
            owner = np.repeat(np.arange(self.faces, dtype=np.int32), self.face_total)
            order = np.argsort(self.loop_edge, kind="stable")
            offsets = np.zeros(self.edges + 1, dtype=np.int64)
            np.cumsum(np.bincount(self.loop_edge, minlength=self.edges), out=offsets[1:])
            self._edge_faces = (offsets, owner[order])
        offsets, faces = self._edge_faces
        return faces[offsets[edge] : offsets[edge + 1]]

    def between(self, a: int, b: int) -> int | None:
        """The quad in which edges `a` and `b` are opposite sides, if there's one."""
        for face in self.faces_of_edge(a):
            if self.face_total[face] != 4:
                continue
            edges = list(self.corners(face)[1])
            if b in edges and abs(edges.index(a) - edges.index(b)) == 2:
                return int(face)
        return None


def _fail(message: str):
    raise RequestError(message, "op_invalid")


def _ring(topology: Topology, edges: list[int]) -> tuple[list[int], int | None]:
    """The quads between each pair of edges in a loop cut's ring, and the quad closing it, if the
    last edge and the first are opposite in a quad the ring hasn't used, both edges having two
    faces."""
    for edge in edges:
        if edge >= topology.edges:
            _fail("a ring edge is past the mesh")
    faces = []
    for a, b in zip(edges, edges[1:]):
        face = topology.between(a, b)
        if face is None or face in faces:
            _fail("the edges aren't a ring")
        faces.append(face)
    closing = None
    if len(edges) >= 2 and all(len(topology.faces_of_edge(e)) == 2 for e in (edges[0], edges[-1])):
        for face in topology.faces_of_edge(edges[-1]):
            if topology.face_total[face] != 4 or int(face) in faces:
                continue
            edge_list = list(topology.corners(face)[1])
            if edges[0] in edge_list and abs(edge_list.index(edges[-1]) - edge_list.index(edges[0])) == 2:
                closing = int(face)
    return faces, closing


def _knife(topology: Topology, op: dict) -> list[tuple[int, int, int]]:
    """A knife cut's runs: the parts of it that cross a face from one side to another, as the face
    and the points they start and end at. Parts ending inside a face cut nothing."""
    points, faces = op["points"], op["faces"]
    for point in points:
        if "vertex" in point and point["vertex"] >= topology.vertices:
            _fail("a knife vertex is past the mesh")
        if "edge" in point:
            if point["edge"] >= topology.edges:
                _fail("a knife edge is past the mesh")
            if not EDGE_END < point["factor"] < 1 - EDGE_END:
                _fail("a knife point is at the end of its edge")
        if "face" in point and point["face"] >= topology.faces:
            _fail("a knife face is past the mesh")
    for face in faces:
        if face >= topology.faces:
            _fail("a knife face is past the mesh")

    def on(point: dict, face: int) -> bool:
        corners, sides = topology.corners(face)
        if "vertex" in point:
            return point["vertex"] in corners
        if "edge" in point:
            return point["edge"] in sides
        return point["face"] == face

    for index, face in enumerate(faces):
        a, b = points[index], points[index + 1]
        if not (on(a, face) and on(b, face)):
            _fail("a part of the knife cut leaves its face")
        if a == b:
            _fail("a knife cut has the same point twice in a row")
    for index, point in enumerate(points):
        if "face" in point:
            near = faces[max(index - 1, 0) : index + 1]
            if any(face != point["face"] for face in near):
                _fail("a knife point inside a face has to stay in that face")
    runs = []
    index = 0
    while index < len(points) - 1:
        if "face" in points[index]:
            index += 1
            continue
        end = index + 1
        while end < len(points) and "face" in points[end]:
            end += 1
        if end == len(points):
            break
        runs.append((faces[index], index, end))
        index = end
    if not runs:
        _fail("the knife cut doesn't cross a face")
    return runs


def _components(op: dict, count: int) -> np.ndarray:
    """An edit's components, read from its file and checked against the mesh."""
    try:
        with open(op["file"], "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise RequestError("the component file is missing", "op_invalid") from error
    if meshfile.digest(data) != op["data"]:
        _fail("the component file doesn't match its hash")
    try:
        component, total, raw = meshfile.read_components(data)
    except ValueError as error:
        raise RequestError(str(error), "op_invalid") from error
    if component != op["component"] or total != op["count"]:
        _fail("the component file doesn't match the edit")
    indices = np.frombuffer(raw, dtype="<u4").astype(np.int64)
    if np.any(np.diff(indices) <= 0):
        _fail("component indices must go up, each once")
    if indices[-1] >= count:
        _fail("a component index is past the mesh")
    return indices


def blocker(objects) -> str | None:
    """Why the topology of a mesh used by these objects can't change, if it can't."""
    for obj in objects:
        for modifier in obj.modifiers:
            if modifier.type in BOUND_MODIFIERS:
                return BOUND_MODIFIERS[modifier.type]
            if modifier.type == "CORRECTIVE_SMOOTH" and modifier.rest_source == "BIND":
                return "Corrective Smooth"
    return None


def check(op: dict, mesh, users, topology: Topology):
    """What an edit needs, once it fits the mesh. `users` are the objects using the mesh."""
    named = blocker(users)
    if named:
        raise RequestError(named, "op_unsupported")
    kind = op["op"]
    if kind == "loop_cut":
        return _ring(topology, op["edges"])
    if kind == "knife":
        return _knife(topology, op)
    count = {
        "vertices": topology.vertices,
        "edges": topology.edges,
        "faces": topology.faces,
    }[op["component"]]
    indices = _components(op, count)
    if kind == "merge" and op["at"] == "vertex" and op["vertex"] not in set(indices.tolist()):
        _fail("the vertex to merge at isn't one of those merged")
    return indices


# Making the edits, on BMesh.


def _split_along(edge, start, end, factors: list[float]) -> list:
    """Splits an edge at each factor (increasing, measured from `start`); the new vertices in order."""
    made = []
    current, origin, done = edge, start, 0.0
    for factor in factors:
        _edge, vertex = bmesh.utils.edge_split(current, origin, (factor - done) / (1 - done))
        current = next(e for e in vertex.link_edges if e.other_vert(vertex) is end)
        origin, done = vertex, factor
        made.append(vertex)
    return made


def _across(face, start, edge):
    """In a quad with `edge` on one side, the corner next to `start` that isn't on `edge`."""
    other = edge.other_vert(start)
    for loop in face.loops:
        if loop.vert is start:
            after, before = loop.link_loop_next.vert, loop.link_loop_prev.vert
            return before if after is other else after
    raise RequestError("the ring lost its way", "op_failed")


class _Pieces:
    """What a face became as it was split: the faces it's made of now."""

    def __init__(self):
        self.of: dict = {}

    def split(self, face, a, b, coords=()):
        """Splits the piece of `face` holding `a` and `b` between them, through `coords`. Returns
        the edges of the new line, or none when `a` and `b` are already joined."""
        pieces = self.of.setdefault(face, {face})
        shared = [f for f in set(a.link_faces) & set(b.link_faces) if f in pieces]
        if not shared:
            raise RequestError("the cut crosses itself inside a face", "op_failed")
        piece = shared[0]
        if not coords and any(e.other_vert(a) is b for e in a.link_edges):
            return []
        new_face, _loop = bmesh.utils.face_split(piece, a, b, coords=[Vector(c) for c in coords])
        pieces.add(new_face)
        return [e for e in piece.edges if new_face in e.link_faces]


def _loop_cut(bm, op, prepared):
    faces, closing = prepared
    # Everything is looked up before the first split, which outdates BMesh's lookup tables.
    edges = [bm.edges[i] for i in op["edges"]]
    ring = [bm.faces[i] for i in faces]
    closer = None if closing is None else bm.faces[closing]
    # Each edge's start: the first runs from its first vertex, and the rest follow it across.
    starts = [edges[0].verts[0]]
    for face, edge in zip(ring, edges):
        starts.append(_across(face, starts[-1], edge))
    aligned = closer is None or _across(closer, starts[-1], edges[-1]) is starts[0]
    count, factor = op["cuts"], op["factor"]
    factors = [(k + 1 + factor) / (count + 1) for k in range(count)]
    cuts = [
        _split_along(edge, start, edge.other_vert(start), factors)
        for edge, start in zip(edges, starts)
    ]
    pieces = _Pieces()
    made = []
    pairs = [(ring[i], cuts[i], cuts[i + 1]) for i in range(len(ring))]
    if closer is not None:
        # A ring that comes back the other way round, like a Möbius strip, meets its far cut.
        last = cuts[0] if aligned else list(reversed(cuts[0]))
        pairs.append((closer, cuts[-1], last))
    for face, here, there in pairs:
        for a, b in zip(here, there):
            made += pieces.split(face, a, b)
    return {"component": "edges", "items": made}


def _subdivide(bm, op, indices):
    edges = [bm.edges[i] for i in indices.tolist()]
    result = bmesh.ops.subdivide_edges(
        bm,
        edges=edges,
        cuts=op["cuts"],
        use_grid_fill=True,
        quad_corner_type="STRAIGHT_CUT",
    )
    made = [
        e
        for e in [*result["geom_inner"], *result["geom_split"]]
        if isinstance(e, bmesh.types.BMEdge)
    ]
    return {"component": "edges", "items": list(dict.fromkeys(made))}


def _knife_cut(bm, op, runs):
    points = op["points"]
    verts = [bm.verts[p["vertex"]] if "vertex" in p else None for p in points]
    faces = {face: bm.faces[face] for face, _start, _end in runs}
    by_edge: dict[int, list[tuple[float, int]]] = {}
    for index, point in enumerate(points):
        if "edge" in point:
            by_edge.setdefault(point["edge"], []).append((point["factor"], index))
    edges = {i: bm.edges[i] for i in by_edge}
    for edge_index, entries in by_edge.items():
        edge = edges[edge_index]
        start, end = edge.verts[0], edge.verts[1]
        factors = sorted({factor for factor, _ in entries})
        made = dict(zip(factors, _split_along(edge, start, end, factors)))
        for factor, index in entries:
            verts[index] = made[factor]
    pieces = _Pieces()
    cut = []
    for face, start, end in runs:
        a, b = verts[start], verts[end]
        if a is b:
            continue
        coords = [points[i]["at"] for i in range(start + 1, end)]
        cut += pieces.split(faces[face], a, b, coords)
    return {"component": "edges", "items": cut}


def _weld(bm, targets: dict):
    """Welds each vertex onto its target. An edge merged onto another is a seam, or sharp, when
    either of them was."""
    marks: dict = {}
    for vertex in {*targets, *targets.values()}:
        for edge in vertex.link_edges:
            a, b = (targets.get(v, v) for v in edge.verts)
            if a is b:
                continue
            key = frozenset((a, b))
            seam, sharp = marks.get(key, (False, False))
            marks[key] = (seam or edge.seam, sharp or not edge.smooth)
    bmesh.ops.weld_verts(bm, targetmap=targets)
    for vertex in set(targets.values()):
        for edge in vertex.link_edges:
            mark = marks.get(frozenset(edge.verts))
            if mark:
                edge.seam = mark[0]
                edge.smooth = not mark[1]


def _gather(bm, groups: list[list], centre: bool) -> dict:
    """Each group merged onto its first vertex; at the middle of the group when `centre`, in its
    place and in every shape key."""
    layers = list(bm.verts.layers.shape.values())
    targets = {}
    for group in groups:
        target = group[0]
        if centre:
            size = len(group)
            target.co = sum((v.co for v in group), Vector()) / size
            for layer in layers:
                target[layer] = sum((v[layer] for v in group), Vector()) / size
        for vertex in group[1:]:
            targets[vertex] = target
    return targets


def _merge(bm, op, indices):
    order = indices.tolist()
    verts = [bm.verts[i] for i in order]
    if op["at"] == "vertex":
        target = bm.verts[op["vertex"]]
        groups = [[target, *[v for v in verts if v is not target]]]
    elif op["at"] == "center":
        groups = [verts]
    else:
        # Each connected part of what's merged collapses on its own.
        chosen = set(verts)
        seen = set()
        groups = []
        for vertex in verts:
            if vertex in seen:
                continue
            group, stack = [], [vertex]
            seen.add(vertex)
            while stack:
                v = stack.pop()
                group.append(v)
                for edge in v.link_edges:
                    other = edge.other_vert(v)
                    if other in chosen and other not in seen:
                        seen.add(other)
                        stack.append(other)
            group.sort(key=lambda v: v.index)
            groups.append(group)
    targets = _gather(bm, [g for g in groups if len(g) > 1], op["at"] != "vertex")
    return _welded(bm, verts, order, targets)


def _welded(bm, verts, order, targets):
    survivors = list(dict.fromkeys(targets.get(v, v) for v in verts))
    pairs = [(old, targets[v]) for old, v in zip(order, verts) if v in targets]
    if targets:
        _weld(bm, targets)
    return {
        "component": "vertices",
        "items": survivors,
        "merged": len(targets),
        "pairs": pairs,
    }


def _merge_by_distance(bm, op, indices):
    """Merges vertices of the set closer than the distance, each onto the lowest numbered vertex
    near it that isn't merged yet, in its place. Only vertices of the set merge."""
    order = indices.tolist()
    verts = [bm.verts[i] for i in order]
    places = [tuple(v.co) for v in verts]
    distance = op["distance"]
    limit = distance * distance
    tree = kdtree.KDTree(len(verts))
    for at, place in enumerate(places):
        tree.insert(place, at)
    tree.balance()
    # The tree works in single precision; the exact test that decides is in double.
    reach = distance * (1 + 1e-5) + 1e-9
    taken = [False] * len(verts)
    groups = []
    for at, (x, y, z) in enumerate(places):
        if taken[at]:
            continue
        taken[at] = True
        group = [verts[at]]
        for _co, other, _d in sorted(tree.find_range((x, y, z), reach), key=lambda f: f[1]):
            if taken[other]:
                continue
            ox, oy, oz = places[other]
            dx, dy, dz = ox - x, oy - y, oz - z
            if dx * dx + dy * dy + dz * dz <= limit:
                taken[other] = True
                group.append(verts[other])
        if len(group) > 1:
            groups.append(group)
    targets = _gather(bm, groups, False)
    return _welded(bm, verts, order, targets)


_CONTEXT = {"vertices": "VERTS", "edges": "EDGES", "faces": "FACES"}


def _sequence(bm, component: str):
    return {"vertices": bm.verts, "edges": bm.edges, "faces": bm.faces}[component]


def _delete(bm, op, indices):
    seq = _sequence(bm, op["component"])
    items = [seq[i] for i in indices.tolist()]
    bmesh.ops.delete(bm, geom=items, context=_CONTEXT[op["component"]])
    return {"component": op["component"], "items": []}


def _dissolve(bm, op, indices):
    """Blender's dissolves with their defaults: vertices without splitting faces, edges taking
    their vertices along, faces keeping theirs."""
    component = op["component"]
    items = [_sequence(bm, component)[i] for i in indices.tolist()]
    if component == "vertices":
        bmesh.ops.dissolve_verts(bm, verts=items, use_face_split=False, use_boundary_tear=False)
        return {"component": "faces", "items": []}
    if component == "edges":
        result = bmesh.ops.dissolve_edges(bm, edges=items, use_verts=True, use_face_split=False)
    else:
        result = bmesh.ops.dissolve_faces(bm, faces=items, use_verts=False)
    return {"component": "faces", "items": list(result.get("region", []))}


_MAKE = {
    "loop_cut": _loop_cut,
    "subdivide": _subdivide,
    "knife": _knife_cut,
    "merge": _merge,
    "merge_by_distance": _merge_by_distance,
    "delete": _delete,
    "dissolve": _dissolve,
}
# How many merged vertices an answer says the place of, so an agent can follow them.
MAX_PAIRS = 1000


def apply(mesh, op: dict, prepared) -> dict:
    """Makes one edit on a mesh. Nothing changes unless it all worked. Returns what to report."""
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        for seq in (bm.verts, bm.edges, bm.faces):
            seq.ensure_lookup_table()
        before = (len(bm.verts), len(bm.edges), len(bm.faces))
        made = _MAKE[op["op"]](bm, op, prepared)
        for seq in (bm.verts, bm.edges, bm.faces):
            seq.index_update()
        items = [item.index for item in made["items"] if item.is_valid]
        report = {
            "object": op["object"],
            "counts": {"vertices": len(bm.verts), "edges": len(bm.edges), "faces": len(bm.faces)},
            "added": {
                "vertices": len(bm.verts) - before[0],
                "edges": len(bm.edges) - before[1],
                "faces": len(bm.faces) - before[2],
            },
        }
        if len(items) <= MAX_REPORTED:
            report["select"] = {"component": made["component"], "indices": sorted(set(items))}
        if "merged" in made:
            # Each merged vertex, by its old index, and the vertex it's part of now.
            report["merged"] = made["merged"]
            report["merged_into"] = [[old, v.index] for old, v in made["pairs"][:MAX_PAIRS]]
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return report
